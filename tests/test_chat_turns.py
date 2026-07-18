import asyncio
from datetime import datetime
import json
import threading
from uuid import uuid4

import pytest

from core import active_turns
from core.chat_turns import TurnCoordinator, TurnStore
from core.session_manager import (
    append_history,
    get_session_store,
    mutate_session,
    new_session,
)


def _sse_json_events(body: str) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


async def _wait_terminal(client, turn_id: str, timeout: float = 3.0) -> dict:
    async def poll() -> dict:
        while True:
            response = await client.get(f"/api/chat/turns/{turn_id}")
            assert response.status_code == 200, response.text
            turn = response.json()
            if turn["status"] in {"completed", "cancelled", "failed"}:
                return turn
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), timeout=timeout)


@pytest.mark.asyncio
async def test_turn_api_accepts_pending_then_completes_and_replays(
    app_client,
    fake_ollama,
    seed_project,
):
    project = seed_project("turn_completed")
    fake_ollama.configure("normal", block_before_first=True)

    created = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": "默认存档",
        "user_input": "持久 turn 测试",
        "model": "fake-model:latest",
        "expected_revision": 0,
    })
    assert created.status_code == 202, created.text
    turn = created.json()
    turn_id = turn["turn_id"]
    assert turn["status"] == "pending"
    assert turn["accepted_revision"] == 1
    diagnostics = turn["prompt_diagnostics"]
    assert diagnostics["context_limit_source"] == "fake_model_metadata"
    assert diagnostics["estimated_prompt_tokens"] <= diagnostics["input_budget_tokens"]
    assert all(
        set(source) == {"source", "id", "estimated_tokens", "kept", "reason"}
        for source in diagnostics["sources"]
    )
    assert "持久 turn 测试" not in json.dumps(diagnostics, ensure_ascii=False)
    await asyncio.wait_for(fake_ollama.entered.wait(), timeout=1)
    assert turn["parameters"]["num_ctx"] == diagnostics["context_limit"]
    assert fake_ollama.chat_calls[-1]["num_ctx"] == diagnostics["context_limit"]

    pending_session = (await app_client.get("/api/session", params={
        "project": project,
        "save": "默认存档",
    })).json()
    assert pending_session["revision"] == 1
    assert len(pending_session["message_history"]) == 1
    pending_user = pending_session["message_history"][0]
    assert pending_user["turn_id"] == turn_id
    assert pending_user["status"] == "pending"
    assert pending_user["error"] is None
    assert pending_user["in_prompt"] is True
    assert pending_user["pinned"] is False
    assert pending_user["timestamps"]["completed_at"] is None

    fake_ollama.release.set()
    terminal = await _wait_terminal(app_client, turn_id)
    assert terminal["status"] == "completed"
    assert terminal["error"] is None
    assert terminal["session_revision"] == 2
    assert terminal["prompt_diagnostics"] == diagnostics

    stream = await app_client.get(f"/api/chat/turns/{turn_id}/events")
    assert stream.status_code == 200
    events = _sse_json_events(stream.text)
    assert [event["id"] for event in events] == list(range(1, len(events) + 1))
    assert [event["type"] for event in events].count("terminal") == 1
    assert events[-1]["type"] == "terminal"
    assert events[-1]["status"] == "completed"
    parsed_event = next(event for event in events if event["type"] == "parsed")
    assert set(parsed_event) == {
        "id",
        "created_at",
        "type",
        "parsed",
        "roleplay_warnings",
        "revision",
        "session_delta",
    }
    assert "session" not in parsed_event
    assert set(parsed_event["session_delta"]) == {
        "scene_meta",
        "characters_state",
        "roleplay_policy",
        "current_model",
    }

    replay = await app_client.get(
        f"/api/chat/turns/{turn_id}/events",
        headers={"Last-Event-ID": "2"},
    )
    replay_events = _sse_json_events(replay.text)
    assert [event["id"] for event in replay_events] == list(
        range(3, len(events) + 1)
    )

    session = (await app_client.get("/api/session", params={
        "project": project,
        "save": "默认存档",
    })).json()
    assert session["revision"] == 2
    assert (
        parsed_event["revision"]
        == terminal["session_revision"]
        == session["revision"]
    )
    assert parsed_event["session_delta"] == {
        "scene_meta": session["scene_meta"],
        "characters_state": session["characters_state"],
        "roleplay_policy": session["roleplay_policy"],
        "current_model": session["current_model"],
    }
    assert "message_history" not in parsed_event["session_delta"]
    assert "summaries" not in parsed_event["session_delta"]
    assert [message["role"] for message in session["message_history"]] == [
        "user",
        "assistant",
    ]
    for message in session["message_history"]:
        assert message["turn_id"] == turn_id
        assert message["status"] == "completed"
        assert message["error"] is None
        assert message["timestamps"]["created_at"]
        assert message["timestamps"]["completed_at"]
        assert message["pinned"] is False


