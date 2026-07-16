"""MSG-1：基于稳定消息 UUID 的原子重生成契约。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from core import active_turns
from core.chat_turns import get_turn_coordinator
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
    detail = payload.get("detail", payload.get("error"))
    return detail.get("code") if isinstance(detail, dict) else None


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
async def test_regenerate_prefers_same_turn_id_over_nearest_user(
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

    assert response.status_code == 202, response.text
    assert response.json()["user_message_id"] == messages[0]["id"]
    await asyncio.wait_for(fake_ollama.entered.wait(), timeout=1)
    await app_client.post(f"/api/chat/turns/{response.json()['turn_id']}/cancel")


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
    assert session["message_history"][1]["turn_id"] == terminal["turn_id"]
    assert "隔离测试响应" in session["message_history"][1]["content"]


@pytest.mark.asyncio
async def test_regenerate_is_one_snapshot_reuses_uuid_and_keeps_only_pinned_suffix(
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

    assert response.status_code == 202, response.text
    turn = response.json()
    await asyncio.wait_for(fake_ollama.entered.wait(), timeout=1)
    prompt_messages = fake_ollama.chat_calls[-1]["messages"]
    assert sum(
        item["content"].count("重生成来源")
        for item in prompt_messages
    ) == 1
    assert sum(
        item["content"].count("目标旧回复")
        for item in prompt_messages
    ) == 1
    after_snapshots = _snapshot_paths(project)
    assert len(after_snapshots) == len(before_snapshots) + 1
    snapshot = json.loads(after_snapshots[-1].read_text(encoding="utf-8"))
    assert [item["id"] for item in snapshot["message_history"]] == [
        item["id"] for item in original["message_history"]
    ]

    accepted = get_session_store().read_sync(project, SAVE)
    assert [item["id"] for item in accepted["message_history"]] == [
        messages[0]["id"],
        messages[1]["id"],
        messages[3]["id"],
        messages[5]["id"],
        messages[7]["id"],
        messages[2]["id"],
    ]
    assert turn["user_message_id"] == messages[2]["id"]
    source = next(
        item for item in accepted["message_history"] if item["id"] == messages[2]["id"]
    )
    assert source["turn_id"] == turn["turn_id"]
    assert source["status"] == "pending"
    assert [
        item["content"]
        for item in accepted["message_history"]
        if item.get("pinned")
    ] == ["目标旧回复", "保留 pinned-1", "保留 pinned-2", "重生成来源"]

    cancelled = await app_client.post(f"/api/chat/turns/{turn['turn_id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    finalized = get_session_store().read_sync(project, SAVE)
    finalized_by_id = {
        item["id"]: item for item in finalized["message_history"]
    }
    assert finalized_by_id[messages[2]["id"]]["pinned"] is True
    assert finalized_by_id[messages[3]["id"]]["pinned"] is True


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
        message_id=messages[1]["id"],
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
