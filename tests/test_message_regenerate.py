"""MSG-1：基于稳定消息 UUID 的原子重生成契约。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest

from core import active_turns
from core.chat_turns import TurnCoordinator, TurnStore, get_turn_coordinator
from core.session_manager import (
    append_history,
    get_session_store,
    mutate_session,
    new_session,
)


SAVE = "默认存档"
MODEL = "fake-model:latest"


def _metadata(*, turn_id: str | None = None, pinned: bool = False) -> dict:
    result = {
        "status": "completed",
        "error": None,
        "pinned": pinned,
        "in_prompt": True,
    }
    if turn_id is not None:
        result["turn_id"] = turn_id
    return result


async def _seed_history(seed_project, project: str, specs: list[dict]):
    seed_project(project)
    desired = new_session(project, SAVE)
    desired["current_model"] = MODEL
    messages = []
    for spec in specs:
        metadata = _metadata(
            turn_id=spec.get("turn_id"),
            pinned=spec.get("pinned", False),
        )
        if "in_prompt" in spec:
            metadata["in_prompt"] = spec["in_prompt"]
        messages.append(append_history(
            desired,
            spec["role"],
            spec["content"],
            metadata=metadata,
        ))

    current = get_session_store().read_sync(project, SAVE)
    expected_revision = current.get("revision", 0) if current else 0

    def replace(session: dict, context) -> None:
        del context
        session.clear()
        session.update(deepcopy(desired))

    mutation = await mutate_session(project, SAVE, expected_revision, replace)
    return mutation.session, messages


async def _regenerate(
    app_client,
    *,
    project: str,
    message_id: str,
    expected_revision: int,
):
    return await app_client.post("/api/chat/turns/regenerate", json={
        "project": project,
        "save": SAVE,
        "message_id": message_id,
        "model": MODEL,
        "expected_revision": expected_revision,
        "temperature": 0.4,
        "top_p": 0.9,
        "top_k": 20,
        "num_predict": 256,
        "think": False,
    })


async def _wait_terminal(app_client, turn_id: str, timeout: float = 3.0) -> dict:
    async def poll() -> dict:
        while True:
            response = await app_client.get(f"/api/chat/turns/{turn_id}")
            assert response.status_code == 200, response.text
            turn = response.json()
            if turn["status"] in {"completed", "cancelled", "failed"}:
                return turn
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), timeout=timeout)


def _error_code(response) -> str | None:
    payload = response.json()
    assert set(payload) == {"error"}, payload
    error = payload["error"]
    return error.get("code") if isinstance(error, dict) else None


def _snapshot_paths(project: str) -> list:
    directory = get_session_store().history_dir(project)
    return sorted(directory.glob(f"{SAVE}.*.json")) if directory.exists() else []


@pytest.mark.parametrize("turn_index", [0, 1, 2], ids=["first", "middle", "last"])
@pytest.mark.parametrize("target_role", ["user", "assistant"])
@pytest.mark.asyncio
async def test_regenerate_maps_first_middle_last_user_and_assistant_to_source_user(
    app_client,
    fake_ollama,
    seed_project,
    turn_index,
    target_role,
):
    project = f"regen_map_{target_role}_{turn_index}"
    specs = []
    for index in range(3):
        old_turn_id = str(uuid4())
        specs.extend([
            {
                "role": "user",
                "content": f"用户-{index}",
                "turn_id": old_turn_id,
            },
            {
                "role": "assistant",
                "content": f"助手-{index}",
                "turn_id": old_turn_id,
            },
        ])
    created, messages = await _seed_history(seed_project, project, specs)
    source = messages[turn_index * 2]
    target = messages[turn_index * 2 + (target_role == "assistant")]
    fake_ollama.configure("normal", block_before_first=True)

    response = await _regenerate(
        app_client,
        project=project,
        message_id=target["id"],
        expected_revision=created["revision"],
    )

    if turn_index < 2:
        assert response.status_code == 409, response.text
        assert _error_code(response) == "regeneration_would_rewrite_history"
        assert get_session_store().read_sync(project, SAVE) == created
        assert fake_ollama.chat_calls == []
        return

    assert response.status_code == 202, response.text
    turn = response.json()
    assert turn["status"] == "pending"
    assert turn["accepted_revision"] == created["revision"] + 1
    assert turn["user_message_id"] == source["id"]
    await asyncio.wait_for(fake_ollama.entered.wait(), timeout=1)

    accepted = get_session_store().read_sync(project, SAVE)
    pending = [
        message
        for message in accepted["message_history"]
        if message.get("turn_id") == turn["turn_id"]
    ]
    assert len(pending) == 1
    assert pending[0]["id"] == source["id"]
    assert pending[0]["role"] == "user"
    assert pending[0]["content"] == source["content"]
    assert pending[0]["status"] == "pending"

    cancelled = await app_client.post(f"/api/chat/turns/{turn['turn_id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text


@pytest.mark.asyncio
async def test_regenerate_rejects_crossed_turn_mapping_without_rewriting_history(
    app_client,
    fake_ollama,
    seed_project,
):
    project = "regen_same_turn"
    source_turn = str(uuid4())
    nearer_turn = str(uuid4())
    created, messages = await _seed_history(seed_project, project, [
        {"role": "user", "content": "正确来源", "turn_id": source_turn},
        {"role": "user", "content": "更近但不同 turn", "turn_id": nearer_turn},
        {"role": "assistant", "content": "目标回复", "turn_id": source_turn},
    ])
    fake_ollama.configure("normal", block_before_first=True)

    response = await _regenerate(
        app_client,
        project=project,
        message_id=messages[2]["id"],
        expected_revision=created["revision"],
    )

    assert response.status_code == 409, response.text
    assert _error_code(response) == "regeneration_would_rewrite_history"
    assert get_session_store().read_sync(project, SAVE) == created
    assert fake_ollama.chat_calls == []


@pytest.mark.asyncio
async def test_regenerate_legacy_assistant_falls_back_to_nearest_preceding_user(
    app_client,
    fake_ollama,
    seed_project,
):
    project = "regen_legacy_fallback"
    created, messages = await _seed_history(seed_project, project, [
        {"role": "user", "content": "旧用户"},
        {"role": "assistant", "content": "旧回复"},
        {"role": "user", "content": "最近用户"},
        {"role": "assistant", "content": "目标旧回复"},
    ])
    fake_ollama.configure("normal", block_before_first=True)

    response = await _regenerate(
        app_client,
        project=project,
        message_id=messages[3]["id"],
        expected_revision=created["revision"],
    )

    assert response.status_code == 202, response.text
    assert response.json()["user_message_id"] == messages[2]["id"]
    await asyncio.wait_for(fake_ollama.entered.wait(), timeout=1)
    await app_client.post(f"/api/chat/turns/{response.json()['turn_id']}/cancel")


@pytest.mark.asyncio
async def test_regenerate_completed_turn_commits_new_assistant_and_preserves_source_pin(
    app_client,
    fake_ollama,
    seed_project,
):
    project = "regen_completed"
    old_turn = str(uuid4())
    created, messages = await _seed_history(seed_project, project, [
        {
            "role": "user",
            "content": "完成路径来源",
            "turn_id": old_turn,
            "pinned": True,
        },
        {
            "role": "assistant",
            "content": "完成路径旧回复",
            "turn_id": old_turn,
        },
    ])
    fake_ollama.configure("normal")

    response = await _regenerate(
        app_client,
        project=project,
        message_id=messages[1]["id"],
        expected_revision=created["revision"],
    )

    assert response.status_code == 202, response.text
    terminal = await _wait_terminal(app_client, response.json()["turn_id"])
    assert terminal["status"] == "completed"
    assert terminal["accepted_revision"] == created["revision"] + 1
    assert terminal["session_revision"] == created["revision"] + 2
    session = get_session_store().read_sync(project, SAVE)
    assert [item["role"] for item in session["message_history"]] == [
        "user",
        "assistant",
    ]
    assert session["message_history"][0]["id"] == messages[0]["id"]
    assert session["message_history"][0]["pinned"] is True
    assert session["message_history"][0]["status"] == "completed"
    reply = session["message_history"][1]
    assert reply["id"] == messages[1]["id"]
    assert reply["turn_id"] == terminal["turn_id"]
    assert "隔离测试响应" in reply["content"]
    assert reply["reply_variant_id"]
    assert reply["reply_variant_state"]["scene_meta"]["location"] == "隔离测试酒馆"
    assert len(reply["reply_alternatives"]) == 1
    assert reply["reply_alternatives"][0]["content"] == "完成路径旧回复"
    assert reply["reply_alternatives"][0]["state_snapshot"]["scene_meta"][
        "location"
    ] == ""


@pytest.mark.asyncio
async def test_reply_alternative_selection_swaps_content_and_branch_state(
    app_client,
    fake_ollama,
    seed_project,
):
    project = "regen_select_alternative"
    old_turn = str(uuid4())
    created, messages = await _seed_history(seed_project, project, [
        {"role": "user", "content": "选择分支", "turn_id": old_turn},
        {"role": "assistant", "content": "保留的原回复", "turn_id": old_turn},
    ])
    fake_ollama.configure("normal")
    response = await _regenerate(
        app_client,
        project=project,
        message_id=messages[1]["id"],
        expected_revision=created["revision"],
    )
    assert response.status_code == 202, response.text
    terminal = await _wait_terminal(app_client, response.json()["turn_id"])
    assert terminal["status"] == "completed"
    regenerated = get_session_store().read_sync(project, SAVE)
    reply = regenerated["message_history"][1]
    generated_content = reply["content"]
    original_variant_id = reply["reply_alternatives"][0]["id"]
    assert regenerated["scene_meta"]["location"] == "隔离测试酒馆"

    selected = await app_client.patch(
        "/api/session/reply-alternative",
        json={
            "project": project,
            "save": SAVE,
            "expected_revision": regenerated["revision"],
            "message_id": messages[1]["id"],
            "alternative_id": original_variant_id,
        },
    )

    assert selected.status_code == 200, selected.text
    switched = selected.json()["session"]
    switched_reply = switched["message_history"][1]
    assert switched_reply["id"] == messages[1]["id"]
    assert switched_reply["reply_variant_id"] == original_variant_id
    assert switched_reply["content"] == "保留的原回复"
    assert switched["scene_meta"]["location"] == ""
    assert [item["content"] for item in switched_reply["reply_alternatives"]] == [
        generated_content
    ]

    generated_variant_id = switched_reply["reply_alternatives"][0]["id"]

    def add_later_history(session: dict, context) -> list[str]:
        del context
        session["scene_meta"]["location"] = "后续剧情地点"
        later_user = append_history(session, "user", "后续用户")
        later_assistant = append_history(session, "assistant", "后续回复")
        return [later_user["id"], later_assistant["id"]]

    continued = await mutate_session(
        project,
        SAVE,
        switched["revision"],
        add_later_history,
    )
    selected_again = await app_client.patch(
        "/api/session/reply-alternative",
        json={
            "project": project,
            "save": SAVE,
            "expected_revision": continued.session["revision"],
            "message_id": messages[1]["id"],
            "alternative_id": generated_variant_id,
        },
    )
    assert selected_again.status_code == 200, selected_again.text
    assert selected_again.json()["state_applied"] is False
    continued_after_switch = selected_again.json()["session"]
    assert continued_after_switch["scene_meta"]["location"] == "后续剧情地点"
    assert [item["id"] for item in continued_after_switch["message_history"][-2:]] == (
        continued.value
    )


@pytest.mark.asyncio
async def test_failed_regeneration_restores_original_reply_without_partial_branch(
    app_client,
    fake_ollama,
    seed_project,
):
    project = "regen_failure_restore"
    old_turn = str(uuid4())
    created, messages = await _seed_history(seed_project, project, [
        {"role": "user", "content": "失败也要保留", "turn_id": old_turn},
        {"role": "assistant", "content": "不可丢失的回复", "turn_id": old_turn},
    ])
    fake_ollama.configure("error")

    response = await _regenerate(
        app_client,
        project=project,
        message_id=messages[1]["id"],
        expected_revision=created["revision"],
    )

    assert response.status_code == 202, response.text
    terminal = await _wait_terminal(app_client, response.json()["turn_id"])
    assert terminal["status"] == "failed"
    session = get_session_store().read_sync(project, SAVE)
    assert [item["id"] for item in session["message_history"]] == [
        messages[0]["id"],
        messages[1]["id"],
    ]
    assert session["message_history"][1]["content"] == "不可丢失的回复"
    assert not any(
        key.startswith("regeneration_")
        for item in session["message_history"]
        for key in item
    )


@pytest.mark.asyncio
async def test_restart_recovery_restores_original_regeneration_reply(
    seed_project,
    isolated_paths,
):
    project = "regen_restart_restore"
    old_turn = str(uuid4())
    created, messages = await _seed_history(seed_project, project, [
        {"role": "user", "content": "重启来源", "turn_id": old_turn},
        {"role": "assistant", "content": "重启前原回复", "turn_id": old_turn},
    ])
    turn_id = str(uuid4())
    created_at = "2026-07-29T00:00:00+08:00"
    turn_store = TurnStore(isolated_paths["data"] / ".regen-restart-turns")
    record = turn_store.create({
        "schema_version": 1,
        "turn_id": turn_id,
        "turn_kind": "regenerate",
        "project": project,
        "save": SAVE,
        "expected_revision": created["revision"],
        "user_input": "重启来源",
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
    acceptance = await get_session_store().accept_regenerated_chat_turn(
        project,
        SAVE,
        created["revision"],
        turn_id=turn_id,
        target_message_id=messages[1]["id"],
        expected_user_input="重启来源",
        created_at=created_at,
    )
    record["accepted_revision"] = acceptance.session["revision"]
    record["user_message_id"] = acceptance.value
    record = turn_store.update(record)
    record, _ = turn_store.append_event(record, {"type": "started"})
    turn_store.append_event(record, {"type": "content", "content": "不应保留"})
    active_turns.unregister(project, SAVE, turn_id)

    coordinator = TurnCoordinator(turn_store)
    assert await coordinator.ensure_recovered() == 1
    session = get_session_store().read_sync(project, SAVE)
    assert [item["id"] for item in session["message_history"]] == [
        messages[0]["id"],
        messages[1]["id"],
    ]
    assert session["message_history"][1]["content"] == "重启前原回复"
    assert all("regeneration_source_snapshot" not in item for item in session[
        "message_history"
    ])


@pytest.mark.asyncio
async def test_regenerate_rejects_non_pinned_suffix_without_rewriting_history(
    app_client,
    fake_ollama,
    seed_project,
):
    project = "regen_atomic_success"
    source_turn = str(uuid4())
    created, messages = await _seed_history(seed_project, project, [
        {"role": "user", "content": "保留前缀-user"},
        {"role": "assistant", "content": "保留前缀-assistant"},
        {
            "role": "user",
            "content": "重生成来源",
            "turn_id": source_turn,
            "pinned": True,
        },
        {
            "role": "assistant",
            "content": "目标旧回复",
            "turn_id": source_turn,
            "pinned": True,
        },
        {"role": "user", "content": "删除普通后缀-1"},
        {"role": "assistant", "content": "保留 pinned-1", "pinned": True},
        {"role": "assistant", "content": "删除普通后缀-2"},
        {"role": "user", "content": "保留 pinned-2", "pinned": True},
    ])
    original = deepcopy(created)
    before_snapshots = _snapshot_paths(project)
    fake_ollama.configure("normal", block_before_first=True)

    response = await _regenerate(
        app_client,
        project=project,
        message_id=messages[3]["id"],
        expected_revision=created["revision"],
    )

    assert response.status_code == 409, response.text
    assert _error_code(response) == "regeneration_would_rewrite_history"
    assert get_session_store().read_sync(project, SAVE) == original
    assert _snapshot_paths(project) == before_snapshots
    assert fake_ollama.chat_calls == []


@pytest.mark.asyncio
async def test_regenerate_budget_rejection_has_zero_turn_session_snapshot_or_model_write(
    app_client,
    fake_ollama,
    seed_project,
    monkeypatch,
):
    project = "regen_prompt_budget_rejection"
    source_marker = "REGENERATE_BUDGET_SECRET_SENTINEL"
    created, messages = await _seed_history(seed_project, project, [
        {"role": "user", "content": source_marker},
        {"role": "assistant", "content": "旧回复"},
    ])
    before_session = deepcopy(created)
    before_snapshots = _snapshot_paths(project)
    coordinator = get_turn_coordinator()
    before_turn_ids = coordinator.store.list_turn_ids()
    store = get_session_store()
    original_write = store._write_session_sync
    writes = 0

    def count_write(*args, **kwargs):
        nonlocal writes
        writes += 1
        return original_write(*args, **kwargs)

    monkeypatch.setattr(store, "_write_session_sync", count_write)
    fake_ollama.context_limits[MODEL] = {
        "context_limit": 512,
        "source": "fake_too_small",
    }

    response = await _regenerate(
        app_client,
        project=project,
        message_id=messages[1]["id"],
        expected_revision=created["revision"],
    )

    assert response.status_code == 422, response.text
    assert _error_code(response) == "prompt_budget_exceeded"
    assert source_marker not in response.text
    assert writes == 0
    assert fake_ollama.chat_calls == []
    assert fake_ollama.summary_calls == []
    assert get_session_store().read_sync(project, SAVE) == before_session
    assert _snapshot_paths(project) == before_snapshots
    assert coordinator.store.list_turn_ids() == before_turn_ids


@pytest.mark.parametrize(
    ("case", "expected_status", "expected_code"),
    [
        ("stale", 409, "revision_conflict"),
        ("missing", 404, "message_not_found"),
        ("no_preceding_user", 422, "regeneration_source_not_found"),
        ("empty_source", 422, "regeneration_source_not_found"),
        ("excluded_source", 422, "regeneration_source_excluded"),
    ],
)
@pytest.mark.asyncio
async def test_regenerate_rejection_changes_neither_history_nor_snapshots(
    app_client,
    fake_ollama,
    seed_project,
    case,
    expected_status,
    expected_code,
):
    project = f"regen_reject_{case}"
    if case == "no_preceding_user":
        specs = [{"role": "assistant", "content": "孤立回复"}]
        target_index = 0
    elif case == "empty_source":
        legacy_turn = str(uuid4())
        specs = [
            {"role": "user", "content": "   ", "turn_id": legacy_turn},
            {"role": "assistant", "content": "空来源回复", "turn_id": legacy_turn},
        ]
        target_index = 1
    elif case == "excluded_source":
        excluded_turn = str(uuid4())
        specs = [
            {
                "role": "user",
                "content": "明确排除的来源",
                "turn_id": excluded_turn,
                "in_prompt": False,
            },
            {
                "role": "assistant",
                "content": "排除来源的旧回复",
                "turn_id": excluded_turn,
            },
        ]
        target_index = 1
    else:
        specs = [
            {"role": "user", "content": "保留用户"},
            {"role": "assistant", "content": "保留回复"},
        ]
        target_index = 1
    created, messages = await _seed_history(seed_project, project, specs)
    target_id = str(uuid4()) if case == "missing" else messages[target_index]["id"]
    expected_revision = (
        created["revision"] - 1 if case == "stale" else created["revision"]
    )
    history_before = deepcopy(created["message_history"])
    snapshots_before = _snapshot_paths(project)

    response = await _regenerate(
        app_client,
        project=project,
        message_id=target_id,
        expected_revision=expected_revision,
    )

    assert response.status_code == expected_status, response.text
    assert _error_code(response) == expected_code
    current = get_session_store().read_sync(project, SAVE)
    assert current["message_history"] == history_before
    assert current["revision"] == created["revision"]
    assert _snapshot_paths(project) == snapshots_before
    assert fake_ollama.chat_calls == []


@pytest.mark.asyncio
async def test_regenerate_active_turn_conflict_has_no_partial_mutation(
    app_client,
    fake_ollama,
    seed_project,
):
    project = "regen_active_conflict"
    created, messages = await _seed_history(seed_project, project, [
        {"role": "user", "content": "已有用户"},
        {"role": "assistant", "content": "已有回复"},
    ])
    fake_ollama.configure("normal", block_before_first=True)
    active = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": SAVE,
        "user_input": "占用当前存档",
        "model": MODEL,
        "expected_revision": created["revision"],
    })
    assert active.status_code == 202, active.text
    await asyncio.wait_for(fake_ollama.entered.wait(), timeout=1)
    before = get_session_store().read_sync(project, SAVE)
    snapshots_before = _snapshot_paths(project)

    response = await _regenerate(
        app_client,
        project=project,
        message_id=before["message_history"][-1]["id"],
        expected_revision=before["revision"],
    )

    assert response.status_code == 409, response.text
    assert _error_code(response) == "active_turn_conflict"
    assert get_session_store().read_sync(project, SAVE) == before
    assert _snapshot_paths(project) == snapshots_before
    assert len(fake_ollama.chat_calls) == 1

    cancelled = await app_client.post(
        f"/api/chat/turns/{active.json()['turn_id']}/cancel"
    )
    assert cancelled.status_code == 200, cancelled.text


@pytest.mark.parametrize("failure_point", ["snapshot", "session_write"])
@pytest.mark.asyncio
async def test_regenerate_storage_failure_rolls_back_snapshot_and_lease(
    seed_project,
    monkeypatch,
    failure_point,
):
    project = f"regen_storage_failure_{failure_point}"
    created, messages = await _seed_history(seed_project, project, [
        {"role": "user", "content": "故障前用户"},
        {"role": "assistant", "content": "故障前回复"},
    ])
    store = get_session_store()
    before = deepcopy(store.read_sync(project, SAVE))
    snapshots_before = _snapshot_paths(project)
    history_dir_existed = store.history_dir(project).exists()
    turn_id = str(uuid4())

    def fail(*args, **kwargs):
        del args, kwargs
        raise OSError(f"injected {failure_point} failure")

    attribute = (
        "_write_snapshot_sync"
        if failure_point == "snapshot"
        else "_write_session_sync"
    )
    monkeypatch.setattr(store, attribute, fail)

    with pytest.raises(OSError, match="injected"):
        await store.accept_regenerated_chat_turn(
            project,
            SAVE,
            created["revision"],
            turn_id=turn_id,
            target_message_id=messages[1]["id"],
            expected_user_input="故障前用户",
            created_at="2026-07-15T00:00:00+08:00",
        )

    assert store.read_sync(project, SAVE) == before
    assert _snapshot_paths(project) == snapshots_before
    assert store.history_dir(project).exists() is history_dir_existed
    assert active_turns.active_turn_id(project, SAVE) is None


@pytest.mark.parametrize(
    ("action", "extra"),
    [
        ("edit", {"content": "不能按 index 编辑"}),
        ("delete", {}),
        ("toggle_pinned", {}),
        ("toggle_in_prompt", {"in_prompt": False}),
        ("truncate", {}),
    ],
)
@pytest.mark.asyncio
async def test_index_only_message_commands_are_rejected_without_mutation(
    app_client,
    seed_project,
    action,
    extra,
):
    project = f"message_id_required_{action}"
    created, _ = await _seed_history(seed_project, project, [
        {"role": "user", "content": "稳定消息"},
    ])
    before = deepcopy(get_session_store().read_sync(project, SAVE))
    response = await app_client.patch(
        "/api/session",
        params={"project": project, "save": SAVE},
        json={
            "action": action,
            "index": 0,
            "expected_revision": created["revision"],
            **extra,
        },
    )

    assert response.status_code == 400, response.text
    assert _error_code(response) == "message_id_required"
    assert get_session_store().read_sync(project, SAVE) == before


@pytest.mark.asyncio
async def test_turn_meta_update_failure_does_not_report_an_accepted_regenerate_as_failed(
    app_client,
    fake_ollama,
    seed_project,
    monkeypatch,
):
    project = "regen_meta_update_failure"
    created, messages = await _seed_history(seed_project, project, [
        {"role": "user", "content": "元数据故障用户"},
        {"role": "assistant", "content": "元数据故障回复"},
    ])
    coordinator = get_turn_coordinator()
    original_update = coordinator.store.update
    calls = 0

    def fail_first_update(turn: dict) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected turn meta update failure")
        return original_update(turn)

    monkeypatch.setattr(coordinator.store, "update", fail_first_update)
    fake_ollama.configure("normal", block_before_first=True)

    response = await _regenerate(
        app_client,
        project=project,
        message_id=messages[1]["id"],
        expected_revision=created["revision"],
    )

    assert response.status_code == 202, response.text
    await asyncio.wait_for(fake_ollama.entered.wait(), timeout=1)
    accepted = get_session_store().read_sync(project, SAVE)
    assert any(
        item.get("turn_id") == response.json()["turn_id"]
        and item.get("status") == "pending"
        for item in accepted["message_history"]
    )
    cancelled = await app_client.post(
        f"/api/chat/turns/{response.json()['turn_id']}/cancel"
    )
    assert cancelled.status_code == 200, cancelled.text
