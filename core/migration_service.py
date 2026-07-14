"""旧版平铺数据到项目目录的可审计迁移服务。"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

import yaml

from core.path_policy import validate_file_id


JOURNAL_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BACKUP_NAMES = frozenset({"backup", "backups", ".backup", ".backups"})
_WINDOWS_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
})
_WINDOWS_INVALID = frozenset('<>:"|?*')
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class MigrationError(RuntimeError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code

    def as_detail(self) -> dict:
        return {"code": self.code, "message": str(self)}


class MigrationConflict(MigrationError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "migration_conflict",
        detail: dict | None = None,
    ):
        super().__init__(message, code=code)
        self.detail = dict(detail or {})

    def as_detail(self) -> dict:
        return {**super().as_detail(), **self.detail}


class MigrationIntegrityError(MigrationError):
    def __init__(self, message: str, *, code: str = "invalid_source"):
        super().__init__(message, code=code)


class MigrationOperationError(MigrationError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "migration_failed",
        migration_id: str | None = None,
    ):
        super().__init__(message, code=code)
        self.migration_id = migration_id

    def as_detail(self) -> dict:
        detail = super().as_detail()
        if self.migration_id is not None:
            detail["migration_id"] = self.migration_id
        return detail


@dataclass(frozen=True)
class _LegacyRecord:
    path: Path
    source: str
    target: str
    size: int
    sha256: str
    category: str


@dataclass(frozen=True)
class _LegacySnapshot:
    records: tuple[_LegacyRecord, ...]
    root_states: dict[str, bool]
    fingerprint: str


@dataclass(frozen=True)
class _TargetRecord:
    path: Path
    logical: str
    size: int
    sha256: str

    def fingerprint_item(self) -> dict:
        return {"path": self.logical, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class _TargetSnapshot:
    records: tuple[_TargetRecord, ...]
    directories: tuple[str, ...]
    present: bool
    fingerprint: str


@dataclass(frozen=True)
class _PlanComputation:
    plan: dict
    records_by_source: dict[str, _LegacyRecord]


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


def _validate_component(name: str, *, code: str = "invalid_source") -> None:
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
        raise MigrationIntegrityError("数据路径包含不安全的名称", code=code)


def _canonical_hash(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _snapshot_fingerprint(
    root_states: dict[str, bool],
    items: list[dict],
    *,
    directories: list[str] | None = None,
) -> str:
    material: dict[str, Any] = {"root_states": root_states, "items": items}
    if directories is not None:
        material["directories"] = directories
    return _canonical_hash(material)


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


class LegacyMigrationService:
    """生成纯读计划，并以 CAS、备份、staging 和补偿完成一次迁移。"""

    def __init__(
        self,
        data_root: Path,
        projects_root: Path,
        migrations_root: Path,
        backup_manager,
        target_project: str,
    ):
        self.data_root = _absolute(Path(data_root))
        self.projects_root = _absolute(Path(projects_root))
        self.migrations_root = _absolute(Path(migrations_root))
        self.backup_manager = backup_manager
        self.target_project = validate_file_id(target_project, label="目标项目 ID")
        self.target_root = _absolute(self.projects_root / self.target_project)
        self._source_roots = {
            "characters": self.data_root / "characters",
            "worldbook": self.data_root / "worldbook",
            "user": self.data_root / "user",
            "saves": self.data_root / "saves",
        }
        self._validate_layout()
        key = str(self.migrations_root).casefold()
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(key, threading.RLock())
        self._install_receipts: dict[Path, dict] = {}
        self._directory_receipts: list[Path] = []

    def _validate_layout(self) -> None:
        if self.target_root.parent != self.projects_root:
            raise ValueError("目标项目路径越界")
        if _overlaps(self.migrations_root, self.projects_root):
            raise ValueError("迁移工作目录不能与项目目录重叠")
        for root in self._source_roots.values():
            root = _absolute(root)
            if _overlaps(root, self.projects_root):
                raise ValueError("旧数据目录不能与项目目录重叠")
            if _overlaps(root, self.migrations_root):
                raise ValueError("迁移工作目录不能与旧数据目录重叠")

    @staticmethod
    def _validate_hash(value: str, *, label: str) -> str:
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise ValueError(f"{label} 必须是 SHA-256")
        return value

    @classmethod
    def _validate_migration_id(cls, value: str) -> str:
        return cls._validate_hash(value, label="migration_id")

    @staticmethod
    def _read_structured_file(
        path: Path,
        *,
        suffix: str,
        change_code: str,
        invalid_code: str,
    ) -> tuple[int, str]:
        try:
            before = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or _is_reparse(path):
                raise MigrationIntegrityError("数据源包含非普通文件", code=invalid_code)
            data = path.read_bytes()
            after = os.stat(path, follow_symlinks=False)
        except MigrationError:
            raise
        except (OSError, FileNotFoundError) as exc:
            raise MigrationConflict("数据在扫描期间发生变化", code=change_code) from exc
        if (
            before.st_size != after.st_size
            or before.st_size != len(data)
            or before.st_mtime_ns != after.st_mtime_ns
            or getattr(before, "st_ino", 0) != getattr(after, "st_ino", 0)
            or _is_reparse(path)
        ):
            raise MigrationConflict("数据在扫描期间发生变化", code=change_code)
        try:
            text = data.decode("utf-8")
            if suffix == ".json":
                json.loads(text)
            elif suffix in {".yaml", ".yml"}:
                yaml.safe_load(text)
        except (UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
            raise MigrationIntegrityError("结构化数据不是有效 UTF-8/JSON/YAML", code=invalid_code) from exc
        return len(data), hashlib.sha256(data).hexdigest()

    @staticmethod
    def _read_raw_file(
        path: Path,
        *,
        change_code: str,
        invalid_code: str,
    ) -> tuple[int, str]:
        try:
            before = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or _is_reparse(path):
                raise MigrationIntegrityError("数据包含非普通文件", code=invalid_code)
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
            after = os.stat(path, follow_symlinks=False)
        except MigrationError:
            raise
        except (OSError, FileNotFoundError) as exc:
            raise MigrationConflict("数据在扫描期间发生变化", code=change_code) from exc
        if (
            before.st_size != after.st_size
            or before.st_size != size
            or before.st_mtime_ns != after.st_mtime_ns
            or getattr(before, "st_ino", 0) != getattr(after, "st_ino", 0)
            or _is_reparse(path)
        ):
            raise MigrationConflict("数据在扫描期间发生变化", code=change_code)
        return size, digest.hexdigest()

    @staticmethod
    def _entry_excluded(
        name: str,
        *,
        category: str,
        relative: tuple[str, ...],
        is_dir: bool,
    ) -> bool:
        lowered = name.casefold()
        if lowered.startswith("_template") or lowered in _BACKUP_NAMES:
            return True
        if name.startswith("."):
            return not (
                category == "saves"
                and not relative
                and is_dir
                and lowered == ".history"
            )
        return False

    def _legacy_target(self, category: str, relative: tuple[str, ...]) -> str:
        if category == "user":
            return PurePosixPath(self.target_project, "user.yaml").as_posix()
        return PurePosixPath(self.target_project, category, *relative).as_posix()

    def _scan_legacy_root(self, category: str, root: Path) -> tuple[list[_LegacyRecord], bool]:
        if _has_reparse_in_chain(root):
            raise MigrationIntegrityError("旧数据根不能是 symlink 或 reparse point")
        if not root.exists():
            return [], False
        if not root.is_dir():
            raise MigrationIntegrityError("旧数据根必须是普通目录")
        extensions = {".yaml", ".yml"} if category != "saves" else {".json"}
        records: list[_LegacyRecord] = []

        def walk(directory: Path, relative: tuple[str, ...]) -> None:
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name)
            except OSError as exc:
                raise MigrationConflict("旧数据目录在扫描期间发生变化", code="source_changed") from exc
            for entry in entries:
                _validate_component(entry.name)
                current = Path(entry.path)
                if entry.is_symlink() or _is_reparse(current):
                    raise MigrationIntegrityError("旧数据包含 symlink 或 reparse point")
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError as exc:
                    raise MigrationConflict("旧数据在扫描期间发生变化", code="source_changed") from exc
                if self._entry_excluded(
                    entry.name,
                    category=category,
                    relative=relative,
                    is_dir=is_dir,
                ):
                    continue
                parts = (*relative, entry.name)
                if is_dir:
                    walk(current, parts)
                    continue
                if not is_file:
                    raise MigrationIntegrityError("旧数据包含不支持的文件类型")
                suffix = current.suffix.casefold()
                if suffix not in extensions:
                    continue
                size, digest = self._read_structured_file(
                    current,
                    suffix=suffix,
                    change_code="source_changed",
                    invalid_code="invalid_source",
                )
                source = PurePosixPath(category, *parts).as_posix()
                records.append(_LegacyRecord(
                    current,
                    source,
                    self._legacy_target(category, parts),
                    size,
                    digest,
                    category,
                ))

        walk(root, ())
        return records, True

    def _scan_legacy(self) -> _LegacySnapshot:
        records: list[_LegacyRecord] = []
        root_states: dict[str, bool] = {}
        for category in ("characters", "worldbook", "user", "saves"):
            scanned, present = self._scan_legacy_root(category, self._source_roots[category])
            records.extend(scanned)
            root_states[category] = present
        records.sort(key=lambda record: (record.target, record.source))
        items = [
            {"path": record.source, "size": record.size, "sha256": record.sha256}
            for record in sorted(records, key=lambda value: value.source)
        ]
        return _LegacySnapshot(
            tuple(records),
            root_states,
            _snapshot_fingerprint(root_states, items),
        )

    def _scan_target(self) -> _TargetSnapshot:
        if _has_reparse_in_chain(self.target_root):
            raise MigrationIntegrityError(
                "目标项目不能是 symlink 或 reparse point",
                code="invalid_target",
            )
        if not self.target_root.exists():
            fingerprint = _snapshot_fingerprint(
                {"project": False},
                [],
                directories=[],
            )
            return _TargetSnapshot((), (), False, fingerprint)
        if not self.target_root.is_dir():
            raise MigrationIntegrityError("目标项目必须是普通目录", code="invalid_target")
        records: list[_TargetRecord] = []
        directories: list[str] = []

        def walk(directory: Path, relative: tuple[str, ...]) -> None:
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name)
            except OSError as exc:
                raise MigrationConflict("目标项目在扫描期间发生变化", code="target_changed") from exc
            for entry in entries:
                _validate_component(entry.name, code="invalid_target")
                current = Path(entry.path)
                if entry.is_symlink() or _is_reparse(current):
                    raise MigrationIntegrityError(
                        "目标项目包含 symlink 或 reparse point",
                        code="invalid_target",
                    )
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError as exc:
                    raise MigrationConflict("目标项目在扫描期间发生变化", code="target_changed") from exc
                parts = (*relative, entry.name)
                logical = PurePosixPath(self.target_project, *parts).as_posix()
                if is_dir:
                    directories.append(logical)
                    walk(current, parts)
                elif is_file:
                    suffix = current.suffix.casefold()
                    if suffix in {".json", ".yaml", ".yml"}:
                        size, digest = self._read_structured_file(
                            current,
                            suffix=suffix,
                            change_code="target_changed",
                            invalid_code="invalid_target",
                        )
                    else:
                        size, digest = self._read_raw_file(
                            current,
                            change_code="target_changed",
                            invalid_code="invalid_target",
                        )
                    records.append(_TargetRecord(current, logical, size, digest))
                else:
                    raise MigrationIntegrityError(
                        "目标项目包含不支持的文件类型",
                        code="invalid_target",
                    )

        walk(self.target_root, ())
        records.sort(key=lambda record: record.logical)
        directories.sort()
        items = [record.fingerprint_item() for record in records]
        fingerprint = _snapshot_fingerprint(
            {"project": True},
            items,
            directories=directories,
        )
        return _TargetSnapshot(tuple(records), tuple(directories), True, fingerprint)

    @staticmethod
    def _target_is_blocked(
        logical: str,
        *,
        file_paths: set[str],
        directories: set[str],
    ) -> bool:
        if logical in directories:
            return True
        pure = PurePosixPath(logical)
        return any(parent.as_posix() in file_paths for parent in pure.parents)

    def _result_target_fingerprint(
        self,
        target: _TargetSnapshot,
        records: tuple[_LegacyRecord, ...],
        actions: dict[str, str],
    ) -> str:
        items = {
            record.logical: record.fingerprint_item()
            for record in target.records
        }
        directories = set(target.directories)
        copied = False
        project_root = PurePosixPath(self.target_project)
        for record in records:
            if actions[record.source] != "copy":
                continue
            copied = True
            items[record.target] = {
                "path": record.target,
                "size": record.size,
                "sha256": record.sha256,
            }
            for parent in PurePosixPath(record.target).parents:
                if parent in {PurePosixPath("."), project_root}:
                    continue
                directories.add(parent.as_posix())
        ordered_items = [items[path] for path in sorted(items)]
        return _snapshot_fingerprint(
            {"project": target.present or copied},
            ordered_items,
            directories=sorted(directories),
        )

    def _compute_plan(self) -> _PlanComputation:
        legacy = self._scan_legacy()
        target = self._scan_target()
        target_by_path = {record.logical: record for record in target.records}
        target_files = set(target_by_path)
        target_directories = set(target.directories)
        user_sources = [record for record in legacy.records if record.category == "user"]
        multiple_users = len(user_sources) > 1
        items: list[dict] = []
        conflicts: list[dict] = []
        skipped: list[dict] = []
        actions: dict[str, str] = {}

        for record in legacy.records:
            existing = target_by_path.get(record.target)
            if multiple_users and record.category == "user":
                action = "conflict"
            elif existing is not None:
                action = (
                    "skip_same"
                    if existing.size == record.size and existing.sha256 == record.sha256
                    else "conflict"
                )
            elif self._target_is_blocked(
                record.target,
                file_paths=target_files,
                directories=target_directories,
            ):
                action = "conflict"
            else:
                action = "copy"
            item = {
                "source": record.source,
                "target": record.target,
                "size": record.size,
                "sha256": record.sha256,
                "action": action,
            }
            items.append(item)
            actions[record.source] = action
            if action == "skip_same":
                skipped.append(copy.deepcopy(item))
            elif action == "conflict":
                conflict = copy.deepcopy(item)
                if multiple_users and record.category == "user":
                    conflict["code"] = "multiple_user_profiles"
                conflicts.append(conflict)

        result_fingerprint = self._result_target_fingerprint(
            target,
            legacy.records,
            actions,
        )
        material = {
            "source_fingerprint": legacy.fingerprint,
            "target_fingerprint": target.fingerprint,
            "result_target_fingerprint": result_fingerprint,
            "target_project": self.target_project,
            "items": items,
            "conflicts": conflicts,
            "skipped": skipped,
            "can_apply": not conflicts,
        }
        plan = {"plan_id": _canonical_hash(material), **material}
        return _PlanComputation(
            plan,
            {record.source: record for record in legacy.records},
        )

    def plan(self) -> dict:
        """返回稳定计划；不创建目录、备份或 journal。"""
        with self._lock:
            return copy.deepcopy(self._compute_plan().plan)

    def _ensure_migrations_root(self, *, write: bool) -> bool:
        if _has_reparse_in_chain(self.migrations_root):
            raise MigrationIntegrityError(
                "迁移工作目录不能是 symlink 或 reparse point",
                code="migration_journal_invalid",
            )
        if self.migrations_root.exists():
            if not self.migrations_root.is_dir():
                raise MigrationIntegrityError(
                    "迁移工作目录必须是普通目录",
                    code="migration_journal_invalid",
                )
            return True
        if not write:
            return False
        try:
            self.migrations_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MigrationOperationError("迁移工作目录创建失败") from exc
        if _has_reparse_in_chain(self.migrations_root) or not self.migrations_root.is_dir():
            raise MigrationIntegrityError(
                "迁移工作目录创建后校验失败",
                code="migration_journal_invalid",
            )
        return True

    def _journal_path(self, migration_id: str) -> Path:
        migration_id = self._validate_migration_id(migration_id)
        path = _absolute(self.migrations_root / f"{migration_id}.json")
        if path.parent != self.migrations_root:
            raise ValueError("migration_id 路径越界")
        return path

    @staticmethod
    def _validate_logical_path(value: object) -> str:
        if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
            raise MigrationIntegrityError(
                "迁移 journal 逻辑路径无效",
                code="migration_journal_invalid",
            )
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise MigrationIntegrityError(
                "迁移 journal 逻辑路径越界",
                code="migration_journal_invalid",
            )
        if path.as_posix() != value:
            raise MigrationIntegrityError(
                "迁移 journal 逻辑路径未规范化",
                code="migration_journal_invalid",
            )
        for component in path.parts:
            _validate_component(component, code="migration_journal_invalid")
        return value

    def _validate_journal(self, value: object, migration_id: str) -> dict:
        if not isinstance(value, dict):
            raise MigrationIntegrityError(
                "迁移 journal 顶层必须是对象",
                code="migration_journal_invalid",
            )
        if value.get("journal_version") != JOURNAL_VERSION:
            raise MigrationIntegrityError(
                "迁移 journal 版本不兼容",
                code="migration_journal_invalid",
            )
        if value.get("migration_id") != migration_id or value.get("plan_id") != migration_id:
            raise MigrationIntegrityError(
                "迁移 journal ID 不一致",
                code="migration_journal_invalid",
            )
        if value.get("status") not in {"prepared", "applied", "rolled_back", "needs_recovery"}:
            raise MigrationIntegrityError(
                "迁移 journal 状态无效",
                code="migration_journal_invalid",
            )
        target_project = value.get("target_project")
        try:
            validate_file_id(target_project, label="迁移 journal 项目 ID")
        except ValueError as exc:
            raise MigrationIntegrityError(
                "迁移 journal 项目 ID 无效",
                code="migration_journal_invalid",
            ) from exc
        for field in ("source_fingerprint", "target_fingerprint", "result_target_fingerprint"):
            if not isinstance(value.get(field), str) or not _SHA256_RE.fullmatch(value[field]):
                raise MigrationIntegrityError(
                    "迁移 journal 指纹无效",
                    code="migration_journal_invalid",
                )
        backup_id = value.get("backup_id")
        if not isinstance(backup_id, str) or not backup_id or len(backup_id) > 200:
            raise MigrationIntegrityError(
                "迁移 journal backup_id 无效",
                code="migration_journal_invalid",
            )
        journal_items = value.get("items")
        if not isinstance(journal_items, list):
            raise MigrationIntegrityError(
                "迁移 journal items 无效",
                code="migration_journal_invalid",
            )
        for item in journal_items:
            if not isinstance(item, dict) or set(item) != {"source", "target", "size", "sha256"}:
                raise MigrationIntegrityError(
                    "迁移 journal item 无效",
                    code="migration_journal_invalid",
                )
            self._validate_logical_path(item["source"])
            self._validate_logical_path(item["target"])
            if (
                isinstance(item["size"], bool)
                or not isinstance(item["size"], int)
                or item["size"] < 0
                or not isinstance(item["sha256"], str)
                or not _SHA256_RE.fullmatch(item["sha256"])
            ):
                raise MigrationIntegrityError(
                    "迁移 journal item 摘要无效",
                    code="migration_journal_invalid",
                )
        for field in ("copied", "skipped"):
            paths = value.get(field)
            if not isinstance(paths, list) or any(
                self._validate_logical_path(path) != path for path in paths
            ):
                raise MigrationIntegrityError(
                    "迁移 journal 路径列表无效",
                    code="migration_journal_invalid",
                )
        if value["status"] == "applied":
            result = value.get("result")
            expected_keys = {
                "applied", "migration_id", "plan_id", "backup_id", "copied", "skipped",
            }
            if (
                not isinstance(result, dict)
                or set(result) != expected_keys
                or result.get("applied") is not True
                or result.get("migration_id") != migration_id
                or result.get("plan_id") != migration_id
                or result.get("backup_id") != backup_id
                or result.get("copied") != value.get("copied")
                or result.get("skipped") != value.get("skipped")
            ):
                raise MigrationIntegrityError(
                    "迁移 journal result 无效",
                    code="migration_journal_invalid",
                )
        return copy.deepcopy(value)

    def _read_journal(self, migration_id: str) -> dict:
        path = self._journal_path(migration_id)
        if not self._ensure_migrations_root(write=False) or not os.path.lexists(path):
            raise FileNotFoundError(f"迁移记录不存在: {migration_id}")
        if _is_reparse(path) or not path.is_file():
            raise MigrationIntegrityError(
                "迁移 journal 必须是普通文件",
                code="migration_journal_invalid",
            )
        try:
            before = os.stat(path, follow_symlinks=False)
            data = path.read_bytes()
            after = os.stat(path, follow_symlinks=False)
            if (
                before.st_size != after.st_size
                or before.st_size != len(data)
                or before.st_mtime_ns != after.st_mtime_ns
                or _is_reparse(path)
            ):
                raise MigrationIntegrityError(
                    "迁移 journal 在读取期间发生变化",
                    code="migration_journal_invalid",
                )
            parsed = json.loads(data.decode("utf-8"))
        except MigrationError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MigrationIntegrityError(
                "迁移 journal 无法验证",
                code="migration_journal_invalid",
            ) from exc
        return self._validate_journal(parsed, migration_id)

    def _read_journal_if_exists(self, migration_id: str) -> dict | None:
        try:
            return self._read_journal(migration_id)
        except FileNotFoundError:
            return None

    @staticmethod
    def _journal_to_migration(journal: dict) -> dict:
        if journal["status"] == "applied":
            return copy.deepcopy(journal["result"])
        return {
            "applied": False,
            "migration_id": journal["migration_id"],
            "plan_id": journal["plan_id"],
            "backup_id": journal["backup_id"],
            "status": journal["status"],
            "copied": copy.deepcopy(journal["copied"]),
            "skipped": copy.deepcopy(journal["skipped"]),
        }

    def get_migration(self, migration_id: str) -> dict:
        with self._lock:
            return self._journal_to_migration(self._read_journal(migration_id))

    def _write_journal(self, path: Path, journal: dict) -> None:
        self._ensure_migrations_root(write=True)
        if path.parent != self.migrations_root or _is_reparse(path):
            raise MigrationIntegrityError(
                "迁移 journal 路径无效",
                code="migration_journal_invalid",
            )
        descriptor = -1
        temp_path: Path | None = None
        try:
            descriptor, temp_name = tempfile.mkstemp(
                dir=self.migrations_root,
                prefix=f".{path.name}.",
                suffix=".tmp",
            )
            temp_path = Path(temp_name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                descriptor = -1
                json.dump(journal, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    def _create_staging_root(self, plan_id: str) -> Path:
        self._ensure_migrations_root(write=True)
        staging_parent = self.migrations_root / ".staging"
        if os.path.lexists(staging_parent):
            if _is_reparse(staging_parent) or not staging_parent.is_dir():
                raise MigrationIntegrityError(
                    "迁移 staging 根无效",
                    code="migration_staging_invalid",
                )
        else:
            try:
                staging_parent.mkdir()
            except OSError as exc:
                raise MigrationOperationError("迁移 staging 根创建失败") from exc
        stage = staging_parent / f"{plan_id[:12]}-{uuid4().hex[:12]}"
        try:
            stage.mkdir()
        except OSError as exc:
            raise MigrationOperationError("迁移 staging 创建失败") from exc
        if _is_reparse(stage) or not stage.is_dir():
            raise MigrationIntegrityError(
                "迁移 staging 创建后校验失败",
                code="migration_staging_invalid",
            )
        return stage

    def _staged_path(self, stage: Path, logical_target: str) -> Path:
        logical_target = self._validate_logical_path(logical_target)
        parts = PurePosixPath(logical_target).parts
        if not parts or parts[0] != self.target_project:
            raise MigrationIntegrityError(
                "迁移目标不属于指定项目",
                code="migration_plan_invalid",
            )
        staged_name = f"{hashlib.sha256(logical_target.encode('utf-8')).hexdigest()}.stage"
        path = _absolute(stage / "payload" / staged_name)
        payload_root = _absolute(stage / "payload")
        if not path.is_relative_to(payload_root):
            raise MigrationIntegrityError(
                "迁移 staging 路径越界",
                code="migration_plan_invalid",
            )
        return path

    def _target_path(self, logical_target: str) -> Path:
        logical_target = self._validate_logical_path(logical_target)
        parts = PurePosixPath(logical_target).parts
        if not parts or parts[0] != self.target_project:
            raise MigrationIntegrityError(
                "迁移目标不属于指定项目",
                code="migration_plan_invalid",
            )
        path = _absolute(self.projects_root / Path(*parts))
        if path == self.projects_root or not path.is_relative_to(self.projects_root):
            raise MigrationIntegrityError(
                "迁移目标路径越界",
                code="migration_plan_invalid",
            )
        return path

    def _copy_to_staging(self, source: Path, staged: Path, item: dict) -> None:
        try:
            staged.parent.mkdir(parents=True, exist_ok=True)
            before = os.stat(source, follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode) or _is_reparse(source):
                raise MigrationIntegrityError("迁移源不再是普通文件")
            digest = hashlib.sha256()
            size = 0
            with source.open("rb") as input_handle, staged.open("xb") as output_handle:
                for chunk in iter(lambda: input_handle.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
                    output_handle.write(chunk)
                output_handle.flush()
                os.fsync(output_handle.fileno())
            after = os.stat(source, follow_symlinks=False)
        except MigrationError:
            raise
        except FileExistsError as exc:
            raise MigrationOperationError("迁移 staging 文件发生碰撞") from exc
        except OSError as exc:
            raise MigrationOperationError("迁移 staging 复制失败") from exc
        if (
            size != item["size"]
            or digest.hexdigest() != item["sha256"]
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or _is_reparse(source)
        ):
            raise MigrationConflict("旧数据在 staging 复制期间发生变化", code="source_changed")
        staged_size, staged_hash = self._read_raw_file(
            staged,
            change_code="source_changed",
            invalid_code="migration_staging_invalid",
        )
        if staged_size != item["size"] or staged_hash != item["sha256"]:
            raise MigrationIntegrityError(
                "迁移 staging 文件摘要不匹配",
                code="migration_staging_invalid",
            )

    def _ensure_target_parents(self, target: Path) -> None:
        if target == self.projects_root or not target.is_relative_to(self.projects_root):
            raise MigrationIntegrityError(
                "迁移目标父目录越界",
                code="migration_plan_invalid",
            )
        chain: list[Path] = []
        current = target.parent
        while current != self.projects_root.parent:
            chain.append(current)
            if current == self.projects_root:
                break
            current = current.parent
        if not chain or chain[-1] != self.projects_root:
            raise MigrationIntegrityError(
                "迁移目标父目录越界",
                code="migration_plan_invalid",
            )
        for directory in reversed(chain):
            if os.path.lexists(directory):
                if _is_reparse(directory) or not directory.is_dir():
                    raise MigrationConflict("迁移目标父路径已变化", code="target_changed")
                continue
            try:
                directory.mkdir()
            except FileExistsError:
                if _is_reparse(directory) or not directory.is_dir():
                    raise MigrationConflict("迁移目标父路径已变化", code="target_changed")
            except OSError as exc:
                raise MigrationOperationError("迁移目标目录创建失败") from exc
            else:
                self._directory_receipts.append(directory)

    def _install_staged_file(
        self,
        staged: Path,
        target: Path,
        item: dict,
        plan_id: str,
    ) -> None:
        staged_size, staged_hash = self._read_raw_file(
            staged,
            change_code="source_changed",
            invalid_code="migration_staging_invalid",
        )
        if staged_size != item["size"] or staged_hash != item["sha256"]:
            raise MigrationIntegrityError(
                "迁移 staging 文件摘要不匹配",
                code="migration_staging_invalid",
            )
        if os.path.lexists(target):
            raise MigrationConflict("迁移目标在安装前已出现", code="target_changed")
        self._ensure_target_parents(target)
        if os.path.lexists(target):
            raise MigrationConflict("迁移目标在安装前已出现", code="target_changed")
        descriptor = -1
        temp_path: Path | None = None
        try:
            descriptor, temp_name = tempfile.mkstemp(
                dir=target.parent,
                prefix=f".{target.name}.{plan_id[:12]}.",
                suffix=".tmp",
            )
            temp_path = Path(temp_name)
            digest = hashlib.sha256()
            size = 0
            with staged.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output_handle:
                descriptor = -1
                for chunk in iter(lambda: input_handle.read(1024 * 1024), b""):
                    size += len(chunk)
                    digest.update(chunk)
                    output_handle.write(chunk)
                output_handle.flush()
                os.fsync(output_handle.fileno())
            if size != item["size"] or digest.hexdigest() != item["sha256"]:
                raise MigrationIntegrityError(
                    "迁移目标临时文件摘要不匹配",
                    code="migration_staging_invalid",
                )
            try:
                os.link(temp_path, target)
            except FileExistsError as exc:
                raise MigrationConflict("迁移目标在原子安装时已出现", code="target_changed") from exc
            self._install_receipts[target] = copy.deepcopy(item)
            temp_path.unlink()
            temp_path = None
        except MigrationError:
            raise
        except OSError as exc:
            raise MigrationOperationError("迁移目标原子安装失败") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
        installed_size, installed_hash = self._read_raw_file(
            target,
            change_code="target_changed",
            invalid_code="invalid_target",
        )
        if installed_size != item["size"] or installed_hash != item["sha256"]:
            raise MigrationIntegrityError(
                "迁移目标安装后摘要不匹配",
                code="invalid_target",
            )

    @staticmethod
    def _remove_staging(stage: Path | None, migrations_root: Path) -> None:
        if stage is None:
            return
        staging_parent = migrations_root / ".staging"
        if stage.parent != staging_parent or not stage.name:
            return
        try:
            if os.path.lexists(stage) and not _is_reparse(stage):
                shutil.rmtree(stage)
        except OSError:
            pass

    def _assert_unchanged_plan(self, current: dict, expected: dict, plan_id: str) -> None:
        if current["source_fingerprint"] != expected["source_fingerprint"]:
            raise MigrationConflict("旧数据指纹已变化", code="source_changed")
        if current["target_fingerprint"] != expected["target_fingerprint"]:
            raise MigrationConflict("目标项目指纹已变化", code="target_changed")
        if current["plan_id"] != plan_id:
            raise MigrationConflict("迁移计划已变化", code="plan_changed")

    def _base_journal(self, plan: dict, backup_id: str) -> dict:
        copied = [item["target"] for item in plan["items"] if item["action"] == "copy"]
        skipped = [item["target"] for item in plan["items"] if item["action"] == "skip_same"]
        return {
            "journal_version": JOURNAL_VERSION,
            "migration_id": plan["plan_id"],
            "plan_id": plan["plan_id"],
            "status": "prepared",
            "target_project": self.target_project,
            "backup_id": backup_id,
            "source_fingerprint": plan["source_fingerprint"],
            "target_fingerprint": plan["target_fingerprint"],
            "result_target_fingerprint": plan["result_target_fingerprint"],
            "items": [
                {
                    "source": item["source"],
                    "target": item["target"],
                    "size": item["size"],
                    "sha256": item["sha256"],
                }
                for item in plan["items"]
            ],
            "copied": copied,
            "skipped": skipped,
        }

    def _compensate(self, expected_target_fingerprint: str) -> None:
        errors: list[BaseException] = []
        for target, item in reversed(list(self._install_receipts.items())):
            try:
                if not os.path.lexists(target):
                    continue
                if _is_reparse(target) or not target.is_file():
                    raise MigrationIntegrityError(
                        "迁移目标补偿时类型不匹配",
                        code="migration_recovery_required",
                    )
                size, digest = self._read_raw_file(
                    target,
                    change_code="target_changed",
                    invalid_code="migration_recovery_required",
                )
                if size != item["size"] or digest != item["sha256"]:
                    raise MigrationIntegrityError(
                        "迁移目标补偿时摘要不匹配",
                        code="migration_recovery_required",
                    )
                target.unlink()
            except BaseException as exc:
                errors.append(exc)
        for directory in sorted(set(self._directory_receipts), key=lambda path: len(path.parts), reverse=True):
            try:
                if os.path.lexists(directory):
                    if _is_reparse(directory) or not directory.is_dir():
                        raise MigrationIntegrityError(
                            "迁移目录补偿时类型不匹配",
                            code="migration_recovery_required",
                        )
                    directory.rmdir()
            except OSError as exc:
                errors.append(exc)
            except MigrationError as exc:
                errors.append(exc)
        try:
            restored = self._scan_target()
            if restored.fingerprint != expected_target_fingerprint:
                raise MigrationOperationError(
                    "迁移补偿后的目标指纹不匹配",
                    code="migration_recovery_required",
                )
        except BaseException as exc:
            errors.append(exc)
        if errors:
            raise MigrationOperationError(
                "迁移补偿未完整结束",
                code="migration_recovery_required",
            ) from errors[0]

    def apply(
        self,
        plan_id: str,
        *,
        expected_source_fingerprint: str,
        expected_target_fingerprint: str,
    ) -> dict:
        plan_id = self._validate_migration_id(plan_id)
        expected_source_fingerprint = self._validate_hash(
            expected_source_fingerprint,
            label="expected_source_fingerprint",
        )
        expected_target_fingerprint = self._validate_hash(
            expected_target_fingerprint,
            label="expected_target_fingerprint",
        )
        with self._lock:
            existing = self._read_journal_if_exists(plan_id)
            if existing is not None:
                if existing["target_project"] != self.target_project:
                    raise MigrationConflict("迁移记录所属项目不一致", code="plan_changed")
                if existing["status"] == "applied":
                    return self._journal_to_migration(existing)
                if existing["status"] in {"prepared", "needs_recovery"}:
                    raise MigrationOperationError(
                        "迁移记录需要人工恢复",
                        code="migration_recovery_required",
                        migration_id=plan_id,
                    )

            computation = self._compute_plan()
            plan = computation.plan
            if plan["source_fingerprint"] != expected_source_fingerprint:
                raise MigrationConflict("旧数据指纹已变化", code="source_changed")
            if plan["target_fingerprint"] != expected_target_fingerprint:
                raise MigrationConflict("目标项目指纹已变化", code="target_changed")
            if plan["plan_id"] != plan_id:
                raise MigrationConflict("迁移计划已变化", code="plan_changed")
            if not plan["can_apply"]:
                raise MigrationConflict(
                    "迁移计划包含冲突",
                    code="migration_conflicts",
                    detail={"conflicts": copy.deepcopy(plan["conflicts"])},
                )

            stage: Path | None = None
            backup_id: str | None = None
            journal: dict | None = None
            journal_path = self._journal_path(plan_id)
            self._install_receipts = {}
            self._directory_receipts = []
            try:
                backup = self.backup_manager.create_backup(
                    f"pre_migration:{plan_id}",
                    kind="pre_migration",
                )
                if not isinstance(backup, dict):
                    raise MigrationOperationError("迁移前备份结果无效")
                backup_id = backup.get("backup_id")
                if not isinstance(backup_id, str) or not backup_id or len(backup_id) > 200:
                    raise MigrationOperationError("迁移前备份缺少 backup_id")

                after_backup = self._compute_plan()
                self._assert_unchanged_plan(after_backup.plan, plan, plan_id)
                computation = after_backup
                stage = self._create_staging_root(plan_id)
                for item in plan["items"]:
                    if item["action"] != "copy":
                        continue
                    record = computation.records_by_source.get(item["source"])
                    if record is None:
                        raise MigrationConflict("迁移源已变化", code="source_changed")
                    staged = self._staged_path(stage, item["target"])
                    self._copy_to_staging(record.path, staged, item)

                before_install = self._compute_plan()
                self._assert_unchanged_plan(before_install.plan, plan, plan_id)
                journal = self._base_journal(plan, backup_id)
                self._write_journal(journal_path, journal)

                for item in plan["items"]:
                    if item["action"] != "copy":
                        continue
                    staged = self._staged_path(stage, item["target"])
                    target = self._target_path(item["target"])
                    self._install_staged_file(staged, target, item, plan_id)

                source_after = self._scan_legacy()
                if source_after.fingerprint != plan["source_fingerprint"]:
                    raise MigrationConflict("旧数据在安装期间发生变化", code="source_changed")
                target_after = self._scan_target()
                if target_after.fingerprint != plan["result_target_fingerprint"]:
                    raise MigrationIntegrityError(
                        "迁移目标安装后指纹不匹配",
                        code="invalid_target",
                    )
                result = {
                    "applied": True,
                    "migration_id": plan_id,
                    "plan_id": plan_id,
                    "backup_id": backup_id,
                    "copied": copy.deepcopy(journal["copied"]),
                    "skipped": copy.deepcopy(journal["skipped"]),
                }
                journal["status"] = "applied"
                journal["result"] = copy.deepcopy(result)
                self._write_journal(journal_path, journal)
                return result
            except Exception as exc:
                compensation_error: BaseException | None = None
                try:
                    self._compensate(plan["target_fingerprint"])
                except BaseException as rollback_exc:
                    compensation_error = rollback_exc

                if journal is None and backup_id is not None:
                    journal = self._base_journal(plan, backup_id)
                if journal is not None:
                    journal.pop("result", None)
                    journal["status"] = (
                        "needs_recovery" if compensation_error is not None else "rolled_back"
                    )
                    try:
                        self._write_journal(journal_path, journal)
                    except BaseException:
                        if compensation_error is None:
                            try:
                                journal_path.unlink(missing_ok=True)
                            except OSError:
                                pass

                if compensation_error is not None:
                    raise MigrationOperationError(
                        "迁移失败且补偿未完整结束",
                        code="migration_recovery_required",
                        migration_id=plan_id,
                    ) from compensation_error
                if isinstance(exc, (MigrationConflict, MigrationIntegrityError)):
                    raise exc
                if isinstance(exc, MigrationOperationError):
                    raise exc
                raise MigrationOperationError(
                    "迁移执行失败，目标数据已补偿",
                    code="migration_failed",
                    migration_id=plan_id,
                ) from exc
            finally:
                self._remove_staging(stage, self.migrations_root)
                self._install_receipts = {}
                self._directory_receipts = []
