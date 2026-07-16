"""Prompt 消息注入必须遵循 in_prompt 与 pinned 的单一来源规则。"""
from __future__ import annotations

from core import prompt_builder


def _build(history: list[dict]) -> list[dict]:
    return prompt_builder.build_messages(
        user_input="CURRENT_USER_INPUT",
        characters=[],
        characters_state={},
        scene_meta={},
        user_profile={},
        worldbook_entries=[],
        history=history,
        summaries=[],
    )


def _occurrences(messages: list[dict], marker: str) -> int:
    return sum(message.get("content", "").count(marker) for message in messages)


def test_in_prompt_false_is_never_injected_even_when_recent_and_pinned():
    hidden_marker = "HIDDEN_IN_PROMPT_FALSE_SENTINEL"
    history = [
        {"role": "user", "content": f"普通-{index}", "in_prompt": True}
        for index in range(prompt_builder.MAX_TURNS_IN_PROMPT * 2)
    ]
    history.append({
        "role": "assistant",
        "content": hidden_marker,
        "in_prompt": False,
        "pinned": True,
    })

    messages = _build(history)

    assert _occurrences(messages, hidden_marker) == 0


def test_recent_and_older_pinned_messages_are_each_injected_exactly_once():
    older_marker = "OLDER_PINNED_SENTINEL"
    recent_marker = "RECENT_PINNED_SENTINEL"
    recent_n = prompt_builder.MAX_TURNS_IN_PROMPT * 2
    history = [{
        "role": "user",
        "content": older_marker,
        "in_prompt": True,
        "pinned": True,
    }]
    history.extend(
        {
            "role": "assistant" if index % 2 else "user",
            "content": f"填充-{index}",
            "in_prompt": True,
            "pinned": False,
        }
        for index in range(recent_n + 1)
    )
    history.append({
        "role": "assistant",
        "content": recent_marker,
        "in_prompt": True,
        "pinned": True,
    })

    messages = _build(history)

    assert _occurrences(messages, older_marker) == 1
    assert _occurrences(messages, recent_marker) == 1
