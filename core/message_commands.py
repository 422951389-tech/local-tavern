"""稳定 message_id 消息命令的纯领域语义。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass


class MessageCommandError(ValueError):
    """可映射为稳定 API 错误码的消息命令错误。"""

    code = "message_command_invalid"


class MessageNotFound(MessageCommandError):
    code = "message_not_found"


class RegenerationSourceNotFound(MessageCommandError):
    code = "regeneration_source_not_found"


class RegenerationSourceExcluded(MessageCommandError):
    code = "regeneration_source_excluded"


@dataclass(frozen=True)
class MessageRegenerationPlan:
    """重生成的确定性内存计划；不持有原 Session 的可变引用。"""

    target_message_id: str
    source_message_id: str
    user_input: str
    retained_history: tuple[dict, ...]
    source_message: dict

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
        return [*deepcopy(list(self.retained_history)), pending]


def plan_message_regeneration(
    session: dict,
    message_id: str,
) -> MessageRegenerationPlan:
    """按稳定 ID 将 user/assistant 目标映射到对应的源 user。

    assistant 优先匹配前方相同 turn_id 的 user；旧消息没有可靠 turn_id 时，
    回退到最近的前置 user。source 之后只有 pinned 会保留，source 自身复用
    原 UUID 并在接受事务中重绑为新的 pending user。
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

    retained = [
        deepcopy(message)
        for message in history[:source_index]
        if isinstance(message, dict)
    ]
    retained.extend(
        deepcopy(message)
        for message in history[source_index + 1:]
        if isinstance(message, dict) and message.get("pinned")
    )
    return MessageRegenerationPlan(
        target_message_id=message_id,
        source_message_id=source_id,
        user_input=user_input.strip(),
        retained_history=tuple(retained),
        source_message=deepcopy(source),
    )
