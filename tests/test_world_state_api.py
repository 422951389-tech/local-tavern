from __future__ import annotations

from uuid import uuid4

import pytest

from core.character_loader import ensure_project, save_worldbook
from core.session_manager import create_session, new_session


@pytest.mark.asyncio
async def test_world_state_api_discovers_records_and_reverses_changes(app_client):
    project = "world_state_api"
    save = "story"
    ensure_project(project)
    save_worldbook(project, "glass_palace", {
        "id": "glass_palace",
        "title": "琉璃宫",
        "visibility": "discovered",
        "content": "帝都权力中心。",
    })
    message_id = str(uuid4())
    initial = new_session(project, save)
    initial["message_history"] = [{
        "id": message_id,
        "role": "assistant",
        "content": "守卫交出了通行令。",
    }]
    created = await create_session(project, save, initial)

    discovered = await app_client.put(
        "/api/session/world-state/discoveries",
        json={
            "project": project,
            "save": save,
            "expected_revision": created["revision"],
            "entry_ids": ["glass_palace"],
        },
    )
    assert discovered.status_code == 200, discovered.text
    session = discovered.json()["session"]
    assert session["world_state"]["discovered_entry_ids"] == ["glass_palace"]

    added = await app_client.post(
        "/api/session/world-state/changes",
        json={
            "project": project,
            "save": save,
            "expected_revision": session["revision"],
            "category": "faction",
            "title": "花园守卫转向",
            "detail": "守卫开始协助主角。",
            "related_entry_ids": ["glass_palace"],
            "evidence_message_ids": [message_id],
        },
    )
    assert added.status_code == 200, added.text
    session = added.json()["session"]
    change = session["world_state"]["changes"][0]
    assert change["evidence_message_ids"] == [message_id]

    updated = await app_client.patch(
        f"/api/session/world-state/changes/{change['id']}",
        json={
            "project": project,
            "save": save,
            "expected_revision": session["revision"],
            "category": "event",
            "title": "花园守卫恢复中立",
            "detail": "旧变化已被剧情推翻。",
            "status": "retconned",
            "related_entry_ids": ["glass_palace"],
            "evidence_message_ids": [message_id],
        },
    )
    assert updated.status_code == 200, updated.text
    session = updated.json()["session"]
    change = session["world_state"]["changes"][0]
    assert change["title"] == "花园守卫恢复中立"
    assert change["status"] == "retconned"

    undiscovered = await app_client.put(
        "/api/session/world-state/discoveries",
        json={
            "project": project,
            "save": save,
            "expected_revision": session["revision"],
            "entry_ids": [],
        },
    )
    assert undiscovered.status_code == 200, undiscovered.text
    session = undiscovered.json()["session"]
    assert session["world_state"]["discovered_entry_ids"] == []

    removed = await app_client.request(
        "DELETE",
        f"/api/session/world-state/changes/{change['id']}",
        json={
            "project": project,
            "save": save,
            "expected_revision": session["revision"],
        },
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["session"]["world_state"]["changes"] == []


@pytest.mark.asyncio
async def test_world_state_api_rejects_unknown_evidence_and_stale_revision(app_client):
    project = "world_state_reject"
    save = "story"
    ensure_project(project)
    created = await create_session(project, save, new_session(project, save))

    unknown = await app_client.post(
        "/api/session/world-state/changes",
        json={
            "project": project,
            "save": save,
            "expected_revision": created["revision"],
            "category": "event",
            "title": "无来源事件",
            "evidence_message_ids": [str(uuid4())],
        },
    )
    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["error"]["code"] == "world_state_invalid"

    stale = await app_client.post(
        "/api/session/world-state/changes",
        json={
            "project": project,
            "save": save,
            "expected_revision": created["revision"] + 1,
            "category": "event",
            "title": "版本冲突",
        },
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "revision_conflict"
