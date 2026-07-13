"""可验证的恢复点、坏档隔离与软删除仓储。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable
from uuid import UUID, uuid4


MANIFEST_VERSION = 1
RECOVERY_CATEGORIES = frozenset({"checkpoint", "quarantine", "trash"})
RECOVERY_STATUSES = frozenset({"complete", "restoring", "restored"})
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class RecoveryError(RuntimeError):
    """恢复仓储错误基类。"""


class RecoveryConflict(RecoveryError):
    """源指纹或恢复目标与调用方预期不一致。"""

    def __init__(self, message: str, *, code: str = "recovery_conflict"):
        super().__init__(message)
        self.code = code

    def as_detail(self) -> dict:
        return {"code": self.code, "message": str(self)}


class RecoveryIntegrityError(RecoveryError):
    """恢复清单或 payload 未通过完整性校验。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.code = "recovery_integrity_error"

    def as_detail(self) -> dict:
        return {"code": self.code, "message": str(self)}


class DataCorruptionError(RecoveryError):
    """数据存在但无法按其声明格式解析；纯读取路径只报告，不写盘。"""

    def __init__(
        self,
        *,
        entity_type: str,
        project: str,
        entity_id: str,
        path: Path,
        fingerprint: str,
        reason: str,
    ):
        super().__init__(f"{entity_type} 数据损坏: {project}/{entity_id}: {reason}")
        self.entity_type = entity_type
        self.project = project
        self.entity_id = entity_id
        self.path = Path(path)
        self.fingerprint = fingerprint
        self.reason = reason
        self.code = "data_corrupt"

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        entity_type: str,
        project: str,
        entity_id: str,
        reason: str,
    ) -> "DataCorruptionError":
        return cls(
            entity_type=entity_type,
            project=project,
            entity_id=entity_id,
            path=path,
            fingerprint=sha256_file(path),
            reason=reason,
        )

    def as_detail(self) -> dict:
        return {
            "code": self.code,
            "entity_type": self.entity_type,
            "project": self.project,
            "entity_id": self.entity_id,
            "fingerprint": self.fingerprint,
            "reason": self.reason,
            "quarantine_available": True,
        }


