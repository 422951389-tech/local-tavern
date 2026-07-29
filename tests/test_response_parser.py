from __future__ import annotations

import math
from pathlib import Path

import pytest

from core.response_parser import build_response_presentation, parse_response
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


def test_parse_screenshot_style_single_line_protocol_without_text_wall():
    raw = (
        "📍 场景：旧港酒馆 | ⏱️ 雨夜 "
        "🎯 主线任务：寻找钥匙 "
        "📌 当前场景：壁炉旁 "
        "➡️ 下一目标: 查看后门 "
        "👤 用户：旅人 | 🆔 身份：访客 "
        "🎭 角色：阿澜｜💝 好感度：62% "
        "💭 内心：不能惊动他。🙂 "
        "👗 穿着: 黑色外套 "
        "🧍 姿势：倚墙 "
        "💬 对白：先别出声🙂，看门边（预期影响: 提醒） "
        "📖 场景旁白：雨水敲打窗棂。 "
        "💡 行动建议：1. 查看门边 2. 询问阿澜 3. 继续等待"
    )

    parsed = parse_response(raw)

    assert parsed["scene_meta"] == {
        "location": "旧港酒馆",
        "time_weather": "雨夜",
        "main_quest": "寻找钥匙",
        "current_scene": "壁炉旁",
        "next_goal": "查看后门",
        "user_line": "旅人 | 🆔 身份：访客",
    }
    assert parsed["characters"] == [{
        "name": "阿澜",
        "affinity": 62,
        "inner_thought": "不能惊动他。🙂",
        "outfit": "黑色外套",
        "posture": "倚墙",
        "dialogue": "先别出声🙂，看门边",
        "expected_effect": "提醒",
    }]
    assert parsed["narration"] == "雨水敲打窗棂。"
    assert parsed["suggestions"] == ["查看门边", "询问阿澜", "继续等待"]
    assert parsed["roleplay_warnings"] == []
    assert parsed["raw"] == raw


def test_markdown_narration_header_and_persisted_presentation_keep_no_raw_copy():
    raw = """📍 琉璃宫·花园 | ⏱️ 午后 / 晴朗
🎯 [主线] 任务名称: 初遇·命运交汇
📌 当前场景: 花园石径上，阳光洒落。
➡️ 下一目标: 等待用户选择行动方向
🎭 炽霞 | 💝 60%
💬 对白: "你就是传说中的游历者吗？"
### 场景旁白
阳光透过树影洒下斑驳光影。
💡 行动建议
- 回应炽霞
"""
    parsed = parse_response(raw)
    presentation = build_response_presentation(
        parsed,
        previous_affinities=[50],
        character_moods=["平静"],
        scene_changes=[{"key": "location", "value": "琉璃宫·花园"}],
    )

    assert parsed["narration"] == "阳光透过树影洒下斑驳光影。"
    assert presentation["schema_version"] == 1
    assert presentation["scene_meta"]["main_quest"] == "初遇·命运交汇"
    assert presentation["characters"][0]["affinity"] == 60
    assert presentation["characters"][0]["previous_affinity"] == 50
    assert presentation["characters"][0]["mood"] == "平静"
    assert presentation["suggestions"] == ["回应炽霞"]
    assert presentation["scene_changes"] == [
        {"key": "location", "value": "琉璃宫·花园"}
    ]
    assert "raw" not in presentation


