"""在隔离目录中创建浏览器 E2E 所需的最小项目与存档。"""
from __future__ import annotations

import asyncio
import os
from copy import deepcopy

from core.character_loader import ensure_project, save_character
from core.session_manager import mutate_session
from tests.e2e_fake_ollama import MODEL_NAME


DEFAULT_PROJECT = "默认项目"
SECOND_PROJECT = "切换项目"
DEFAULT_SAVE = "默认存档"
SECOND_SAVE = "切换存档"
MESSAGES = [
    {
        "id": "11111111-1111-4111-8111-111111111111",
        "role": "user",
        "content": "阿尔法在雨夜向贝塔交付了密信。",
        "pinned": False,
        "in_prompt": True,
    },
    {
        "id": "22222222-2222-4222-8222-222222222222",
        "role": "assistant",
        "content": """📍 隔离测试酒馆 | ⏱️ 雨夜 / 小雨
🎯 [主线] 任务名称：守护城门
📌 当前场景：贝塔收下密信，并答应共同守护城门。
➡️ 下一目标：确认城门守卫
👤 用户：测试用户
🎭 贝塔 | 💝 88%
💭 内心想法：密信必须妥善保管。
👗 穿着：深色守卫制服
🧍 当前姿势：站在城门内侧
💬 对白："我会守住城门。"（预期影响：建立信任）
### 场景旁白
雨水沿着城墙缓慢滑落。
💡 行动建议
- 检查城门
- 询问贝塔
""",
        "pinned": False,
        "in_prompt": True,
    },
]


def _variant_messages(
    *,
    user_id: str,
    assistant_id: str,
    label: str,
    affinity: int,
) -> list[dict]:
    return [
        {
            "id": user_id,
            "role": "user",
            "content": f"进入{label}。",
            "pinned": False,
            "in_prompt": True,
        },
        {
            "id": assistant_id,
            "role": "assistant",
            "content": f"""📍 {label}地点 | ⏱️ 清晨 / 晴
🎯 [主线] 任务名称：{label}主线
📌 当前场景：{label}场景已载入。
➡️ 下一目标：确认{label}
👤 用户：测试用户
🎭 测试角色 | 💝 {affinity}%
💭 内心想法：这是{label}的独立历史。
👗 穿着：测试服
🧍 当前姿势：站立
💬 对白：\"已切换到{label}。\"（预期影响：验证切换）
### 场景旁白
{label}旁白。
💡 行动建议
- {label}建议
""",
            "pinned": False,
            "in_prompt": True,
        },
    ]


SECOND_SAVE_MESSAGES = _variant_messages(
    user_id="33333333-3333-4333-8333-333333333333",
    assistant_id="44444444-4444-4444-8444-444444444444",
    label="切换存档",
    affinity=27,
)
SECOND_PROJECT_MESSAGES = _variant_messages(
    user_id="55555555-5555-4555-8555-555555555555",
    assistant_id="66666666-6666-4666-8666-666666666666",
    label="切换项目",
    affinity=73,
)


def _seed_command(messages: list[dict], characters_state: dict):
    def seed(session: dict, context) -> bool:
        del context
        session["current_model"] = MODEL_NAME
        session["characters_state"] = deepcopy(characters_state)
        session["message_history"] = deepcopy(messages)
        session["relationship_edges"] = []
        return True

    return seed


DEFAULT_CHARACTER_STATE = {
    "alpha": {"name": "阿尔法", "mood": "警觉", "affinity": 12},
    "beta": {"name": "贝塔", "mood": "坚定", "affinity": 88},
    "test_character": {"name": "测试角色", "mood": "平静", "affinity": 40},
}


async def seed_isolated_library() -> None:
    if os.getenv("TAVERN_E2E_SEED") != "1":
        raise RuntimeError("拒绝在未标记的环境中创建 E2E 数据")
    seeds = (
        (
            DEFAULT_PROJECT,
            SECOND_SAVE,
            SECOND_SAVE_MESSAGES,
            {"test_character": {"name": "测试角色", "mood": "平静", "affinity": 27}},
        ),
        (DEFAULT_PROJECT, DEFAULT_SAVE, MESSAGES, DEFAULT_CHARACTER_STATE),
        (
            SECOND_PROJECT,
            DEFAULT_SAVE,
            SECOND_PROJECT_MESSAGES,
            {"test_character": {"name": "测试角色", "mood": "平静", "affinity": 73}},
        ),
    )
    for project in {project for project, _save, _messages, _state in seeds}:
        ensure_project(project)
        save_character(project, "test_character", {
            "id": "test_character",
            "name": "测试角色",
            "active": True,
            "initial_stats": {"affinity": 40, "mood": "平静", "posture": "站立"},
            "appearance": {"outfit": "测试服"},
        })

    for project, save, messages, characters_state in seeds:
        result = await mutate_session(
            project,
            save,
            0,
            _seed_command(messages, characters_state),
        )
        if result.session["revision"] != 1:
            raise RuntimeError(f"E2E 初始 revision 异常: {project}/{save}")


def main() -> int:
    asyncio.run(seed_isolated_library())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