@pytest.mark.parametrize("legacy_scene_meta", ["missing", "null", "string"])
@pytest.mark.asyncio
async def test_turn_normalizes_legacy_scene_meta_before_parsed_delta(
    app_client,
    fake_ollama,
    seed_project,
    legacy_scene_meta,
):
    project = seed_project(f"turn_legacy_scene_{legacy_scene_meta}")
    legacy = new_session(project, "默认存档")
    if legacy_scene_meta == "missing":
        legacy.pop("scene_meta")
    elif legacy_scene_meta == "null":
        legacy["scene_meta"] = None
    else:
        legacy["scene_meta"] = "旧版无效场景"
    path = get_session_store().session_path(project, "默认存档")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    fake_ollama.events = [
        {"type": "content", "content": "无结构正文"},
        {"type": "done", "content": ""},
    ]
    created = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": "默认存档",
        "user_input": "验证旧存档场景字段",
        "model": "fake-model:latest",
        "expected_revision": 0,
    })
    assert created.status_code == 202, created.text
    terminal = await _wait_terminal(app_client, created.json()["turn_id"])
    assert terminal["status"] == "completed"
    assert terminal["session_revision"] == 2

    stream = await app_client.get(
        f"/api/chat/turns/{created.json()['turn_id']}/events"
    )
    events = _sse_json_events(stream.text)
    parsed_event = next(event for event in events if event["type"] == "parsed")
    assert parsed_event["revision"] == 2
    assert parsed_event["session_delta"]["scene_meta"] == {
        "location": "",
        "time": "",
        "weather": "",
        "main_quest": "",
        "current_scene": "",
        "next_goal": "",
    }
    assert events[-1]["type"] == "terminal"
    assert events[-1]["status"] == "completed"


@pytest.mark.asyncio
async def test_cancel_before_first_chunk_keeps_user_and_one_terminal(
    app_client,
    fake_ollama,
    seed_project,
):
    project = seed_project("turn_cancel_empty")
    fake_ollama.configure("normal", block_before_first=True)
    created = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": "默认存档",
        "user_input": "立即取消",
        "model": "fake-model:latest",
        "expected_revision": 0,
    })
    turn_id = created.json()["turn_id"]
    await asyncio.wait_for(fake_ollama.entered.wait(), timeout=1)

    cancelled = await app_client.post(f"/api/chat/turns/{turn_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    repeated = await app_client.post(f"/api/chat/turns/{turn_id}/cancel")
    assert repeated.json()["status"] == "cancelled"

    session = (await app_client.get("/api/session", params={
        "project": project,
        "save": "默认存档",
    })).json()
    assert [message["role"] for message in session["message_history"]] == ["user"]
    assert session["message_history"][0]["status"] == "cancelled"
    assert session["message_history"][0]["error"]["code"] == "cancelled"
    events = _sse_json_events(
        (await app_client.get(f"/api/chat/turns/{turn_id}/events")).text
    )
    assert sum(event["type"] == "terminal" for event in events) == 1


@pytest.mark.asyncio
async def test_cancel_after_content_preserves_partial_assistant(
    app_client,
    fake_ollama,
    seed_project,
):
    project = seed_project("turn_cancel_partial")
    fake_ollama.configure("normal", pause_after=2)
    created = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": "默认存档",
        "user_input": "部分取消",
        "model": "fake-model:latest",
        "expected_revision": 0,
    })
    turn_id = created.json()["turn_id"]
    await asyncio.wait_for(fake_ollama.paused.wait(), timeout=1)

    cancelled = await app_client.post(f"/api/chat/turns/{turn_id}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    session = (await app_client.get("/api/session", params={
        "project": project,
        "save": "默认存档",
    })).json()
    assert [message["role"] for message in session["message_history"]] == [
        "user",
        "assistant",
    ]
    assistant = session["message_history"][-1]
    assert assistant["status"] == "cancelled"
    assert assistant["in_prompt"] is False
    assert "隔离测试响应" in assistant["content"]
    assert assistant["thinking"] == "测试思考"