@dataclass(frozen=True)
class VerifiedRecovery:
    manifest: dict
    entry_dir: Path


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _copy_verified(source: Path, target: Path) -> tuple[int, str]:
    """复制并校验；源在复制期间变化时拒绝生成恢复点。"""
    source = Path(source)
    before = sha256_file(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as target_handle:
            for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
                target_handle.write(chunk)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        copied = digest.hexdigest()
        if copied != before or sha256_file(source) != before:
            raise RecoveryConflict("恢复点创建期间源文件发生变化", code="source_changed")
        if sha256_file(temp_path) != copied:
            raise RecoveryIntegrityError("恢复点 payload 复制校验失败")
        os.replace(temp_path, target)
        return size, copied
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class RecoveryStore:
    """项目数据恢复仓储；读取方法不创建任何目录。"""

    def __init__(
        self,
        recovery_root: Path,
        *,
        projects_root: Path,
        retention_days: int = 30,
    ):
        self.root = Path(recovery_root).resolve(strict=False)
        self.projects_root = Path(projects_root).resolve(strict=False)
        if isinstance(retention_days, bool) or not isinstance(retention_days, int):
            raise ValueError("retention_days 必须是非负整数")
        if retention_days < 0:
            raise ValueError("retention_days 必须是非负整数")
        self.retention_days = retention_days

    @staticmethod
    def _validate_category(category: str) -> str:
        if category not in RECOVERY_CATEGORIES:
            raise ValueError(f"未知恢复分类: {category}")
        return category

    @staticmethod
    def _validate_token(value: str, *, label: str) -> str:
        if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
            raise ValueError(f"无效{label}: {value}")
        return value

    @staticmethod
    def validate_recovery_id(recovery_id: str) -> str:
        try:
            canonical = str(UUID(str(recovery_id)))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("无效 recovery_id") from exc
        if canonical != str(recovery_id).lower():
            raise ValueError("recovery_id 必须使用规范 UUID")
        return canonical

    @staticmethod
    def _safe_relative(value: str) -> PurePosixPath:
        if not isinstance(value, str) or not value:
            raise RecoveryIntegrityError("恢复清单包含空路径")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise RecoveryIntegrityError("恢复清单包含越界路径")
        return path

    def _source_relative(self, source: Path) -> str:
        resolved = Path(source).resolve(strict=False)
        try:
            relative = resolved.relative_to(self.projects_root)
        except ValueError as exc:
            raise ValueError("恢复源文件不在 projects 根目录内") from exc
        return PurePosixPath(*relative.parts).as_posix()

    def target_path(self, source_relpath: str) -> Path:
        relative = self._safe_relative(source_relpath)
        target = self.projects_root.joinpath(*relative.parts).resolve(strict=False)
        if not target.is_relative_to(self.projects_root):
            raise RecoveryIntegrityError("恢复目标越出 projects 根目录")
        return target

    def _entry_dir(self, category: str, recovery_id: str) -> Path:
        category = self._validate_category(category)
        recovery_id = self.validate_recovery_id(recovery_id)
        path = (self.root / category / recovery_id).resolve(strict=False)
        category_root = (self.root / category).resolve(strict=False)
        if not path.is_relative_to(category_root):
            raise ValueError("恢复项路径越界")
        return path

    @staticmethod
    def _manifest_json(manifest: dict) -> str:
        return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)

    def create_entry(
        self,
        *,
        category: str,
        operation: str,
        entity_type: str,
        project: str,
        entity_id: str,
        source_paths: Iterable[Path],
        source_revision: int | None = None,
        metadata: dict | None = None,
    ) -> dict:
        category = self._validate_category(category)
        operation = self._validate_token(operation, label=" operation")
        entity_type = self._validate_token(entity_type, label=" entity_type")
        sources = [Path(path) for path in source_paths]
        if not sources:
            raise ValueError("恢复项至少包含一个源文件")
        if source_revision is not None and (
            isinstance(source_revision, bool)
            or not isinstance(source_revision, int)
            or source_revision < 0
        ):
            raise ValueError("source_revision 必须是非负整数或 null")

        recovery_id = str(uuid4())
        staging_root = (self.root / ".staging").resolve(strict=False)
        staging = (staging_root / recovery_id).resolve(strict=False)
        if not staging.is_relative_to(staging_root):
            raise ValueError("恢复 staging 路径越界")
        final_dir = self._entry_dir(category, recovery_id)
        now = utc_now()
        expires_at = None
        if category == "trash" and self.retention_days:
            expires_at = (now + timedelta(days=self.retention_days)).isoformat()

        try:
            staging.mkdir(parents=True, exist_ok=False)
            items: list[dict] = []
            seen: set[str] = set()
            for source in sources:
                if not source.is_file():
                    raise FileNotFoundError(f"恢复源文件不存在: {source.name}")
                source_relpath = self._source_relative(source)
                if source_relpath in seen:
                    continue
                seen.add(source_relpath)
                payload_relpath = PurePosixPath("payload", source_relpath).as_posix()
                payload_path = staging.joinpath(*PurePosixPath(payload_relpath).parts)
                size, digest = _copy_verified(source, payload_path)
                items.append({
                    "source_relpath": source_relpath,
                    "payload_relpath": payload_relpath,
                    "size": size,
                    "sha256": digest,
                })

            manifest = {
                "manifest_version": MANIFEST_VERSION,
                "recovery_id": recovery_id,
                "category": category,
                "operation": operation,
                "entity_type": entity_type,
                "project": project,
                "entity_id": entity_id,
                "created_at": now.isoformat(),
                "expires_at": expires_at,
                "source_revision": source_revision,
                "source_relpath": items[0]["source_relpath"],
                "items": items,
                "status": "complete",
                "restored_at": None,
                "metadata": deepcopy(metadata or {}),
            }
            _atomic_write_text(staging / "manifest.json", self._manifest_json(manifest))
            self._verify_manifest(manifest, staging, require_final_location=False)
            final_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, final_dir)
            self._verify_manifest(manifest, final_dir)
            return deepcopy(manifest)
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

    def create_session_entry(
        self,
        *,
        category: str,
        operation: str,
        project: str,
        save_id: str,
        session_path: Path,
        source_revision: int | None,
        history_paths: Iterable[Path] = (),
        metadata: dict | None = None,
    ) -> dict:
        return self.create_entry(
            category=category,
            operation=operation,
            entity_type="session",
            project=project,
            entity_id=save_id,
            source_paths=[session_path, *history_paths],
            source_revision=source_revision,
            metadata=metadata,
        )

    def _read_manifest_path(self, path: Path) -> dict:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryIntegrityError("恢复 manifest 无法解析") from exc
        if not isinstance(manifest, dict):
            raise RecoveryIntegrityError("恢复 manifest 顶层必须是对象")
        return manifest

    def _validate_manifest_shape(
        self,
        manifest: dict,
        entry_dir: Path,
        *,
        require_final_location: bool = True,
    ) -> None:
        if manifest.get("manifest_version") != MANIFEST_VERSION:
            raise RecoveryIntegrityError("恢复 manifest 版本不兼容")
        recovery_id = self.validate_recovery_id(manifest.get("recovery_id", ""))
        category = self._validate_category(manifest.get("category", ""))
        if entry_dir.name != recovery_id or (
            require_final_location and entry_dir.parent.name != category
        ):
            raise RecoveryIntegrityError("恢复 manifest 归属与目录不一致")
        if manifest.get("status") not in RECOVERY_STATUSES:
            raise RecoveryIntegrityError("恢复 manifest 状态无效")
        self._validate_token(manifest.get("operation", ""), label=" operation")
        entity_type = self._validate_token(
            manifest.get("entity_type", ""),
            label=" entity_type",
        )
        project = manifest.get("project")
        entity_id = manifest.get("entity_id")
        if not isinstance(project, str) or not project:
            raise RecoveryIntegrityError("恢复 manifest project 无效")
        if not isinstance(entity_id, str) or not entity_id:
            raise RecoveryIntegrityError("恢复 manifest entity_id 无效")
        if not isinstance(manifest.get("metadata"), dict):
            raise RecoveryIntegrityError("恢复 manifest metadata 无效")
        if "restore_journal" in manifest and not isinstance(
            manifest.get("restore_journal"),
            dict,
        ):
            raise RecoveryIntegrityError("恢复 manifest restore_journal 无效")
        items = manifest.get("items")
        if not isinstance(items, list) or not items:
            raise RecoveryIntegrityError("恢复 manifest items 无效")
        if manifest.get("source_relpath") != items[0].get("source_relpath"):
            raise RecoveryIntegrityError("恢复 manifest 主路径不一致")
        seen_sources: set[str] = set()
        seen_payloads: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise RecoveryIntegrityError("恢复 manifest item 必须是对象")
            source_relpath = self._safe_relative(item.get("source_relpath", "")).as_posix()
            payload_relpath = self._safe_relative(item.get("payload_relpath", "")).as_posix()
            if not payload_relpath.startswith("payload/"):
                raise RecoveryIntegrityError("恢复 payload 路径前缀无效")
            if source_relpath in seen_sources or payload_relpath in seen_payloads:
                raise RecoveryIntegrityError("恢复 manifest 路径重复")
            seen_sources.add(source_relpath)
            seen_payloads.add(payload_relpath)
            if isinstance(item.get("size"), bool) or not isinstance(item.get("size"), int):
                raise RecoveryIntegrityError("恢复 manifest size 无效")
            digest = item.get("sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise RecoveryIntegrityError("恢复 manifest sha256 无效")
        if entity_type == "session":
            primary = PurePosixPath(project, "saves", f"{entity_id}.json").as_posix()
            if manifest.get("source_relpath") != primary:
                raise RecoveryIntegrityError("session 恢复项主路径与归属不一致")
            history_prefix = PurePosixPath(project, "saves", ".history").as_posix() + "/"
            for item in items[1:]:
                source_relpath = item["source_relpath"]
                name = PurePosixPath(source_relpath).name
                if (
                    not source_relpath.startswith(history_prefix)
                    or not name.startswith(f"{entity_id}.")
                    or not name.endswith(".json")
                ):
                    raise RecoveryIntegrityError("session 恢复项包含跨归属文件")

    def _verify_manifest(
        self,
        manifest: dict,
        entry_dir: Path,
        *,
        require_final_location: bool = True,
    ) -> None:
        self._validate_manifest_shape(
            manifest,
            entry_dir,
            require_final_location=require_final_location,
        )
        expected_payloads: set[str] = set()
        for item in manifest["items"]:
            payload_relpath = self._safe_relative(item["payload_relpath"])
            payload_path = entry_dir.joinpath(*payload_relpath.parts).resolve(strict=False)
            if not payload_path.is_relative_to(entry_dir.resolve(strict=False)):
                raise RecoveryIntegrityError("恢复 payload 路径越界")
            if not payload_path.is_file():
                raise RecoveryIntegrityError("恢复 payload 缺失")
            if payload_path.stat().st_size != item["size"]:
                raise RecoveryIntegrityError("恢复 payload 大小不匹配")
            if sha256_file(payload_path) != item["sha256"]:
                raise RecoveryIntegrityError("恢复 payload 哈希不匹配")
            expected_payloads.add(payload_relpath.as_posix())
        payload_root = entry_dir / "payload"
        actual_payloads = {
            path.relative_to(entry_dir).as_posix()
            for path in payload_root.rglob("*")
            if path.is_file()
        }
        if actual_payloads != expected_payloads:
            raise RecoveryIntegrityError("恢复 payload 文件集合与 manifest 不一致")

    def get_verified(self, recovery_id: str) -> VerifiedRecovery:
        recovery_id = self.validate_recovery_id(recovery_id)
        matches = [
            self._entry_dir(category, recovery_id)
            for category in sorted(RECOVERY_CATEGORIES)
            if self._entry_dir(category, recovery_id).is_dir()
        ]
        if not matches:
            raise FileNotFoundError(f"恢复项不存在: {recovery_id}")
        if len(matches) != 1:
            raise RecoveryIntegrityError("recovery_id 在多个分类中重复")
        entry_dir = matches[0]
        manifest = self._read_manifest_path(entry_dir / "manifest.json")
        self._verify_manifest(manifest, entry_dir)
        return VerifiedRecovery(manifest=manifest, entry_dir=entry_dir)

    def list_entries(
        self,
        *,
        category: str | None = None,
        project: str | None = None,
        entity_type: str | None = None,
    ) -> list[dict]:
        categories = [self._validate_category(category)] if category else sorted(RECOVERY_CATEGORIES)
        entries: list[dict] = []
        for current_category in categories:
            category_dir = self.root / current_category
            if not category_dir.is_dir():
                continue
            for entry_dir in category_dir.iterdir():
                if not entry_dir.is_dir():
                    continue
                try:
                    recovery_id = self.validate_recovery_id(entry_dir.name)
                    verified = self.get_verified(recovery_id)
                    manifest = verified.manifest
                except (ValueError, RecoveryIntegrityError) as exc:
                    entries.append({
                        "recovery_id": entry_dir.name,
                        "category": current_category,
                        "status": "invalid",
                        "error": str(exc),
                    })
                    continue
                if project is not None and manifest.get("project") != project:
                    continue
                if entity_type is not None and manifest.get("entity_type") != entity_type:
                    continue
                entries.append(deepcopy(manifest))
        entries.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return entries

    def find_matching_quarantine(
        self,
        *,
        project: str,
        entity_id: str,
        fingerprint: str,
    ) -> dict | None:
        for manifest in self.list_entries(
            category="quarantine",
            project=project,
            entity_type="session",
        ):
            if manifest.get("status") == "invalid":
                continue
            if (
                manifest.get("entity_id") == entity_id
                and manifest.get("items", [{}])[0].get("sha256") == fingerprint
            ):
                return manifest
        return None

    def quarantine_session(
        self,
        *,
        project: str,
        save_id: str,
        session_path: Path,
        fingerprint: str,
    ) -> tuple[dict, bool]:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(fingerprint)):
            raise ValueError("fingerprint 必须是 64 位 SHA-256")
        expected = str(fingerprint).lower()
        existing = self.find_matching_quarantine(
            project=project,
            entity_id=save_id,
            fingerprint=expected,
        )
        if session_path.is_file():
            current = sha256_file(session_path)
            if current != expected:
                raise RecoveryConflict("源文件指纹已变化，拒绝隔离", code="source_changed")
            if existing is not None:
                session_path.unlink()
                return existing, True
        elif existing is not None:
            return existing, True
        else:
            raise FileNotFoundError(f"存档 {save_id} 不存在")

        manifest = self.create_session_entry(
                category="quarantine",
                operation="parse_failure",
                project=project,
                save_id=save_id,
                session_path=session_path,
                source_revision=None,
                metadata={"fingerprint": expected},
        )
        session_path.unlink()
        return manifest, False

    @staticmethod
    def payload_path(verified: VerifiedRecovery, item: dict) -> Path:
        relative = RecoveryStore._safe_relative(item["payload_relpath"])
        path = verified.entry_dir.joinpath(*relative.parts).resolve(strict=False)
        if not path.is_relative_to(verified.entry_dir.resolve(strict=False)):
            raise RecoveryIntegrityError("恢复 payload 路径越界")
        return path

    def restore_primary_exact(self, verified: VerifiedRecovery) -> Path:
        primary_relpath = verified.manifest["source_relpath"]
        primary_item = next(
            item
            for item in verified.manifest["items"]
            if item["source_relpath"] == primary_relpath
        )
        payload = self.payload_path(verified, primary_item)
        target = self.target_path(primary_relpath)
        size, digest = _copy_verified(payload, target)
        if size != primary_item["size"] or digest != primary_item["sha256"]:
            raise RecoveryIntegrityError("恢复主文件精确校验失败")
        return target

    def restore_non_primary_items(
        self,
        verified: VerifiedRecovery,
        *,
        primary_relpath: str,
    ) -> list[str]:
        """先完整预检，再恢复非主文件；相同目标视为幂等。"""
        pending, restored = self.preflight_non_primary_items(
            verified,
            primary_relpath=primary_relpath,
        )
        for item, target in pending:
            payload = self.payload_path(verified, item)
            size, digest = _copy_verified(payload, target)
            if size != item["size"] or digest != item["sha256"]:
                raise RecoveryIntegrityError("恢复后的文件校验失败")
            restored.append(item["source_relpath"])
        return restored

    def preflight_non_primary_items(
        self,
        verified: VerifiedRecovery,
        *,
        primary_relpath: str,
    ) -> tuple[list[tuple[dict, Path]], list[str]]:
        pending: list[tuple[dict, Path]] = []
        identical: list[str] = []
        for item in verified.manifest["items"]:
            if item["source_relpath"] == primary_relpath:
                continue
            target = self.target_path(item["source_relpath"])
            if target.exists():
                if not target.is_file() or sha256_file(target) != item["sha256"]:
                    raise RecoveryConflict(
                        f"恢复目标已存在且内容不同: {item['source_relpath']}",
                        code="target_exists",
                    )
                identical.append(item["source_relpath"])
                continue
            pending.append((item, target))
        return pending, identical

    def begin_restore(
        self,
        verified: VerifiedRecovery,
        journal: dict,
    ) -> VerifiedRecovery:
        if not isinstance(journal, dict):
            raise ValueError("restore journal 必须是对象")
        manifest = deepcopy(verified.manifest)
        manifest["status"] = "restoring"
        manifest["restore_journal"] = {
            "started_at": utc_now().isoformat(),
            **deepcopy(journal),
        }
        _atomic_write_text(
            verified.entry_dir / "manifest.json",
            self._manifest_json(manifest),
        )
        self._verify_manifest(manifest, verified.entry_dir)
        return VerifiedRecovery(manifest=manifest, entry_dir=verified.entry_dir)

    def replace_manifest(
        self,
        verified: VerifiedRecovery,
        manifest: dict,
    ) -> VerifiedRecovery:
        replacement = deepcopy(manifest)
        self._verify_manifest(replacement, verified.entry_dir)
        _atomic_write_text(
            verified.entry_dir / "manifest.json",
            self._manifest_json(replacement),
        )
        self._verify_manifest(replacement, verified.entry_dir)
        return VerifiedRecovery(
            manifest=replacement,
            entry_dir=verified.entry_dir,
        )

    def mark_restored(self, verified: VerifiedRecovery) -> dict:
        manifest = deepcopy(verified.manifest)
        manifest["status"] = "restored"
        manifest["restored_at"] = utc_now().isoformat()
        if isinstance(manifest.get("restore_journal"), dict):
            manifest["restore_journal"]["completed_at"] = manifest["restored_at"]
        _atomic_write_text(
            verified.entry_dir / "manifest.json",
            self._manifest_json(manifest),
        )
        self._verify_manifest(manifest, verified.entry_dir)
        return manifest
