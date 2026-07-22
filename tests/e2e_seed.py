"""在隔离目录中创建浏览器 E2E 所需的最小项目与存档。"""
from __future__ import annotations

import asyncio
import os
from copy import deepcopy

from core.character_loader import ensure_project
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
        "content": "贝塔收下密信，并答应共同守护城门。",
        "pinned": False,
        "in_prompt": True,
    },
]


def _seed(session: dict, context) -> bool:
    del context
    session["current_model"] = MODEL_NAME
    session["characters_state"] = {
        "alpha": {"name": "阿尔法", "mood": "警觉", "affinity": 12},
        "beta": {"name": "贝塔", "mood": "坚定", "affinity": 88},
    }
    session["message_history"] = deepcopy(MESSAGES)
    session["relationship_edges"] = []
    return True


async def seed_isolated_library() -> None:
    if os.getenv("TAVERN_E2E_SEED") != "1":
        raise RuntimeError("拒绝在未标记的环境中创建 E2E 数据")
    for project, save in (
        (DEFAULT_PROJECT, SECOND_SAVE),
        (DEFAULT_PROJECT, DEFAULT_SAVE),
        (SECOND_PROJECT, DEFAULT_SAVE),
    ):
        ensure_project(project)
        result = await mutate_session(project, save, 0, _seed)
        if result.session["revision"] != 1:
            raise RuntimeError(f"E2E 初始 revision 异常: {project}/{save}")


def main() -> int:
    asyncio.run(seed_isolated_library())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