@pytest.mark.asyncio
async def test_same_save_rejects_second_turn_while_other_save_can_start(
    app_client,
    fake_ollama,
    seed_project,
):
    project = seed_project("turn_uniqueness")
    store = get_session_store()
    other = await store.create(project, "其他存档", new_session(project, "其他存档"))
    fake_ollama.configure("normal", block_before_first=True)

    first = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": "默认存档",
        "user_input": "第一个",
        "model": "fake-model:latest",
        "expected_revision": 0,
    })
    assert first.status_code == 202
    duplicate = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": "默认存档",
        "user_input": "第二个",
        "model": "fake-model:latest",
        "expected_revision": 1,
    })
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "active_turn_conflict"

    parallel = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": "其他存档",
        "user_input": "异档并行",
        "model": "fake-model:latest",
        "expected_revision": other["revision"],
    })
    assert parallel.status_code == 202, parallel.text
    await asyncio.gather(
        app_client.post(f"/api/chat/turns/{first.json()['turn_id']}/cancel"),
        app_client.post(f"/api/chat/turns/{parallel.json()['turn_id']}/cancel"),
    )


@pytest.mark.asyncio
async def test_revision_conflict_preserves_foreign_change_and_fails_turn(
    app_client,
    fake_ollama,
    seed_project,
):
    project = seed_project("turn_revision_conflict")
    fake_ollama.configure("normal", block_before_first=True)
    created = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": "默认存档",
        "user_input": "冲突测试",
        "model": "fake-model:latest",
        "expected_revision": 0,
    })
    turn_id = created.json()["turn_id"]
    await asyncio.wait_for(fake_ollama.entered.wait(), timeout=1)

    def foreign_change(session: dict, context) -> None:
        del context
        session["conflict_marker"] = "preserved"

    with active_turns.turn_write_context(turn_id):
        await mutate_session(project, "默认存档", 1, foreign_change)
    fake_ollama.release.set()
    terminal = await _wait_terminal(app_client, turn_id)
    assert terminal["status"] == "failed"
    assert terminal["error"]["code"] == "revision_conflict"
    session = (await app_client.get("/api/session", params={
        "project": project,
        "save": "默认存档",
    })).json()
    assert session["conflict_marker"] == "preserved"
    assert session["message_history"][0]["status"] == "failed"
    assert session["message_history"][-1]["role"] == "assistant"
    assert session["message_history"][-1]["in_prompt"] is False


