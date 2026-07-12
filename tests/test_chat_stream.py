import hashlib
import json

import pytest


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
    })

    events = _sse_events(response.text)
    assert response.status_code == 200
    assert events == [{"type": "content", "content": "未完成的 fake 响应"}]
    assert hashlib.sha256(save_path.read_bytes()).hexdigest() == before
