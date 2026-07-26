from __future__ import annotations

import math
from pathlib import Path

import pytest

from core.response_parser import parse_response
from routes.common import apply_character_state


def test_parse_explicit_multiline_narration_and_clamp_negative_affinity():
    parsed = parse_response(
        """📍 测试酒馆 | ⏱️ 夜晚

🎭 阿澜 | 💝 ███░░░░░░░ -20%

💭 内心想法：保持警觉。
👗 穿着：黑色外套
🧍 当前姿势：倚墙
💬 对白：\"听见了吗？\" (预期影响：提醒)

📖 场景旁白
<p class=\"scene\">灯光 <strong>暗了下来</strong>。</p>
门外传来两声轻响。
💡 行动建议
- 查看门外
- 留在原地
"""
    )

    assert parsed["characters"][0]["affinity"] == 0
    assert parsed["narration"] == (
        '<p class="scene">灯光 <strong>暗了下来</strong>。</p>\n'
        "门外传来两声轻响。"
    )
    assert parsed["suggestions"] == ["查看门外", "留在原地"]


def test_parse_inline_narration_without_character_card():
    parsed = parse_response(
        """📍 空场景 | ⏱️ 清晨
📖 场景旁白：<em>风掠过空荡的长廊。</em>
尘埃在光柱中缓慢下落。
💡 行动建议
1. 继续等待
2. 观察长廊
"""
    )

    assert parsed["characters"] == []
    assert parsed["narration"] == (
        "<em>风掠过空荡的长廊。</em>\n尘埃在光柱中缓慢下落。"
    )


def test_fallback_affinity_parser_preserves_negative_sign_before_clamp():
    parsed = parse_response(
        """🎭 退化格式角色
备注行
💝 -20%
💬 对白："测试退化解析"
📖 场景旁白
退化格式仍应安全解析。
💡 行动建议
- 继续
"""
    )

    assert parsed["characters"][0]["name"] == "退化格式角色"
    assert parsed["characters"][0]["affinity"] == 0


@pytest.mark.parametrize(
    "raw",
    [
        "普通回复，没有显式标签。\n💡 行动建议\n- 继续",
        "📖 场景旁白\n没有行动建议边界",
    ],
)
def test_narration_is_empty_without_complete_explicit_section(raw):
    assert parse_response(raw)["narration"] == ""


@pytest.mark.parametrize(
    ("old_affinity", "parsed_affinity", "expected"),
    [
        (-50, -20, 0),
        (150, 140, 100),
        (50, 90, 60),
        (50, 10, 40),
        (95, 1000, 100),
        (True, float("nan"), 0),
        (50, "invalid", 50),
        (float("nan"), 50, 10),
    ],
)
def test_apply_character_state_normalizes_limits_and_clamps_affinity(
    old_affinity,
    parsed_affinity,
    expected,
):
    state = {"affinity": old_affinity}

    apply_character_state(state, {"affinity": parsed_affinity})

    assert state["affinity"] == expected
    assert not isinstance(state["affinity"], bool)
    assert math.isfinite(state["affinity"])
    assert 0 <= state["affinity"] <= 100


def test_prompts_have_one_ordered_narration_label():
    root = Path(__file__).resolve().parents[1]
    for path in (
        root / "prompts" / "system.md",
        root / "prompts" / ".default" / "system.md",
    ):
        prompt = path.read_text(encoding="utf-8")
        assert prompt.count("📖 场景旁白") == 1
        assert prompt.index("🎭") < prompt.index("📖 场景旁白")
        assert prompt.index("📖 场景旁白") < prompt.index("💡 行动建议")