@pytest.mark.asyncio
async def test_restart_recovery_is_idempotent_and_preserves_partial(
    seed_project,
    isolated_paths,
):
    project = seed_project("turn_restart")
    save = "默认存档"
    turn_id = str(uuid4())
    created_at = datetime.now().astimezone().isoformat()
    turn_store = TurnStore(isolated_paths["data"] / ".restart-turns")
    record = turn_store.create({
        "schema_version": 1,
        "turn_id": turn_id,
        "project": project,
        "save": save,
        "expected_revision": 0,
        "user_input": "重启恢复",
        "model": "fake-model:latest",
        "parameters": {},
        "status": "pending",
        "created_at": created_at,
        "updated_at": created_at,
        "started_at": None,
        "completed_at": None,
        "cancel_requested_at": None,
        "content": "",
        "thinking": "",
        "error": None,
    })
    acceptance = await get_session_store().accept_chat_turn(
        project,
        save,
        0,
        turn_id=turn_id,
        user_input="重启恢复",
        created_at=created_at,
        initial_session=new_session(project, save),
    )
    record["accepted_revision"] = acceptance.session["revision"]
    record["user_message_id"] = acceptance.value
    record = turn_store.update(record)
    record, _ = turn_store.append_event(record, {"type": "started"})
    record, _ = turn_store.append_event(record, {
        "type": "content",
        "content": "已生成部分",
    })
    active_turns.unregister(project, save, turn_id)

    coordinator = TurnCoordinator(turn_store)
    assert await coordinator.ensure_recovered() == 1
    assert await coordinator.ensure_recovered() == 0
    turn = await coordinator.get(turn_id)
    assert turn["status"] == "failed"
    assert turn["error"]["code"] == "server_restarted"
    session = get_session_store().read_sync(project, save)
    assert [message["role"] for message in session["message_history"]] == [
        "user",
        "assistant",
    ]
    assert {message["status"] for message in session["message_history"]} == {"failed"}
    assert session["message_history"][-1]["content"] == "已生成部分"


@pytest.mark.asyncio
async def test_cancel_during_final_disk_commit_has_single_completed_winner(
    app_client,
    fake_ollama,
    seed_project,
    monkeypatch,
):
    project = seed_project("turn_cancel_commit")
    store = get_session_store()
    original_write = store._write_session_sync
    commit_entered = threading.Event()
    commit_release = threading.Event()

    def blocked_write(session: dict, write_project: str, save_id: str) -> None:
        has_completed_assistant = any(
            message.get("role") == "assistant"
            and message.get("status") == "completed"
            for message in session.get("message_history", [])
        )
        if has_completed_assistant:
            commit_entered.set()
            if not commit_release.wait(timeout=3):
                raise TimeoutError("测试未释放 completed commit")
        original_write(session, write_project, save_id)

    monkeypatch.setattr(store, "_write_session_sync", blocked_write)
    fake_ollama.configure("normal")
    created = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": "默认存档",
        "user_input": "提交临界区取消",
        "model": "fake-model:latest",
        "expected_revision": 0,
    })
    turn_id = created.json()["turn_id"]
    assert await asyncio.to_thread(commit_entered.wait, 2)

    cancel_task = asyncio.create_task(
        app_client.post(f"/api/chat/turns/{turn_id}/cancel")
    )
    await asyncio.sleep(0)
    commit_release.set()
    cancelled = await asyncio.wait_for(cancel_task, timeout=3)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "completed"
    events = _sse_json_events(
        (await app_client.get(f"/api/chat/turns/{turn_id}/events")).text
    )
    assert sum(event["type"] == "terminal" for event in events) == 1
    assert events[-1]["status"] == "completed"
    session = get_session_store().read_sync(project, "默认存档")
    assert [message["status"] for message in session["message_history"]] == [
        "completed",
        "completed",
    ]


