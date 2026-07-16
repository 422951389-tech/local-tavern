"""项目级破坏性操作：先写入可验证 trash，再执行可补偿变更。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable

from core.active_turns import assert_project_write_allowed
from core.library_lock import library_lock
from core.path_policy import resolve_project_dir, resolve_under, validate_file_id
from core.recovery_store import (
    RecoveryConflict,
    RecoveryIntegrityError,
    VerifiedRecovery,
    sha256_file,
)
from core.session_store import (
    RevisionConflict,
    SessionStore,
    atomic_write,
    normalize_session,
)


EMPTY_USER_STATUS = {
    "name": "",
    "identity": "",
    "condition": "",
    "abilities": [],
}
DESTRUCTIVE_ENTITY_TYPES = frozenset({"project", "character", "user", "worldbook"})


def _run_with_library_shared(callback, *args, **kwargs):
    with library_lock.shared():
        return callback(*args, **kwargs)


def _run_with_library_exclusive(callback, *args, **kwargs):
    with library_lock.exclusive():
        return callback(*args, **kwargs)


class DestructiveOperationError(RuntimeError):
    """破坏性事务失败；错误详情只暴露稳定分类。"""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        recovery_id: str | None = None,
        needs_recovery: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.recovery_id = recovery_id
        self.needs_recovery = needs_recovery

    def as_detail(self) -> dict:
        return {
            "code": self.code,
            "message": str(self),
            "recovery_id": self.recovery_id,
            "needs_recovery": self.needs_recovery,
        }


class DestructiveRecoveryRequired(DestructiveOperationError):
    def __init__(self, recovery_id: str):
        super().__init__(
            "事务补偿未完整结束，请使用 recovery_id 执行人工恢复",
            code="needs_recovery",
            recovery_id=recovery_id,
            needs_recovery=True,
        )


class DestructiveService:
    """所有删除统一持有 project lock，再按稳定顺序持有 save locks。"""

    def __init__(self, session_store: SessionStore):
        self.session_store = session_store
        self.recovery_store = session_store.recovery_store

    @staticmethod
    def _validate_entity_id(value: str, *, label: str) -> str:
        value = validate_file_id(value, label=label)
        if value.startswith("_"):
            raise ValueError(f"{label}不能以下划线开头")
        return value

    def _source_relpath(self, source: Path) -> str:
        resolved = Path(source).resolve(strict=False)
        try:
            relative = resolved.relative_to(self.session_store.projects_root)
        except ValueError as exc:
            raise RecoveryIntegrityError("删除源路径越出 projects 根目录") from exc
        return PurePosixPath(*relative.parts).as_posix()

    def _project_dir(self, project: str) -> Path:
        return resolve_project_dir(self.session_store.projects_root, project)

    def _character_path(self, project: str, char_id: str) -> Path:
        return resolve_under(
            self._project_dir(project),
            "characters",
            f"{char_id}.yaml",
        )

    def _user_path(self, project: str) -> Path:
        return resolve_under(self._project_dir(project), "user.yaml")

    def _worldbook_path(self, project: str, entry_id: str) -> Path:
        directory = resolve_under(self._project_dir(project), "worldbook")
        yaml_path = resolve_under(directory, f"{entry_id}.yaml")
        yml_path = resolve_under(directory, f"{entry_id}.yml")
        existing = [path for path in (yaml_path, yml_path) if path.is_file()]
        if len(existing) > 1:
            raise ValueError(f"世界书条目 {entry_id} 同时存在 .yaml 与 .yml")
        return existing[0] if existing else yaml_path

    def _tombstone_metadata(self, source: Path, *, kind: str) -> dict:
        source_relpath = self._source_relpath(source)
        tombstone_relpath = PurePosixPath(
            "tombstone",
            *PurePosixPath(source_relpath).parts,
        ).as_posix()
        return {
            "tombstone_relpath": tombstone_relpath,
            "tombstone_source_relpath": source_relpath,
            "tombstone_kind": kind,
            "needs_recovery": False,
        }

    def _list_save_ids_sync(self, project: str) -> list[str]:
        saves_dir = self.session_store.saves_dir(project)
        if not saves_dir.is_dir():
            return []
        save_ids: list[str] = []
        for path in saves_dir.glob("*.json"):
            if not path.is_file() or path.stem.startswith("."):
                continue
            save_ids.append(validate_file_id(path.stem, label="存档 ID"))
        return sorted(set(save_ids))

    @staticmethod
    def _session_digest(store: SessionStore, session: dict) -> str:
        serialized = store._serialize_session(session)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _manifest_item_digest(verified: VerifiedRecovery, source_relpath: str) -> str:
        for item in verified.manifest["items"]:
            if item["source_relpath"] == source_relpath:
                return item["sha256"]
        raise RecoveryIntegrityError("恢复项缺少指定源文件")

    def _project_files_sync(self, project_dir: Path) -> list[Path]:
        files: list[Path] = []
        for path in project_dir.rglob("*"):
            if path.is_symlink():
                raise ValueError("项目目录不能包含符号链接")
            if path.is_file():
                files.append(path)
        files.sort(key=lambda path: self._source_relpath(path))
        if not files:
            raise ValueError("空项目无法创建完整 trash")
        return files

    def _verify_project_tree(
        self,
        verified: VerifiedRecovery,
        root: Path,
        project: str,
    ) -> None:
        expected: dict[str, str] = {}
        project_path = PurePosixPath(project)
        for item in verified.manifest["items"]:
            source = PurePosixPath(item["source_relpath"])
            try:
                relative = source.relative_to(project_path)
            except ValueError as exc:
                raise RecoveryIntegrityError("project payload 归属不一致") from exc
            expected[relative.as_posix()] = item["sha256"]

        actual: dict[str, Path] = {}
        for path in root.rglob("*"):
            if path.is_symlink():
                raise RecoveryIntegrityError("project tombstone 包含符号链接")
            if path.is_file():
                actual[PurePosixPath(*path.relative_to(root).parts).as_posix()] = path
        if set(actual) != set(expected):
            raise RecoveryIntegrityError("project tombstone 文件集合不匹配")
        for relative, path in actual.items():
            if sha256_file(path) != expected[relative]:
                raise RecoveryIntegrityError("project tombstone 文件哈希不匹配")

    def _verify_primary_file(
        self,
        verified: VerifiedRecovery,
        path: Path,
    ) -> None:
        if not path.is_file():
            raise RecoveryIntegrityError("实体 tombstone 缺失")
        expected = self._manifest_item_digest(
            verified,
            verified.manifest["source_relpath"],
        )
        if sha256_file(path) != expected:
            raise RecoveryIntegrityError("实体 tombstone 哈希不匹配")

    def _record_needs_recovery(self, recovery_id: str, stage: str) -> None:
        try:
            self.recovery_store.record_needs_recovery(
                recovery_id,
                stage=stage,
                error_code="compensation_failed",
            )
        except BaseException:
            pass

    def _create_trash(self, **kwargs) -> dict:
        try:
            return self.recovery_store.create_entry(category="trash", **kwargs)
        except (
            FileNotFoundError,
            RecoveryConflict,
            RecoveryIntegrityError,
            ValueError,
        ):
            raise
        except BaseException as exc:
            raise DestructiveOperationError(
                "trash 创建失败，源数据未变更",
                code="trash_create_failed",
            ) from exc

    def _compensate_delete(
        self,
        verified: VerifiedRecovery,
        *,
        primary_source: Path,
        written_save_ids: list[str],
        move_attempted: bool,
    ) -> None:
        tombstone = self.recovery_store.tombstone_path(verified)
        failures: list[BaseException] = []

        if move_attempted and not primary_source.exists():
            try:
                if not tombstone.exists():
                    raise RecoveryIntegrityError("删除补偿所需 tombstone 缺失")
                primary_source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tombstone, primary_source)
            except BaseException as exc:
                failures.append(exc)
        elif primary_source.exists() and tombstone.exists():
            failures.append(RecoveryIntegrityError("删除补偿检测到双重主源"))

        for save_id in reversed(list(dict.fromkeys(written_save_ids))):
            source_relpath = self._source_relpath(
                self.session_store.session_path(
                    verified.manifest["project"],
                    save_id,
                )
            )
            try:
                target = self.recovery_store.restore_item_exact(
                    verified,
                    source_relpath,
                )
                item = self.recovery_store.item_for_source(verified, source_relpath)
                if sha256_file(target) != item["sha256"]:
                    raise RecoveryIntegrityError("删除补偿后的存档哈希不匹配")
            except BaseException as exc:
                failures.append(exc)

        if failures:
            raise RecoveryIntegrityError("删除事务补偿未完整结束") from failures[0]

    def _raise_delete_failure(
        self,
        recovery_id: str,
        compensation_error: BaseException | None,
    ) -> None:
        if compensation_error is not None:
            self._record_needs_recovery(recovery_id, "delete_compensation")
            raise DestructiveRecoveryRequired(recovery_id) from compensation_error
        raise DestructiveOperationError(
            "删除事务未提交，原数据已恢复",
            code="delete_failed",
            recovery_id=recovery_id,
        )

    def _prepare_session_changes(
        self,
        project: str,
        save_ids: list[str],
        active_save_id: str,
        expected_revision: int,
        mutator: Callable[[dict], bool],
    ) -> tuple[dict, dict[str, dict]]:
        sessions: dict[str, dict] = {}
        for save_id in save_ids:
            session = self.session_store._read_sync(project, save_id)
            if session is not None:
                sessions[save_id] = session
        active = sessions.get(active_save_id)
        if active is None:
            raise FileNotFoundError(f"存档 {active_save_id} 不存在")
        self.session_store._check_revision(expected_revision, active)

        changed: dict[str, dict] = {}
        changed_at = datetime.now().isoformat()
        for save_id in sorted(sessions):
            candidate = deepcopy(sessions[save_id])
            if not mutator(candidate):
                continue
            candidate["revision"] = sessions[save_id]["revision"] + 1
            candidate["updated_at"] = changed_at
            changed[save_id] = candidate
        return active, changed

    def _delete_profile_sync(
        self,
        *,
        entity_type: str,
        entity_id: str,
        project: str,
        source: Path,
        save_ids: list[str],
        active_save_id: str,
        expected_revision: int,
        mutator: Callable[[dict], bool],
    ) -> dict:
        if not source.is_file():
            raise FileNotFoundError(f"{entity_type} {entity_id} 不存在")
        active, changed = self._prepare_session_changes(
            project,
            save_ids,
            active_save_id,
            expected_revision,
            mutator,
        )
        metadata = self._tombstone_metadata(source, kind="file")
        metadata.update({
            "active_save_id": active_save_id,
            "affected_saves": sorted(changed),
            "post_delete_sessions": {
                save_id: {
                    "revision": changed[save_id]["revision"],
                    "sha256": self._session_digest(
                        self.session_store,
                        changed[save_id],
                    ),
                }
                for save_id in sorted(changed)
            },
        })
        operation = f"delete_{entity_type}"
        source_paths = [
            source,
            *(
                self.session_store.session_path(project, save_id)
                for save_id in sorted(changed)
            ),
        ]
        manifest = self._create_trash(
            operation=operation,
            entity_type=entity_type,
            project=project,
            entity_id=entity_id,
            source_paths=source_paths,
            source_revision=active["revision"],
            metadata=metadata,
        )
        recovery_id = manifest["recovery_id"]
        verified = self.recovery_store.get_verified(recovery_id)
        written: list[str] = []
        move_attempted = False
        try:
            for save_id in sorted(changed):
                written.append(save_id)
                self.session_store._write_session_sync(
                    changed[save_id],
                    project,
                    save_id,
                )
                expected = metadata["post_delete_sessions"][save_id]["sha256"]
                if sha256_file(self.session_store.session_path(project, save_id)) != expected:
                    raise RecoveryIntegrityError("删除后的存档哈希不匹配")

            primary_digest = self._manifest_item_digest(
                verified,
                verified.manifest["source_relpath"],
            )
            if not source.is_file() or sha256_file(source) != primary_digest:
                raise RecoveryConflict("删除主源已发生变化", code="source_changed")
            tombstone = self.recovery_store.tombstone_path(verified)
            tombstone.parent.mkdir(parents=True, exist_ok=True)
            move_attempted = True
            os.replace(source, tombstone)
            self._verify_primary_file(verified, tombstone)
        except BaseException as exc:
            compensation_error: BaseException | None = None
            try:
                self._compensate_delete(
                    verified,
                    primary_source=source,
                    written_save_ids=written,
                    move_attempted=move_attempted,
                )
            except BaseException as rollback_exc:
                compensation_error = rollback_exc
            self._raise_delete_failure(recovery_id, compensation_error)
            raise AssertionError("unreachable") from exc

        active_result = changed.get(active_save_id, active)
        return {
            "deleted": True,
            "id": entity_id,
            "recovery_id": recovery_id,
            "affected_saves": sorted(changed),
            "session": deepcopy(active_result),
        }

    async def delete_character(
        self,
        project: str,
        char_id: str,
        save_id: str,
        expected_revision: int,
    ) -> dict:
        project = validate_file_id(project, label="项目 ID")
        assert_project_write_allowed(project)
        char_id = self._validate_entity_id(char_id, label="角色 ID")
        save_id = validate_file_id(save_id, label="存档 ID")
        project_lock = await self.session_store.project_lock(project)
        async with project_lock:
            save_ids = self._list_save_ids_sync(project)
            async with self.session_store._save_lock_group(
                project,
                [*save_ids, save_id],
            ):
                assert_project_write_allowed(project)
                def remove_character(session: dict) -> bool:
                    states = session.get("characters_state")
                    if not isinstance(states, dict) or char_id not in states:
                        return False
                    del states[char_id]
                    return True

                return await asyncio.to_thread(
                    _run_with_library_shared,
                    self._delete_profile_sync,
                    entity_type="character",
                    entity_id=char_id,
                    project=project,
                    source=self._character_path(project, char_id),
                    save_ids=save_ids,
                    active_save_id=save_id,
                    expected_revision=expected_revision,
                    mutator=remove_character,
                )

    async def delete_user(
        self,
        project: str,
        save_id: str,
        expected_revision: int,
    ) -> dict:
        project = validate_file_id(project, label="项目 ID")
        assert_project_write_allowed(project)
        save_id = validate_file_id(save_id, label="存档 ID")
        project_lock = await self.session_store.project_lock(project)
        async with project_lock:
            save_ids = self._list_save_ids_sync(project)
            async with self.session_store._save_lock_group(
                project,
                [*save_ids, save_id],
            ):
                assert_project_write_allowed(project)
                def clear_user(session: dict) -> bool:
                    if session.get("user_status") == EMPTY_USER_STATUS:
                        return False
                    session["user_status"] = deepcopy(EMPTY_USER_STATUS)
                    return True

                return await asyncio.to_thread(
                    _run_with_library_shared,
                    self._delete_profile_sync,
                    entity_type="user",
                    entity_id="user",
                    project=project,
                    source=self._user_path(project),
                    save_ids=save_ids,
                    active_save_id=save_id,
                    expected_revision=expected_revision,
                    mutator=clear_user,
                )

    def _delete_file_only_sync(
        self,
        *,
        entity_type: str,
        entity_id: str,
        project: str,
        source: Path,
    ) -> dict:
        if not source.is_file():
            raise FileNotFoundError(f"{entity_type} {entity_id} 不存在")
        metadata = self._tombstone_metadata(source, kind="file")
        metadata.update({"affected_saves": [], "post_delete_sessions": {}})
        manifest = self._create_trash(
            operation=f"delete_{entity_type}",
            entity_type=entity_type,
            project=project,
            entity_id=entity_id,
            source_paths=[source],
            metadata=metadata,
        )
        recovery_id = manifest["recovery_id"]
        verified = self.recovery_store.get_verified(recovery_id)
        move_attempted = False
        try:
            expected = self._manifest_item_digest(
                verified,
                verified.manifest["source_relpath"],
            )
            if not source.is_file() or sha256_file(source) != expected:
                raise RecoveryConflict("删除主源已发生变化", code="source_changed")
            tombstone = self.recovery_store.tombstone_path(verified)
            tombstone.parent.mkdir(parents=True, exist_ok=True)
            move_attempted = True
            os.replace(source, tombstone)
            self._verify_primary_file(verified, tombstone)
        except BaseException as exc:
            compensation_error: BaseException | None = None
            try:
                self._compensate_delete(
                    verified,
                    primary_source=source,
                    written_save_ids=[],
                    move_attempted=move_attempted,
                )
            except BaseException as rollback_exc:
                compensation_error = rollback_exc
            self._raise_delete_failure(recovery_id, compensation_error)
            raise AssertionError("unreachable") from exc
        return {
            "deleted": True,
            "id": entity_id,
            "recovery_id": recovery_id,
            "affected_saves": [],
        }

    async def delete_worldbook(
        self,
        project: str,
        entry_id: str,
    ) -> dict:
        project = validate_file_id(project, label="项目 ID")
        assert_project_write_allowed(project)
        entry_id = self._validate_entity_id(entry_id, label="世界书 ID")
        project_lock = await self.session_store.project_lock(project)
        async with project_lock:
            assert_project_write_allowed(project)
            return await asyncio.to_thread(
                _run_with_library_shared,
                self._delete_file_only_sync,
                entity_type="worldbook",
                entity_id=entry_id,
                project=project,
                source=self._worldbook_path(project, entry_id),
            )

    def _delete_project_sync(self, project: str, project_dir: Path) -> dict:
        if not project_dir.is_dir():
            raise FileNotFoundError(f"项目 {project} 不存在")
        project_count = sum(
            1
            for path in self.session_store.projects_root.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )
        if project_count <= 1:
            raise ValueError("至少保留 1 个项目")
        source_paths = self._project_files_sync(project_dir)
        metadata = self._tombstone_metadata(project_dir, kind="directory")
        metadata.update({"affected_saves": [], "post_delete_sessions": {}})
        manifest = self._create_trash(
            operation="delete_project",
            entity_type="project",
            project=project,
            entity_id=project,
            source_paths=source_paths,
            metadata=metadata,
        )
        recovery_id = manifest["recovery_id"]
        verified = self.recovery_store.get_verified(recovery_id)
        move_attempted = False
        try:
            for item in verified.manifest["items"]:
                source = self.recovery_store.target_path(item["source_relpath"])
                if not source.is_file() or sha256_file(source) != item["sha256"]:
                    raise RecoveryConflict(
                        "项目内容在删除期间发生变化",
                        code="source_changed",
                    )
            tombstone = self.recovery_store.tombstone_path(verified)
            tombstone.parent.mkdir(parents=True, exist_ok=True)
            move_attempted = True
            os.replace(project_dir, tombstone)
            self._verify_project_tree(verified, tombstone, project)
        except BaseException as exc:
            compensation_error: BaseException | None = None
            try:
                self._compensate_delete(
                    verified,
                    primary_source=project_dir,
                    written_save_ids=[],
                    move_attempted=move_attempted,
                )
            except BaseException as rollback_exc:
                compensation_error = rollback_exc
            self._raise_delete_failure(recovery_id, compensation_error)
            raise AssertionError("unreachable") from exc
        return {"deleted": True, "recovery_id": recovery_id}

    async def delete_project(self, project: str) -> dict:
        project = validate_file_id(project, label="项目 ID")
        assert_project_write_allowed(project)
        project_lock = await self.session_store.project_lock(project)
        async with project_lock:
            save_ids = self._list_save_ids_sync(project)
            async with self.session_store._save_lock_group(project, save_ids):
                assert_project_write_allowed(project)
                return await asyncio.to_thread(
                    _run_with_library_exclusive,
                    self._delete_project_sync,
                    project,
                    self._project_dir(project),
                )

    def _affected_save_ids(self, verified: VerifiedRecovery) -> list[str]:
        metadata = verified.manifest.get("metadata", {})
        value = metadata.get("affected_saves", [])
        if not isinstance(value, list):
            raise RecoveryIntegrityError("恢复项 affected_saves 无效")
        save_ids = [validate_file_id(item, label="存档 ID") for item in value]
        if save_ids != sorted(set(save_ids)):
            raise RecoveryIntegrityError("恢复项 affected_saves 必须有序且唯一")
        return save_ids

    def _preflight_restore_sessions(
        self,
        verified: VerifiedRecovery,
        save_ids: list[str],
    ) -> tuple[dict[str, dict], dict[str, str], dict[str, dict]]:
        manifest = verified.manifest
        project = manifest["project"]
        metadata = manifest["metadata"]
        post_delete = metadata.get("post_delete_sessions", {})
        if not isinstance(post_delete, dict) or set(post_delete) != set(save_ids):
            raise RecoveryIntegrityError("恢复项 post_delete_sessions 无效")

        current_sessions: dict[str, dict] = {}
        current_text: dict[str, str] = {}
        restored_sessions: dict[str, dict] = {}
        restored_at = datetime.now().isoformat()
        if manifest["entity_type"] in {"character", "user"}:
            expected_item_paths = {
                self._source_relpath(self.session_store.session_path(project, save_id))
                for save_id in save_ids
            }
            actual_item_paths = {
                item["source_relpath"]
                for item in manifest["items"]
                if item["source_relpath"] != manifest["source_relpath"]
            }
            if actual_item_paths != expected_item_paths:
                raise RecoveryIntegrityError("恢复项受影响存档集合不一致")

        for save_id in save_ids:
            expectation = post_delete[save_id]
            if not isinstance(expectation, dict):
                raise RecoveryIntegrityError("恢复项存档后置状态无效")
            revision = expectation.get("revision")
            digest = expectation.get("sha256")
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 0
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise RecoveryIntegrityError("恢复项存档后置校验字段无效")
            path = self.session_store.session_path(project, save_id)
            if not path.is_file() or sha256_file(path) != digest:
                raise RecoveryConflict(
                    "删除后的存档已发生变化",
                    code="target_changed",
                )
            current = self.session_store._read_sync(project, save_id)
            if current is None or current["revision"] != revision:
                raise RecoveryConflict(
                    "删除后的存档版本已发生变化",
                    code="target_changed",
                )
            current_sessions[save_id] = current
            try:
                current_text[save_id] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise RecoveryIntegrityError("删除后的存档无法读取") from exc

            source_relpath = self._source_relpath(path)
            item = self.recovery_store.item_for_source(verified, source_relpath)
            payload = self.recovery_store.payload_path(verified, item)
            try:
                restored = json.loads(payload.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RecoveryIntegrityError("恢复项存档 payload 无法解析") from exc
            if not isinstance(restored, dict):
                raise RecoveryIntegrityError("恢复项存档 payload 顶层必须是对象")
            restored = normalize_session(restored, project, save_id)
            restored["revision"] = revision + 1
            restored["updated_at"] = restored_at
            restored_sessions[save_id] = restored
        return current_sessions, current_text, restored_sessions

    def _compensate_restore(
        self,
        verified: VerifiedRecovery,
        manifest_before: dict,
        *,
        target: Path,
        tombstone: Path,
        move_attempted: bool,
        written_save_ids: list[str],
        current_text: dict[str, str],
        post_delete: dict,
    ) -> None:
        failures: list[BaseException] = []
        if move_attempted and target.exists() and not tombstone.exists():
            try:
                tombstone.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, tombstone)
            except BaseException as exc:
                failures.append(exc)
        elif target.exists() and tombstone.exists():
            failures.append(RecoveryIntegrityError("恢复补偿检测到双重主源"))

        project = manifest_before["project"]
        for save_id in reversed(list(dict.fromkeys(written_save_ids))):
            try:
                path = self.session_store.session_path(project, save_id)
                atomic_write(path, current_text[save_id])
                if sha256_file(path) != post_delete[save_id]["sha256"]:
                    raise RecoveryIntegrityError("恢复补偿后的存档哈希不匹配")
            except BaseException as exc:
                failures.append(exc)

        try:
            current_verified = self.recovery_store.get_verified(
                manifest_before["recovery_id"]
            )
            self.recovery_store.replace_manifest(current_verified, manifest_before)
        except BaseException as exc:
            failures.append(exc)
        if failures:
            raise RecoveryIntegrityError("恢复事务补偿未完整结束") from failures[0]

    def _restore_trash_sync(
        self,
        recovery_id: str,
        locked_project: str,
        locked_save_ids: list[str],
        inspected_entity_type: str,
    ) -> dict:
        verified = self.recovery_store.get_verified(recovery_id)
        manifest = verified.manifest
        manifest_before = deepcopy(manifest)
        entity_type = manifest.get("entity_type")
        if entity_type not in DESTRUCTIVE_ENTITY_TYPES:
            raise ValueError("恢复项不属于破坏性实体 trash")
        if manifest.get("category") != "trash":
            raise ValueError("仅 destructive trash 支持该恢复入口")
        if manifest.get("status") == "restored":
            raise RecoveryConflict("恢复项已经应用", code="recovery_already_applied")
        if manifest.get("status") != "complete":
            raise RecoveryConflict("恢复项正在处理", code="recovery_in_progress")

        project = validate_file_id(manifest["project"], label="项目 ID")
        entity_id = validate_file_id(manifest["entity_id"], label="实体 ID")
        save_ids = self._affected_save_ids(verified)
        if (
            project != locked_project
            or save_ids != locked_save_ids
            or entity_type != inspected_entity_type
        ):
            raise RecoveryIntegrityError("恢复项归属在加锁期间发生变化")
        metadata = manifest["metadata"]
        source_relpath = metadata["tombstone_source_relpath"]
        target = self.recovery_store.target_path(source_relpath)
        tombstone = self.recovery_store.tombstone_path(verified)
        if target.exists():
            raise RecoveryConflict("恢复目标已存在", code="target_changed")
        if entity_type == "worldbook":
            sibling_suffix = ".yml" if target.suffix.casefold() == ".yaml" else ".yaml"
            if target.with_suffix(sibling_suffix).exists():
                raise RecoveryConflict("世界书另一扩展目标已存在", code="target_changed")
        if not target.parent.is_dir():
            raise RecoveryConflict("恢复目标父目录不存在", code="target_changed")
        if metadata["tombstone_kind"] == "directory":
            if not tombstone.is_dir():
                raise RecoveryIntegrityError("project tombstone 缺失")
            self._verify_project_tree(verified, tombstone, project)
        else:
            self._verify_primary_file(verified, tombstone)

        _current, current_text, restored_sessions = self._preflight_restore_sessions(
            verified,
            save_ids,
        )
        post_delete = metadata.get("post_delete_sessions", {})
        result_sessions = {
            save_id: {
                "revision": restored_sessions[save_id]["revision"],
                "sha256": self._session_digest(
                    self.session_store,
                    restored_sessions[save_id],
                ),
            }
            for save_id in save_ids
        }

        transition_attempted = False
        written: list[str] = []
        move_attempted = False
        try:
            transition_attempted = True
            verified = self.recovery_store.begin_restore(
                verified,
                {
                    "entity_type": entity_type,
                    "result_sessions": result_sessions,
                    "tombstone_source_relpath": source_relpath,
                },
            )
            for save_id in save_ids:
                written.append(save_id)
                self.session_store._write_session_sync(
                    restored_sessions[save_id],
                    project,
                    save_id,
                )
                path = self.session_store.session_path(project, save_id)
                if sha256_file(path) != result_sessions[save_id]["sha256"]:
                    raise RecoveryIntegrityError("恢复后的存档哈希不匹配")
            move_attempted = True
            os.replace(tombstone, target)
            if entity_type == "project":
                self._verify_project_tree(verified, target, project)
            else:
                self._verify_primary_file(verified, target)
            self.recovery_store.mark_restored(verified)
        except BaseException as exc:
            if not transition_attempted:
                raise
            compensation_error: BaseException | None = None
            try:
                self._compensate_restore(
                    verified,
                    manifest_before,
                    target=target,
                    tombstone=tombstone,
                    move_attempted=move_attempted,
                    written_save_ids=written,
                    current_text=current_text,
                    post_delete=post_delete,
                )
            except BaseException as rollback_exc:
                compensation_error = rollback_exc
            if compensation_error is not None:
                self._record_needs_recovery(recovery_id, "restore_compensation")
                raise DestructiveRecoveryRequired(recovery_id) from compensation_error
            raise DestructiveOperationError(
                "恢复事务未提交，删除后状态已恢复",
                code="restore_failed",
                recovery_id=recovery_id,
            ) from exc

        return {
            "restored": True,
            "recovery_id": recovery_id,
            "undo_recovery_id": None,
            "entity_type": entity_type,
            "project": project,
            "entity_id": entity_id,
        }

    async def restore_trash(self, recovery_id: str) -> dict:
        inspected = await asyncio.to_thread(
            self.recovery_store.get_verified,
            recovery_id,
        )
        manifest = inspected.manifest
        entity_type = manifest.get("entity_type")
        if entity_type not in DESTRUCTIVE_ENTITY_TYPES:
            raise ValueError("恢复项不属于破坏性实体 trash")
        project = validate_file_id(manifest.get("project"), label="项目 ID")
        save_ids = self._affected_save_ids(inspected)
        project_lock = await self.session_store.project_lock(project)
        async with project_lock:
            async with self.session_store._save_lock_group(project, save_ids):
                assert_project_write_allowed(project)
                return await asyncio.to_thread(
                    _run_with_library_shared,
                    self._restore_trash_sync,
                    recovery_id,
                    project,
                    save_ids,
                    entity_type,
                )
