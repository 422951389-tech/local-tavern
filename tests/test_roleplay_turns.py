"""ROLE-1：冻结发言上下文、受控写回、倒计时与完成态权威性。"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
from uuid import uuid4

import pytest

from core import active_turns
from core.chat_turns import TurnCoordinator, TurnStore, get_turn_coordinator
from core.response_parser import parse_response
from core.roleplay_policy import build_roleplay_context
from core.session_manager import append_history, get_session_store, mutate_session
from routes.chat import _apply_parsed_state


SAVE = "默认存档"
MODEL = "fake-model:latest"


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


def _events(response_text: str) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


async def _seed_roleplay_session(
    project: str,
    *,
    silent_turns: int,
    strict: bool = False,
) -> dict:
    store = get_session_store()
    current = store.read_sync(project, SAVE)

    def seed(session: dict, context) -> None:
        del context
        session["current_model"] = MODEL
        session["characters_state"] = {
            "test_character": {
                "name": "测试角色",
                "affinity": 40,
                "mood": "平静",
                "outfit": "测试服",
                "posture": "站立",
                "remaining_silent_turns": silent_turns,
            }
        }
        session["roleplay_policy"] = {"strict_muted_writeback": strict}

    return (
        await mutate_session(project, SAVE, current["revision"], seed)
    ).session


def test_parse_response_always_returns_empty_roleplay_warning_contract():
    assert parse_response("")["roleplay_warnings"] == []
    assert parse_response("无结构正文")["roleplay_warnings"] == []


def test_identity_resolution_and_loose_muted_writeback_are_safe_and_deterministic():
    characters = [
        {"id": "alpha", "name": "阿尔法", "aliases": ["共享别名"]},
        {"id": "beta", "name": "贝塔", "aliases": ["共享别名"]},
        {"id": "muted", "name": "静默者", "aliases": ["小静"]},
    ]
    session = {
        "scene_meta": {"location": "旧地点", "time": "", "weather": ""},
        "characters_state": {
            "alpha": {"name": "阿尔法现名", "affinity": 0},
            "beta": {"name": "贝塔", "affinity": 0},
            "muted": {
                "name": "静默者",
                "affinity": 0,
                "remaining_silent_turns": 1,
            },
        },
    }
    context = build_roleplay_context(
        characters,
        session["characters_state"],
        {"strict_muted_writeback": False},
    )
    parsed = {
        "scene_meta": {"location": "新地点"},
        "characters": [
            {"name": "alpha", "affinity": 20, "dialogue": "正常"},
            {"name": "小静", "affinity": 20, "dialogue": "违规但宽松写回"},
            {"name": "共享别名", "affinity": 20},
            {"name": "不得出现在警告里的模型原名", "affinity": 20},
        ],
        "roleplay_warnings": [],
    }

    warnings = _apply_parsed_state(session, parsed, context)

    assert session["scene_meta"]["location"] == "新地点"
    assert session["characters_state"]["alpha"]["affinity"] == 10
    assert session["characters_state"]["muted"]["affinity"] == 10
    assert session["characters_state"]["beta"]["affinity"] == 0
    assert warnings == [
        {
            "code": "muted_character_output",
            "action": "writeback_applied",
            "character_id": "muted",
        },
        {
            "code": "ambiguous_character_identity",
            "action": "unresolved_skipped",
            "candidate_ids": ["alpha", "beta"],
        },
        {
            "code": "unknown_character_identity",
            "action": "unresolved_skipped",
        },
    ]
    assert parsed["roleplay_warnings"] == warnings
    assert "不得出现在警告里的模型原名" not in json.dumps(
        warnings,
        ensure_ascii=False,
    )


def test_strict_muted_writeback_skips_whole_character_but_keeps_scene_and_others():
    characters = [
        {"id": "muted", "name": "静默者"},
        {"id": "speaker", "name": "发言者"},
    ]
    session = {
        "scene_meta": {"location": "旧地点", "time": "", "weather": ""},
        "characters_state": {
            "muted": {
                "name": "静默者",
                "affinity": 5,
                "remaining_silent_turns": 1,
            },
            "speaker": {
                "name": "发言者",
                "affinity": 5,
                "remaining_silent_turns": 0,
            },
        },
    }
    context = build_roleplay_context(
        characters,
        session["characters_state"],
        {"strict_muted_writeback": True},
    )
    parsed = {
        "scene_meta": {"location": "新地点"},
        "characters": [
            {
                "name": "静默者",
                "affinity": 30,
                "dialogue": "必须整份跳过",
                "outfit": "不得写回",
            },
            {"name": "speaker", "affinity": 30, "dialogue": "正常写回"},
        ],
    }

    warnings = _apply_parsed_state(session, parsed, context)

    assert session["scene_meta"]["location"] == "新地点"
    assert session["characters_state"]["muted"] == {
        "name": "静默者",
        "affinity": 5,
        "remaining_silent_turns": 1,
    }
    assert session["characters_state"]["speaker"]["affinity"] == 15
    assert session["characters_state"]["speaker"]["dialogue"] == "正常写回"
    assert warnings == [{
        "code": "muted_character_output",
        "action": "writeback_skipped",
        "character_id": "muted",
    }]


@pytest.mark.asyncio
async def test_completed_chat_decrements_once_and_regenerate_never_decrements(
    app_client,
    fake_ollama,
    seed_project,
):
    project = seed_project("roleplay_turn_kind")
    seeded = await _seed_roleplay_session(project, silent_turns=2)

    created = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": SAVE,
        "user_input": "普通聊天",
        "model": MODEL,
        "expected_revision": seeded["revision"],
    })
    assert created.status_code == 202, created.text
    assert created.json()["turn_kind"] == "chat"
    terminal = await _wait_terminal(app_client, created.json()["turn_id"])
    assert terminal["status"] == "completed"

    after_chat = get_session_store().read_sync(project, SAVE)
    assert after_chat["characters_state"]["test_character"][
        "remaining_silent_turns"
    ] == 1
    assistant = after_chat["message_history"][-1]
    assert assistant["turn_kind"] == "chat"
    assert assistant["roleplay_warnings"] == [{
        "code": "muted_character_output",
        "action": "writeback_applied",
        "character_id": "test_character",
    }]
    chat_events = _events(
        (await app_client.get(
            f"/api/chat/turns/{created.json()['turn_id']}/events"
        )).text
    )
    parsed_event = next(event for event in chat_events if event["type"] == "parsed")
    assert parsed_event["roleplay_warnings"] == assistant["roleplay_warnings"]
    assert parsed_event["parsed"]["roleplay_warnings"] == assistant[
        "roleplay_warnings"
    ]
    prompt_text = "\n".join(
        message["content"] for message in fake_ollama.chat_calls[-1]["messages"]
    )
    assert '"remaining_silent_turns":2' in prompt_text
    assert '"may_speak":false' in prompt_text

    regenerated = await app_client.post("/api/chat/turns/regenerate", json={
        "project": project,
        "save": SAVE,
        "message_id": assistant["id"],
        "model": MODEL,
        "expected_revision": after_chat["revision"],
    })
    assert regenerated.status_code == 202, regenerated.text
    assert regenerated.json()["turn_kind"] == "regenerate"
    regenerated_terminal = await _wait_terminal(
        app_client,
        regenerated.json()["turn_id"],
    )
    assert regenerated_terminal["status"] == "completed"
    after_regenerate = get_session_store().read_sync(project, SAVE)
    assert after_regenerate["characters_state"]["test_character"][
        "remaining_silent_turns"
    ] == 1
    assert after_regenerate["message_history"][-1]["turn_kind"] == "regenerate"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["cancelled", "failed"])
async def test_non_completed_turn_never_decrements_silence_counter(
    app_client,
    fake_ollama,
    seed_project,
    outcome,
):
    project = seed_project(f"roleplay_non_completed_{outcome}")
    seeded = await _seed_roleplay_session(project, silent_turns=2)
    if outcome == "cancelled":
        fake_ollama.configure("normal", block_before_first=True)
    else:
        fake_ollama.configure("error")

    created = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": SAVE,
        "user_input": "不应递减",
        "model": MODEL,
        "expected_revision": seeded["revision"],
    })
    turn_id = created.json()["turn_id"]
    if outcome == "cancelled":
        await asyncio.wait_for(fake_ollama.entered.wait(), timeout=1)
        cancelled = await app_client.post(f"/api/chat/turns/{turn_id}/cancel")
        assert cancelled.json()["status"] == "cancelled"
    terminal = await _wait_terminal(app_client, turn_id)
    assert terminal["status"] == outcome
    session = get_session_store().read_sync(project, SAVE)
    assert session["characters_state"]["test_character"][
        "remaining_silent_turns"
    ] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_event_type", ["parsed", "terminal"])
async def test_completed_session_remains_authoritative_when_event_logging_fails(
    app_client,
    fake_ollama,
    seed_project,
    monkeypatch,
    failed_event_type,
):
    project = seed_project(f"roleplay_event_failure_{failed_event_type}")
    seeded = await _seed_roleplay_session(project, silent_turns=2)
    coordinator = get_turn_coordinator()
    original_append_event = coordinator.store.append_event
    failed = False

    def fail_once(turn: dict, event: dict):
        nonlocal failed
        if not failed and event.get("type") == failed_event_type:
            failed = True
            raise OSError(f"injected {failed_event_type} event failure")
        return original_append_event(turn, event)

    monkeypatch.setattr(coordinator.store, "append_event", fail_once)
    created = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": SAVE,
        "user_input": "完成态事件故障",
        "model": MODEL,
        "expected_revision": seeded["revision"],
    })
    assert created.status_code == 202, created.text
    turn_id = created.json()["turn_id"]

    terminal = await _wait_terminal(app_client, turn_id)
    assert failed is True
    assert terminal["status"] == "completed"
    assert terminal["error"] is None
    assert terminal["session_committed_revision"] == terminal["session_revision"]
    session = get_session_store().read_sync(project, SAVE)
    assert session["characters_state"]["test_character"][
        "remaining_silent_turns"
    ] == 1
    assert [message["status"] for message in session["message_history"]] == [
        "completed",
        "completed",
    ]
    replay = _events(
        (await app_client.get(f"/api/chat/turns/{turn_id}/events")).text
    )
    assert replay[-1]["type"] == "terminal"
    assert replay[-1]["status"] == "completed"
    assert sum(event["type"] == "terminal" for event in replay) == 1


@pytest.mark.asyncio
async def test_permanent_terminal_log_failure_falls_back_to_completed_metadata(
    app_client,
    fake_ollama,
    seed_project,
    monkeypatch,
):
    project = seed_project("roleplay_permanent_terminal_failure")
    seeded = await _seed_roleplay_session(project, silent_turns=2)
    coordinator = get_turn_coordinator()
    original_append_event = coordinator.store.append_event

    def reject_terminal(turn: dict, event: dict):
        if event.get("type") == "terminal":
            raise OSError("injected permanent terminal failure")
        return original_append_event(turn, event)

    monkeypatch.setattr(coordinator.store, "append_event", reject_terminal)
    created = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": SAVE,
        "user_input": "永久 terminal 故障",
        "model": MODEL,
        "expected_revision": seeded["revision"],
    })
    turn_id = created.json()["turn_id"]

    terminal = await _wait_terminal(app_client, turn_id)
    assert terminal["status"] == "completed"
    assert terminal["error"] is None
    session = get_session_store().read_sync(project, SAVE)
    assert session["characters_state"]["test_character"][
        "remaining_silent_turns"
    ] == 1
    assert {message["status"] for message in session["message_history"]} == {
        "completed"
    }
    replay = _events(
        (await app_client.get(f"/api/chat/turns/{turn_id}/events")).text
    )
    assert all(event["type"] != "terminal" for event in replay)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_event_type", ["parsed", "terminal"])
async def test_event_written_before_meta_error_is_reloaded_without_overwrite(
    app_client,
    fake_ollama,
    seed_project,
    monkeypatch,
    failed_event_type,
):
    project = seed_project(f"roleplay_post_write_failure_{failed_event_type}")
    seeded = await _seed_roleplay_session(project, silent_turns=2)
    coordinator = get_turn_coordinator()
    original_append_event = coordinator.store.append_event
    failed = False

    def fail_after_write(turn: dict, event: dict):
        nonlocal failed
        result = original_append_event(turn, event)
        if not failed and event.get("type") == failed_event_type:
            failed = True
            raise OSError(f"injected post-write {failed_event_type} meta failure")
        return result

    monkeypatch.setattr(coordinator.store, "append_event", fail_after_write)
    created = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": SAVE,
        "user_input": "事件已写后 meta 故障",
        "model": MODEL,
        "expected_revision": seeded["revision"],
    })
    turn_id = created.json()["turn_id"]

    terminal = await _wait_terminal(app_client, turn_id)
    assert failed is True
    assert terminal["status"] == "completed"
    replay = _events(
        (await app_client.get(f"/api/chat/turns/{turn_id}/events")).text
    )
    assert sum(event["type"] == "parsed" for event in replay) == 1
    assert sum(event["type"] == "terminal" for event in replay) == 1
    assert replay[-1]["type"] == "terminal"
    assert [event["id"] for event in replay] == list(range(1, len(replay) + 1))
    session = get_session_store().read_sync(project, SAVE)
    assert session["characters_state"]["test_character"][
        "remaining_silent_turns"
    ] == 1


@pytest.mark.asyncio
async def test_restart_recognizes_completed_session_without_second_commit(
    seed_project,
    isolated_paths,
):
    project = seed_project("roleplay_completed_restart")
    save = SAVE
    turn_id = str(uuid4())
    created_at = datetime.now().astimezone().isoformat()
    current = get_session_store().read_sync(project, save)

    def seed_completed(session: dict, context) -> None:
        del context
        session["characters_state"] = {
            "test_character": {
                "name": "测试角色",
                "remaining_silent_turns": 2,
            }
        }
        metadata = {
            "turn_id": turn_id,
            "turn_kind": "chat",
            "status": "completed",
            "error": None,
            "timestamps": {
                "created_at": created_at,
                "completed_at": created_at,
            },
            "in_prompt": True,
        }
        append_history(session, "user", "已提交输入", metadata=metadata)
        append_history(
            session,
            "assistant",
            "已提交回复",
            metadata={**metadata, "in_prompt": True, "roleplay_warnings": []},
        )

    seeded = (
        await mutate_session(project, save, current["revision"], seed_completed)
    ).session
    turn_store = TurnStore(isolated_paths["data"] / ".completed-restart-turns")
    record = turn_store.create({
        "schema_version": 1,
        "turn_id": turn_id,
        "project": project,
        "save": save,
        "expected_revision": 0,
        "accepted_revision": 1,
        "user_input": "已提交输入",
        "model": MODEL,
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
        # 故意不写 turn_kind，验证旧 turn 按 chat 兼容。
    })
    turn_store.append_event(record, {"type": "started"})

    coordinator = TurnCoordinator(turn_store)
    assert await coordinator.ensure_recovered() == 1
    assert await coordinator.ensure_recovered() == 0
    turn = await coordinator.get(turn_id)
    assert turn["status"] == "completed"
    assert turn["turn_kind"] == "chat"
    assert turn["session_revision"] == seeded["revision"]
    after = get_session_store().read_sync(project, save)
    assert after["revision"] == seeded["revision"]
    assert after["characters_state"]["test_character"][
        "remaining_silent_turns"
    ] == 2


@pytest.mark.asyncio
async def test_restart_of_interrupted_turn_marks_failed_without_decrement(
    seed_project,
    isolated_paths,
):
    project = seed_project("roleplay_interrupted_restart")
    seeded = await _seed_roleplay_session(project, silent_turns=2)
    turn_id = str(uuid4())
    created_at = datetime.now().astimezone().isoformat()
    turn_store = TurnStore(isolated_paths["data"] / ".interrupted-roleplay-turns")
    record = turn_store.create({
        "schema_version": 2,
        "turn_id": turn_id,
        "turn_kind": "chat",
        "project": project,
        "save": SAVE,
        "expected_revision": seeded["revision"],
        "user_input": "中断输入",
        "model": MODEL,
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
        SAVE,
        seeded["revision"],
        turn_id=turn_id,
        user_input="中断输入",
        created_at=created_at,
        initial_session=seeded,
    )
    record["accepted_revision"] = acceptance.session["revision"]
    record["user_message_id"] = acceptance.value
    record = turn_store.update(record)
    turn_store.append_event(record, {"type": "started"})
    # 模拟旧进程退出：内存 lease 不会跨进程存活。
    active_turns.unregister(project, SAVE, turn_id)

    coordinator = TurnCoordinator(turn_store)
    assert await coordinator.ensure_recovered() == 1
    turn = await coordinator.get(turn_id)
    assert turn["status"] == "failed"
    assert turn["error"]["code"] == "server_restarted"
    after = get_session_store().read_sync(project, SAVE)
    assert after["characters_state"]["test_character"][
        "remaining_silent_turns"
    ] == 2
