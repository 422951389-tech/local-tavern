"""稳定 message_id 消息命令的纯领域语义。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from uuid import uuid4


MAX_REPLY_ALTERNATIVES = 9
_REPLY_VARIANT_FIELDS = (
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
)
_BRANCH_STATE_FIELDS = (
    "scene_meta",
    "characters_state",
    "relationship_edges",
)


class MessageCommandError(ValueError):
    """可映射为稳定 API 错误码的消息命令错误。"""

    code = "message_command_invalid"


class MessageNotFound(MessageCommandError):
    code = "message_not_found"


class RegenerationSourceNotFound(MessageCommandError):
    code = "regeneration_source_not_found"


class RegenerationSourceExcluded(MessageCommandError):
    code = "regeneration_source_excluded"


class RegenerationWouldRewriteHistory(MessageCommandError):
    code = "regeneration_would_rewrite_history"


class ReplyAlternativeNotFound(MessageCommandError):
    code = "reply_alternative_not_found"


class ReplyAlternativeInvalid(MessageCommandError):
    code = "reply_alternative_invalid"


def capture_reply_state(session: dict) -> dict:
    """只冻结会随模型回复变化的会话状态。"""

    return {
        field: deepcopy(session.get(field))
        for field in _BRANCH_STATE_FIELDS
    }


def assistant_reply_variant(
    message: dict,
    *,
    state_snapshot: dict,
) -> dict:
    """将当前 assistant 回复冻结为可无损切换的轻量变体。"""

    variant = {
        "id": message.get("reply_variant_id") or str(uuid4()),
        "state_snapshot": deepcopy(state_snapshot),
    }
    for field in _REPLY_VARIANT_FIELDS:
        if field in message:
            variant[field] = deepcopy(message[field])
    return variant


def _dedupe_reply_alternatives(alternatives: list[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for alternative in alternatives:
        if not isinstance(alternative, dict):
            continue
        alternative_id = alternative.get("id")
        if not isinstance(alternative_id, str) or alternative_id in seen:
            continue
        seen.add(alternative_id)
        result.append(deepcopy(alternative))
        if len(result) >= MAX_REPLY_ALTERNATIVES:
            break
    return result


def attach_original_reply_as_alternative(
    session: dict,
    user_message: dict,
    assistant_message: dict,
) -> None:
    """完成重生成后保留原回复、原状态与原稳定 message_id。"""

    original = user_message.pop("regeneration_original_assistant", None)
    original_state = user_message.pop("regeneration_original_state", None)
    user_message.pop("regeneration_source_snapshot", None)
    if not isinstance(original, dict) or not isinstance(original_state, dict):
        return

    existing = original.get("reply_alternatives", [])
    if not isinstance(existing, list):
        existing = []
    original_variant = assistant_reply_variant(
        original,
        state_snapshot=original_state,
    )
    assistant_message["id"] = original["id"]
    assistant_message["pinned"] = bool(original.get("pinned", False))
    assistant_message["reply_variant_id"] = str(uuid4())
    assistant_message["reply_variant_state"] = capture_reply_state(session)
    assistant_message["reply_alternatives"] = _dedupe_reply_alternatives([
        original_variant,
        *existing,
    ])


def select_reply_alternative(
    session: dict,
    *,
    message_id: str,
    alternative_id: str,
) -> dict:
    """在同一 assistant 容器中交换回复与分支状态，不改写后续历史。"""

    resolved = next(
        (
            (index, item)
            for index, item in enumerate(session.get("message_history", []))
            if isinstance(item, dict) and item.get("id") == message_id
        ),
        None,
    )
    if resolved is None or resolved[1].get("role") != "assistant":
        raise MessageNotFound("assistant 消息 ID 不存在")
    message_index, message = resolved
    alternatives = message.get("reply_alternatives")
    if not isinstance(alternatives, list):
        raise ReplyAlternativeNotFound("该回复没有可切换的备选")
    selected = next(
        (
            item
            for item in alternatives
            if isinstance(item, dict) and item.get("id") == alternative_id
        ),
        None,
    )
    if selected is None:
        raise ReplyAlternativeNotFound("备选回复 ID 不存在")
    selected_state = selected.get("state_snapshot")
    if not isinstance(selected_state, dict):
        raise ReplyAlternativeInvalid("备选回复缺少状态快照")

    current_state = message.get("reply_variant_state")
    if not isinstance(current_state, dict):
        current_state = capture_reply_state(session)
    current_variant = assistant_reply_variant(
        message,
        state_snapshot=current_state,
    )
    preserved = {
        "id": message["id"],
        "role": "assistant",
        "pinned": bool(message.get("pinned", False)),
        "in_prompt": bool(message.get("in_prompt", True)),
    }
    for field in _REPLY_VARIANT_FIELDS:
        message.pop(field, None)
    message.update(preserved)
    for field in _REPLY_VARIANT_FIELDS:
        if field in selected:
            message[field] = deepcopy(selected[field])
    message["reply_variant_id"] = selected["id"]
    message["reply_variant_state"] = deepcopy(selected_state)
    message["reply_alternatives"] = _dedupe_reply_alternatives([
        current_variant,
        *(
            item
            for item in alternatives
            if isinstance(item, dict) and item.get("id") != alternative_id
        ),
    ])
    has_later_history = any(
        isinstance(item, dict)
        for item in session.get("message_history", [])[message_index + 1:]
    )
    if not has_later_history:
        for field in _BRANCH_STATE_FIELDS:
            if field in selected_state:
                session[field] = deepcopy(selected_state[field])
    return {
        "message_id": message_id,
        "reply_variant_id": alternative_id,
        "alternative_count": len(message["reply_alternatives"]),
        "state_applied": not has_later_history,
    }


@dataclass(frozen=True)
class MessageRegenerationPlan:
    """重生成的确定性内存计划；不持有原 Session 的可变引用。"""

    target_message_id: str
    source_message_id: str
    user_input: str
    retained_history: tuple[dict, ...]
    source_message: dict
    target_assistant: dict | None
    original_state: dict

    @property
    def prompt_history(self) -> list[dict]:
        return deepcopy(list(self.retained_history))

    def pending_history(self, *, turn_id: str, created_at: str) -> list[dict]:
        pending = deepcopy(self.source_message)
        pending.update({
            "id": self.source_message_id,
            "role": "user",
            "content": self.user_input,
            "turn_id": turn_id,
            "status": "pending",
            "error": None,
            "timestamps": {
                "created_at": created_at,
                "completed_at": None,
            },
            "pinned": bool(self.source_message.get("pinned", False)),
            "in_prompt": True,
        })
        pending.pop("thinking", None)
        pending["regeneration_source_snapshot"] = deepcopy(self.source_message)
        if self.target_assistant is not None:
            pending["regeneration_original_assistant"] = deepcopy(
                self.target_assistant
            )
            pending["regeneration_original_state"] = deepcopy(
                self.original_state
            )
        return [*deepcopy(list(self.retained_history)), pending]


def restore_regeneration_original(
    session: dict,
    user_message: dict,
) -> dict | None:
    """失败或重启时用接受事务留下的快照恢复原 user/assistant。"""

    source = user_message.pop("regeneration_source_snapshot", None)
    original = user_message.pop("regeneration_original_assistant", None)
    user_message.pop("regeneration_original_state", None)
    if not isinstance(source, dict):
        return None
    history = session.setdefault("message_history", [])
    try:
        user_index = history.index(user_message)
    except ValueError:
        return None
    user_message.clear()
    user_message.update(deepcopy(source))
    if not isinstance(original, dict):
        return None
    original_id = original.get("id")
    if not any(
        isinstance(message, dict) and message.get("id") == original_id
        for message in history
    ):
        history.insert(user_index + 1, deepcopy(original))
    return deepcopy(original)


def plan_message_regeneration(
    session: dict,
    message_id: str,
) -> MessageRegenerationPlan:
    """按稳定 ID 将 user/assistant 目标映射到对应的源 user。

    assistant 优先匹配前方相同 turn_id 的 user；旧消息没有可靠 turn_id 时，
    回退到最近的前置 user。只有历史末尾的连续 user/assistant 对允许重生成，
    source 自身复用原 UUID 并在接受事务中重绑为新的 pending user。
    """
    if not isinstance(message_id, str) or not message_id:
        raise MessageNotFound("缺少 message_id")

    history = session.get("message_history", [])
    if not isinstance(history, list):
        raise MessageNotFound("消息历史无效")

    target_index = next(
        (
            index
            for index, message in enumerate(history)
            if isinstance(message, dict) and message.get("id") == message_id
        ),
        None,
    )
    if target_index is None:
        raise MessageNotFound("消息 ID 不存在")

    target = history[target_index]
    source_index: int | None = None
    if target.get("role") == "user":
        source_index = target_index
    elif target.get("role") == "assistant":
        target_turn_id = target.get("turn_id")
        if target_turn_id:
            source_index = next(
                (
                    index
                    for index in range(target_index - 1, -1, -1)
                    if isinstance(history[index], dict)
                    and history[index].get("role") == "user"
                    and history[index].get("turn_id") == target_turn_id
                ),
                None,
            )
        if source_index is None:
            source_index = next(
                (
                    index
                    for index in range(target_index - 1, -1, -1)
                    if isinstance(history[index], dict)
                    and history[index].get("role") == "user"
                ),
                None,
            )

    if source_index is None:
        raise RegenerationSourceNotFound("目标消息没有可重生成的前置 user")

    source = history[source_index]
    source_id = source.get("id")
    user_input = source.get("content")
    if not isinstance(source_id, str) or not source_id:
        raise RegenerationSourceNotFound("源 user 缺少稳定 message_id")
    if not isinstance(user_input, str) or not user_input.strip():
        raise RegenerationSourceNotFound("源 user 内容为空")
    if source.get("in_prompt", True) is False:
        raise RegenerationSourceExcluded(
            "源 user 已排除出 Prompt；请先显式重新勾选该消息"
        )

    target_assistant_index: int | None = None
    if target.get("role") == "assistant":
        target_assistant_index = target_index
    else:
        source_turn_id = source.get("turn_id")
        for index in range(source_index + 1, len(history)):
            candidate = history[index]
            if not isinstance(candidate, dict):
                continue
            if candidate.get("role") == "user":
                break
            if candidate.get("role") != "assistant":
                continue
            if (
                source_turn_id
                and candidate.get("turn_id")
                and candidate.get("turn_id") != source_turn_id
            ):
                continue
            target_assistant_index = index
            break

    boundary_index = (
        target_assistant_index
        if target_assistant_index is not None
        else source_index
    )
    if any(
        isinstance(message, dict)
        for message in history[boundary_index + 1:]
    ):
        raise RegenerationWouldRewriteHistory(
            "目标回复后已有继续发展的对话；只能重生成最新回复"
        )
    if target_assistant_index is not None and any(
        isinstance(message, dict)
        for message in history[source_index + 1:target_assistant_index]
    ):
        raise RegenerationWouldRewriteHistory(
            "目标回复与来源消息之间已有其他内容；不能无损重生成"
        )

    retained = [
        deepcopy(message)
        for message in history[:source_index]
        if isinstance(message, dict)
    ]
    retained.extend(
        deepcopy(message)
        for index, message in enumerate(
            history[source_index + 1:],
            start=source_index + 1,
        )
        if index != target_assistant_index
        if isinstance(message, dict) and message.get("pinned")
    )
    return MessageRegenerationPlan(
        target_message_id=message_id,
        source_message_id=source_id,
        user_input=user_input.strip(),
        retained_history=tuple(retained),
        source_message=deepcopy(source),
        target_assistant=(
            deepcopy(history[target_assistant_index])
            if target_assistant_index is not None
            else None
        ),
        original_state=capture_reply_state(session),
    )
