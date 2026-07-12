"""带 revision 的单进程事务化 SessionStore。"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar
from uuid import UUID, uuid4, uuid5

from core.path_policy import (
    resolve_saves_dir,
    resolve_session_path,
    resolve_under,
    validate_file_id,
)


T = TypeVar("T")
LEGACY_MESSAGE_NAMESPACE = UUID("5ed9739c-d4a1-4baa-92a9-0e105fdd72a1")


class RevisionConflict(RuntimeError):
    """调用方持有的 revision 已落后于磁盘当前版本。"""

    def __init__(self, expected: int, current: int, session: dict):
        super().__init__(f"存档版本冲突：expected={expected}, current={current}")
        self.expected = expected
        self.current = current
        self.session = session


@dataclass
class MutationResult(Generic[T]):
    session: dict
    value: T


def atomic_write(path: Path, data: str) -> None:
    """同目录唯一临时文件 → flush/fsync → os.replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def snapshot_timestamp() -> str:
    return f"{datetime.now():%Y%m%d_%H%M%S_%f}_{uuid4().hex[:8]}"


def _coerce_revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _valid_uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        return None


def _legacy_message_uuid(
    project: str,
    save_id: str,
    index: int,
    message: dict,
) -> str:
    stable_fields = {
        "project": project,
        "save": save_id,
        "index": index,
        "role": message.get("role", ""),
        "content": message.get("content", ""),
        "thinking": message.get("thinking", ""),
        "created_at": message.get("created_at", ""),
    }
    seed = json.dumps(stable_fields, ensure_ascii=False, sort_keys=True)
    return str(uuid5(LEGACY_MESSAGE_NAMESPACE, seed))


def normalize_session(session: dict, project: str, save_id: str) -> dict:
    """只改内存副本；为旧存档补 revision、默认字段和稳定消息 UUID。"""
    session["project"] = project
    session["session_id"] = save_id
    session["revision"] = _coerce_revision(session.get("revision", 0))
    session.setdefault("summaries", [])
    session.setdefault("summary_error", "")
    history = session.setdefault("message_history", [])
    seen: set[str] = set()
    for index, message in enumerate(history):
        if not isinstance(message, dict):
            continue
        message.setdefault("pinned", False)
        message_id = _valid_uuid(message.get("id"))
        if message_id is None or message_id in seen:
            message_id = _legacy_message_uuid(project, save_id, index, message)
            while message_id in seen:
                message_id = str(uuid4())
        message["id"] = message_id
        seen.add(message_id)
    return session


class MutationContext:
    def __init__(
        self,
        store: "SessionStore",
        project: str,
        save_id: str,
        *,
        existed: bool,
    ):
        self.store = store
        self.project = project
        self.save_id = save_id
        self.existed = existed
        self.snapshot_paths: list[Path] = []

    def snapshot(self, kind: str, payload: dict) -> Path:
        path = self.store._write_snapshot_sync(
            self.project,
            self.save_id,
            kind,
            payload,
        )
        self.snapshot_paths.append(path)
        return path


