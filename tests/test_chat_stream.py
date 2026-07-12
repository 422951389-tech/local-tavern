import hashlib
import json
import asyncio

import pytest

from core.session_manager import append_history, get_session_store, new_session


def _sse_events(body: str) -> list[dict | str]:
    events: list[dict | str] = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        data = line[6:]
        events.append(data if data == "[DONE]" else json.loads(data))
    return events


@pytest.mark.asyncio
async def test_chat_normal_flow_uses_fake_and_persists_only_in_sandbox(
    app_client, fake_ollama, seed_project, isolated_paths
):
    project = seed_project("qa_normal")
    response = await app_client.post("/api/chat", json={
        "project": project,
        "save": "默认存档",
        "user_input": "执行隔离测试",
        "model": "fake-model:latest",
        "expected_revision": 0,
    })

    assert response.status_code == 200
    events = _sse_events(response.text)
    event_types = [event.get("type") for event in events if isinstance(event, dict)]
    assert event_types == ["thinking", "content", "parsed"]
    assert events[-1] == "[DONE]"
    assert len(fake_ollama.chat_calls) == 1

    save_path = isolated_paths["projects"] / project / "saves" / "默认存档.json"
    assert save_path.is_relative_to(isolated_paths["root"])
    session = json.loads(save_path.read_text(encoding="utf-8"))
    assert [item["role"] for item in session["message_history"][-2:]] == ["user", "assistant"]
    assert "隔离测试响应" in session["message_history"][-1]["content"]


@pytest.mark.asyncio
async def test_chat_upstream_error_does_not_mutate_the_save(
    app_client, fake_ollama, seed_project, isolated_paths
):
    project = seed_project("qa_error")
    save_path = isolated_paths["projects"] / project / "saves" / "默认存档.json"
    before = hashlib.sha256(save_path.read_bytes()).hexdigest()
    fake_ollama.configure("error")

    response = await app_client.post("/api/chat", json={
        "project": project,
        "save": "默认存档",
        "user_input": "触发错误",
        "model": "fake-model:latest",
        "expected_revision": 0,
    })

    events = _sse_events(response.text)
    assert response.status_code == 200
    assert events == [{"type": "error", "content": "fake Ollama 上游错误"}]
    assert hashlib.sha256(save_path.read_bytes()).hexdigest() == before


@pytest.mark.asyncio
async def test_chat_eof_without_done_is_visible_and_does_not_mutate_the_save(
    app_client, fake_ollama, seed_project, isolated_paths
):
    project = seed_project("qa_eof")
    save_path = isolated_paths["projects"] / project / "saves" / "默认存档.json"
    before = hashlib.sha256(save_path.read_bytes()).hexdigest()
    fake_ollama.configure("eof")

    response = await app_client.post("/api/chat", json={
        "project": project,
        "save": "默认存档",
        "user_input": "触发 EOF",
        "model": "fake-model:latest",
        "expected_revision": 0,
    })

    events = _sse_events(response.text)
    assert response.status_code == 200
    assert events == [{"type": "content", "content": "未完成的 fake 响应"}]
    assert hashlib.sha256(save_path.read_bytes()).hexdigest() == before


@pytest.mark.asyncio
async def test_chat_commit_conflict_is_reported_as_sse_and_does_not_overwrite(
    app_client, fake_ollama, seed_project
):
    project = seed_project("qa_chat_conflict")
    fake_ollama.configure("normal", delay=0.05)
    chat_task = asyncio.create_task(app_client.post("/api/chat", json={
        "project": project,
        "save": "默认存档",
        "user_input": "延迟聊天",
        "model": "fake-model:latest",
        "expected_revision": 0,
    }))
    await asyncio.sleep(0.01)
    switch = await app_client.post("/api/model/switch", json={
        "project": project,
        "save": "默认存档",
        "model": "concurrent-model",
        "expected_revision": 0,
    })
    assert switch.status_code == 200
    response = await chat_task
    events = _sse_events(response.text)
    conflicts = [
        event for event in events
        if isinstance(event, dict) and event.get("type") == "conflict"
    ]
    assert conflicts == [{
        "type": "conflict",
        "expected_revision": 0,
        "current_revision": 1,
    }]
    session = (await app_client.get(
        f"/api/session?project={project}&save=默认存档"
    )).json()
    assert session["revision"] == 1
    assert session["current_model"] == "concurrent-model"
    assert session["message_history"] == []


@pytest.mark.asyncio
async def test_one_hundred_edit_pin_chat_reset_races_are_explicit(
    app_client, fake_ollama, seed_project
):
    project = seed_project("qa_api_races")
    store = get_session_store()
    cases = []
    for number in range(100):
        save = f"race_{number:03d}"
        initial = new_session(project, save)
        initial["current_model"] = "fake-model:latest"
        first = append_history(initial, "user", f"first-{number}")
        second = append_history(initial, "assistant", f"second-{number}")
        created = await store.create(project, save, initial)
        cases.append((save, first["id"], second["id"], created["revision"]))

    fake_ollama.configure("normal")

    async def run_race(case):
        save, first_id, second_id, revision = case
        session_url = f"/api/session?project={project}&save={save}"
        requests = {
            "edit": app_client.patch(session_url, json={
                "action": "edit",
                "message_id": first_id,
                "content": f"edited-{save}",
                "expected_revision": revision,
            }),
            "pin": app_client.patch(session_url, json={
                "action": "toggle_pinned",
                "message_id": second_id,
                "expected_revision": revision,
            }),
            "chat": app_client.post("/api/chat", json={
                "project": project,
                "save": save,
                "user_input": f"chat-{save}",
                "model": "fake-model:latest",
                "expected_revision": revision,
            }),
            "reset": app_client.post("/api/session/reset", json={
                "project": project,
                "save": save,
                "expected_revision": revision,
            }),
        }
        responses = dict(zip(
            requests,
            await asyncio.gather(*requests.values()),
        ))
        successes = []
        conflicts = []
        for name, response in responses.items():
            if response.status_code == 409:
                conflicts.append(name)
                continue
            assert response.status_code == 200
            if name == "chat":
                events = _sse_events(response.text)
                if any(
                    isinstance(event, dict) and event.get("type") == "conflict"
                    for event in events
                ):
                    conflicts.append(name)
                    continue
                assert events[-1] == "[DONE]"
            successes.append(name)

        assert len(successes) == 1
        assert len(conflicts) == 3
        final = (await app_client.get(session_url)).json()
        assert final["revision"] == revision + 1
        ids = [message["id"] for message in final["message_history"]]
        assert len(ids) == len(set(ids))
        winner = successes[0]
        if winner == "edit":
            assert final["message_history"][0]["content"] == f"edited-{save}"
        elif winner == "pin":
            assert final["message_history"][1]["pinned"] is True
        elif winner == "chat":
            assert any(message["content"] == f"chat-{save}" for message in final["message_history"])
        else:
            assert winner == "reset"
            assert final["message_history"] == []

    await asyncio.gather(*(run_race(case) for case in cases))
