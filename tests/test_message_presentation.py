import pytest

from core.session_manager import append_history, get_session_store, new_session


@pytest.mark.asyncio
async def test_editing_assistant_message_rebuilds_persisted_presentation(
    app_client,
    seed_project,
):
    project = seed_project("message_presentation_edit")
    save = "展示存档"
    initial = new_session(project, save)
    message = append_history(initial, "assistant", "旧的普通回复")
    created = await get_session_store().create(project, save, initial)
    content = """📍 山门 | ⏱️ 清晨
🎯 [主线] 任务名称：拜访守门人
📌 当前场景：石阶前
➡️ 下一目标：说明来意
🎭 守门人 | 💝 55%
💬 对白："来者何人？"
### 场景旁白
晨雾沿石阶散开。
💡 行动建议
- 自报姓名
"""

    response = await app_client.patch(
        "/api/session",
        params={"project": project, "save": save},
        json={
            "action": "edit",
            "message_id": message["id"],
            "content": content,
            "expected_revision": created["revision"],
        },
    )

    assert response.status_code == 200, response.text
    edited = response.json()["message_history"][0]
    assert edited["content"] == content
    assert edited["presentation"]["scene_meta"]["main_quest"] == "拜访守门人"
    assert edited["presentation"]["characters"][0]["affinity"] == 55
    assert edited["presentation"]["narration"] == "晨雾沿石阶散开。"
    assert "raw" not in edited["presentation"]
