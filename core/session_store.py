"""带 revision 的单进程事务化 SessionStore。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Generic, TypeVar
from uuid import UUID, uuid4, uuid5

from core.active_turns import assert_write_allowed
from core.async_utils import run_sync_critical
from core.library_lock import library_lock
from core.message_commands import MAX_REPLY_ALTERNATIVES, plan_message_regeneration
from core.path_policy import (
    resolve_project_dir,
    resolve_saves_dir,
    resolve_session_path,
    resolve_under,
    validate_file_id,
    validate_snapshot_filename,
)
from core.recovery_store import (
    DataCorruptionError,
    RecoveryConflict,
    RecoveryIntegrityError,
    RecoveryStore,
    sha256_file,
)
from core.roleplay_policy import normalize_silent_turns
from core.relationship_edges import reconcile_relationship_evidence
from core.worldbook_policy import MAX_MANUAL_WORLDBOOK_IDS


T = TypeVar("T")
LEGACY_MESSAGE_NAMESPACE = UUID("5ed9739c-d4a1-4baa-92a9-0e105fdd72a1")
LEGACY_SUMMARY_NAMESPACE = UUID("1322c1c3-22f9-4cad-94f1-4c12ca7dfb21")


def _run_with_library_shared_write(
    callback: Callable[..., T],
    *args,
    **kwargs,
) -> T:
    with library_lock.shared_write():
        return callback(*args, **kwargs)


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
    recovery_ids: tuple[str, ...] = ()


class SessionRecoveryResult(dict):
    """兼容 dict 调用方，同时携带不写入 Session 的 recovery_id。"""

    def __init__(self, session: dict, recovery_id: str):
        super().__init__(session)
        self.recovery_id = recovery_id


def atomic_write(path: Path, data: str) -> None:
    """同目录唯一临时文件 → flush/fsync → os.replace。"""
    with library_lock.shared_write():
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


def _legacy_summary_uuid(
    project: str,
    save_id: str,
    index: int,
    summary: dict,
    *,
    salt: int = 0,
) -> str:
    stable_fields = {
        "project": project,
        "save": save_id,
        "index": index,
        "source_snapshot_id": str(summary.get("source_snapshot_id", "")),
        "created_at": str(summary.get("created_at", "")),
        "text": str(summary.get("text", "")),
        "salt": salt,
    }
    seed = json.dumps(stable_fields, ensure_ascii=False, sort_keys=True)
    return str(uuid5(LEGACY_SUMMARY_NAMESPACE, seed))


class _SessionShapeError(ValueError):
    """磁盘 Session 已有字段不满足可无损规范化的最低结构。"""


def _shape_error(field: str, message: str) -> None:
    raise _SessionShapeError(f"{field} {message}")


def _validate_reply_state_snapshot(value: object, field: str) -> None:
    if not isinstance(value, dict):
        _shape_error(field, "必须是对象")
    unknown = set(value) - {
        "scene_meta",
        "characters_state",
        "relationship_edges",
    }
    if unknown:
        _shape_error(field, "包含未知字段")
    if "scene_meta" in value and not isinstance(value["scene_meta"], dict):
        _shape_error(f"{field}.scene_meta", "必须是对象")
    if "characters_state" in value and not isinstance(value["characters_state"], dict):
        _shape_error(f"{field}.characters_state", "必须是对象")
    if "relationship_edges" in value and not isinstance(
        value["relationship_edges"], list
    ):
        _shape_error(f"{field}.relationship_edges", "必须是数组")


def _validate_reply_variant(value: object, field: str) -> str:
    if not isinstance(value, dict):
        _shape_error(field, "必须是对象")
    allowed = {
        "id",
        "content",
        "thinking",
        "presentation",
        "context_diagnostics",
        "generation_telemetry",
        "roleplay_warnings",
        "status",
        "error",
        "timestamps",
        "turn_id",
        "turn_kind",
        "state_snapshot",
    }
    if set(value) - allowed:
        _shape_error(field, "包含未知字段")
    variant_id = _valid_uuid(value.get("id"))
    if variant_id is None:
        _shape_error(f"{field}.id", "必须是 UUID")
    if not isinstance(value.get("content"), str):
        _shape_error(f"{field}.content", "必须是字符串")
    if "thinking" in value and not isinstance(value["thinking"], str):
        _shape_error(f"{field}.thinking", "必须是字符串")
    for object_field in (
        "presentation",
        "context_diagnostics",
        "generation_telemetry",
        "timestamps",
    ):
        object_value = value.get(object_field)
        if object_field in value and not (
            isinstance(object_value, dict)
            or (object_field == "presentation" and object_value is None)
        ):
            _shape_error(f"{field}.{object_field}", "必须是对象")
    if "roleplay_warnings" in value and not isinstance(
        value["roleplay_warnings"], list
    ):
        _shape_error(f"{field}.roleplay_warnings", "必须是数组")
    if "error" in value and not (
        value["error"] is None or isinstance(value["error"], dict)
    ):
        _shape_error(f"{field}.error", "必须是对象或 null")
    for text_field in ("status", "turn_id", "turn_kind"):
        if text_field in value and not isinstance(value[text_field], str):
            _shape_error(f"{field}.{text_field}", "必须是字符串")
    _validate_reply_state_snapshot(
        value.get("state_snapshot"),
        f"{field}.state_snapshot",
    )
    return variant_id


def _validate_session_shape(session: dict, project: str, save_id: str) -> None:
    """严格检查磁盘数据；缺失字段由 ``normalize_session`` 兼容补齐。

    这里仅拒绝会在规范化或后续消费时被静默清空、截断、覆盖或隐藏的
    已有数据。校验只读取内存中的 JSON 对象，不执行任何磁盘写入。
    """
    for field, expected in (("project", project), ("session_id", save_id)):
        if field not in session:
            continue
        value = session[field]
        if not isinstance(value, str):
            _shape_error(field, "必须是字符串")
        if value != expected:
            _shape_error(field, "与存档路径不一致")

    if "revision" in session:
        revision = session["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            _shape_error("revision", "必须是非负整数")

    container_types = {
        "scene_meta": dict,
        "characters_state": dict,
        "summaries": list,
        "message_history": list,
        "manual_worldbook_ids": list,
        "roleplay_policy": dict,
        "relationship_edges": list,
    }
    for field, expected_type in container_types.items():
        if field in session and not isinstance(session[field], expected_type):
            label = "对象" if expected_type is dict else "数组"
            _shape_error(field, f"必须是{label}")

    if "current_provider" in session:
        provider = session["current_provider"]
        if not isinstance(provider, str) or not provider.strip():
            _shape_error("current_provider", "必须是非空字符串")
    if "current_model" in session and not isinstance(session["current_model"], str):
        _shape_error("current_model", "必须是字符串")

    manual_ids = session.get("manual_worldbook_ids")
    if isinstance(manual_ids, list):
        if len(manual_ids) > MAX_MANUAL_WORLDBOOK_IDS:
            _shape_error(
                "manual_worldbook_ids",
                f"最多包含 {MAX_MANUAL_WORLDBOOK_IDS} 项",
            )
        seen_manual_ids: set[str] = set()
        for index, entry_id in enumerate(manual_ids):
            try:
                normalized_id = validate_file_id(
                    entry_id,
                    label=f"manual_worldbook_ids[{index}]",
                )
            except (TypeError, ValueError) as exc:
                _shape_error(f"manual_worldbook_ids[{index}]", f"无效：{exc}")
            if normalized_id in seen_manual_ids:
                _shape_error("manual_worldbook_ids", "不能包含重复 ID")
            seen_manual_ids.add(normalized_id)

    policy = session.get("roleplay_policy")
    if isinstance(policy, dict) and "strict_muted_writeback" in policy:
        if not isinstance(policy["strict_muted_writeback"], bool):
            _shape_error("roleplay_policy.strict_muted_writeback", "必须是布尔值")

    states = session.get("characters_state")
    if isinstance(states, dict):
        for character_id, state in states.items():
            field = f"characters_state.{character_id}"
            if not isinstance(state, dict):
                _shape_error(field, "必须是对象")
            if "remaining_silent_turns" in state:
                turns = state["remaining_silent_turns"]
                if (
                    isinstance(turns, bool)
                    or not isinstance(turns, int)
                    or not 0 <= turns <= 999
                ):
                    _shape_error(
                        f"{field}.remaining_silent_turns",
                        "必须是 0 到 999 的整数",
                    )

    edges = session.get("relationship_edges")
    if isinstance(edges, list):
        for index, edge in enumerate(edges):
            if not isinstance(edge, dict):
                _shape_error(f"relationship_edges[{index}]", "必须是对象")

    summaries = session.get("summaries")
    if isinstance(summaries, list):
        seen_summary_ids: set[str] = set()
        for index, summary in enumerate(summaries):
            field = f"summaries[{index}]"
            if not isinstance(summary, dict):
                _shape_error(field, "必须是对象")
            if "id" in summary:
                summary_id = _valid_uuid(summary["id"])
                if summary_id is None:
                    _shape_error(f"{field}.id", "必须是 UUID")
                if summary_id in seen_summary_ids:
                    _shape_error("summaries.id", "不能重复")
            else:
                salt = 0
                summary_id = _legacy_summary_uuid(
                    project,
                    save_id,
                    index,
                    summary,
                    salt=salt,
                )
                while summary_id in seen_summary_ids:
                    salt += 1
                    summary_id = _legacy_summary_uuid(
                        project,
                        save_id,
                        index,
                        summary,
                        salt=salt,
                    )
            seen_summary_ids.add(summary_id)
            if "source_snapshot_id" in summary:
                source_id = summary["source_snapshot_id"]
                if source_id is not None and (
                    not isinstance(source_id, str) or not source_id
                ):
                    _shape_error(
                        f"{field}.source_snapshot_id",
                        "必须是非空字符串或 null",
                    )
            if "status" in summary and summary["status"] not in {
                "pending",
                "completed",
                "failed",
            }:
                _shape_error(f"{field}.status", "取值无效")
            for text_field in ("text", "time"):
                if text_field in summary and not isinstance(summary[text_field], str):
                    _shape_error(f"{field}.{text_field}", "必须是字符串")
            for list_field in ("facts", "relations"):
                if list_field in summary and not isinstance(summary[list_field], list):
                    _shape_error(f"{field}.{list_field}", "必须是数组")
                if list_field in summary:
                    for item_index, item in enumerate(summary[list_field]):
                        if not isinstance(item, str):
                            _shape_error(
                                f"{field}.{list_field}[{item_index}]",
                                "必须是字符串",
                            )
            if "error" in summary and not (
                summary["error"] is None or isinstance(summary["error"], str)
            ):
                _shape_error(f"{field}.error", "必须是字符串或 null")
            if "content_status" in summary and summary["content_status"] not in {
                "valid",
                "empty",
            }:
                _shape_error(f"{field}.content_status", "取值无效")
            if "generation_attempt" in summary:
                attempt = summary["generation_attempt"]
                if (
                    isinstance(attempt, bool)
                    or not isinstance(attempt, int)
                    or attempt < 0
                ):
                    _shape_error(
                        f"{field}.generation_attempt",
                        "必须是非负整数",
                    )
            if "kind" in summary and (
                not isinstance(summary["kind"], str) or not summary["kind"]
            ):
                _shape_error(f"{field}.kind", "必须是非空字符串")
            if summary.get("kind") == "memory_note":
                character_id = summary.get("character_id")
                if character_id is not None and (
                    not isinstance(character_id, str)
                    or character_id not in session.get("characters_state", {})
                ):
                    _shape_error(
                        f"{field}.character_id",
                        "必须为空或引用当前存档角色",
                    )

    history = session.get("message_history")
    if isinstance(history, list):
        seen_message_ids: set[str] = set()
        for index, message in enumerate(history):
            field = f"message_history[{index}]"
            if not isinstance(message, dict):
                _shape_error(field, "必须是对象")
            if message.get("role") not in {"user", "assistant"}:
                _shape_error(f"{field}.role", "必须是 user 或 assistant")
            if not isinstance(message.get("content"), str):
                _shape_error(f"{field}.content", "必须是字符串")
            if "thinking" in message and not isinstance(message["thinking"], str):
                _shape_error(f"{field}.thinking", "必须是字符串")
            for boolean_field in ("pinned", "in_prompt"):
                if boolean_field in message and not isinstance(
                    message[boolean_field], bool
                ):
                    _shape_error(f"{field}.{boolean_field}", "必须是布尔值")
            if "presentation" in message and not (
                message["presentation"] is None
                or isinstance(message["presentation"], dict)
            ):
                _shape_error(f"{field}.presentation", "必须是对象或 null")
            for object_field in (
                "context_diagnostics",
                "generation_telemetry",
            ):
                if object_field in message and not isinstance(
                    message[object_field], dict
                ):
                    _shape_error(f"{field}.{object_field}", "必须是对象")
            for temporary_field in (
                "regeneration_source_snapshot",
                "regeneration_original_assistant",
            ):
                if temporary_field in message and not isinstance(
                    message[temporary_field], dict
                ):
                    _shape_error(f"{field}.{temporary_field}", "必须是对象")
            if "regeneration_original_state" in message:
                _validate_reply_state_snapshot(
                    message["regeneration_original_state"],
                    f"{field}.regeneration_original_state",
                )
            active_variant_id = None
            if "reply_variant_id" in message:
                active_variant_id = _valid_uuid(message["reply_variant_id"])
                if active_variant_id is None:
                    _shape_error(f"{field}.reply_variant_id", "必须是 UUID")
            if "reply_variant_state" in message:
                _validate_reply_state_snapshot(
                    message["reply_variant_state"],
                    f"{field}.reply_variant_state",
                )
            if "reply_alternatives" in message:
                alternatives = message["reply_alternatives"]
                if message.get("role") != "assistant":
                    _shape_error(f"{field}.reply_alternatives", "只允许 assistant")
                if not isinstance(alternatives, list):
                    _shape_error(f"{field}.reply_alternatives", "必须是数组")
                if len(alternatives) > MAX_REPLY_ALTERNATIVES:
                    _shape_error(f"{field}.reply_alternatives", "数量超过上限")
                if active_variant_id is None or "reply_variant_state" not in message:
                    _shape_error(field, "备选回复缺少当前变体状态")
                seen_variant_ids = {active_variant_id}
                for alternative_index, alternative in enumerate(alternatives):
                    alternative_id = _validate_reply_variant(
                        alternative,
                        f"{field}.reply_alternatives[{alternative_index}]",
                    )
                    if alternative_id in seen_variant_ids:
                        _shape_error(
                            f"{field}.reply_alternatives.id",
                            "不能重复",
                        )
                    seen_variant_ids.add(alternative_id)
            if "id" in message:
                message_id = _valid_uuid(message["id"])
                if message_id is None:
                    _shape_error(f"{field}.id", "必须是 UUID")
                if message_id in seen_message_ids:
                    _shape_error("message_history.id", "不能重复")
                seen_message_ids.add(message_id)
            else:
                legacy_id = _legacy_message_uuid(
                    project,
                    save_id,
                    index,
                    message,
                )
                if legacy_id not in seen_message_ids:
                    seen_message_ids.add(legacy_id)


def normalize_session(session: dict, project: str, save_id: str) -> dict:
    """只改内存副本；为旧存档补 revision、默认字段和稳定消息 UUID。"""
    session["project"] = project
    session["session_id"] = save_id
    session["revision"] = _coerce_revision(session.get("revision", 0))
    raw_scene_meta = session.get("scene_meta")
    scene_meta = dict(raw_scene_meta) if isinstance(raw_scene_meta, dict) else {}
    for field in (
        "location",
        "time",
        "weather",
        "main_quest",
        "current_scene",
        "next_goal",
    ):
        scene_meta.setdefault(field, "")
    session["scene_meta"] = scene_meta
    current_provider = session.get("current_provider", "ollama")
    if not isinstance(current_provider, str) or not current_provider.strip():
        current_provider = "ollama"
    session["current_provider"] = current_provider.strip().casefold()
    if not isinstance(session.get("current_model"), str):
        session["current_model"] = ""
    manual_worldbook_ids = session.get("manual_worldbook_ids", [])
    normalized_manual_ids: set[str] = set()
    if isinstance(manual_worldbook_ids, list):
        for entry_id in manual_worldbook_ids:
            try:
                normalized_manual_ids.add(
                    validate_file_id(entry_id, label="手动世界书条目 ID")
                )
            except (TypeError, ValueError):
                continue
    session["manual_worldbook_ids"] = sorted(normalized_manual_ids)[
        :MAX_MANUAL_WORLDBOOK_IDS
    ]
    raw_policy = session.get("roleplay_policy")
    roleplay_policy = dict(raw_policy) if isinstance(raw_policy, dict) else {}
    roleplay_policy["strict_muted_writeback"] = (
        roleplay_policy.get("strict_muted_writeback") is True
    )
    session["roleplay_policy"] = roleplay_policy
    if "relationship_edges" not in session:
        session["relationship_edges"] = []
    raw_character_states = session.get("characters_state")
    if not isinstance(raw_character_states, dict):
        raw_character_states = {}
        session["characters_state"] = raw_character_states
    for character_id, raw_state in tuple(raw_character_states.items()):
        if not isinstance(raw_state, dict):
            raw_state = {}
            raw_character_states[character_id] = raw_state
        raw_state["remaining_silent_turns"] = normalize_silent_turns(
            raw_state.get("remaining_silent_turns", 0)
        )
    summaries = session.setdefault("summaries", [])
    if not isinstance(summaries, list):
        summaries = []
        session["summaries"] = summaries
    seen_summary_ids: set[str] = set()
    for index, raw_summary in enumerate(tuple(summaries)):
        if not isinstance(raw_summary, dict):
            raw_summary = {
                "text": str(raw_summary) if raw_summary is not None else "",
            }
            summaries[index] = raw_summary
        summary_id = _valid_uuid(raw_summary.get("id"))
        if summary_id is None or summary_id in seen_summary_ids:
            salt = 0
            summary_id = _legacy_summary_uuid(
                project,
                save_id,
                index,
                raw_summary,
                salt=salt,
            )
            while summary_id in seen_summary_ids:
                salt += 1
                summary_id = _legacy_summary_uuid(
                    project,
                    save_id,
                    index,
                    raw_summary,
                    salt=salt,
                )
        raw_summary["id"] = summary_id
        seen_summary_ids.add(summary_id)

        source_snapshot_id = raw_summary.get("source_snapshot_id")
        if not isinstance(source_snapshot_id, str) or not source_snapshot_id:
            raw_summary["source_snapshot_id"] = None
        status = raw_summary.get("status")
        if status not in {"pending", "completed", "failed"}:
            status = (
                "failed"
                if raw_summary.get("failed") or raw_summary.get("error")
                else "completed"
            )
            raw_summary["status"] = status
        raw_summary.setdefault("text", "")
        raw_summary.setdefault("time", "")
        raw_summary.setdefault("facts", [])
        raw_summary.setdefault("relations", [])
        raw_summary.setdefault("error", None)
        if raw_summary.get("content_status") not in {"valid", "empty"}:
            raw_summary["content_status"] = (
                "valid"
                if isinstance(raw_summary.get("text"), str) and raw_summary.get("text")
                else "empty"
            )
        attempt = raw_summary.get("generation_attempt", 0)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
            raw_summary["generation_attempt"] = 0
    session.setdefault("summary_error", "")
    history = session.setdefault("message_history", [])
    seen: set[str] = set()
    for index, message in enumerate(history):
        if not isinstance(message, dict):
            continue
        message.setdefault("pinned", False)
        message.setdefault("in_prompt", True)
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
        source_revision: int,
    ):
        self.store = store
        self.project = project
        self.save_id = save_id
        self.existed = existed
        self.source_revision = source_revision
        self.snapshot_paths: list[Path] = []
        self.recovery_manifests: list[dict] = []

    def snapshot(self, kind: str, payload: dict) -> Path:
        path = self.store._write_snapshot_sync(
            self.project,
            self.save_id,
            kind,
            payload,
        )
        self.snapshot_paths.append(path)
        return path

    def checkpoint(
        self,
        operation: str,
        *,
        include_history: bool = False,
        metadata: dict | None = None,
    ) -> dict:
        if not self.existed:
            raise FileNotFoundError(f"存档 {self.save_id} 不存在")
        manifest = self.store._create_session_recovery_sync(
            category="checkpoint",
            operation=operation,
            project=self.project,
            save_id=self.save_id,
            source_revision=self.source_revision,
            include_history=include_history,
            metadata=metadata,
        )
        self.recovery_manifests.append(manifest)
        return manifest


class SessionStore:
    """单进程、按 project/save 隔离的异步事务入口。"""

    def __init__(
        self,
        projects_root: Path,
        recovery_store: RecoveryStore | None = None,
    ):
        self.projects_root = Path(projects_root).resolve(strict=False)
        self.recovery_store = recovery_store or RecoveryStore(
            self.projects_root.parent / ".recovery",
            projects_root=self.projects_root,
        )
        self._save_locks: dict[str, asyncio.Lock] = {}
        self._project_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    def saves_dir(self, project: str) -> Path:
        return resolve_saves_dir(self.projects_root, project)

    def session_path(self, project: str, save_id: str) -> Path:
        return resolve_session_path(self.projects_root, project, save_id)

    def history_dir(self, project: str) -> Path:
        return resolve_under(self.saves_dir(project), ".history")

    def _require_project_sync(self, project: str) -> Path:
        project_dir = resolve_project_dir(self.projects_root, project)
        if project_dir.is_dir():
            return project_dir
        deleted = any(
            manifest.get("entity_id") == project
            and manifest.get("status") in {"complete", "restoring"}
            for manifest in self.recovery_store.list_entries(
                category="trash",
                project=project,
                entity_type="project",
            )
        )
        if deleted:
            raise FileNotFoundError(f"项目 {project} 已移入回收区")
        raise FileNotFoundError(f"项目 {project} 不存在")

    def _history_paths_sync(self, project: str, save_id: str) -> list[Path]:
        history_dir = self.history_dir(project)
        if not history_dir.is_dir():
            return []
        return sorted(
            path
            for path in history_dir.iterdir()
            if path.is_file() and path.name.startswith(f"{save_id}.")
        )

    def _create_session_recovery_sync(
        self,
        *,
        category: str,
        operation: str,
        project: str,
        save_id: str,
        source_revision: int | None,
        include_history: bool,
        history_paths: list[Path] | None = None,
        metadata: dict | None = None,
    ) -> dict:
        path = self.session_path(project, save_id)
        selected_history_paths = (
            list(history_paths)
            if history_paths is not None
            else (self._history_paths_sync(project, save_id) if include_history else [])
        )
        return self.recovery_store.create_session_entry(
            category=category,
            operation=operation,
            project=project,
            save_id=save_id,
            session_path=path,
            source_revision=source_revision,
            history_paths=selected_history_paths,
            metadata=metadata,
        )

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
        try:
            payload = path.read_bytes()
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DataCorruptionError.from_bytes(
                path,
                payload,
                entity_type="session",
                project=project,
                entity_id=save_id,
                reason=f"JSON 解析失败: {exc}",
            ) from exc
        if not isinstance(data, dict):
            raise DataCorruptionError.from_bytes(
                path,
                payload,
                entity_type="session",
                project=project,
                entity_id=save_id,
                reason="JSON 顶层必须是对象",
            )
        try:
            _validate_session_shape(data, project, save_id)
        except _SessionShapeError as exc:
            raise DataCorruptionError.from_bytes(
                path,
                payload,
                entity_type="session",
                project=project,
                entity_id=save_id,
                reason=f"Session 结构无效：{exc}",
            ) from exc
        normalized = normalize_session(data, project, save_id)
        self._refresh_summary_source_status_sync(normalized, project, save_id)
        return normalized

    def _refresh_summary_source_status_sync(
        self,
        session: dict,
        project: str,
        save_id: str,
    ) -> None:
        """只在内存副本标注原文映射；纯读取不回写 Session。"""
        history_dir = self.history_dir(project)
        for summary in session.get("summaries", []):
            source_id = summary.get("source_snapshot_id")
            if not isinstance(source_id, str) or not source_id:
                summary["source_status"] = "unlinked"
                continue
            try:
                filename, kind = validate_snapshot_filename(save_id, source_id)
            except ValueError:
                summary["source_status"] = "invalid"
                continue
            if kind != "trim":
                summary["source_status"] = "invalid"
                continue
            summary["source_status"] = (
                "available" if (history_dir / filename).is_file() else "missing"
            )

    def read_sync(self, project: str, save_id: str) -> dict | None:
        return self._read_sync(project, save_id)

    async def read(self, project: str, save_id: str) -> dict | None:
        """纯读取；与同存档写事务串行，避免读到 Windows 替换窗口。"""
        lock = await self.save_lock(project, save_id)
        async with lock:
            return await asyncio.to_thread(self._read_sync, project, save_id)

    def list_sessions_sync(self, project: str) -> list[dict]:
        """纯读取列表；坏档作为可识别条目返回，禁止静默隐藏。"""
        saves_dir = self.saves_dir(project)
        if not saves_dir.is_dir():
            return []
        sessions: list[dict] = []
        for path in saves_dir.glob("*.json"):
            if path.stem.startswith("."):
                continue
            try:
                data = self._read_sync(project, path.stem)
                if data is None:
                    continue
                sessions.append(
                    {
                        "session_id": data.get("session_id", path.stem),
                        "name": data.get("name", path.stem),
                        "revision": data.get("revision", 0),
                        "updated_at": data.get("updated_at", ""),
                        "created_at": data.get("created_at", ""),
                        "message_count": len(data.get("message_history", [])),
                        "current_provider": data.get("current_provider", "ollama"),
                        "current_model": data.get("current_model", ""),
                        "status": "ready",
                    }
                )
            except DataCorruptionError as exc:
                sessions.append(
                    {
                        "session_id": path.stem,
                        "name": path.stem,
                        "revision": None,
                        "updated_at": "",
                        "created_at": "",
                        "message_count": None,
                        "current_provider": "ollama",
                        "current_model": "",
                        "status": "corrupt",
                        "error_code": exc.code,
                        "fingerprint": exc.fingerprint,
                        "quarantine_available": True,
                    }
                )
        sessions.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return sessions

    async def list_sessions(self, project: str) -> list[dict]:
        return await asyncio.to_thread(self.list_sessions_sync, project)

    def _write_session_sync(self, session: dict, project: str, save_id: str) -> None:
        path = self.session_path(project, save_id)
        atomic_write(path, self._serialize_session(session))

    @staticmethod
    def _serialize_session(session: dict) -> str:
        payload = deepcopy(session)
        for summary in payload.get("summaries", []):
            if not isinstance(summary, dict):
                continue
            summary.pop("source_status", None)
            summary.pop("generation_active", None)
        return json.dumps(payload, ensure_ascii=False, indent=2)

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
            raise RevisionConflict(
                expected_revision, current_revision, deepcopy(current)
            )

    async def create(self, project: str, save_id: str, session: dict) -> dict:
        project_lock = await self.project_lock(project)
        async with project_lock:
            save_lock = await self.save_lock(project, save_id)
            async with save_lock:
                assert_write_allowed(project, save_id)

                def create_sync() -> dict:
                    self._require_project_sync(project)
                    if self.session_path(project, save_id).exists():
                        raise FileExistsError(f"存档 ID {save_id} 已存在")
                    created = normalize_session(deepcopy(session), project, save_id)
                    created["revision"] = 1
                    created["updated_at"] = datetime.now().isoformat()
                    self._write_session_sync(created, project, save_id)
                    return created

                return await run_sync_critical(
                    _run_with_library_shared_write,
                    create_sync,
                )

    async def accept_chat_turn(
        self,
        project: str,
        save_id: str,
        expected_revision: int,
        *,
        turn_id: str,
        user_input: str,
        created_at: str,
        initial_session: dict,
    ) -> MutationResult[str]:
        """在 save lock 内同时校验 revision、登记 lease 并写入 pending user。"""
        from core import active_turns

        project_lock = await self.project_lock(project)
        async with project_lock:
            lock = await self.save_lock(project, save_id)
            async with lock:
                assert_write_allowed(project, save_id)

                def accept_sync() -> MutationResult[str]:
                    self._require_project_sync(project)
                    current = self._read_sync(project, save_id)
                    if current is None:
                        current = normalize_session(
                            deepcopy(initial_session),
                            project,
                            save_id,
                        )
                    self._check_revision(expected_revision, current)
                    active_turns.register(project, save_id, turn_id)
                    try:
                        # 项目创建流程会预先落下 revision=0 的空默认存档。
                        # _prepare_turn 会基于角色卡和用户卡初始化同 revision 的
                        # initial_session；若这里仍一律采用磁盘空壳，accepted 回调
                        # 就会把已初始化角色状态整体覆盖掉。只允许在磁盘会话尚无
                        # 角色且尚无消息时采用这份同 revision、无历史的初始化基线，
                        # 并继续在当前 save lock 内原子登记 pending user。
                        initial = normalize_session(
                            deepcopy(initial_session),
                            project,
                            save_id,
                        )
                        use_initialized_baseline = (
                            not current.get("characters_state")
                            and not current.get("message_history")
                            and not initial.get("message_history")
                            and initial.get("revision") == current.get("revision")
                        )
                        working = deepcopy(
                            initial if use_initialized_baseline else current
                        )
                        message_id = str(uuid4())
                        working.setdefault("message_history", []).append(
                            {
                                "id": message_id,
                                "role": "user",
                                "content": user_input,
                                "turn_id": turn_id,
                                "status": "pending",
                                "error": None,
                                "timestamps": {
                                    "created_at": created_at,
                                    "completed_at": None,
                                },
                                "pinned": False,
                                "in_prompt": True,
                            }
                        )
                        working = normalize_session(working, project, save_id)
                        working["revision"] = current["revision"] + 1
                        working["updated_at"] = datetime.now().isoformat()
                        self._write_session_sync(working, project, save_id)
                    except BaseException:
                        active_turns.unregister(project, save_id, turn_id)
                        raise
                    return MutationResult(
                        session=working,
                        value=message_id,
                    )

                return await run_sync_critical(
                    _run_with_library_shared_write,
                    accept_sync,
                )

    async def accept_regenerated_chat_turn(
        self,
        project: str,
        save_id: str,
        expected_revision: int,
        *,
        turn_id: str,
        target_message_id: str,
        expected_user_input: str,
        created_at: str,
    ) -> MutationResult[str]:
        """原子接受重生成：一次快照、语义截断、lease 与 pending user。"""
        from core import active_turns

        project_lock = await self.project_lock(project)
        async with project_lock:
            lock = await self.save_lock(project, save_id)
            async with lock:
                assert_write_allowed(project, save_id)

                def accept_sync() -> MutationResult[str]:
                    self._require_project_sync(project)
                    current = self._read_sync(project, save_id)
                    if current is None:
                        raise FileNotFoundError(f"存档 {save_id} 不存在")
                    self._check_revision(expected_revision, current)
                    plan = plan_message_regeneration(current, target_message_id)
                    if plan.user_input != expected_user_input:
                        raise RuntimeError("重生成源消息在接受前发生变化")

                    working = deepcopy(current)
                    working["message_history"] = plan.pending_history(
                        turn_id=turn_id,
                        created_at=created_at,
                    )
                    reconcile_relationship_evidence(working)
                    working = normalize_session(working, project, save_id)
                    working["revision"] = current["revision"] + 1
                    working["updated_at"] = datetime.now().isoformat()

                    snapshot_path: Path | None = None
                    history_dir = self.history_dir(project)
                    history_dir_existed = history_dir.exists()
                    active_turns.register(project, save_id, turn_id)
                    try:
                        snapshot_path = self._write_snapshot_sync(
                            project,
                            save_id,
                            "snapshot",
                            current,
                        )
                        self._write_session_sync(working, project, save_id)
                    except BaseException:
                        active_turns.unregister(project, save_id, turn_id)
                        if snapshot_path is not None:
                            try:
                                snapshot_path.unlink(missing_ok=True)
                            except OSError:
                                pass
                        if not history_dir_existed:
                            try:
                                history_dir.rmdir()
                            except OSError:
                                pass
                        raise
                    return MutationResult(
                        session=working,
                        value=plan.source_message_id,
                    )

                return await run_sync_critical(
                    _run_with_library_shared_write,
                    accept_sync,
                )

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
            assert_write_allowed(project, save_id)

            def mutate_sync() -> MutationResult[T]:
                self._require_project_sync(project)
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
                    source_revision=current["revision"],
                )
                value = command(working, context)
                working = normalize_session(working, project, save_id)
                working["revision"] = current["revision"] + 1
                working["updated_at"] = datetime.now().isoformat()
                self._write_session_sync(working, project, save_id)
                return MutationResult(
                    session=working,
                    value=value,
                    recovery_ids=tuple(
                        manifest["recovery_id"]
                        for manifest in context.recovery_manifests
                    ),
                )

            return await run_sync_critical(
                _run_with_library_shared_write,
                mutate_sync,
            )

    async def snapshot(
        self,
        project: str,
        save_id: str,
        expected_revision: int,
        kind: str = "snapshot",
    ) -> tuple[dict, Path]:
        lock = await self.save_lock(project, save_id)
        async with lock:
            assert_write_allowed(project, save_id)

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

            return await run_sync_critical(
                _run_with_library_shared_write,
                snapshot_sync,
            )

    async def delete(
        self,
        project: str,
        save_id: str,
        expected_revision: int,
        *,
        minimum_remaining: int = 1,
    ) -> SessionRecoveryResult | None:
        project_lock = await self.project_lock(project)
        async with project_lock:
            save_lock = await self.save_lock(project, save_id)
            async with save_lock:
                assert_write_allowed(project, save_id)

                def delete_sync() -> SessionRecoveryResult | None:
                    current = self._read_sync(project, save_id)
                    if current is None:
                        return None
                    self._check_revision(expected_revision, current)
                    saves_dir = self.saves_dir(project)
                    count = (
                        len(list(saves_dir.glob("*.json"))) if saves_dir.exists() else 0
                    )
                    if count <= minimum_remaining:
                        raise ValueError("至少保留 1 个存档")
                    history_paths = self._history_paths_sync(project, save_id)
                    manifest = self._create_session_recovery_sync(
                        category="trash",
                        operation="delete",
                        project=project,
                        save_id=save_id,
                        source_revision=current["revision"],
                        include_history=True,
                        history_paths=history_paths,
                    )
                    try:
                        for history_path in history_paths:
                            history_path.unlink()
                        self.session_path(project, save_id).unlink()
                    except OSError:
                        verified = self.recovery_store.get_verified(
                            manifest["recovery_id"]
                        )
                        self.recovery_store.restore_non_primary_items(
                            verified,
                            primary_relpath=manifest["source_relpath"],
                        )
                        raise
                    return SessionRecoveryResult(
                        current,
                        manifest["recovery_id"],
                    )

                return await run_sync_critical(
                    _run_with_library_shared_write,
                    delete_sync,
                )

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
                assert_write_allowed(project, old_id)
                assert_write_allowed(project, new_id)

                def rename_sync() -> dict:
                    current = self._read_sync(project, old_id)
                    if current is None:
                        raise FileNotFoundError(f"存档 {old_id} 不存在")
                    self._check_revision(expected_revision, current)
                    if new_id != old_id and self.session_path(project, new_id).exists():
                        raise FileExistsError(f"存档 {new_id} 已存在")

                    history_paths = self._history_paths_sync(project, old_id)
                    if new_id != old_id:
                        history_dir = self.history_dir(project)
                        for source in history_paths:
                            target = resolve_under(
                                history_dir,
                                new_id + source.name[len(old_id) :],
                            )
                            if target.exists():
                                raise FileExistsError(
                                    f"目标存档历史已存在: {target.name}"
                                )
                    recovery = self._create_session_recovery_sync(
                        category="checkpoint",
                        operation="rename",
                        project=project,
                        save_id=old_id,
                        source_revision=current["revision"],
                        include_history=True,
                        history_paths=history_paths,
                        metadata={"renamed_to": new_id, "new_name": new_name},
                    )

                    renamed = deepcopy(current)
                    renamed["name"] = new_name
                    renamed["session_id"] = new_id
                    renamed["revision"] = current["revision"] + 1
                    renamed["updated_at"] = datetime.now().isoformat()
                    if new_id == old_id:
                        self._write_session_sync(renamed, project, old_id)
                        return SessionRecoveryResult(
                            renamed,
                            recovery["recovery_id"],
                        )

                    self._write_session_sync(renamed, project, new_id)
                    moved: list[tuple[Path, Path]] = []
                    history_dir = self.history_dir(project)
                    try:
                        if history_dir.exists():
                            for source in history_paths:
                                target = resolve_under(
                                    history_dir,
                                    new_id + source.name[len(old_id) :],
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
                    return SessionRecoveryResult(
                        renamed,
                        recovery["recovery_id"],
                    )

                return await run_sync_critical(
                    _run_with_library_shared_write,
                    rename_sync,
                )

    async def list_recoveries(
        self,
        *,
        category: str | None = None,
        project: str | None = None,
        entity_type: str | None = None,
    ) -> list[dict]:
        return await asyncio.to_thread(
            self.recovery_store.list_entries,
            category=category,
            project=project,
            entity_type=entity_type,
        )

    async def quarantine_corrupt(
        self,
        project: str,
        save_id: str,
        fingerprint: str,
    ) -> tuple[dict, bool]:
        lock = await self.save_lock(project, save_id)
        async with lock:
            assert_write_allowed(project, save_id)
            return await run_sync_critical(
                _run_with_library_shared_write,
                self.recovery_store.quarantine_session,
                project=project,
                save_id=save_id,
                session_path=self.session_path(project, save_id),
                fingerprint=fingerprint,
            )

    async def restore_recovery(
        self,
        recovery_id: str,
        *,
        expected_revision: int | None = None,
        overwrite: bool = False,
    ) -> dict:
        if overwrite:
            raise ValueError("恢复禁止覆盖；请先处理目标冲突")
        inspected = await asyncio.to_thread(
            self.recovery_store.get_verified,
            recovery_id,
        )
        manifest = inspected.manifest
        if manifest.get("entity_type") != "session":
            raise ValueError("当前仅支持恢复 session")
        if manifest.get("category") == "quarantine":
            raise ValueError("quarantine 仅保留损坏原件，不能直接恢复")
        project = validate_file_id(manifest.get("project"), label="项目 ID")
        save_id = validate_file_id(manifest.get("entity_id"), label="存档 ID")
        metadata = manifest.get("metadata", {})
        active_override = metadata.get("restore_active_id")
        if active_override is None and manifest.get("operation") == "rename":
            active_override = metadata.get("renamed_to")
        active_save_id = (
            validate_file_id(active_override, label="撤销目标存档 ID")
            if active_override is not None
            else save_id
        )
        replaces_other_save = active_save_id != save_id

        project_lock = await self.project_lock(project)
        async with project_lock:
            async with self._save_lock_group(
                project,
                [save_id, active_save_id],
            ):
                assert_write_allowed(project, save_id)
                assert_write_allowed(project, active_save_id)

                def restore_sync() -> dict:
                    verified = self.recovery_store.get_verified(recovery_id)
                    current_manifest = verified.manifest
                    manifest_before_restore = deepcopy(current_manifest)
                    if (
                        current_manifest.get("project") != project
                        or current_manifest.get("entity_id") != save_id
                        or current_manifest.get("entity_type") != "session"
                    ):
                        raise RecoveryIntegrityError("恢复项归属在执行前发生变化")
                    current_metadata = current_manifest.get("metadata", {})
                    current_active_override = current_metadata.get("restore_active_id")
                    if (
                        current_active_override is None
                        and current_manifest.get("operation") == "rename"
                    ):
                        current_active_override = current_metadata.get("renamed_to")
                    if (current_active_override or save_id) != active_save_id:
                        raise RecoveryIntegrityError("恢复项撤销目标在执行前发生变化")
                    category = current_manifest["category"]
                    primary_relpath = current_manifest["source_relpath"]
                    primary_target = self.recovery_store.target_path(primary_relpath)
                    expected_target = self.session_path(project, save_id)
                    if primary_target != expected_target:
                        raise RecoveryIntegrityError(
                            "恢复项主路径与 session 归属不一致"
                        )

                    primary_item = next(
                        (
                            item
                            for item in current_manifest["items"]
                            if item["source_relpath"] == primary_relpath
                        ),
                        None,
                    )
                    if primary_item is None:
                        raise RecoveryIntegrityError("恢复项缺少主 payload")
                    payload_path = self.recovery_store.payload_path(
                        verified,
                        primary_item,
                    )
                    try:
                        restored = json.loads(payload_path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise RecoveryIntegrityError(
                            "恢复项主 payload 无法解析"
                        ) from exc
                    if not isinstance(restored, dict):
                        raise RecoveryIntegrityError("恢复项主 payload 顶层必须是对象")
                    restored = normalize_session(restored, project, save_id)

                    journal = current_manifest.get("restore_journal")
                    output_path = self.session_path(project, save_id)
                    active_path = self.session_path(project, active_save_id)

                    def compensate_commit_failure(
                        created_targets: list[Path],
                        undo_id: str | None,
                    ) -> None:
                        for target in reversed(created_targets):
                            target.unlink(missing_ok=True)
                        if undo_id is None:
                            output_path.unlink(missing_ok=True)
                        else:
                            undo_verified = self.recovery_store.get_verified(undo_id)
                            if replaces_other_save:
                                output_path.unlink(missing_ok=True)
                            self.recovery_store.restore_non_primary_items(
                                undo_verified,
                                primary_relpath=undo_verified.manifest[
                                    "source_relpath"
                                ],
                            )
                            self.recovery_store.restore_primary_exact(undo_verified)
                        current_verified = self.recovery_store.get_verified(recovery_id)
                        self.recovery_store.replace_manifest(
                            current_verified,
                            manifest_before_restore,
                        )

                    def response(session: dict, undo_id: str | None) -> dict:
                        return {
                            "restored": True,
                            "session": session,
                            "recovery_id": recovery_id,
                            "undo_recovery_id": undo_id,
                        }

                    def remove_active_save() -> None:
                        if not replaces_other_save:
                            return
                        for history_path in self._history_paths_sync(
                            project,
                            active_save_id,
                        ):
                            history_path.unlink()
                        active_path.unlink(missing_ok=True)

                    if isinstance(journal, dict):
                        result_sha256 = journal.get("result_sha256")
                        result_revision = journal.get("result_revision")
                        result_updated_at = journal.get("result_updated_at")
                        undo_recovery_id = journal.get("undo_recovery_id")
                        pre_restore_sha256 = journal.get("pre_restore_sha256")
                        if (
                            not isinstance(result_sha256, str)
                            or len(result_sha256) != 64
                            or any(
                                char not in "0123456789abcdef" for char in result_sha256
                            )
                            or isinstance(result_revision, bool)
                            or not isinstance(result_revision, int)
                            or not isinstance(result_updated_at, str)
                            or (
                                undo_recovery_id is not None
                                and not isinstance(undo_recovery_id, str)
                            )
                            or (
                                pre_restore_sha256 is not None
                                and (
                                    not isinstance(pre_restore_sha256, str)
                                    or len(pre_restore_sha256) != 64
                                    or any(
                                        char not in "0123456789abcdef"
                                        for char in pre_restore_sha256
                                    )
                                )
                            )
                            or journal.get("active_save_id") != active_save_id
                        ):
                            raise RecoveryIntegrityError("恢复 journal 字段无效")

                        if (
                            output_path.is_file()
                            and sha256_file(output_path) == result_sha256
                        ):
                            pending, _ = (
                                self.recovery_store.preflight_non_primary_items(
                                    verified,
                                    primary_relpath=primary_relpath,
                                )
                            )
                            created_targets = [target for _item, target in pending]
                            self.recovery_store.restore_non_primary_items(
                                verified,
                                primary_relpath=primary_relpath,
                            )
                            if replaces_other_save and active_path.is_file():
                                if (
                                    pre_restore_sha256 is None
                                    or sha256_file(active_path) != pre_restore_sha256
                                ):
                                    raise RecoveryConflict(
                                        "rename 撤销目标在恢复期间发生变化",
                                        code="rename_target_changed",
                                    )
                            remove_active_save()
                            current_restored = self._read_sync(project, save_id)
                            if current_restored is None:
                                raise RecoveryIntegrityError("已恢复主存档意外缺失")
                            if current_manifest.get("status") != "restored":
                                try:
                                    self.recovery_store.mark_restored(verified)
                                except BaseException as commit_exc:
                                    try:
                                        compensate_commit_failure(
                                            created_targets,
                                            undo_recovery_id,
                                        )
                                    except BaseException as rollback_exc:
                                        raise RecoveryIntegrityError(
                                            "恢复提交失败且补偿未完成"
                                        ) from rollback_exc
                                    raise commit_exc
                            return response(current_restored, undo_recovery_id)

                        if current_manifest.get("status") == "restored":
                            raise RecoveryConflict(
                                "恢复项已经应用，目标随后发生变化",
                                code="recovery_already_applied",
                            )

                        if replaces_other_save:
                            if output_path.exists():
                                raise RecoveryConflict(
                                    "rename 原存档 ID 已被占用",
                                    code="target_exists",
                                )
                            if active_path.is_file() and (
                                pre_restore_sha256 is None
                                or sha256_file(active_path) != pre_restore_sha256
                            ):
                                raise RecoveryConflict(
                                    "rename 撤销目标在恢复期间发生变化",
                                    code="rename_target_changed",
                                )
                        else:
                            if output_path.is_file():
                                if (
                                    pre_restore_sha256 is None
                                    or sha256_file(output_path) != pre_restore_sha256
                                ):
                                    raise RecoveryConflict(
                                        "恢复目标在重试前发生变化",
                                        code="target_changed",
                                    )
                            elif category != "trash":
                                raise RecoveryConflict(
                                    "checkpoint 恢复目标意外缺失",
                                    code="target_missing",
                                )
                        restored["revision"] = result_revision
                        restored["updated_at"] = result_updated_at
                    else:
                        if current_manifest.get("status") != "complete":
                            raise RecoveryIntegrityError("恢复状态缺少可重试 journal")
                        output_current = self._read_sync(project, save_id)
                        active_current = self._read_sync(project, active_save_id)
                        if replaces_other_save:
                            if output_current is not None:
                                raise RecoveryConflict(
                                    "rename 原存档 ID 已被占用",
                                    code="target_exists",
                                )
                            if active_current is None:
                                raise RecoveryConflict(
                                    "rename 撤销目标不存在",
                                    code="rename_target_missing",
                                )
                            if expected_revision is None:
                                raise ValueError(
                                    "恢复 rename checkpoint 必须提供 expected_revision"
                                )
                            self._check_revision(expected_revision, active_current)
                            current = active_current
                        else:
                            current = output_current
                            if category == "trash":
                                if current is not None:
                                    raise RecoveryConflict(
                                        "恢复目标存档已存在，拒绝覆盖",
                                        code="target_exists",
                                    )
                                if expected_revision not in (None, 0):
                                    raise ValueError(
                                        "恢复已删除存档时 expected_revision 只能省略或为 0"
                                    )
                            elif category == "checkpoint":
                                if current is not None:
                                    if expected_revision is None:
                                        raise ValueError(
                                            "恢复 checkpoint 必须提供 expected_revision"
                                        )
                                    self._check_revision(expected_revision, current)
                                elif expected_revision not in (None, 0):
                                    raise ValueError(
                                        "恢复缺失存档时 expected_revision 只能省略或为 0"
                                    )
                            else:
                                raise ValueError(f"恢复分类不可恢复: {category}")

                        self.recovery_store.preflight_non_primary_items(
                            verified,
                            primary_relpath=primary_relpath,
                        )
                        undo_recovery_id: str | None = None
                        if current is not None and category == "checkpoint":
                            undo_metadata = {"restored_from": recovery_id}
                            if replaces_other_save:
                                undo_metadata["restore_active_id"] = save_id
                            undo = self._create_session_recovery_sync(
                                category="checkpoint",
                                operation="restore_undo",
                                project=project,
                                save_id=active_save_id,
                                source_revision=current["revision"],
                                include_history=(
                                    len(current_manifest["items"]) > 1
                                    or replaces_other_save
                                ),
                                metadata=undo_metadata,
                            )
                            undo_recovery_id = undo["recovery_id"]

                        base_revision = (
                            current["revision"]
                            if current is not None
                            else _coerce_revision(
                                current_manifest.get("source_revision", 0)
                            )
                        )
                        result_updated_at = datetime.now().isoformat()
                        restored["revision"] = base_revision + 1
                        restored["updated_at"] = result_updated_at
                        result_text = self._serialize_session(restored)
                        result_sha256 = hashlib.sha256(
                            result_text.encode("utf-8")
                        ).hexdigest()
                        pre_restore_sha256 = (
                            sha256_file(active_path) if current is not None else None
                        )
                        verified = self.recovery_store.begin_restore(
                            verified,
                            {
                                "result_sha256": result_sha256,
                                "result_revision": restored["revision"],
                                "result_updated_at": result_updated_at,
                                "undo_recovery_id": undo_recovery_id,
                                "pre_restore_sha256": pre_restore_sha256,
                                "active_save_id": active_save_id,
                            },
                        )

                    remove_active_save()
                    pending, _ = self.recovery_store.preflight_non_primary_items(
                        verified,
                        primary_relpath=primary_relpath,
                    )
                    created_targets = [target for _item, target in pending]
                    self.recovery_store.restore_non_primary_items(
                        verified,
                        primary_relpath=primary_relpath,
                    )
                    self._write_session_sync(restored, project, save_id)
                    try:
                        self.recovery_store.mark_restored(verified)
                    except BaseException as commit_exc:
                        try:
                            compensate_commit_failure(
                                created_targets,
                                undo_recovery_id,
                            )
                        except BaseException as rollback_exc:
                            raise RecoveryIntegrityError(
                                "恢复提交失败且补偿未完成"
                            ) from rollback_exc
                        raise commit_exc
                    return response(restored, undo_recovery_id)

                return await run_sync_critical(
                    _run_with_library_shared_write,
                    restore_sync,
                )