class SessionStore:
    """单进程、按 project/save 隔离的异步事务入口。"""

    def __init__(self, projects_root: Path):
        self.projects_root = Path(projects_root).resolve(strict=False)
        self._save_locks: dict[str, asyncio.Lock] = {}
        self._project_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    def saves_dir(self, project: str) -> Path:
        return resolve_saves_dir(self.projects_root, project)

    def session_path(self, project: str, save_id: str) -> Path:
        return resolve_session_path(self.projects_root, project, save_id)

    def history_dir(self, project: str) -> Path:
        return resolve_under(self.saves_dir(project), ".history")

    async def _get_lock(self, namespace: str, key: str) -> asyncio.Lock:
        mapping = self._project_locks if namespace == "project" else self._save_locks
        full_key = f"{namespace}:{key}"
        lock = mapping.get(full_key)
        if lock is not None:
            return lock
        async with self._locks_guard:
            lock = mapping.get(full_key)
            if lock is None:
                lock = asyncio.Lock()
                mapping[full_key] = lock
            return lock

    async def save_lock(self, project: str, save_id: str) -> asyncio.Lock:
        project = validate_file_id(project, label="项目 ID")
        save_id = validate_file_id(save_id, label="存档 ID")
        return await self._get_lock("save", f"{project}/{save_id}")

    async def project_lock(self, project: str) -> asyncio.Lock:
        project = validate_file_id(project, label="项目 ID")
        return await self._get_lock("project", project)

    @asynccontextmanager
    async def _save_lock_group(self, project: str, save_ids: list[str]):
        async with AsyncExitStack() as stack:
            for save_id in sorted(set(save_ids)):
                lock = await self.save_lock(project, save_id)
                await stack.enter_async_context(lock)
            yield

    def _read_sync(self, project: str, save_id: str) -> dict | None:
        path = self.session_path(project, save_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("存档 JSON 顶层必须是对象")
        return normalize_session(data, project, save_id)

    def read_sync(self, project: str, save_id: str) -> dict | None:
        return self._read_sync(project, save_id)

    async def read(self, project: str, save_id: str) -> dict | None:
        """纯读取：不创建目录、不 trim、不持久化兼容字段。"""
        return await asyncio.to_thread(self._read_sync, project, save_id)

    def _write_session_sync(self, session: dict, project: str, save_id: str) -> None:
        path = self.session_path(project, save_id)
        atomic_write(path, json.dumps(session, ensure_ascii=False, indent=2))

    def _write_snapshot_sync(
        self,
        project: str,
        save_id: str,
        kind: str,
        payload: dict,
    ) -> Path:
        if kind not in {"snapshot", "reset", "trim"}:
            raise ValueError(f"未知快照类型: {kind}")
        timestamp = snapshot_timestamp()
        middle = "" if kind == "snapshot" else f".{kind}"
        path = resolve_under(
            self.history_dir(project),
            f"{save_id}{middle}.{timestamp}.json",
        )
        snapshot = deepcopy(payload)
        snapshot["_snapshot_at"] = datetime.now().isoformat()
        snapshot["_snapshot_type"] = kind
        snapshot["session_id"] = save_id
        snapshot["project"] = project
        atomic_write(path, json.dumps(snapshot, ensure_ascii=False, indent=2))
        return path

    @staticmethod
    def _check_revision(expected_revision: int, current: dict) -> None:
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("expected_revision 必须是非负整数")
        current_revision = _coerce_revision(current.get("revision", 0))
        if expected_revision != current_revision:
            raise RevisionConflict(expected_revision, current_revision, deepcopy(current))

    async def create(self, project: str, save_id: str, session: dict) -> dict:
        project_lock = await self.project_lock(project)
        async with project_lock:
            save_lock = await self.save_lock(project, save_id)
            async with save_lock:
                def create_sync() -> dict:
                    if self.session_path(project, save_id).exists():
                        raise FileExistsError(f"存档 ID {save_id} 已存在")
                    created = normalize_session(deepcopy(session), project, save_id)
                    created["revision"] = 1
                    created["updated_at"] = datetime.now().isoformat()
                    self._write_session_sync(created, project, save_id)
                    return created

                return await asyncio.to_thread(create_sync)

    async def mutate(
        self,
        project: str,
        save_id: str,
        expected_revision: int,
        command: Callable[[dict, MutationContext], T],
        *,
        initial_factory: Callable[[], dict],
    ) -> MutationResult[T]:
        lock = await self.save_lock(project, save_id)
        async with lock:
            def mutate_sync() -> MutationResult[T]:
                current = self._read_sync(project, save_id)
                existed = current is not None
                if current is None:
                    current = normalize_session(initial_factory(), project, save_id)
                self._check_revision(expected_revision, current)
                working = deepcopy(current)
                context = MutationContext(
                    self,
                    project,
                    save_id,
                    existed=existed,
                )
                value = command(working, context)
                working = normalize_session(working, project, save_id)
                working["revision"] = current["revision"] + 1
                working["updated_at"] = datetime.now().isoformat()
                self._write_session_sync(working, project, save_id)
                return MutationResult(session=working, value=value)

            return await asyncio.to_thread(mutate_sync)

    async def snapshot(
        self,
        project: str,
        save_id: str,
        expected_revision: int,
        kind: str = "snapshot",
    ) -> tuple[dict, Path]:
        lock = await self.save_lock(project, save_id)
        async with lock:
            def snapshot_sync() -> tuple[dict, Path]:
                current = self._read_sync(project, save_id)
                if current is None:
                    raise FileNotFoundError(f"存档 {save_id} 不存在")
                self._check_revision(expected_revision, current)
                return current, self._write_snapshot_sync(
                    project,
                    save_id,
                    kind,
                    current,
                )

            return await asyncio.to_thread(snapshot_sync)

    async def delete(
        self,
        project: str,
        save_id: str,
        expected_revision: int,
        *,
        minimum_remaining: int = 1,
    ) -> bool:
        project_lock = await self.project_lock(project)
        async with project_lock:
            save_lock = await self.save_lock(project, save_id)
            async with save_lock:
                def delete_sync() -> bool:
                    current = self._read_sync(project, save_id)
                    if current is None:
                        return False
                    self._check_revision(expected_revision, current)
                    saves_dir = self.saves_dir(project)
                    count = len(list(saves_dir.glob("*.json"))) if saves_dir.exists() else 0
                    if count <= minimum_remaining:
                        raise ValueError("至少保留 1 个存档")
                    self.session_path(project, save_id).unlink()
                    return True

                return await asyncio.to_thread(delete_sync)

    async def rename(
        self,
        project: str,
        old_id: str,
        new_id: str,
        new_name: str,
        expected_revision: int,
    ) -> dict:
        project_lock = await self.project_lock(project)
        async with project_lock:
            async with self._save_lock_group(project, [old_id, new_id]):
                def rename_sync() -> dict:
                    current = self._read_sync(project, old_id)
                    if current is None:
                        raise FileNotFoundError(f"存档 {old_id} 不存在")
                    self._check_revision(expected_revision, current)
                    if new_id != old_id and self.session_path(project, new_id).exists():
                        raise FileExistsError(f"存档 {new_id} 已存在")

                    renamed = deepcopy(current)
                    renamed["name"] = new_name
                    renamed["session_id"] = new_id
                    renamed["revision"] = current["revision"] + 1
                    renamed["updated_at"] = datetime.now().isoformat()
                    if new_id == old_id:
                        self._write_session_sync(renamed, project, old_id)
                        return renamed

                    self._write_session_sync(renamed, project, new_id)
                    moved: list[tuple[Path, Path]] = []
                    history_dir = self.history_dir(project)
                    try:
                        if history_dir.exists():
                            for source in history_dir.iterdir():
                                if source.name.startswith(old_id + "."):
                                    target = resolve_under(
                                        history_dir,
                                        new_id + source.name[len(old_id):],
                                    )
                                    source.rename(target)
                                    moved.append((source, target))
                        self.session_path(project, old_id).unlink()
                    except OSError:
                        for source, target in reversed(moved):
                            if target.exists() and not source.exists():
                                target.rename(source)
                        self.session_path(project, new_id).unlink(missing_ok=True)
                        raise
                    return renamed

                return await asyncio.to_thread(rename_sync)