def test_standard_multiline_fields_stop_at_the_next_known_label():
    parsed = parse_response(
        """📍 山门 | ⏱️ 清晨
🎯 [主线] 任务名称：拜访守门人
📌 当前场景: 石阶前
➡️ 下一目标：说明来意
👤 用户: 访客
🎭 守门人 | 💝 █████░░░░░ 50%
💭 内心想法：先听听来意。
第二句内心仍属于同一字段。
👗 穿着：灰袍
🧍 当前姿势：执杖而立
💬 对白："来者何人？" (预期影响：询问)
📖 场景旁白
晨雾沿石阶缓缓散开。
💡 行动建议
- 自报姓名
- 出示信物
"""
    )

    assert parsed["scene_meta"]["main_quest"] == "拜访守门人"
    assert parsed["scene_meta"]["current_scene"] == "石阶前"
    assert parsed["characters"][0]["inner_thought"] == (
        "先听听来意。\n第二句内心仍属于同一字段。"
    )
    assert parsed["characters"][0]["dialogue"] == "来者何人？"
    assert parsed["characters"][0]["expected_effect"] == "询问"
    assert parsed["narration"] == "晨雾沿石阶缓缓散开。"


def test_character_header_variant_and_missing_optional_fields_are_safe():
    parsed = parse_response(
        "🎭角色名：阿澜 - 💝好感度: 55% "
        "💬对白: 不必加引号🙂 "
        "💡行动建议: 1) 继续交谈"
    )

    assert parsed["characters"] == [{
        "name": "阿澜",
        "affinity": 55,
        "inner_thought": "",
        "outfit": "",
        "posture": "",
        "dialogue": "不必加引号🙂",
        "expected_effect": "",
    }]
    assert parsed["suggestions"] == ["继续交谈"]


def test_no_character_single_line_still_parses_narration_and_suggestions():
    parsed = parse_response(
        "📍 地点: 空庭院 | ⏱️ 时间/天气：午后 / 晴 "
        "🎯 任务: 等待回音 "
        "📌 当前场景：树影摇动 "
        "➡️ 下一目标: 观察入口 "
        "👤 玩家：访客 "
        "📖 场景旁白: 风吹过石板，💡 灵感只是正文里的普通 emoji。 "
        "💡 行动建议: 1. 原地等待 2. 查看入口"
    )

    assert parsed["characters"] == []
    assert parsed["scene_meta"]["location"] == "空庭院"
    assert parsed["scene_meta"]["time_weather"] == "午后 / 晴"
    assert parsed["narration"] == "风吹过石板，💡 灵感只是正文里的普通 emoji。"
    assert parsed["suggestions"] == ["原地等待", "查看入口"]


def test_unrelated_emoji_inside_dialogue_do_not_create_protocol_sections():
    parsed = parse_response(
        "🎭 阿澜 | 💝 40% "
        "💬 对白：舞台符号 🎭、地点符号 📍、目标符号 🎯 和笑脸 🙂 都只是对白。 "
        "💡 行动建议：- 回应阿澜"
    )

    assert len(parsed["characters"]) == 1
    assert parsed["characters"][0]["dialogue"] == (
        "舞台符号 🎭、地点符号 📍、目标符号 🎯 和笑脸 🙂 都只是对白。"
    )
    assert parsed["scene_meta"] is None


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


def test_single_line_narration_stays_empty_without_action_boundary():
    parsed = parse_response("📖 场景旁白：这段文字没有行动建议闭合标签。")

    assert parsed["narration"] == ""
    assert parsed["suggestions"] == []


def test_parser_keeps_existing_name_and_suggestion_limits():
    long_name = "甲" * 80
    suggestions = " ".join(f"{index}. 选项{index}" for index in range(1, 7))

    parsed = parse_response(
        f"🎭 {long_name} | 💝 80% 💬 对白：测试 "
        f"💡 行动建议：{suggestions}"
    )

    assert parsed["characters"][0]["name"] == "甲" * 50
    assert parsed["suggestions"] == [f"选项{index}" for index in range(1, 6)]


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


def test_active_system_prompt_forbids_single_paragraph_protocol_output():
    root = Path(__file__).resolve().parents[1]
    prompt = (root / "prompts" / "system.md").read_text(encoding="utf-8")

    assert "换行属于输出协议" in prompt
    assert "不得把回复压缩成单段文字" in prompt
    assert "每条行动建议独占一行" in prompt
