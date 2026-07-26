"""可验证的全量备份与带补偿恢复仓储。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable
from uuid import UUID, uuid4

import yaml

from core.library_lock import library_lock


MANIFEST_VERSION = 1
RESTORE_JOURNAL_VERSION = 1
DRILL_RECORD_VERSION = 1
APPLICATION = "local-tavern"
APPLICATION_VERSION = "1.0"
DATA_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PROJECT_INTERNAL_ROOTS = frozenset({".migration-receipts"})
_WINDOWS_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
})
_WINDOWS_INVALID = frozenset('<>:"|?*')
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_RESTORE_STATUSES = frozenset({
    "prepared",
    "switching",
    "recovering",
    "committed",
    "rolled_back",
    "needs_recovery",
})
_PENDING_RESTORE_STATUSES = frozenset({
    "prepared",
    "switching",
    "recovering",
    "needs_recovery",
})


class BackupError(RuntimeError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code

    def as_detail(self) -> dict:
        return {"code": self.code, "message": str(self)}


class BackupConflict(BackupError):
    def __init__(self, message: str, *, code: str = "backup_conflict", detail: dict | None = None):
        super().__init__(message, code=code)
        self.detail = dict(detail or {})

    def as_detail(self) -> dict:
        return {**super().as_detail(), **self.detail}


class BackupIntegrityError(BackupError):
    def __init__(self, message: str, *, code: str = "backup_integrity_error"):
        super().__init__(message, code=code)


class BackupOperationError(BackupError):
    def __init__(self, message: str, *, code: str = "backup_operation_failed", backup_id: str | None = None):
        super().__init__(message, code=code)
        self.backup_id = backup_id

    def as_detail(self) -> dict:
        return {**super().as_detail(), "backup_id": self.backup_id}


@dataclass(frozen=True)
class _SourceRecord:
    path: Path
    archive_path: str
    size: int
    sha256: str

    def item(self) -> dict:
        return {"path": self.archive_path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class _SourceSnapshot:
    records: tuple[_SourceRecord, ...]
    root_states: dict[str, bool]
    fingerprint: str

    @property
    def items(self) -> list[dict]:
        return [record.item() for record in self.records]


@dataclass(frozen=True)
class VerifiedBackup:
    path: Path
    manifest: dict


@dataclass
class _RootPlan:
    label: str
    target: Path
    stage: Path
    rollback: Path
    desired: bool
    directory: bool
    had_current: bool
    move_attempted: bool = False
    old_moved: bool = False
    install_attempted: bool = False
    new_installed: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(flag and getattr(info, "st_file_attributes", 0) & flag)


def _has_reparse_in_chain(path: Path) -> bool:
    current = _absolute(path)
    while True:
        if _is_reparse(current):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _validate_component(name: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or name.rstrip(" .") != name
        or any(ord(character) < 32 or character in _WINDOWS_INVALID for character in name)
        or name.split(".", 1)[0].casefold() in _WINDOWS_RESERVED
    ):
        raise BackupIntegrityError("路径包含跨平台不安全的名称")


def _fingerprint(root_states: dict[str, bool], items: list[dict]) -> str:
    canonical = json.dumps(
        {"root_states": root_states, "items": items},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class BackupManager:
    """备份创建、校验、预检和恢复；list/dry-run 保持纯读取。"""

    def __init__(
        self,
        backups_root: Path,
        projects_root: Path,
        prompts_dir: Path,
        settings_path: Path,
        retention_days: int = 30,
        retention_count: int = 10,
    ):
        self.backups_root = _absolute(Path(backups_root))
        self.projects_root = _absolute(Path(projects_root))
        self.prompts_dir = _absolute(Path(prompts_dir))
        self.settings_path = _absolute(Path(settings_path))
        for value, label in ((retention_days, "retention_days"), (retention_count, "retention_count")):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} 必须是正整数")
        self.retention_days = retention_days
        self.retention_count = retention_count
        self._validate_root_layout()
        key = str(self.backups_root).casefold()
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(key, threading.RLock())

    def _validate_root_layout(self) -> None:
        roots = [self.backups_root, self.projects_root, self.prompts_dir]
        for index, left in enumerate(roots):
            for right in roots[index + 1:]:
                if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                    raise ValueError("备份目录与数据根目录不能重叠")
        if self.settings_path.is_relative_to(self.backups_root):
            raise ValueError("settings 不能位于备份目录内")
        if self.settings_path.is_relative_to(self.projects_root) or self.settings_path.is_relative_to(self.prompts_dir):
            raise ValueError("settings 不能位于目录型备份根内")

    @staticmethod
    def _excluded(prefix: str, parts: tuple[str, ...], *, is_file: bool) -> bool:
        del is_file
        if not parts:
            return False
        return (
            prefix == "projects"
            and parts[0].casefold() in _PROJECT_INTERNAL_ROOTS
        )

    @staticmethod
    def _validate_name(name: str) -> None:
        _validate_component(name)

    @staticmethod
    def _hash_stable_file(path: Path) -> tuple[int, str]:
        try:
            before = os.stat(path, follow_symlinks=False)
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
            after = os.stat(path, follow_symlinks=False)
        except (OSError, FileNotFoundError) as exc:
            raise BackupConflict("源文件在扫描期间发生变化", code="source_changed") from exc
        stable = (
            before.st_size == after.st_size == size
            and before.st_mtime_ns == after.st_mtime_ns
            and getattr(before, "st_ino", 0) == getattr(after, "st_ino", 0)
        )
        if not stable or _is_reparse(path):
            raise BackupConflict("源文件在扫描期间发生变化", code="source_changed")
        return size, digest.hexdigest()

    def _scan_tree(self, root: Path, prefix: str) -> tuple[list[_SourceRecord], bool]:
        if _has_reparse_in_chain(root):
            raise BackupIntegrityError("备份根不能是 symlink 或 reparse point")
        if not root.exists():
            return [], False
        if not root.is_dir():
            raise BackupIntegrityError("备份根必须是普通目录")
        records: list[_SourceRecord] = []

        def walk(directory: Path, relative: tuple[str, ...]) -> None:
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name)
            except OSError as exc:
                raise BackupOperationError("源目录扫描失败", code="source_scan_failed") from exc
            for entry in entries:
                self._validate_name(entry.name)
                current = Path(entry.path)
                parts = (*relative, entry.name)
                if _is_reparse(current) or entry.is_symlink():
                    raise BackupIntegrityError("源目录包含 symlink 或 reparse point")
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError as exc:
                    raise BackupConflict("源文件在扫描期间发生变化", code="source_changed") from exc
                if self._excluded(prefix, parts, is_file=is_file):
                    continue
                if is_dir:
                    walk(current, parts)
                elif is_file:
                    size, digest = self._hash_stable_file(current)
                    archive = PurePosixPath(prefix, *parts).as_posix()
                    records.append(_SourceRecord(current, archive, size, digest))
                else:
                    raise BackupIntegrityError("源目录包含不支持的文件类型")

        walk(root, ())
        return records, True

    def _scan_sources(self) -> _SourceSnapshot:
        project_records, projects_present = self._scan_tree(self.projects_root, "projects")
        prompt_records, prompts_present = self._scan_tree(self.prompts_dir, "prompts")
        records = [*project_records, *prompt_records]
        settings_present = False
        if _has_reparse_in_chain(self.settings_path):
            raise BackupIntegrityError("settings 不能是 symlink 或 reparse point")
        if self.settings_path.exists():
            if not self.settings_path.is_file():
                raise BackupIntegrityError("settings 必须是普通文件")
            size, digest = self._hash_stable_file(self.settings_path)
            records.append(_SourceRecord(self.settings_path, "settings.json", size, digest))
            settings_present = True
        records.sort(key=lambda record: record.archive_path)
        root_states = {
            "projects": projects_present,
            "prompts": prompts_present,
            "settings": settings_present,
        }
        items = [record.item() for record in records]
        return _SourceSnapshot(tuple(records), root_states, _fingerprint(root_states, items))

    def current_fingerprint(self) -> str:
        with self._lock:
            return self._scan_sources().fingerprint

    @staticmethod
    def _validate_uuid(value: str, label: str) -> str:
        try:
            canonical = str(UUID(str(value)))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"无效 {label}") from exc
        if canonical != str(value).lower():
            raise ValueError(f"{label} 必须使用规范 UUID")
        return canonical

    @classmethod
    def _validate_backup_id(cls, backup_id: str) -> str:
        return cls._validate_uuid(backup_id, "backup_id")

    @classmethod
    def _validate_restore_id(cls, restore_id: str) -> str:
        return cls._validate_uuid(restore_id, "restore_id")

    @classmethod
    def _validate_drill_id(cls, drill_id: str) -> str:
        return cls._validate_uuid(drill_id, "drill_id")

    def _backup_path(self, backup_id: str) -> Path:
        backup_id = self._validate_backup_id(backup_id)
        path = _absolute(self.backups_root / f"{backup_id}.zip")
        if path.parent != self.backups_root:
            raise ValueError("backup_id 路径越界")
        return path

    @staticmethod
    def _validate_reason(reason: str | None) -> str | None:
        if reason is None:
            return None
        if not isinstance(reason, str):
            raise ValueError("reason 必须是字符串或 null")
        reason = reason.strip()
        if len(reason) > 500:
            raise ValueError("reason 不能超过 500 个字符")
        return reason or None

    @staticmethod
    def _zip_info(name: str) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (stat.S_IFREG | 0o600) << 16
        return info

    @staticmethod
    def _safe_member(name: object) -> str:
        if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
            raise BackupIntegrityError("ZIP 成员路径无效")
        path = PurePosixPath(name)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise BackupIntegrityError("ZIP 成员路径越界")
        normalized = path.as_posix()
        if normalized != name:
            raise BackupIntegrityError("ZIP 成员路径未规范化")
        for component in path.parts:
            _validate_component(component)
        return normalized

    @classmethod
    def _validate_item_path(cls, path: object) -> str:
        path = cls._safe_member(path)
        pure = PurePosixPath(path)
        allowed = (
            path == "settings.json"
            or (len(pure.parts) > 1 and pure.parts[0] in {"projects", "prompts"})
        )
        if not allowed or path == "manifest.json":
            raise BackupIntegrityError("manifest item 不在备份白名单")
        if (
            path != "settings.json"
            and cls._excluded(pure.parts[0], tuple(pure.parts[1:]), is_file=True)
        ):
            raise BackupIntegrityError("manifest item 命中排除规则")
        return path

    def _build_manifest(
        self,
        backup_id: str,
        snapshot: _SourceSnapshot,
        *,
        reason: str | None,
        kind: str,
    ) -> dict:
        return {
            "manifest_version": MANIFEST_VERSION,
            "application": APPLICATION,
            "application_version": APPLICATION_VERSION,
            "data_schema_version": DATA_SCHEMA_VERSION,
            "schema_version": DATA_SCHEMA_VERSION,
            "backup_id": backup_id,
            "kind": kind,
            "reason": reason,
            "created_at": _utc_now().isoformat(),
            "source_fingerprint": snapshot.fingerprint,
            "root_states": dict(snapshot.root_states),
            "items": snapshot.items,
        }

    def _write_zip_staging(
        self,
        staging_path: Path,
        manifest: dict,
        records: list[_SourceRecord],
    ) -> None:
        manifest_bytes = json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        try:
            with zipfile.ZipFile(
                staging_path,
                mode="x",
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True,
            ) as archive:
                archive.writestr(self._zip_info("manifest.json"), manifest_bytes)
                for record in records:
                    before = os.stat(record.path, follow_symlinks=False)
                    digest = hashlib.sha256()
                    size = 0
                    with record.path.open("rb") as source, archive.open(
                        self._zip_info(record.archive_path),
                        mode="w",
                        force_zip64=True,
                    ) as target:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            size += len(chunk)
                            digest.update(chunk)
                            target.write(chunk)
                    after = os.stat(record.path, follow_symlinks=False)
                    if (
                        size != record.size
                        or digest.hexdigest() != record.sha256
                        or before.st_size != after.st_size
                        or before.st_mtime_ns != after.st_mtime_ns
                        or _is_reparse(record.path)
                    ):
                        raise BackupConflict(
                            "源文件在备份写入期间发生变化",
                            code="source_changed",
                        )
        except BackupError:
            raise
        except (OSError, zipfile.BadZipFile) as exc:
            raise BackupOperationError("备份 ZIP 写入失败", code="backup_write_failed") from exc

    def _validate_manifest(self, manifest: object, backup_id: str) -> dict:
        if not isinstance(manifest, dict):
            raise BackupIntegrityError("backup manifest 顶层必须是对象")
        if manifest.get("manifest_version") != MANIFEST_VERSION:
            raise BackupIntegrityError("backup manifest 版本不兼容")
        if manifest.get("application") != APPLICATION:
            raise BackupIntegrityError("backup application 不兼容")
        version = manifest.get("application_version")
        if not isinstance(version, str) or version.split(".", 1)[0] != "1":
            raise BackupIntegrityError("backup application 主版本不兼容")
        schema = manifest.get("data_schema_version")
        if (
            isinstance(schema, bool)
            or not isinstance(schema, int)
            or schema < 0
            or schema > DATA_SCHEMA_VERSION
            or manifest.get("schema_version") != schema
        ):
            raise BackupIntegrityError("backup data schema 不兼容")
        if manifest.get("backup_id") != backup_id:
            raise BackupIntegrityError("backup_id 与 ZIP 文件不一致")
        kind = manifest.get("kind")
        if not isinstance(kind, str) or not _TOKEN_RE.fullmatch(kind):
            raise BackupIntegrityError("backup kind 无效")
        reason = manifest.get("reason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
            raise BackupIntegrityError("backup reason 无效")
        created_at = manifest.get("created_at")
        try:
            created = datetime.fromisoformat(created_at)
        except (TypeError, ValueError) as exc:
            raise BackupIntegrityError("backup created_at 无效") from exc
        if created.tzinfo is None:
            raise BackupIntegrityError("backup created_at 缺少时区")
        root_states = manifest.get("root_states")
        if (
            not isinstance(root_states, dict)
            or set(root_states) != {"projects", "prompts", "settings"}
            or any(type(value) is not bool for value in root_states.values())
        ):
            raise BackupIntegrityError("backup root_states 无效")
        items = manifest.get("items")
        if not isinstance(items, list):
            raise BackupIntegrityError("backup items 无效")
        normalized: list[dict] = []
        seen: set[str] = set()
        seen_casefold: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
                raise BackupIntegrityError("backup item 字段无效")
            path = self._validate_item_path(item.get("path"))
            size = item.get("size")
            digest = item.get("sha256")
            if (
                path in seen
                or path.casefold() in seen_casefold
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or not isinstance(digest, str)
                or not _SHA256_RE.fullmatch(digest)
            ):
                raise BackupIntegrityError("backup item 校验字段无效")
            seen.add(path)
            seen_casefold.add(path.casefold())
            normalized.append({"path": path, "size": size, "sha256": digest})
        if normalized != sorted(normalized, key=lambda item: item["path"]):
            raise BackupIntegrityError("backup items 未按路径稳定排序")
        for label in ("projects", "prompts"):
            has_items = any(item["path"].startswith(f"{label}/") for item in normalized)
            if has_items and not root_states[label]:
                raise BackupIntegrityError("backup root state 与 items 不一致")
        settings_items = [item for item in normalized if item["path"] == "settings.json"]
        if bool(settings_items) != root_states["settings"] or len(settings_items) > 1:
            raise BackupIntegrityError("settings root state 与 item 不一致")
        fingerprint = manifest.get("source_fingerprint")
        if (
            not isinstance(fingerprint, str)
            or not _SHA256_RE.fullmatch(fingerprint)
            or fingerprint != _fingerprint(root_states, normalized)
        ):
            raise BackupIntegrityError("backup source_fingerprint 无效")
        return manifest

    def _verify_zip_path(self, path: Path, backup_id: str) -> VerifiedBackup:
        if not path.is_file() or _has_reparse_in_chain(path):
            raise BackupIntegrityError("备份 ZIP 缺失或不是普通文件")
        try:
            with zipfile.ZipFile(path, mode="r") as archive:
                infos = archive.infolist()
                names = [self._safe_member(info.filename) for info in infos]
                if (
                    names != sorted(names)
                    or len(names) != len(set(names))
                    or len(names) != len({name.casefold() for name in names})
                ):
                    raise BackupIntegrityError("ZIP 成员未排序或包含重复项")
                for info in infos:
                    mode = (info.external_attr >> 16) & 0xFFFF
                    if info.is_dir() or (mode and not stat.S_ISREG(mode)):
                        raise BackupIntegrityError("ZIP 成员不是普通文件")
                if names.count("manifest.json") != 1:
                    raise BackupIntegrityError("ZIP 缺少唯一 manifest.json")
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                manifest = self._validate_manifest(manifest, backup_id)
                expected_names = ["manifest.json", *(item["path"] for item in manifest["items"])]
                if names != sorted(expected_names):
                    raise BackupIntegrityError("ZIP 成员集合与 manifest 不一致")
                by_name = {info.filename: info for info in infos}
                for item in manifest["items"]:
                    info = by_name[item["path"]]
                    if info.file_size != item["size"]:
                        raise BackupIntegrityError("ZIP 成员大小与 manifest 不一致")
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(info, mode="r") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            size += len(chunk)
                            digest.update(chunk)
                    if size != item["size"] or digest.hexdigest() != item["sha256"]:
                        raise BackupIntegrityError("ZIP 成员 SHA-256 校验失败")
        except BackupError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile, RuntimeError) as exc:
            raise BackupIntegrityError("备份 ZIP 无法验证") from exc
        return VerifiedBackup(path=path, manifest=manifest)

    def get_verified(self, backup_id: str) -> VerifiedBackup:
        backup_id = self._validate_backup_id(backup_id)
        path = self._backup_path(backup_id)
        if _is_reparse(path):
            raise BackupIntegrityError("备份 ZIP 不能是 symlink 或 reparse point")
        if not path.exists():
            raise FileNotFoundError(f"备份不存在: {backup_id}")
        return self._verify_zip_path(path, backup_id)

    @staticmethod
    def _summary(manifest: dict) -> dict:
        return {
            "backup_id": manifest["backup_id"],
            "kind": manifest["kind"],
            "reason": manifest["reason"],
            "created_at": manifest["created_at"],
            "source_fingerprint": manifest["source_fingerprint"],
            "file_count": len(manifest["items"]),
            "total_size": sum(item["size"] for item in manifest["items"]),
            "application": manifest["application"],
            "application_version": manifest["application_version"],
            "data_schema_version": manifest["data_schema_version"],
        }

    def create_backup(self, reason: str | None = None, *, kind: str = "manual") -> dict:
        """在整库独占锁内创建一致性备份。"""
        with library_lock.exclusive():
            return self._create_backup_locked(reason, kind=kind)

    def _create_backup_locked(
        self,
        reason: str | None = None,
        *,
        kind: str = "manual",
    ) -> dict:
        reason = self._validate_reason(reason)
        if kind not in {"manual", "scheduled", "pre_restore", "pre_migration"}:
            raise ValueError(
                "kind 仅支持 manual、scheduled、pre_restore 或 pre_migration"
            )
        with self._lock:
            self._assert_no_unresolved_restore()
            before = self._scan_sources()
            backup_id = str(uuid4())
            manifest = self._build_manifest(backup_id, before, reason=reason, kind=kind)
            staging_root = self.backups_root / ".staging"
            staging_path = staging_root / f"{backup_id}.zip.tmp"
            final_path = self._backup_path(backup_id)
            published = False
            publish_attempted = False
            try:
                if _has_reparse_in_chain(self.backups_root):
                    raise BackupIntegrityError("备份目录不能是 symlink 或 reparse point")
                self.backups_root.mkdir(parents=True, exist_ok=True)
                if _has_reparse_in_chain(self.backups_root):
                    raise BackupIntegrityError("备份目录不能是 symlink 或 reparse point")
                if _has_reparse_in_chain(staging_root):
                    raise BackupIntegrityError("备份 staging 不能是 reparse point")
                staging_root.mkdir(parents=True, exist_ok=True)
                self._write_zip_staging(staging_path, manifest, list(before.records))
                after = self._scan_sources()
                if after.fingerprint != before.fingerprint:
                    raise BackupConflict("备份期间源数据发生变化", code="source_changed")
                self._verify_zip_path(staging_path, backup_id)
                if final_path.exists():
                    raise BackupOperationError("备份 ID 已存在", code="backup_exists")
                if _is_reparse(final_path):
                    raise BackupIntegrityError("备份目标不能是 reparse point")
                publish_attempted = True
                os.replace(staging_path, final_path)
                published = True
                verified = self._verify_zip_path(final_path, backup_id)
                return {**self._summary(verified.manifest), "expired": False}
            except BaseException as exc:
                try:
                    staging_path.unlink(missing_ok=True)
                except OSError:
                    pass
                if (
                    published
                    or (publish_attempted and not staging_path.exists())
                ) and final_path.exists():
                    try:
                        final_path.unlink()
                    except OSError:
                        pass
                try:
                    staging_root.rmdir()
                except OSError:
                    pass
                if isinstance(exc, (BackupError, ValueError, FileNotFoundError)):
                    raise
                raise BackupOperationError(
                    "备份创建失败",
                    code="backup_create_failed",
                ) from exc

    def list_backups(self) -> list[dict]:
        with self._lock:
            if _has_reparse_in_chain(self.backups_root):
                raise BackupIntegrityError("备份目录不能是 symlink 或 reparse point")
            if not self.backups_root.exists():
                return []
            if not self.backups_root.is_dir():
                raise BackupIntegrityError("备份目录必须是普通目录")
            valid: list[dict] = []
            invalid: list[dict] = []
            for path in sorted(self.backups_root.glob("*.zip")):
                backup_id = path.stem
                try:
                    backup_id = self._validate_backup_id(backup_id)
                    verified = self._verify_zip_path(path, backup_id)
                    valid.append(self._summary(verified.manifest))
                except (ValueError, BackupIntegrityError) as exc:
                    invalid.append({
                        "backup_id": backup_id,
                        "status": "invalid",
                        "code": getattr(exc, "code", "invalid_backup"),
                        "expired": False,
                    })
            valid.sort(key=lambda item: item["created_at"], reverse=True)
            cutoff = _utc_now() - timedelta(days=self.retention_days)
            for index, item in enumerate(valid):
                created = datetime.fromisoformat(item["created_at"])
                reasons: list[str] = []
                if index >= self.retention_count:
                    reasons.append("count")
                if created < cutoff:
                    reasons.append("age")
                item["expired"] = bool(reasons)
                item["expired_reasons"] = reasons
            invalid.sort(key=lambda item: item["backup_id"])
            return [*valid, *invalid]

    def expired_backup_ids(self) -> list[str]:
        return [
            item["backup_id"]
            for item in self.list_backups()
            if item.get("expired") and item.get("status") != "invalid"
        ]

    @staticmethod
    def _changes(current: _SourceSnapshot, manifest: dict) -> list[dict]:
        current_items = {item["path"]: item for item in current.items}
        backup_items = {item["path"]: item for item in manifest["items"]}
        changes: list[dict] = []
        for path in sorted(set(current_items) | set(backup_items)):
            now = current_items.get(path)
            saved = backup_items.get(path)
            if now is None:
                action = "create"
            elif saved is None:
                action = "remove"
            elif now["sha256"] != saved["sha256"] or now["size"] != saved["size"]:
                action = "replace"
            else:
                continue
            changes.append({
                "path": path,
                "action": action,
                "current_sha256": now["sha256"] if now else None,
                "backup_sha256": saved["sha256"] if saved else None,
            })
        for label in ("projects", "prompts"):
            if current.root_states[label] != manifest["root_states"][label]:
                changes.append({
                    "path": f"{label}/",
                    "action": "create" if manifest["root_states"][label] else "remove",
                    "current_sha256": None,
                    "backup_sha256": None,
                })
        changes.sort(key=lambda item: item["path"])
        return changes

    def _validate_structured_payloads(self, verified: VerifiedBackup) -> None:
        try:
            with zipfile.ZipFile(verified.path, mode="r") as archive:
                for item in verified.manifest["items"]:
                    path = item["path"]
                    if not path.lower().endswith((".json", ".yaml", ".yml")):
                        continue
                    text = archive.read(path).decode("utf-8")
                    if path.lower().endswith(".json"):
                        json.loads(text)
                    else:
                        yaml.safe_load(text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError, zipfile.BadZipFile) as exc:
            raise BackupIntegrityError("备份包含无法解析的 JSON/YAML", code="invalid_payload") from exc

    def dry_run(self, backup_id: str) -> dict:
        with self._lock:
            verified = self.get_verified(backup_id)
            self._validate_structured_payloads(verified)
            current = self._scan_sources()
            changes = self._changes(current, verified.manifest)
            conflicts = [change for change in changes if change["action"] in {"replace", "remove"}]
            return {
                "backup_id": backup_id,
                "current_fingerprint": current.fingerprint,
                "backup_fingerprint": verified.manifest["source_fingerprint"],
                "changes": changes,
                "conflicts": conflicts,
                "requires_confirmation": bool(changes),
            }

    @staticmethod
    def _remove_generated(path: Path) -> None:
        if not path.exists():
            return
        if _is_reparse(path):
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    def _root_plans(self, restore_id: str, manifest: dict) -> list[_RootPlan]:
        specifications = (
            ("projects", self.projects_root, True),
            ("prompts", self.prompts_dir, True),
            ("settings", self.settings_path, False),
        )
        plans: list[_RootPlan] = []
        for label, target, directory in specifications:
            stage = target.parent / f".{target.name}.restore-{restore_id}.stage"
            rollback = target.parent / f".{target.name}.restore-{restore_id}.rollback"
            if stage.exists() or rollback.exists() or _is_reparse(stage) or _is_reparse(rollback):
                raise BackupOperationError("恢复 staging 名称冲突", code="restore_staging_conflict")
            plans.append(_RootPlan(
                label=label,
                target=target,
                stage=stage,
                rollback=rollback,
                desired=manifest["root_states"][label],
                directory=directory,
                had_current=target.exists(),
            ))
        return plans

    @staticmethod
    def _plan_for_item(plans: list[_RootPlan], archive_path: str) -> tuple[_RootPlan, tuple[str, ...]]:
        pure = PurePosixPath(archive_path)
        if archive_path == "settings.json":
            return next(plan for plan in plans if plan.label == "settings"), ()
        label = pure.parts[0]
        plan = next((item for item in plans if item.label == label), None)
        if plan is None or len(pure.parts) < 2:
            raise BackupIntegrityError("恢复 item 不属于声明根")
        return plan, tuple(pure.parts[1:])

    def _prepare_staging(self, verified: VerifiedBackup, restore_id: str) -> list[_RootPlan]:
        plans = self._root_plans(restore_id, verified.manifest)
        try:
            for plan in plans:
                if _has_reparse_in_chain(plan.target.parent):
                    raise BackupIntegrityError("恢复目标父目录不能是 reparse point")
                plan.target.parent.mkdir(parents=True, exist_ok=True)
                if _is_reparse(plan.target.parent):
                    raise BackupIntegrityError("恢复目标父目录不能是 reparse point")
                if plan.desired and plan.directory:
                    plan.stage.mkdir()
            with zipfile.ZipFile(verified.path, mode="r") as archive:
                for item in verified.manifest["items"]:
                    plan, relative = self._plan_for_item(plans, item["path"])
                    if not plan.desired:
                        raise BackupIntegrityError("恢复 item 与 root state 不一致")
                    target = plan.stage if not relative else plan.stage.joinpath(*relative)
                    if plan.directory:
                        target.parent.mkdir(parents=True, exist_ok=True)
                    elif relative:
                        raise BackupIntegrityError("settings item 路径无效")
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(item["path"], mode="r") as source, target.open("xb") as output:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            size += len(chunk)
                            digest.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if size != item["size"] or digest.hexdigest() != item["sha256"]:
                        raise BackupIntegrityError("恢复 staging 文件校验失败")
            return plans
        except BaseException:
            for plan in reversed(plans):
                try:
                    self._remove_generated(plan.stage)
                except OSError:
                    pass
            raise

    def _write_journal(self, path: Path, journal: dict) -> None:
        if _has_reparse_in_chain(path.parent):
            raise BackupIntegrityError("恢复 journal 目录不能是 reparse point")
        _atomic_json(path, journal)

    def _replace_path(self, source: Path, target: Path) -> None:
        os.replace(source, target)

    @staticmethod
    def _journal_data(
        *,
        restore_id: str,
        backup_id: str,
        pre_restore_backup_id: str,
        expected_fingerprint: str,
        status: str,
        plans: list[_RootPlan],
    ) -> dict:
        return {
            "journal_version": RESTORE_JOURNAL_VERSION,
            "restore_id": restore_id,
            "backup_id": backup_id,
            "pre_restore_backup_id": pre_restore_backup_id,
            "expected_current_fingerprint": expected_fingerprint,
            "status": status,
            "updated_at": _utc_now().isoformat(),
            "roots": [
                {
                    "label": plan.label,
                    "desired": plan.desired,
                    "had_current": plan.had_current,
                    "move_attempted": plan.move_attempted,
                    "old_moved": plan.old_moved,
                    "install_attempted": plan.install_attempted,
                    "new_installed": plan.new_installed,
                    "stage": str(plan.stage),
                    "rollback": str(plan.rollback),
                }
                for plan in plans
            ],
        }

    def _persist_journal(
        self,
        journal_path: Path,
        *,
        restore_id: str,
        backup_id: str,
        pre_restore_backup_id: str,
        expected_fingerprint: str,
        status: str,
        plans: list[_RootPlan],
    ) -> None:
        self._write_journal(
            journal_path,
            self._journal_data(
                restore_id=restore_id,
                backup_id=backup_id,
                pre_restore_backup_id=pre_restore_backup_id,
                expected_fingerprint=expected_fingerprint,
                status=status,
                plans=plans,
            ),
        )

    def _journal_path(self, restore_id: str) -> Path:
        restore_id = self._validate_restore_id(restore_id)
        path = _absolute(self.backups_root / ".journals" / f"{restore_id}.json")
        expected_parent = _absolute(self.backups_root / ".journals")
        if path.parent != expected_parent:
            raise ValueError("restore_id 路径越界")
        return path

    @staticmethod
    def _strict_bool(value: object, label: str) -> bool:
        if type(value) is not bool:
            raise BackupIntegrityError(f"恢复 journal 的 {label} 必须是布尔值")
        return value

    def _load_restore_journal(self, restore_id: str) -> tuple[Path, dict]:
        restore_id = self._validate_restore_id(restore_id)
        path = self._journal_path(restore_id)
        if _has_reparse_in_chain(path.parent):
            raise BackupIntegrityError("恢复 journal 目录不能是 reparse point")
        if _is_reparse(path):
            raise BackupIntegrityError("恢复 journal 不能是 reparse point")
        if not path.is_file():
            raise FileNotFoundError(f"恢复 journal 不存在: {restore_id}")
        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupIntegrityError("恢复 journal 无法读取") from exc
        required = {
            "journal_version",
            "restore_id",
            "backup_id",
            "pre_restore_backup_id",
            "expected_current_fingerprint",
            "status",
            "updated_at",
            "roots",
        }
        if not isinstance(journal, dict) or set(journal) != required:
            raise BackupIntegrityError("恢复 journal 结构无效")
        if journal["journal_version"] != RESTORE_JOURNAL_VERSION:
            raise BackupIntegrityError("恢复 journal 版本不兼容")
        if journal["restore_id"] != restore_id:
            raise BackupIntegrityError("恢复 journal ID 与文件名不一致")
        try:
            self._validate_backup_id(journal["backup_id"])
            self._validate_backup_id(journal["pre_restore_backup_id"])
        except ValueError as exc:
            raise BackupIntegrityError("恢复 journal 的 backup_id 无效") from exc
        if journal["backup_id"] == journal["pre_restore_backup_id"]:
            raise BackupIntegrityError("恢复 journal 的备份引用不能相同")
        fingerprint = journal["expected_current_fingerprint"]
        if not isinstance(fingerprint, str) or not _SHA256_RE.fullmatch(fingerprint):
            raise BackupIntegrityError("恢复 journal 的预期指纹无效")
        if journal["status"] not in _RESTORE_STATUSES:
            raise BackupIntegrityError("恢复 journal 状态无效")
        try:
            updated_at = datetime.fromisoformat(journal["updated_at"])
        except (TypeError, ValueError) as exc:
            raise BackupIntegrityError("恢复 journal 时间无效") from exc
        if updated_at.tzinfo is None:
            raise BackupIntegrityError("恢复 journal 时间必须包含时区")

        roots = journal["roots"]
        if not isinstance(roots, list) or len(roots) != 3:
            raise BackupIntegrityError("恢复 journal 根列表无效")
        specifications = (
            ("projects", self.projects_root, True),
            ("prompts", self.prompts_dir, True),
            ("settings", self.settings_path, False),
        )
        root_required = {
            "label",
            "desired",
            "had_current",
            "move_attempted",
            "old_moved",
            "install_attempted",
            "new_installed",
            "stage",
            "rollback",
        }
        for root, (label, target, _directory) in zip(roots, specifications, strict=True):
            if not isinstance(root, dict) or set(root) != root_required:
                raise BackupIntegrityError("恢复 journal 根结构无效")
            if root["label"] != label:
                raise BackupIntegrityError("恢复 journal 根顺序或名称无效")
            for field_name in (
                "desired",
                "had_current",
                "move_attempted",
                "old_moved",
                "install_attempted",
                "new_installed",
            ):
                self._strict_bool(root[field_name], field_name)
            if root["old_moved"] and not root["had_current"]:
                raise BackupIntegrityError("恢复 journal 根状态矛盾")
            if (root["install_attempted"] or root["new_installed"]) and not root["desired"]:
                raise BackupIntegrityError("恢复 journal 根状态矛盾")
            expected_stage = target.parent / f".{target.name}.restore-{restore_id}.stage"
            expected_rollback = target.parent / f".{target.name}.restore-{restore_id}.rollback"
            if root["stage"] != str(expected_stage) or root["rollback"] != str(expected_rollback):
                raise BackupIntegrityError("恢复 journal 生成路径越界")
        return path, journal

    def _plans_from_journal(self, journal: dict) -> list[_RootPlan]:
        specifications = (
            ("projects", self.projects_root, True),
            ("prompts", self.prompts_dir, True),
            ("settings", self.settings_path, False),
        )
        plans: list[_RootPlan] = []
        for root, (label, target, directory) in zip(
            journal["roots"],
            specifications,
            strict=True,
        ):
            plans.append(_RootPlan(
                label=label,
                target=target,
                stage=Path(root["stage"]),
                rollback=Path(root["rollback"]),
                desired=root["desired"],
                directory=directory,
                had_current=root["had_current"],
                move_attempted=root["move_attempted"],
                old_moved=root["old_moved"],
                install_attempted=root["install_attempted"],
                new_installed=root["new_installed"],
            ))
        return plans

    def _restore_journal_ids(self) -> list[str]:
        journals_root = self.backups_root / ".journals"
        if _has_reparse_in_chain(journals_root):
            raise BackupIntegrityError("恢复 journal 目录不能是 reparse point")
        if not journals_root.exists():
            return []
        if not journals_root.is_dir():
            raise BackupIntegrityError("恢复 journal 路径必须是目录")
        identifiers: list[str] = []
        for path in sorted(journals_root.iterdir()):
            if _is_reparse(path) or not path.is_file():
                raise BackupIntegrityError("恢复 journal 目录包含非法条目")
            if path.name.startswith(".") and path.name.endswith(".tmp"):
                candidate, separator, random_suffix = path.name[1:-4].partition(".json.")
                if separator and random_suffix:
                    try:
                        self._validate_restore_id(candidate)
                    except ValueError:
                        pass
                    else:
                        # 原子 journal 写入被进程终止时会留下该精确命名的普通
                        # 临时文件；恢复枚举只信任最终 UUID.json，不让残留阻断自救。
                        continue
            if path.suffix != ".json":
                raise BackupIntegrityError("恢复 journal 目录包含非法条目")
            try:
                identifiers.append(self._validate_restore_id(path.stem))
            except ValueError as exc:
                raise BackupIntegrityError("恢复 journal 文件名无效") from exc
        return identifiers

    def _pending_restore_ids(self) -> list[str]:
        pending: list[str] = []
        for restore_id in self._restore_journal_ids():
            _path, journal = self._load_restore_journal(restore_id)
            if journal["status"] in _PENDING_RESTORE_STATUSES:
                pending.append(restore_id)
        return pending

    def pending_restore_ids(self) -> list[str]:
        """返回未完成整库恢复 ID；只读，用于服务维护态门禁。"""
        with self._lock:
            return list(self._pending_restore_ids())

    def _assert_no_unresolved_restore(self) -> None:
        pending = self._pending_restore_ids()
        if pending:
            raise BackupConflict(
                "存在未完成的整库恢复，必须先执行恢复处理",
                code="restore_recovery_required",
                detail={"restore_ids": pending},
            )

    @staticmethod
    def _public_journal(journal: dict) -> dict:
        return {
            "restore_id": journal["restore_id"],
            "backup_id": journal["backup_id"],
            "pre_restore_backup_id": journal["pre_restore_backup_id"],
            "expected_current_fingerprint": journal["expected_current_fingerprint"],
            "status": journal["status"],
            "updated_at": journal["updated_at"],
            "roots": [
                {
                    key: root[key]
                    for key in (
                        "label",
                        "desired",
                        "had_current",
                        "move_attempted",
                        "old_moved",
                        "install_attempted",
                        "new_installed",
                    )
                }
                for root in journal["roots"]
            ],
        }

    def list_restore_journals(self) -> list[dict]:
        """严格读取所有恢复日志，不改变活动数据。"""
        with self._lock:
            journals = [
                self._public_journal(self._load_restore_journal(identifier)[1])
                for identifier in self._restore_journal_ids()
            ]
            journals.sort(key=lambda item: item["updated_at"], reverse=True)
            return journals

    def get_restore_journal(self, restore_id: str) -> dict:
        """查询恢复日志及当前整库状态，不执行恢复。"""
        with self._lock:
            _path, journal = self._load_restore_journal(restore_id)
            current = self._scan_sources().fingerprint
            try:
                target = self.get_verified(journal["backup_id"]).manifest[
                    "source_fingerprint"
                ]
            except FileNotFoundError:
                target = None
            if target is not None and current == target:
                active_state = "backup"
                suggested_action = "commit"
            elif current == journal["expected_current_fingerprint"]:
                active_state = "expected_current"
                suggested_action = "rollback"
            else:
                active_state = "mixed"
                suggested_action = "rollback"
            result = self._public_journal(journal)
            result.update({
                "current_fingerprint": current,
                "backup_fingerprint": target,
                "active_state": active_state,
                "suggested_action": (
                    None
                    if journal["status"] not in _PENDING_RESTORE_STATUSES
                    else suggested_action
                ),
            })
            return result

    def _recover_plans_to_expected(self, plans: list[_RootPlan]) -> None:
        """根据磁盘拓扑把硬退出后的多根状态补偿回原指纹。"""
        for plan in plans:
            for path in (plan.target, plan.stage, plan.rollback):
                if _is_reparse(path):
                    raise BackupIntegrityError("恢复补偿路径不能是 reparse point")
            if plan.had_current and not plan.target.exists() and not plan.rollback.exists():
                raise BackupOperationError(
                    "恢复补偿缺少原始根",
                    code="restore_recovery_required",
                )
            if not plan.had_current and plan.rollback.exists():
                raise BackupIntegrityError("恢复补偿发现不应存在的 rollback 根")

        for plan in reversed(plans):
            if plan.had_current and plan.rollback.exists():
                if plan.target.exists():
                    self._remove_generated(plan.target)
                self._replace_path(plan.rollback, plan.target)
                plan.old_moved = False
                plan.new_installed = False
            elif not plan.had_current and plan.target.exists():
                self._remove_generated(plan.target)
                plan.new_installed = False

    def _hydrate_missing_rollback(
        self,
        plan: _RootPlan,
        pre_restore: VerifiedBackup,
    ) -> None:
        """从已验证的 pre_restore ZIP 重建丢失的单个 rollback 根。"""
        if (
            not plan.had_current
            or not pre_restore.manifest["root_states"][plan.label]
            or plan.rollback.exists()
            or _is_reparse(plan.rollback)
        ):
            raise BackupIntegrityError("恢复前备份与缺失 rollback 根状态不一致")
        if _has_reparse_in_chain(plan.target.parent):
            raise BackupIntegrityError("恢复补偿目标父目录不能是 reparse point")
        if os.path.lexists(plan.stage):
            self._remove_generated(plan.stage)
        if plan.directory:
            plan.stage.mkdir(parents=True)

        matched = 0
        try:
            with zipfile.ZipFile(pre_restore.path, mode="r") as archive:
                for item in pre_restore.manifest["items"]:
                    pure = PurePosixPath(item["path"])
                    if plan.label == "settings":
                        if item["path"] != "settings.json":
                            continue
                        relative: tuple[str, ...] = ()
                    else:
                        if not pure.parts or pure.parts[0] != plan.label:
                            continue
                        relative = tuple(pure.parts[1:])
                        if not relative:
                            raise BackupIntegrityError("恢复前备份根成员路径无效")
                    matched += 1
                    target = plan.stage if not relative else plan.stage.joinpath(*relative)
                    if plan.directory:
                        target.parent.mkdir(parents=True, exist_ok=True)
                    elif relative:
                        raise BackupIntegrityError("settings 恢复前备份路径无效")
                    digest = hashlib.sha256()
                    size = 0
                    with archive.open(item["path"], mode="r") as source, target.open("xb") as output:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            size += len(chunk)
                            digest.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if size != item["size"] or digest.hexdigest() != item["sha256"]:
                        raise BackupIntegrityError("恢复前备份 rollback 重建校验失败")
            if not plan.directory and matched != 1:
                raise BackupIntegrityError("恢复前备份缺少 settings rollback")
            self._replace_path(plan.stage, plan.rollback)
        except BaseException:
            try:
                self._remove_generated(plan.stage)
            except OSError:
                pass
            raise

    def _hydrate_missing_rollbacks(
        self,
        plans: list[_RootPlan],
        pre_restore: VerifiedBackup,
    ) -> bool:
        """仅在 journal 表明原根已参与切换时，用 pre_restore 补齐丢失根。"""
        hydrated = False
        for plan in plans:
            needs_fallback = (
                plan.had_current
                and not plan.rollback.exists()
                and (plan.move_attempted or not plan.target.exists())
            )
            if needs_fallback:
                self._hydrate_missing_rollback(plan, pre_restore)
                hydrated = True
        return hydrated

    def recover_restore(self, restore_id: str) -> dict:
        """显式解析硬退出恢复：完整目标则提交，其余状态回滚到原指纹。"""
        with library_lock.exclusive():
            with self._lock:
                pending = self._pending_restore_ids()
                if restore_id not in pending:
                    _path, journal = self._load_restore_journal(restore_id)
                    if journal["status"] not in _PENDING_RESTORE_STATUSES:
                        return {
                            "recovered": False,
                            "action": "none",
                            **self.get_restore_journal(restore_id),
                        }
                if pending != [restore_id]:
                    raise BackupConflict(
                        "恢复日志集合不唯一，禁止自动处理",
                        code="restore_recovery_conflict",
                        detail={"restore_ids": pending},
                    )
                journal_path, journal = self._load_restore_journal(restore_id)
                pre_restore = self.get_verified(journal["pre_restore_backup_id"])
                expected = journal["expected_current_fingerprint"]
                try:
                    target_backup = self.get_verified(journal["backup_id"])
                    target = target_backup.manifest["source_fingerprint"]
                except FileNotFoundError:
                    target_backup = None
                    target = None
                if pre_restore.manifest["source_fingerprint"] != expected:
                    raise BackupIntegrityError(
                        "恢复前备份与 journal 预期指纹不一致",
                    )
                plans = self._plans_from_journal(journal)
                if target_backup is None:
                    raise BackupIntegrityError("恢复目标备份缺失")
                for root, plan in zip(journal["roots"], plans, strict=True):
                    label = plan.label
                    if (
                        root["had_current"]
                        != pre_restore.manifest["root_states"][label]
                        or root["desired"]
                        != target_backup.manifest["root_states"][label]
                    ):
                        raise BackupIntegrityError(
                            "恢复 journal 根状态与已验证备份不一致"
                        )
                current = self._scan_sources().fingerprint
                recovered_from_pre_restore = False
                if target is not None and current == target:
                    action = "commit"
                    status = "committed"
                elif current == expected:
                    action = "rollback"
                    status = "rolled_back"
                else:
                    self._persist_journal(
                        journal_path,
                        restore_id=restore_id,
                        backup_id=journal["backup_id"],
                        pre_restore_backup_id=journal["pre_restore_backup_id"],
                        expected_fingerprint=expected,
                        status="recovering",
                        plans=plans,
                    )
                    try:
                        recovered_from_pre_restore = self._hydrate_missing_rollbacks(
                            plans,
                            pre_restore,
                        )
                        self._recover_plans_to_expected(plans)
                        if self._scan_sources().fingerprint != expected:
                            raise BackupOperationError(
                                "恢复补偿后的活动数据指纹不匹配",
                                code="restore_recovery_required",
                            )
                    except Exception as exc:
                        try:
                            self._persist_journal(
                                journal_path,
                                restore_id=restore_id,
                                backup_id=journal["backup_id"],
                                pre_restore_backup_id=journal["pre_restore_backup_id"],
                                expected_fingerprint=expected,
                                status="needs_recovery",
                                plans=plans,
                            )
                        except Exception:
                            pass
                        if isinstance(exc, BackupError):
                            raise
                        raise BackupOperationError(
                            "恢复补偿未完整结束",
                            code="restore_recovery_required",
                            backup_id=journal["backup_id"],
                        ) from exc
                    action = "rollback"
                    status = "rolled_back"
                self._persist_journal(
                    journal_path,
                    restore_id=restore_id,
                    backup_id=journal["backup_id"],
                    pre_restore_backup_id=journal["pre_restore_backup_id"],
                    expected_fingerprint=expected,
                    status=status,
                    plans=plans,
                )
                self._cleanup_plans(plans)
                return {
                    "recovered": True,
                    "action": action,
                    "recovered_from_pre_restore": recovered_from_pre_restore,
                    **self.get_restore_journal(restore_id),
                }

    def _rollback_plans(self, plans: list[_RootPlan]) -> None:
        failures: list[BaseException] = []
        for plan in reversed(plans):
            try:
                installed = (
                    plan.install_attempted
                    and plan.target.exists()
                    and not plan.stage.exists()
                )
                if installed:
                    self._replace_path(plan.target, plan.stage)
                    plan.new_installed = False
                old_was_moved = plan.old_moved or (
                    plan.move_attempted
                    and plan.rollback.exists()
                    and not plan.target.exists()
                )
                if old_was_moved:
                    if plan.target.exists() or not plan.rollback.exists():
                        raise BackupIntegrityError("恢复补偿检测到根状态冲突")
                    self._replace_path(plan.rollback, plan.target)
                    plan.old_moved = False
                elif not plan.had_current and plan.target.exists():
                    raise BackupIntegrityError("恢复补偿检测到意外目标")
            except BaseException as exc:
                failures.append(exc)
        if failures:
            raise BackupOperationError(
                "恢复补偿未完整结束",
                code="restore_recovery_required",
            ) from failures[0]

    def _cleanup_plans(self, plans: Iterable[_RootPlan]) -> None:
        for plan in plans:
            for path in (plan.stage, plan.rollback):
                try:
                    self._remove_generated(path)
                except OSError:
                    pass

    def restore(
        self,
        backup_id: str,
        *,
        expected_current_fingerprint: str,
        confirm_conflicts: bool = False,
    ) -> dict:
        """在整库独占锁内执行多根恢复。"""
        with library_lock.exclusive():
            return self._restore_locked(
                backup_id,
                expected_current_fingerprint=expected_current_fingerprint,
                confirm_conflicts=confirm_conflicts,
            )

    def _restore_locked(
        self,
        backup_id: str,
        *,
        expected_current_fingerprint: str,
        confirm_conflicts: bool = False,
    ) -> dict:
        if (
            not isinstance(expected_current_fingerprint, str)
            or not _SHA256_RE.fullmatch(expected_current_fingerprint)
        ):
            raise ValueError("expected_current_fingerprint 必须是 SHA-256")
        if type(confirm_conflicts) is not bool:
            raise ValueError("confirm_conflicts 必须是布尔值")
        with self._lock:
            self._assert_no_unresolved_restore()
            verified = self.get_verified(backup_id)
            self._validate_structured_payloads(verified)
            current = self._scan_sources()
            if current.fingerprint != expected_current_fingerprint:
                raise BackupConflict(
                    "当前数据指纹已变化",
                    code="current_changed",
                    detail={
                        "expected_current_fingerprint": expected_current_fingerprint,
                        "current_fingerprint": current.fingerprint,
                    },
                )
            changes = self._changes(current, verified.manifest)
            conflicts = [change for change in changes if change["action"] in {"replace", "remove"}]
            if changes and not confirm_conflicts:
                raise BackupConflict(
                    "恢复包含数据差异，需要显式确认",
                    code="confirmation_required",
                    detail={
                        "backup_id": backup_id,
                        "current_fingerprint": current.fingerprint,
                        "backup_fingerprint": verified.manifest["source_fingerprint"],
                        "changes": changes,
                        "conflicts": conflicts,
                        "requires_confirmation": True,
                    },
                )

            pre_restore = self.create_backup(
                f"pre_restore:{backup_id}",
                kind="pre_restore",
            )
            pre_restore_backup_id = pre_restore["backup_id"]
            if pre_restore["source_fingerprint"] != expected_current_fingerprint:
                raise BackupConflict("创建恢复前备份时当前数据已变化", code="current_changed")
            if not changes:
                return {
                    "restored": True,
                    "backup_id": backup_id,
                    "pre_restore_backup_id": pre_restore_backup_id,
                    "current_fingerprint": current.fingerprint,
                }

            restore_id = str(uuid4())
            try:
                plans = self._prepare_staging(verified, restore_id)
            except BackupError:
                raise
            except BaseException as exc:
                raise BackupOperationError(
                    "恢复 staging 准备失败",
                    code="restore_failed",
                    backup_id=backup_id,
                ) from exc
            journal_path = self.backups_root / ".journals" / f"{restore_id}.json"
            try:
                rechecked = self._scan_sources()
                if rechecked.fingerprint != expected_current_fingerprint:
                    raise BackupConflict("恢复切换前当前数据已变化", code="current_changed")
                self._persist_journal(
                    journal_path,
                    restore_id=restore_id,
                    backup_id=backup_id,
                    pre_restore_backup_id=pre_restore_backup_id,
                    expected_fingerprint=expected_current_fingerprint,
                    status="prepared",
                    plans=plans,
                )
                for plan in plans:
                    if plan.target.exists():
                        plan.move_attempted = True
                        self._persist_journal(
                            journal_path,
                            restore_id=restore_id,
                            backup_id=backup_id,
                            pre_restore_backup_id=pre_restore_backup_id,
                            expected_fingerprint=expected_current_fingerprint,
                            status="switching",
                            plans=plans,
                        )
                        self._replace_path(plan.target, plan.rollback)
                        plan.old_moved = True
                        self._persist_journal(
                            journal_path,
                            restore_id=restore_id,
                            backup_id=backup_id,
                            pre_restore_backup_id=pre_restore_backup_id,
                            expected_fingerprint=expected_current_fingerprint,
                            status="switching",
                            plans=plans,
                        )
                    if plan.desired:
                        plan.install_attempted = True
                        self._persist_journal(
                            journal_path,
                            restore_id=restore_id,
                            backup_id=backup_id,
                            pre_restore_backup_id=pre_restore_backup_id,
                            expected_fingerprint=expected_current_fingerprint,
                            status="switching",
                            plans=plans,
                        )
                        self._replace_path(plan.stage, plan.target)
                        plan.new_installed = True
                        self._persist_journal(
                            journal_path,
                            restore_id=restore_id,
                            backup_id=backup_id,
                            pre_restore_backup_id=pre_restore_backup_id,
                            expected_fingerprint=expected_current_fingerprint,
                            status="switching",
                            plans=plans,
                        )
                restored = self._scan_sources()
                if restored.fingerprint != verified.manifest["source_fingerprint"]:
                    raise BackupIntegrityError("恢复后活动数据指纹不匹配")
                self._persist_journal(
                    journal_path,
                    restore_id=restore_id,
                    backup_id=backup_id,
                    pre_restore_backup_id=pre_restore_backup_id,
                    expected_fingerprint=expected_current_fingerprint,
                    status="committed",
                    plans=plans,
                )
            except Exception as exc:
                attempted_switch = any(
                    plan.move_attempted or plan.install_attempted
                    for plan in plans
                )
                if not attempted_switch:
                    self._cleanup_plans(plans)
                    if isinstance(exc, (BackupConflict, BackupIntegrityError)):
                        raise exc
                    raise BackupOperationError(
                        "恢复准备失败，活动数据未变更",
                        code="restore_failed",
                        backup_id=backup_id,
                    ) from exc
                compensation_error: BaseException | None = None
                try:
                    self._rollback_plans(plans)
                    rolled_back = self._scan_sources()
                    if rolled_back.fingerprint != expected_current_fingerprint:
                        raise BackupOperationError(
                            "恢复补偿后的活动数据指纹不匹配",
                            code="restore_recovery_required",
                        )
                    try:
                        self._persist_journal(
                            journal_path,
                            restore_id=restore_id,
                            backup_id=backup_id,
                            pre_restore_backup_id=pre_restore_backup_id,
                            expected_fingerprint=expected_current_fingerprint,
                            status="rolled_back",
                            plans=plans,
                        )
                    except BaseException:
                        pass
                except BaseException as rollback_exc:
                    compensation_error = rollback_exc
                if compensation_error is not None:
                    try:
                        self._persist_journal(
                            journal_path,
                            restore_id=restore_id,
                            backup_id=backup_id,
                            pre_restore_backup_id=pre_restore_backup_id,
                            expected_fingerprint=expected_current_fingerprint,
                            status="needs_recovery",
                            plans=plans,
                        )
                    except BaseException:
                        pass
                    raise BackupOperationError(
                        "恢复失败且补偿未完整结束",
                        code="restore_recovery_required",
                        backup_id=backup_id,
                    ) from compensation_error
                self._cleanup_plans(plans)
                raise BackupOperationError(
                    "恢复切换失败，活动数据已补偿",
                    code="restore_failed",
                    backup_id=backup_id,
                ) from exc

            self._cleanup_plans(plans)
            return {
                "restored": True,
                "backup_id": backup_id,
                "pre_restore_backup_id": pre_restore_backup_id,
                "current_fingerprint": verified.manifest["source_fingerprint"],
            }

    @staticmethod
    def _canonical_sha256(payload: object) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _retention_plan_locked(self) -> dict:
        listed = self.list_backups()
        states: list[dict] = []
        deletions: list[dict] = []
        for item in listed:
            backup_id = item["backup_id"]
            if item.get("status") == "invalid":
                path = self.backups_root / f"{backup_id}.zip"
                _size, archive_sha256 = self._hash_stable_file(path)
                states.append({
                    "backup_id": backup_id,
                    "status": "invalid",
                    "code": item["code"],
                    "archive_sha256": archive_sha256,
                })
                continue
            path = self._backup_path(backup_id)
            _size, archive_sha256 = self._hash_stable_file(path)
            state = {
                "backup_id": backup_id,
                "status": "valid",
                "created_at": item["created_at"],
                "expired": item["expired"],
                "expired_reasons": list(item["expired_reasons"]),
                "archive_sha256": archive_sha256,
            }
            states.append(state)
            if item["expired"]:
                deletions.append({
                    "backup_id": backup_id,
                    "created_at": item["created_at"],
                    "expired_reasons": list(item["expired_reasons"]),
                    "archive_sha256": archive_sha256,
                })
        canonical = {
            "policy": {
                "retention_days": self.retention_days,
                "retention_count": self.retention_count,
            },
            "backup_states": states,
            "deletions": deletions,
        }
        return {
            **canonical,
            "plan_fingerprint": self._canonical_sha256(canonical),
            "generated_at": _utc_now().isoformat(),
            "delete_count": len(deletions),
        }

    def plan_retention(self) -> dict:
        """生成纯读取保留计划；不会删除或改写任何 ZIP。"""
        with self._lock:
            return self._retention_plan_locked()

    def _persist_operation_record(
        self,
        directory_name: str,
        filename: str,
        record: dict,
    ) -> Path:
        root = self.backups_root / directory_name
        if _has_reparse_in_chain(root):
            raise BackupIntegrityError("备份操作记录目录不能是 reparse point")
        self.backups_root.mkdir(parents=True, exist_ok=True)
        root.mkdir(parents=True, exist_ok=True)
        if _has_reparse_in_chain(root):
            raise BackupIntegrityError("备份操作记录目录不能是 reparse point")
        path = root / filename
        if path.parent != root or _is_reparse(path):
            raise BackupIntegrityError("备份操作记录路径越界")
        _atomic_json(path, record)
        return path

    def _delete_backup_path(self, path: Path) -> None:
        path.unlink()

    def apply_retention(
        self,
        *,
        plan_fingerprint: str,
        confirm: bool,
    ) -> dict:
        """持计划指纹和显式确认执行过期 ZIP 清理。"""
        if not isinstance(plan_fingerprint, str) or not _SHA256_RE.fullmatch(plan_fingerprint):
            raise ValueError("plan_fingerprint 必须是 SHA-256")
        if type(confirm) is not bool:
            raise ValueError("confirm 必须是布尔值")
        if not confirm:
            raise BackupConflict(
                "保留清理需要显式确认",
                code="confirmation_required",
                detail={"requires_confirmation": True},
            )
        with library_lock.exclusive():
            with self._lock:
                self._assert_no_unresolved_restore()
                plan = self._retention_plan_locked()
                if plan["plan_fingerprint"] != plan_fingerprint:
                    raise BackupConflict(
                        "保留计划已变化",
                        code="retention_plan_changed",
                        detail={
                            "expected_plan_fingerprint": plan_fingerprint,
                            "current_plan_fingerprint": plan["plan_fingerprint"],
                        },
                    )
                operation_id = str(uuid4())
                deleted: list[str] = []
                for index, item in enumerate(plan["deletions"]):
                    backup_id = item["backup_id"]
                    path = self._backup_path(backup_id)
                    self.get_verified(backup_id)
                    _size, archive_sha256 = self._hash_stable_file(path)
                    if archive_sha256 != item["archive_sha256"]:
                        raise BackupConflict(
                            "保留计划中的 ZIP 已变化",
                            code="retention_plan_changed",
                        )
                    audit_base = f"{operation_id}-{index:04d}-{backup_id}"
                    self._persist_operation_record(
                        ".retention-audit",
                        f"{audit_base}-intent.json",
                        {
                            "record_version": 1,
                            "operation_id": operation_id,
                            "action": "delete_intent",
                            "backup_id": backup_id,
                            "plan_fingerprint": plan_fingerprint,
                            "archive_sha256": archive_sha256,
                            "persisted_at": _utc_now().isoformat(),
                        },
                    )
                    try:
                        self._delete_backup_path(path)
                    except OSError as exc:
                        raise BackupOperationError(
                            "过期备份删除失败，审计意图已持久化",
                            code="retention_delete_failed",
                            backup_id=backup_id,
                        ) from exc
                    self._persist_operation_record(
                        ".retention-audit",
                        f"{audit_base}-deleted.json",
                        {
                            "record_version": 1,
                            "operation_id": operation_id,
                            "action": "deleted",
                            "backup_id": backup_id,
                            "plan_fingerprint": plan_fingerprint,
                            "archive_sha256": archive_sha256,
                            "persisted_at": _utc_now().isoformat(),
                        },
                    )
                    deleted.append(backup_id)
                return {
                    "applied": True,
                    "operation_id": operation_id,
                    "plan_fingerprint": plan_fingerprint,
                    "deleted_backup_ids": deleted,
                }

    def _drill_path(self, drill_id: str) -> Path:
        drill_id = self._validate_drill_id(drill_id)
        root = _absolute(self.backups_root / ".drills")
        path = _absolute(root / f"{drill_id}.json")
        if path.parent != root:
            raise ValueError("drill_id 路径越界")
        return path

    def _write_drill_record(self, drill_id: str, record: dict) -> None:
        self._persist_operation_record(
            ".drills",
            f"{self._validate_drill_id(drill_id)}.json",
            record,
        )

    def drill(self, backup_id: str) -> dict:
        """临时解包并扫描备份，不切换活动数据根。"""
        backup_id = self._validate_backup_id(backup_id)
        drill_id = str(uuid4())
        started_at = _utc_now().isoformat()
        with library_lock.exclusive():
            with self._lock:
                self._assert_no_unresolved_restore()
                active_before = self._scan_sources().fingerprint
                workspace_root = _absolute(self.backups_root / ".drill-workspaces")
                workspace = _absolute(workspace_root / drill_id)
                workspace_cleaned = not os.path.lexists(workspace)
                try:
                    verified = self.get_verified(backup_id)
                    self._validate_structured_payloads(verified)
                    manifest = verified.manifest
                    if workspace.parent != workspace_root:
                        raise BackupIntegrityError("演练临时目录路径越界")
                    for active in (self.projects_root, self.prompts_dir, self.settings_path):
                        if workspace.is_relative_to(active) or active.is_relative_to(workspace):
                            raise BackupIntegrityError("演练临时目录与活动数据根重叠")
                    if _has_reparse_in_chain(self.backups_root):
                        raise BackupIntegrityError("备份目录不能是 reparse point")
                    self.backups_root.mkdir(parents=True, exist_ok=True)
                    workspace_root.mkdir(exist_ok=True)
                    if _has_reparse_in_chain(workspace_root) or os.path.lexists(workspace):
                        raise BackupIntegrityError("演练临时目录无效")
                    workspace.mkdir()
                    try:
                        projects = workspace / "projects"
                        prompts = workspace / "prompts"
                        settings = workspace / "settings.json"
                        if manifest["root_states"]["projects"]:
                            projects.mkdir()
                        if manifest["root_states"]["prompts"]:
                            prompts.mkdir()
                        with zipfile.ZipFile(verified.path, mode="r") as archive:
                            for item in manifest["items"]:
                                pure = PurePosixPath(item["path"])
                                if item["path"] == "settings.json":
                                    target = settings
                                elif pure.parts[0] == "projects":
                                    target = projects.joinpath(*pure.parts[1:])
                                elif pure.parts[0] == "prompts":
                                    target = prompts.joinpath(*pure.parts[1:])
                                else:
                                    raise BackupIntegrityError("演练成员不属于白名单根")
                                target.parent.mkdir(parents=True, exist_ok=True)
                                digest = hashlib.sha256()
                                size = 0
                                with archive.open(item["path"], "r") as source, target.open("xb") as output:
                                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                        size += len(chunk)
                                        digest.update(chunk)
                                        output.write(chunk)
                                if size != item["size"] or digest.hexdigest() != item["sha256"]:
                                    raise BackupIntegrityError("演练解包文件校验失败")
                        scanner = BackupManager(
                            workspace / "archives",
                            projects,
                            prompts,
                            settings,
                            retention_days=self.retention_days,
                            retention_count=self.retention_count,
                        )
                        unpacked = scanner._scan_sources()
                        if unpacked.fingerprint != manifest["source_fingerprint"]:
                            raise BackupIntegrityError("演练解包后的数据指纹不匹配")
                    finally:
                        try:
                            if os.path.lexists(workspace) and not _is_reparse(workspace):
                                shutil.rmtree(workspace)
                        except OSError:
                            pass
                        workspace_cleaned = not os.path.lexists(workspace)
                    if not workspace_cleaned:
                        raise BackupOperationError(
                            "演练临时目录清理失败",
                            code="backup_drill_cleanup_failed",
                            backup_id=backup_id,
                        )
                    active_after = self._scan_sources().fingerprint
                    if active_after != active_before:
                        raise BackupConflict(
                            "演练期间活动数据发生变化",
                            code="source_changed",
                        )
                    record = {
                        "record_version": DRILL_RECORD_VERSION,
                        "drill_id": drill_id,
                        "backup_id": backup_id,
                        "status": "passed",
                        "backup_fingerprint": manifest["source_fingerprint"],
                        "active_fingerprint": active_after,
                        "checked_file_count": len(manifest["items"]),
                        "started_at": started_at,
                        "completed_at": _utc_now().isoformat(),
                        "workspace_cleaned": workspace_cleaned,
                        "error": None,
                    }
                    self._write_drill_record(drill_id, record)
                    return record
                except Exception as exc:
                    detail = (
                        exc.as_detail()
                        if isinstance(exc, BackupError)
                        else {"code": "backup_drill_failed", "message": "备份恢复演练失败"}
                    )
                    record = {
                        "record_version": DRILL_RECORD_VERSION,
                        "drill_id": drill_id,
                        "backup_id": str(backup_id),
                        "status": "failed",
                        "backup_fingerprint": None,
                        "active_fingerprint": active_before,
                        "checked_file_count": 0,
                        "started_at": started_at,
                        "completed_at": _utc_now().isoformat(),
                        "workspace_cleaned": workspace_cleaned,
                        "error": detail,
                    }
                    try:
                        self._write_drill_record(drill_id, record)
                    except Exception as record_error:
                        raise BackupOperationError(
                            "备份演练失败且结果记录无法持久化",
                            code="backup_drill_record_failed",
                            backup_id=str(backup_id),
                        ) from record_error
                    raise

    def get_drill(self, drill_id: str) -> dict:
        with self._lock:
            path = self._drill_path(drill_id)
            if _has_reparse_in_chain(path.parent) or _is_reparse(path):
                raise BackupIntegrityError("演练记录不能是 reparse point")
            if not path.is_file():
                raise FileNotFoundError(f"演练记录不存在: {drill_id}")
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BackupIntegrityError("演练记录无法读取") from exc
            required = {
                "record_version",
                "drill_id",
                "backup_id",
                "status",
                "backup_fingerprint",
                "active_fingerprint",
                "checked_file_count",
                "started_at",
                "completed_at",
                "workspace_cleaned",
                "error",
            }
            if not isinstance(record, dict) or set(record) != required:
                raise BackupIntegrityError("演练记录结构无效")
            if record["record_version"] != DRILL_RECORD_VERSION:
                raise BackupIntegrityError("演练记录版本不兼容")
            if record["drill_id"] != self._validate_drill_id(drill_id):
                raise BackupIntegrityError("演练记录 ID 与文件名不一致")
            try:
                self._validate_backup_id(record["backup_id"])
            except ValueError as exc:
                raise BackupIntegrityError("演练记录的 backup_id 无效") from exc
            if record["status"] not in {"passed", "failed"}:
                raise BackupIntegrityError("演练记录状态无效")
            for time_field in ("started_at", "completed_at"):
                try:
                    timestamp = datetime.fromisoformat(record[time_field])
                except (TypeError, ValueError) as exc:
                    raise BackupIntegrityError("演练记录时间无效") from exc
                if timestamp.tzinfo is None:
                    raise BackupIntegrityError("演练记录时间必须包含时区")
            if (
                isinstance(record["checked_file_count"], bool)
                or not isinstance(record["checked_file_count"], int)
                or record["checked_file_count"] < 0
                or type(record["workspace_cleaned"]) is not bool
            ):
                raise BackupIntegrityError("演练记录计数或清理状态无效")
            for fingerprint_field in ("backup_fingerprint", "active_fingerprint"):
                value = record[fingerprint_field]
                if value is not None and (
                    not isinstance(value, str) or not _SHA256_RE.fullmatch(value)
                ):
                    raise BackupIntegrityError("演练记录指纹无效")
            error = record["error"]
            if error is not None and (
                not isinstance(error, dict)
                or set(error) - {"code", "message", "backup_id"}
                or not isinstance(error.get("code"), str)
                or not isinstance(error.get("message"), str)
            ):
                raise BackupIntegrityError("演练记录错误结构无效")
            return record

    def list_drills(self) -> list[dict]:
        with self._lock:
            root = self.backups_root / ".drills"
            if _has_reparse_in_chain(root):
                raise BackupIntegrityError("演练记录目录不能是 reparse point")
            if not root.exists():
                return []
            if not root.is_dir():
                raise BackupIntegrityError("演练记录路径必须是目录")
            records: list[dict] = []
            for path in sorted(root.iterdir()):
                if _is_reparse(path) or not path.is_file() or path.suffix != ".json":
                    raise BackupIntegrityError("演练记录目录包含非法条目")
                records.append(self.get_drill(path.stem))
            records.sort(key=lambda item: item["completed_at"], reverse=True)
            return records
