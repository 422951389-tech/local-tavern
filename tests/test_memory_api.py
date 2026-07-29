import asyncio
import json

import pytest

from core import active_turns


@pytest.mark.asyncio
async def test_memory_note_crud_is_revision_guarded(app_client, seed_project):
    project = seed_project("memory_note_crud")
    created = await app_client.post("/api/memory-notes", json={
        "project": project,
        "save": "默认存档",
        "expected_revision": 0,
        "text": "她不喜欢下雨天。",
        "facts": ["雨天会影响心情"],
    })
    assert created.status_code == 200, created.text
    body = created.json()
    note = body["note"]
    assert note["kind"] == "memory_note"
    assert note["status"] == "completed"
    assert body["session"]["revision"] == 1

    listed = await app_client.get("/api/memory-notes", params={
        "project": project,
        "save": "默认存档",
    })
    assert listed.status_code == 200
    listed_note = listed.json()["notes"][0]
    assert {key: listed_note[key] for key in note} == note
    assert listed_note["source_status"] == "unlinked"

    stale = await app_client.patch(
        f"/api/memory-notes/{note['id']}",
        json={
            "project": project,
            "save": "默认存档",
            "expected_revision": 0,
            "text": "不应覆盖",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "revision_conflict"

    edited = await app_client.patch(
        f"/api/memory-notes/{note['id']}",
        json={
            "project": project,
            "save": "默认存档",
            "expected_revision": 1,
            "text": "她喜欢雨声，但讨厌被淋湿。",
            "facts": ["喜欢室内听雨"],
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["session"]["revision"] == 2
    assert edited.json()["note"]["text"] == "她喜欢雨声，但讨厌被淋湿。"

    deleted = await app_client.request(
        "DELETE",
        f"/api/memory-notes/{note['id']}",
        json={
            "project": project,
            "save": "默认存档",
            "expected_revision": 2,
        },
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["session"]["revision"] == 3
    assert deleted.json()["session"]["summaries"] == []


@pytest.mark.asyncio
async def test_memory_note_is_injected_as_named_prompt_source(
    app_client,
    fake_ollama,
    seed_project,
):
    project = seed_project("memory_note_prompt")
    marker = "MEMORY-NOTE-UNIQUE-MARKER"
    created = await app_client.post("/api/memory-notes", json={
        "project": project,
        "save": "默认存档",
        "expected_revision": 0,
        "text": marker,
    })
    assert created.status_code == 200, created.text
    note_id = created.json()["note"]["id"]

    turn = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": "默认存档",
        "user_input": "请回忆长期信息",
        "model": "fake-model:latest",
        "expected_revision": 1,
    })
    assert turn.status_code == 202, turn.text
    await asyncio.wait_for(fake_ollama.entered.wait(), timeout=1)
    prompt = json.dumps(fake_ollama.chat_calls[-1]["messages"], ensure_ascii=False)
    assert marker in prompt
    sources = turn.json()["prompt_diagnostics"]["sources"]
    assert any(
        item["source"] == "summary"
        and item["id"] == note_id
        and item["kept"] is True
        for item in sources
    )
    cancelled = await app_client.post(
        f"/api/chat/turns/{turn.json()['turn_id']}/cancel"
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert active_turns.active_turn_id(project, "默认存档") is None
