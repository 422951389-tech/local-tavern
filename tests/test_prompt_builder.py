"""Prompt 消息注入必须遵循 in_prompt 与 pinned 的单一来源规则。"""
from __future__ import annotations

from core import prompt_builder


def _build(
    history: list[dict],
    *,
    user_input: str = "CURRENT_USER_INPUT",
    worldbook_entries: list[dict] | None = None,
    manual_worldbook_ids: list[str] | None = None,
) -> list[dict]:
    return prompt_builder.build_messages(
        user_input=user_input,
        characters=[],
        characters_state={},
        scene_meta={},
        user_profile={},
        worldbook_entries=worldbook_entries or [],
        history=history,
        summaries=[],
        manual_worldbook_ids=manual_worldbook_ids,
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


def test_compat_builder_uses_keyword_and_manual_activation_without_control_fields():
    messages = _build(
        [],
        user_input="前往灯塔",
        worldbook_entries=[
            {
                "id": "keyword",
                "activation": "keywords",
                "keywords": ["灯塔"],
                "content": "KEYWORD_WORLD_BODY",
            },
            {
                "id": "manual",
                "activation": "manual",
                "content": "MANUAL_WORLD_BODY",
            },
            {
                "id": "manual_off",
                "activation": "manual",
                "content": "MANUAL_OFF_WORLD_BODY",
            },
        ],
        manual_worldbook_ids=["manual"],
    )

    assert _occurrences(messages, "KEYWORD_WORLD_BODY") == 1
    assert _occurrences(messages, "MANUAL_WORLD_BODY") == 1
    assert _occurrences(messages, "MANUAL_OFF_WORLD_BODY") == 0
    assert _occurrences(messages, '"activation"') == 0
    assert _occurrences(messages, '"keywords"') == 0
    assert _occurrences(messages, '"priority"') == 0


def test_compat_builder_keeps_roleplay_controls_in_character_slot_once():
    messages = prompt_builder.build_messages(
        user_input="继续",
        characters=[{"id": "solo", "name": "独角", "chattiness": 90}],
        characters_state={"solo": {"remaining_silent_turns": 1}},
        scene_meta={},
        user_profile={},
        worldbook_entries=[],
        history=[],
        summaries=[],
        roleplay_context={
            "strict_muted_writeback": False,
            "characters": [{
                "id": "solo",
                "name": "独角",
                "aliases": [],
                "chattiness": 90,
                "remaining_silent_turns": 1,
                "may_speak": False,
            }],
            "speakable_ids": [],
            "muted_ids": ["solo"],
        },
    )

    assert _occurrences(messages, '"id":"solo"') == 1
    assert _occurrences(messages, '"may_speak":false') == 1
    assert _occurrences(messages, '"remaining_silent_turns":1') == 1
    assert _occurrences(messages, '"chattiness":90') == 1
