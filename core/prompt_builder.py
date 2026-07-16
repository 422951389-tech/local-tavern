"""旧调用兼容层；新聊天链路使用 :mod:`core.prompt_assembler`。"""
from __future__ import annotations

from typing import Optional

from core.config import MAX_TURNS_IN_PROMPT
from core.prompt_assembler import PromptAssembler


_compat_assembler = PromptAssembler()


def build_messages(
    user_input: str,
    characters: list[dict],
    characters_state: dict,
    scene_meta: dict,
    user_profile: dict,
    worldbook_entries: list[dict],
    history: list[dict],
    summaries: Optional[list] = None,
    manual_worldbook_ids: Optional[list[str]] = None,
) -> list[dict]:
    """保留原公开函数，按数量窗口组装但不施加运行时模型预算。

    路由不得使用此兼容入口；路由必须把 Ollama 上下文上限和 num_predict
    交给 PromptAssembler，以便在 Session/turn 写入前执行预算拒绝。
    """

    return _compat_assembler.assemble(
        user_input=user_input,
        characters=characters,
        characters_state=characters_state,
        scene_meta=scene_meta,
        user_profile=user_profile,
        worldbook_entries=worldbook_entries,
        history=history,
        summaries=summaries,
        context_limit=2_147_483_647,
        context_limit_source="compat_unbounded",
        num_predict=0,
        manual_worldbook_ids=manual_worldbook_ids,
        safety_margin=0,
    ).messages


__all__ = ["MAX_TURNS_IN_PROMPT", "build_messages"]
