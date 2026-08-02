"""WORLD-1：世界书三态触发、匹配边界和稳定排序。"""
from __future__ import annotations

import json
import random
from copy import deepcopy

import pytest

from core.worldbook_policy import (
    WorldbookValidationError,
    activate_worldbook_entries,
    normalize_worldbook_entry,
    worldbook_prompt_payload,
)


def _active_ids(candidates) -> list[str]:
    return [candidate.entry_id for candidate in candidates if candidate.activated]


def _candidate(candidates, entry_id: str):
    return next(candidate for candidate in candidates if candidate.entry_id == entry_id)


def _activate(entries, **overrides):
    values = {
        "user_input": "",
        "history": [],
        "scene_meta": {},
        "characters": [],
        "characters_state": {},
        "manual_worldbook_ids": [],
        "max_turns": 10,
    }
    values.update(overrides)
    return activate_worldbook_entries(entries, **values)


def test_legacy_normalization_defaults_to_always_without_mutating_input():
    legacy = {
        "id": "legacy_lore",
        "keys": ["旧关键词"],
        "constant": False,
        "position": "before",
        "content": "旧正文",
    }
    before = deepcopy(legacy)

    normalized = normalize_worldbook_entry(legacy)

    assert legacy == before
    assert normalized == {
        **legacy,
        "enabled": True,
        "activation": "always",
        "keywords": [],
        "priority": 0,
        "category": "general",
        "summary": "",
        "visibility": "public",
        "knowledge_scope": "global",
        "known_by_character_ids": [],
        "linked_character_ids": [],
        "linked_entry_ids": [],
        "location_aliases": [],
    }
    candidate = _activate([legacy])[0]
    assert candidate.activated is True
    assert candidate.trigger == "legacy_always"


@pytest.mark.parametrize(
    ("entry", "violation"),
    [
        ({"id": "bad", "enabled": 1}, "enabled:expected_boolean"),
        ({"id": "bad", "activation": "sometimes"}, "activation:invalid"),
        ({"id": "bad", "priority": True}, "priority:expected_integer"),
        ({"id": "bad", "keywords": "word"}, "keywords:expected_array"),
        (
            {"id": "bad", "activation": "keywords", "keywords": []},
            "keywords:required_for_activation",
        ),
        (
            {"id": "bad", "keywords": ["Ａ", "A"]},
            "keywords:item_duplicate",
        ),
    ],
)
def test_explicit_invalid_worldbook_fields_are_rejected(entry, violation):
    with pytest.raises(WorldbookValidationError) as caught:
        normalize_worldbook_entry(entry)
    assert violation in caught.value.violations
    assert set(caught.value.as_detail()) == {"code", "message", "violations"}


def test_strict_normalization_validates_path_id_and_payload_fields():
    with pytest.raises(WorldbookValidationError) as caught:
        normalize_worldbook_entry(
            {"id": "body_id", "content": ["not text"]},
            entry_id="path_id",
            strict=True,
        )
    assert set(caught.value.violations) == {
        "content:expected_string",
        "id:path_mismatch",
    }


def test_activation_modes_and_enabled_override_are_exact():
    entries = [
        {"id": "always", "activation": "always", "content": "A"},
        {
            "id": "keyword_hit",
            "activation": "keywords",
            "keywords": ["灯塔"],
            "content": "B",
        },
        {
            "id": "keyword_miss",
            "activation": "keywords",
            "keywords": ["雪山"],
            "content": "C",
        },
        {"id": "manual_on", "activation": "manual", "content": "D"},
        {"id": "manual_off", "activation": "manual", "content": "E"},
        {
            "id": "disabled",
            "activation": "always",
            "enabled": False,
            "content": "F",
        },
    ]

    candidates = _activate(
        entries,
        user_input="我走近灯塔",
        manual_worldbook_ids=["manual_on"],
    )

    assert set(_active_ids(candidates)) == {"always", "keyword_hit", "manual_on"}
    assert _candidate(candidates, "always").hit_count == 0
    assert _candidate(candidates, "manual_on").hit_count == 0
    assert _candidate(candidates, "keyword_hit").hit_count == 1
    assert _candidate(candidates, "keyword_miss").reason == "keyword_not_matched"
    assert _candidate(candidates, "manual_off").reason == "manual_not_selected"
    assert _candidate(candidates, "disabled").reason == "disabled"