@pytest.mark.asyncio
async def test_active_turn_blocks_reset_delete_rename_restore_and_project_delete(
    app_client,
    fake_ollama,
    seed_project,
):
    project = seed_project("turn_destructive_gate")
    seed_project("turn_keep_project")
    store = get_session_store()
    await store.create(project, "保留存档", new_session(project, "保留存档"))
    snapshot = await app_client.patch(
        "/api/session",
        params={"project": project, "save": "默认存档"},
        json={"action": "snapshot", "expected_revision": 0},
    )
    assert snapshot.status_code == 200
    snapshot_name = snapshot.json()["filename"]
    backup = (await app_client.post(
        "/api/backups",
        json={"reason": "active turn gate"},
    )).json()["backup"]
    backup_dry_run = await app_client.post(
        f"/api/backups/{backup['backup_id']}/dry-run"
    )
    assert backup_dry_run.status_code == 200

    fake_ollama.configure("normal", block_before_first=True)
    created = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": "默认存档",
        "user_input": "门禁测试",
        "model": "fake-model:latest",
        "expected_revision": 0,
    })
    turn_id = created.json()["turn_id"]
    await asyncio.wait_for(fake_ollama.entered.wait(), timeout=1)

    responses = [
        await app_client.post("/api/session/reset", json={
            "project": project,
            "save": "默认存档",
            "expected_revision": 1,
        }),
        await app_client.post("/api/sessions/rename", json={
            "project": project,
            "save": "默认存档",
            "new_name": "重命名目标",
            "expected_revision": 1,
        }),
        await app_client.post("/api/sessions/delete", json={
            "project": project,
            "save": "默认存档",
            "expected_revision": 1,
        }),
        await app_client.post("/api/session/restore", json={
            "project": project,
            "save": "默认存档",
            "filename": snapshot_name,
            "expected_revision": 1,
        }),
        await app_client.request(
            "DELETE",
            "/api/projects",
            json={"name": project},
        ),
        await app_client.post(
            f"/api/backups/{backup['backup_id']}/restore",
            json={
                "expected_current_fingerprint": backup_dry_run.json()[
                    "current_fingerprint"
                ],
                "confirm_conflicts": False,
            },
        ),
    ]
    for response in responses:
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "active_turn_conflict"

    await app_client.post(f"/api/chat/turns/{turn_id}/cancel")


@pytest.mark.asyncio
async def test_base_turn_commits_before_stateful_summary_finishes(
    app_client,
    fake_ollama,
    seed_project,
    monkeypatch,
):
    project = seed_project("turn_summary_async")
    initial = new_session(project, "长对话")
    initial["current_model"] = "fake-model:latest"
    for index in range(40):
        append_history(
            initial,
            "user" if index % 2 == 0 else "assistant",
            f"历史-{index}",
        )
    created_session = await get_session_store().create(project, "长对话", initial)
    summary_entered = asyncio.Event()
    summary_release = asyncio.Event()

    async def blocked_summary(model, dropped_messages, num_predict=1024, temperature=0.5):
        del model, num_predict, temperature
        fake_ollama.summary_calls.append({"dropped_messages": dropped_messages})
        summary_entered.set()
        await summary_release.wait()
        return "前情提要: 异步总结完成"

    monkeypatch.setattr(fake_ollama, "summarize_once", blocked_summary)
    turn_response = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": "长对话",
        "user_input": "触发异步总结",
        "model": "fake-model:latest",
        "expected_revision": created_session["revision"],
    })
    turn_id = turn_response.json()["turn_id"]
    terminal = await _wait_terminal(app_client, turn_id)
    assert terminal["status"] == "completed"
    await asyncio.wait_for(summary_entered.wait(), timeout=1)

    pending = get_session_store().read_sync(project, "长对话")
    assert pending["summaries"][-1]["status"] == "pending"
    source_snapshot = pending["summaries"][-1]["source_snapshot_id"]
    assert (
        get_session_store().history_dir(project) / source_snapshot
    ).is_file()

    summary_release.set()

    async def wait_summary() -> dict:
        while True:
            session = get_session_store().read_sync(project, "长对话")
            if session["summaries"][-1]["status"] != "pending":
                return session
            await asyncio.sleep(0.01)

    completed = await asyncio.wait_for(wait_summary(), timeout=2)
    assert completed["summaries"][-1]["status"] == "completed"
    assert "异步总结完成" in completed["summaries"][-1]["text"]
