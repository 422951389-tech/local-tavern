from core.roleplay_policy import (
    build_roleplay_context,
    decrement_silence_counters,
    resolve_character_id,
)


def test_build_context_is_strict_ordered_deduplicated_and_redacted():
    context = build_roleplay_context(
        [
            {
                "id": "alpha",
                "name": "阿尔法",
                "aliases": ["队长", "队长", 7],
                "chattiness": 0,
                "persona": "不得进入上下文的正文",
            },
            {"id": "alpha", "name": "重复项", "chattiness": 100},
            {"id": "beta", "name": "贝塔", "chattiness": 100},
            {"name": "没有稳定 ID"},
        ],
        {
            "alpha": {
                "name": "夜莺",
                "remaining_silent_turns": 2,
                "inner_thought": "不得泄露",
            },
            "beta": {"remaining_silent_turns": 0},
        },
        {"strict_muted_writeback": True, "private": "不得泄露"},
    )

    assert context == {
        "strict_muted_writeback": True,
        "characters": [
            {
                "id": "alpha",
                "name": "阿尔法",
                "aliases": ["队长", "夜莺"],
                "chattiness": 0,
                "remaining_silent_turns": 2,
                "may_speak": False,
            },
            {
                "id": "beta",
                "name": "贝塔",
                "aliases": [],
                "chattiness": 100,
                "remaining_silent_turns": 0,
                "may_speak": True,
            },
        ],
        "speakable_ids": ["beta"],
        "muted_ids": ["alpha"],
    }
    assert "正文" not in repr(context)
    assert "inner_thought" not in repr(context)


def test_build_context_defensively_defaults_untrusted_values():
    context = build_roleplay_context(
        [
            {"id": "bad_bool", "chattiness": True},
            {"id": "bad_range", "chattiness": 101},
            {"id": "missing"},
        ],
        {
            "bad_bool": {"remaining_silent_turns": True},
            "bad_range": {"remaining_silent_turns": 1000},
            "missing": "invalid state",
        },
        {"strict_muted_writeback": "true"},
    )
    assert context["strict_muted_writeback"] is False
    assert [item["chattiness"] for item in context["characters"]] == [50, 50, 50]
    assert [item["remaining_silent_turns"] for item in context["characters"]] == [0, 0, 0]
    assert context["speakable_ids"] == ["bad_bool", "bad_range", "missing"]


def test_resolver_accepts_id_card_name_session_name_and_nfkc_alias():
    context = build_roleplay_context(
        [
            {"id": "alpha", "name": "艾拉", "aliases": ["ＡＬＰＨＡ", "队长"]},
            {"id": "beta", "name": "贝塔", "aliases": ["副手"]},
        ],
        {
            "alpha": {"name": "夜莺"},
            "beta": {"name": "白塔"},
        },
    )

    for value, expected in (
        ("alpha", "alpha"),
        ("艾拉", "alpha"),
        ("夜莺", "alpha"),
        ("ａｌｐｈａ", "alpha"),
        ("副手", "beta"),
        (" 白塔 ", "beta"),
    ):
        assert resolve_character_id(value, context) == {
            "status": "matched",
            "character_id": expected,
        }


def test_resolver_never_guesses_on_cross_field_collisions():
    context = build_roleplay_context(
        [
            {"id": "zeta", "name": "同名", "aliases": ["共享别名"]},
            {"id": "alpha", "name": "另一人", "aliases": ["同名"]},
            {"id": "shared", "name": "第三人"},
            {"id": "omega", "name": "第四人", "aliases": ["ＳＨＡＲＥＤ"]},
        ],
        {},
    )
    assert resolve_character_id("同名", context) == {
        "status": "ambiguous",
        "candidate_ids": ["alpha", "zeta"],
    }
    assert resolve_character_id("shared", context) == {
        "status": "ambiguous",
        "candidate_ids": ["omega", "shared"],
    }
    assert resolve_character_id("模型虚构名和正文", context) == {
        "status": "unknown",
        "candidate_ids": [],
    }


def test_decrement_only_changes_strict_positive_integer_counters():
    session = {
        "characters_state": {
            "zeta": {"remaining_silent_turns": 2},
            "alpha": {"remaining_silent_turns": 1},
            "zero": {"remaining_silent_turns": 0},
            "bool": {"remaining_silent_turns": True},
            "float": {"remaining_silent_turns": 1.0},
            "negative": {"remaining_silent_turns": -1},
            "invalid": "state",
        }
    }
    assert decrement_silence_counters(session) == ["alpha", "zeta"]
    assert session["characters_state"]["alpha"]["remaining_silent_turns"] == 0
    assert session["characters_state"]["zeta"]["remaining_silent_turns"] == 1
    assert session["characters_state"]["bool"]["remaining_silent_turns"] is True
    assert decrement_silence_counters(None) == []