def test_stored_manual_selection_tolerates_dormant_unknown_and_changed_entries():
    entries = [
        {"id": "manual", "activation": "manual"},
        {"id": "always", "activation": "always"},
        {
            "id": "changed_to_keywords",
            "activation": "keywords",
            "keywords": ["没有命中"],
        },
        {
            "id": "disabled_manual",
            "activation": "manual",
            "enabled": False,
        },
    ]
    candidates = _activate(
        entries,
        manual_worldbook_ids=[
            "manual",
            "manual",
            "missing",
            "always",
            "changed_to_keywords",
            "disabled_manual",
            "bad id",
            7,
        ],
    )

    assert set(_active_ids(candidates)) == {"manual", "always"}
    assert _candidate(candidates, "manual").trigger == "manual_selected"
    assert _candidate(candidates, "always").trigger == "always"
    assert _candidate(candidates, "changed_to_keywords").reason == "keyword_not_matched"
    assert _candidate(candidates, "disabled_manual").reason == "disabled"


def test_matching_uses_only_current_recent_scene_and_active_character_aliases():
    history = [
        {
            "id": f"message-{index}",
            "turn_id": f"turn-{index}",
            "role": "user",
            "content": "窗口外密语" if index == 0 else f"普通历史 {index}",
            "in_prompt": True,
            "thinking": "思维秘密",
        }
        for index in range(11)
    ]
    history[-1]["content"] = "最近港口消息"
    history.append({
        "id": "hidden-message",
        "turn_id": "hidden-turn",
        "role": "assistant",
        "content": "排除秘密",
        "in_prompt": False,
    })
    entries = [
        {"id": "current", "activation": "keywords", "keywords": ["abc"]},
        {"id": "recent", "activation": "keywords", "keywords": ["港口"]},
        {"id": "old", "activation": "keywords", "keywords": ["窗口外密语"]},
        {"id": "hidden", "activation": "keywords", "keywords": ["排除秘密"]},
        {"id": "thinking", "activation": "keywords", "keywords": ["思维秘密"]},
        {"id": "scene", "activation": "keywords", "keywords": ["雨夜"]},
        {"id": "character_id", "activation": "keywords", "keywords": ["hero"]},
        {"id": "character_name", "activation": "keywords", "keywords": ["阿澜"]},
        {"id": "character_alias", "activation": "keywords", "keywords": ["代号"]},
        {"id": "state_name", "activation": "keywords", "keywords": ["现名"]},
        {"id": "inactive", "activation": "keywords", "keywords": ["幽灵别名"]},
    ]
    candidates = _activate(
        entries,
        user_input="全角 ＡＢＣ",
        history=history,
        scene_meta={"weather": "雨夜", "nested": {"count": 3}},
        characters=[
            {"id": "hero", "name": "阿澜", "aliases": ["深海代号"]},
            {"id": "ghost", "name": "幽灵", "aliases": ["幽灵别名"]},
        ],
        characters_state={"hero": {"name": "角色现名"}},
    )

    assert set(_active_ids(candidates)) == {
        "current",
        "recent",
        "scene",
        "character_id",
        "character_name",
        "character_alias",
        "state_name",
    }
    assert _candidate(candidates, "current").recency_distance == 0
    assert _candidate(candidates, "recent").recency_distance == 1
    assert _candidate(candidates, "scene").recency_distance == 11
    diagnostic = json.dumps(
        _candidate(candidates, "character_alias").diagnostic(),
        ensure_ascii=False,
    )
    assert "character:hero" in diagnostic
    assert "深海代号" not in diagnostic
    assert "最近港口消息" not in json.dumps(
        [candidate.diagnostic() for candidate in candidates],
        ensure_ascii=False,
    )


