"""存档领域操作与事务化 SessionStore 适配层。"""
from __future__ import annotations

import json
import logging
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, TypeVar
from uuid import uuid4

from core.config import (
    DEFAULT_SAVE,
    HARD_LIMIT,
    MAX_MESSAGES_IN_SAVE,
    PROJECTS_DIR,
    RECOVERY_DIR,
    TRASH_RETENTION_DAYS,
)
from core.import_validation import validate_import_json
from core.path_policy import (
    display_name_to_id,
    resolve_snapshot_path as _resolve_snapshot_path,
    validate_file_id,
)
from core.session_store import (
    MutationContext,
    MutationResult,
    RevisionConflict,
    SessionStore,
    atomic_write,
)
from core.recovery_store import RecoveryStore


logger = logging.getLogger(__name__)
T = TypeVar("T")

ROOT_DIR = PROJECTS_DIR
_stores: dict[str, SessionStore] = {}


def get_session_store() -> SessionStore:
    root = Path(ROOT_DIR).resolve(strict=False)
    recovery_root = Path(RECOVERY_DIR).resolve(strict=False)
    key = f"{root}|{recovery_root}"
    store = _stores.get(key)
    if store is None:
        store = SessionStore(
            root,
            RecoveryStore(
                recovery_root,
                projects_root=root,
                retention_days=TRASH_RETENTION_DAYS,
            ),
        )
        _stores[key] = store
    return store


def clear_session_stores_for_testing() -> None:
    _stores.clear()


def _saves_dir(project: str) -> Path:
    """纯路径解析；读取路径不得创建目录。"""
    return get_session_store().saves_dir(project)


def _session_path(project: str, save_id: str) -> Path:
    return get_session_store().session_path(project, save_id)


def _history_dir(project: str) -> Path:
    return get_session_store().history_dir(project)


def resolve_snapshot_path(
    project: str,
    save_id: str,
    filename: str,
    *,
    allowed_types: tuple[str, ...] = ("snapshot", "reset", "trim"),
) -> tuple[Path, str]:
    return _resolve_snapshot_path(
        Path(ROOT_DIR),
        project,
        save_id,
        filename,
        allowed_types=allowed_types,
    )


def _empty_session(save_id: str = DEFAULT_SAVE, project: str = "默认项目") -> dict:
    now = datetime.now().isoformat()
    return {
        "session_id": save_id,
        "name": save_id,
        "project": project,
        "revision": 0,
        "created_at": now,
        "updated_at": now,
        "current_model": "",
        "scene_meta": {
            "location": "", "time": "", "weather": "",
            "main_quest": "", "current_scene": "", "next_goal": "",
        },
        "user_status": {
            "name": "", "identity": "", "condition": "", "abilities": [],
        },
        "characters_state": {},
        "message_history": [],
        "summaries": [],
        "summary_error": "",
    }


def new_session(project: str, save_id: str = DEFAULT_SAVE) -> dict:
    return _empty_session(save_id, project)


def load_session(project: str, save_id: str = DEFAULT_SAVE) -> dict:
    """纯读取：不创建目录、不 trim、不迁移磁盘内容。"""
    session = get_session_store().read_sync(project, save_id)
    return session if session is not None else _empty_session(save_id, project)


async def aload_session(project: str, save_id: str = DEFAULT_SAVE) -> dict:
    session = await get_session_store().read(project, save_id)
    return session if session is not None else _empty_session(save_id, project)


async def mutate_session(
    project: str,
    save_id: str,
    expected_revision: int,
    command: Callable[[dict, MutationContext], T],
) -> MutationResult[T]:
    return await get_session_store().mutate(
        project,
        save_id,
        expected_revision,
        command,
        initial_factory=lambda: _empty_session(save_id, project),
    )


async def save_session(
    session: dict,
    project: str,
    save_id: str,
    expected_revision: int | None = None,
) -> dict:
    """兼容替换入口；不再做字段猜测合并。"""
    expected = session.get("revision", 0) if expected_revision is None else expected_revision
    replacement = deepcopy(session)

    def replace(current: dict, context: MutationContext) -> None:
        current.clear()
        current.update(deepcopy(replacement))

    result = await mutate_session(project, save_id, expected, replace)
    session.clear()
    session.update(deepcopy(result.session))
    return result.session


def append_history(session: dict, role: str, content: str, thinking: str = "") -> dict:
    message = {
        "id": str(uuid4()),
        "role": role,
        "content": content,
        "pinned": False,
        "in_prompt": True,
    }
    if thinking:
        message["thinking"] = thinking
    session.setdefault("message_history", []).append(message)
    return message


def resolve_message(
    session: dict,
    *,
    message_id: str | None = None,
    index: int | None = None,
) -> tuple[int, dict]:
    history = session.setdefault("message_history", [])
    if message_id:
        for position, message in enumerate(history):
            if message.get("id") == message_id:
                return position, message
        raise IndexError("消息 ID 不存在")
    if index is None or index < 0 or index >= len(history):
        raise IndexError("无效的 index")
    return index, history[index]


def trim_history(
    session: dict,
    max_messages: int = MAX_MESSAGES_IN_SAVE,
    project: Optional[str] = None,
) -> list[dict]:
    """纯内存 trim；快照由调用方在同一事务的 MutationContext 中写入。"""
    del project  # 兼容旧调用签名；读取/纯函数路径不再写盘。
    max_messages = HARD_LIMIT if len(session.get("message_history", [])) > HARD_LIMIT else max_messages
    history = session.get("message_history", [])
    non_pinned = [message for message in history if not message.get("pinned")]
    if len(non_pinned) <= max_messages:
        return []
    keep_non = non_pinned[-max_messages:]
    dropped = non_pinned[:-max_messages]
    keep_ids = {message.get("id") for message in keep_non}
    session["message_history"] = [
        message
        for message in history
        if message.get("pinned") or message.get("id") in keep_ids
    ]
    return dropped


def trim_snapshot_payload(dropped: list[dict]) -> dict:
    return {
        "dropped_count": len(dropped),
        "dropped_messages": deepcopy(dropped),
    }


def list_trim_snapshots(project: str, save_id: str = DEFAULT_SAVE) -> list[dict]:
    save_id = validate_file_id(save_id, label="存档 ID")
    history_dir = _history_dir(project)
    if not history_dir.exists():
        return []
    snapshots: list[dict] = []
    for path in sorted(history_dir.glob(f"{save_id}.trim.*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            snapshots.append({
                "filename": path.name,
                "timestamp": path.stem.split(".trim.", 1)[-1],
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
                "dropped_count": data.get("dropped_count", 0),
                "snapshot_at": data.get("_snapshot_at", ""),
            })
        except (json.JSONDecodeError, OSError):
            continue
    return snapshots


async def snapshot_session(
    project: str,
    save_id: str,
    expected_revision: int,
    kind: str = "snapshot",
) -> tuple[dict, Path]:
    return await get_session_store().snapshot(
        project,
        save_id,
        expected_revision,
        kind,
    )


async def reset_session(
    project: str,
    save_id: str,
    expected_revision: int,
    replacement: dict | None = None,
) -> dict:
    reset_value = deepcopy(replacement or _empty_session(save_id, project))

    def reset(current: dict, context: MutationContext) -> None:
        if context.existed:
            context.checkpoint("reset")
            context.snapshot("reset", current)
        current.clear()
        current.update(deepcopy(reset_value))

    mutation = await mutate_session(project, save_id, expected_revision, reset)
    return {
        "session": mutation.session,
        "recovery_id": mutation.recovery_ids[-1] if mutation.recovery_ids else None,
    }


def _safe_filename(name: str) -> str:
    return display_name_to_id(name, label="存档显示名")


def list_sessions(project: str) -> list[dict]:
    return get_session_store().list_sessions_sync(project)


def session_exists(project: str, save_id: str) -> bool:
    return _session_path(project, save_id).is_file()


async def create_session(project: str, name: str, initial: dict | None = None) -> dict:
    save_id = _safe_filename(name)
    session = deepcopy(initial or _empty_session(save_id, project))
    session["name"] = name.strip()
    return await get_session_store().create(project, save_id, session)


async def rename_session(
    project: str,
    old_id: str,
    new_name: str,
    expected_revision: int,
) -> dict:
    new_id = _safe_filename(new_name)
    renamed = await get_session_store().rename(
        project,
        old_id,
        new_id,
        new_name.strip(),
        expected_revision,
    )
    return {
        "session": dict(renamed),
        "recovery_id": renamed.recovery_id,
    }


async def delete_session(
    project: str,
    save_id: str,
    expected_revision: int,
) -> dict:
    deleted = await get_session_store().delete(
        project,
        save_id,
        expected_revision,
    )
    return {
        "deleted": deleted is not None,
        "recovery_id": deleted.recovery_id if deleted is not None else None,
    }


async def list_recovery_items(
    *,
    category: str | None = None,
    project: str | None = None,
    entity_type: str | None = None,
) -> list[dict]:
    return await get_session_store().list_recoveries(
        category=category,
        project=project,
        entity_type=entity_type,
    )


async def quarantine_session(
    project: str,
    save_id: str,
    fingerprint: str,
) -> dict:
    recovery, deduplicated = await get_session_store().quarantine_corrupt(
        project,
        save_id,
        fingerprint,
    )
    return {
        "recovery": recovery,
        "deduplicated": deduplicated,
    }


async def restore_recovery_item(
    recovery_id: str,
    *,
    expected_revision: int | None = None,
    overwrite: bool = False,
) -> dict:
    return await get_session_store().restore_recovery(
        recovery_id,
        expected_revision=expected_revision,
        overwrite=overwrite,
    )


def export_session(project: str, save_id: str) -> str:
    return json.dumps(load_session(project, save_id), ensure_ascii=False, indent=2)


async def import_session(project: str, json_str: str, name: str = None) -> dict:
    data = validate_import_json(json_str)
    target_name = name or data.get("name") or data["session_id"]
    save_id = _safe_filename(target_name)
    data["session_id"] = save_id
    data["name"] = target_name.strip()
    data["project"] = project
    data["revision"] = 0
    return await get_session_store().create(project, save_id, data)


def toggle_pinned(
    session: dict,
    *,
    message_id: str | None = None,
    index: int | None = None,
) -> dict:
    _, message = resolve_message(session, message_id=message_id, index=index)
    message["pinned"] = not message.get("pinned", False)
    return message


def count_pinned(session: dict) -> int:
    return sum(1 for message in session.get("message_history", []) if message.get("pinned"))