def test_sorting_is_priority_then_distinct_hits_then_recency_then_stable_id():
    entries = [
        {
            "id": "priority",
            "activation": "keywords",
            "priority": 5,
            "keywords": ["旧词"],
        },
        {
            "id": "two_hits",
            "activation": "keywords",
            "keywords": ["新词", "第二词"],
        },
        {
            "id": "recent",
            "activation": "keywords",
            "keywords": ["新词"],
        },
        {
            "id": "older",
            "activation": "keywords",
            "keywords": ["旧词"],
        },
        {"id": "z_always", "activation": "always"},
        {"id": "a_always", "activation": "always"},
    ]
    kwargs = {
        "user_input": "新词和第二词",
        "history": [{
            "id": "history-one",
            "turn_id": "turn-one",
            "role": "user",
            "content": "旧词",
        }],
    }
    expected = [
        "priority",
        "two_hits",
        "recent",
        "older",
        "a_always",
        "z_always",
    ]
    assert _active_ids(_activate(entries, **kwargs)) == expected

    rng = random.Random(20260716)
    for _ in range(100):
        shuffled = deepcopy(entries)
        rng.shuffle(shuffled)
        assert _active_ids(_activate(shuffled, **kwargs)) == expected


def test_duplicate_entry_ids_are_rejected_and_control_fields_never_enter_payload():
    with pytest.raises(WorldbookValidationError) as caught:
        _activate([{"id": "same"}, {"id": "same"}])
    assert "id:duplicate" in caught.value.violations

    payload = worldbook_prompt_payload({
        "id": "lore",
        "enabled": True,
        "activation": "keywords",
        "keywords": ["秘密触发词"],
        "priority": 3,
        "keys": ["旧词"],
        "constant": False,
        "position": "before",
        "content": "事实正文",
        "custom": {"era": "未来"},
    })
    assert payload == {
        "id": "lore",
        "content": "事实正文",
        "custom": {"era": "未来"},
    }


def test_worldbook_structured_fields_and_knowledge_boundary_are_normalized():
    normalized = normalize_worldbook_entry({
        "id": "glass_palace",
        "title": "琉璃宫",
        "summary": "帝都权力中心",
        "category": "location",
        "visibility": "discovered",
        "knowledge_scope": "characters",
        "known_by_character_ids": ["hero", "hero", "attendant"],
        "linked_character_ids": ["hero"],
        "linked_entry_ids": ["court_rules"],
        "location_aliases": ["琉璃宫", "宫城"],
        "activation": "scene",
        "content": "琉璃宫由十二座悬桥连接。",
    })

    assert normalized["category"] == "location"
    assert normalized["visibility"] == "discovered"
    assert normalized["knowledge_scope"] == "characters"
    assert normalized["known_by_character_ids"] == ["hero", "attendant"]
    assert normalized["linked_entry_ids"] == ["court_rules"]
    assert normalized["location_aliases"] == ["琉璃宫", "宫城"]

    payload = worldbook_prompt_payload(normalized)
    assert payload["knowledge_scope"] == "characters"
    assert payload["known_by_character_ids"] == ["hero", "attendant"]
    assert "activation" not in payload
    assert "location_aliases" not in payload


def test_scene_activation_uses_location_aliases_and_active_character_links():
    entries = [
        {
            "id": "palace",
            "title": "琉璃宫",
            "activation": "scene",
            "location_aliases": ["琉璃宫"],
        },
        {
            "id": "hero_secret",
            "title": "守门人的旧约",
            "activation": "scene",
            "linked_character_ids": ["hero"],
        },
        {
            "id": "distant",
            "activation": "scene",
            "location_aliases": ["雪山"],
        },
    ]

    candidates = _activate(
        entries,
        scene_meta={"location": "琉璃宫·花园"},
        characters=[{"id": "hero", "name": "阿澜"}],
        characters_state={"hero": {"name": "阿澜"}},
    )

    assert set(_active_ids(candidates)) == {"palace", "hero_secret"}
    assert _candidate(candidates, "palace").trigger == "scene_link_match"
    assert _candidate(candidates, "hero_secret").trigger == "scene_link_match"
    assert _candidate(candidates, "distant").reason == "scene_link_not_matched"
    diagnostic = _candidate(candidates, "palace").diagnostic()
    assert diagnostic["title"] == "琉璃宫"
    assert diagnostic["category"] == "general" or diagnostic["category"] == "location"
    assert "content" not in diagnostic
