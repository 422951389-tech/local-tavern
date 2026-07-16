import asyncio
from copy import deepcopy
from datetime import datetime
import json
from uuid import UUID, uuid4

import pytest

from core import active_turns
from core.import_validation import validate_import_json
from core.session_manager import (
    get_session_store,
    mutate_session,
    new_session,
    trim_snapshot_payload,
)
from core.session_store import atomic_write
from core.summary_lifecycle import (
    SummaryValidationError,
    is_summary_generation_active,
    shutdown_summary_tasks,
    validate_generated_summary,
)


def _summary_item(summary_id: str, text: str) -> dict:
    now = datetime.now().astimezone().isoformat()
    return {
        "id": summary_id,
        "source_snapshot_id": None,
        "source_status": "unlinked",
        "status": "completed",
        "content_status": "valid",
        "generation_attempt": 0,
        "text": text,
        "time": "第一日",
        "facts": [f"{text}-事实"],
        "relations": [],
        "created_at": now,
        "error": None,
    }


async def _seed_summaries(seed_project, project: str, specs: list[tuple[str, str]]):
    seed_project(project)
    store = get_session_store()
    save = "记忆存档"
    initial = new_session(project, save)
    initial["current_model"] = "fake-model:latest"
    created = await store.create(project, save, initial)
    summary_ids = [str(uuid4()) for _spec in specs]

    def add(current: dict, context) -> dict:
        sources = {}
        for summary_id, (text, sentinel) in zip(summary_ids, specs):
            dropped = [{
                "id": str(uuid4()),
                "role": "user",
                "content": sentinel,
                "pinned": False,
                "in_prompt": True,
            }]
            path = context.snapshot(
                "trim",
                trim_snapshot_payload(dropped, summary_id=summary_id),
            )
            item = _summary_item(summary_id, text)
            item["source_snapshot_id"] = path.name
            item["source_status"] = "available"
            current.setdefault("summaries", []).append(item)
            sources[summary_id] = path.name
        return sources

    mutation = await mutate_session(
        project,
        save,
        created["revision"],
        add,
    )
    return save, mutation.session, mutation.value


async def _wait_summary(project: str, save: str, summary_id: str, status: str) -> dict:
    async def poll() -> dict:
        while True:
            session = get_session_store().read_sync(project, save)
            item = next(entry for entry in session["summaries"] if entry["id"] == summary_id)
            if item["status"] == status:
                return session
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), timeout=3)


def _detail_code(response) -> str | None:
    payload = response.json()
    detail = payload.get("detail") or payload.get("error") or {}
    return detail.get("code") if isinstance(detail, dict) else None


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("前情提要: 无", "summary_text_required"),
        ("x" * 2_001, "summary_field_too_long"),
        (
            "前情提要: 有效\n关键事件:\n" + "\n".join(f"- 事件{i}" for i in range(6)),
            "summary_items_too_many",
        ),
        (
            "前情提要: 有效\n关键事件:\n- " + "事" * 201,
            "summary_field_too_long",
        ),
    ],
)
def test_generated_summary_validation_never_silently_truncates(raw, code):
    with pytest.raises(SummaryValidationError) as caught:
        validate_generated_summary(raw)
    assert caught.value.code == code


@pytest.mark.asyncio
async def test_legacy_summary_ids_are_deterministic_unique_and_pure_read(seed_project):
    project = seed_project("legacy_summary_identity")
    save = "旧摘要"
    store = get_session_store()
    raw = new_session(project, save)
    duplicate = "4be434f4-d0c4-48dd-bd32-69137a2a042f"
    raw["summaries"] = [
        {"text": "旧段一", "facts": [], "relations": []},
        {"id": duplicate, "text": "旧段二", "facts": [], "relations": []},
        {"id": duplicate, "text": "旧段三", "facts": [], "relations": []},
    ]
    path = store.session_path(project, save)
    atomic_write(path, json.dumps(raw, ensure_ascii=False, indent=2))
    before = path.read_bytes()

    first = store.read_sync(project, save)
    second = store.read_sync(project, save)
    first_ids = [item["id"] for item in first["summaries"]]
    assert first_ids == [item["id"] for item in second["summaries"]]
    assert len(set(first_ids)) == 3
    assert all(str(UUID(value)) == value for value in first_ids)
    assert first_ids[1] == duplicate
    assert all(item["source_status"] == "unlinked" for item in first["summaries"])
    assert all(item["status"] == "completed" for item in first["summaries"])
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_patch_uses_summary_id_and_rejects_invalid_or_noop_without_writes(
    app_client,
    seed_project,
):
    project = "summary_patch_contract"
    save, session, sources = await _seed_summaries(
        seed_project,
        project,
        [("段一", "SOURCE-A"), ("段二", "SOURCE-B")],
    )
    first_id, second_id = [item["id"] for item in session["summaries"]]
    path = get_session_store().session_path(project, save)
    before = path.read_bytes()
    bad_payloads = [
        {"summary_id": first_id},
        {"summary_id": first_id, "text": "段一"},
        {"summary_id": first_id, "text": 7},
        {"summary_id": first_id, "time": []},
        {"summary_id": first_id, "facts": "不是数组"},
        {"summary_id": first_id, "facts": ["1", "2", "3", "4", "5", "6"]},
        {"summary_id": first_id, "relations": ["x" * 201]},
    ]
    for payload in bad_payloads:
        response = await app_client.patch("/api/session/summary", json={
            "project": project,
            "save": save,
            "expected_revision": session["revision"],
            **payload,
        })
        assert response.status_code == 422, response.text
        assert path.read_bytes() == before

    non_object = await app_client.patch("/api/session/summary", json=[])
    assert non_object.status_code == 400
    assert _detail_code(non_object) == "summary_request_invalid"
    assert path.read_bytes() == before

    index_only = await app_client.patch("/api/session/summary", json={
        "project": project,
        "save": save,
        "expected_revision": session["revision"],
        "index": 0,
        "text": "禁止按下标",
    })
    assert index_only.status_code == 400
    assert _detail_code(index_only) == "summary_id_required"
    assert path.read_bytes() == before

    updated = await app_client.patch("/api/session/summary", json={
        "project": project,
        "save": save,
        "expected_revision": session["revision"],
        "summary_id": first_id,
        "text": "人工修正段一",
        "time": "第二日",
        "facts": ["修正事实"],
        "relations": ["甲与乙合作"],
    })
    assert updated.status_code == 200, updated.text
    result = updated.json()["session"]
    first, second = result["summaries"]
    assert first["id"] == first_id
    assert first["source_snapshot_id"] == sources[first_id]
    assert first["text"] == "人工修正段一"
    assert first["status"] == "completed"
    assert second["id"] == second_id
    assert second["text"] == "段二"


@pytest.mark.asyncio
async def test_regenerate_reads_only_target_snapshot_and_duplicate_request_is_single_task(
    app_client,
    fake_ollama,
    seed_project,
    monkeypatch,
):
    project = "summary_regenerate_exact_source"
    save, session, sources = await _seed_summaries(
        seed_project,
        project,
        [("段一", "SOURCE-A"), ("段二", "SOURCE-B")],
    )
    first_id, second_id = [item["id"] for item in session["summaries"]]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(model, dropped_messages, num_predict=1024, temperature=0.5):
        fake_ollama.summary_calls.append({
            "model": model,
            "dropped_messages": deepcopy(dropped_messages),
            "num_predict": num_predict,
            "temperature": temperature,
        })
        entered.set()
        await release.wait()
        return "前情提要: B 的新总结\n时间线: 第二日"

    monkeypatch.setattr(fake_ollama, "summarize_once", blocked)
    accepted = await app_client.post("/api/session/summary/regenerate", json={
        "project": project,
        "save": save,
        "expected_revision": session["revision"],
        "summary_id": second_id,
    })
    assert accepted.status_code == 202, accepted.text
    pending = accepted.json()["session"]
    assert pending["summaries"][0]["id"] == first_id
    assert pending["summaries"][0]["status"] == "completed"
    assert pending["summaries"][1]["id"] == second_id
    assert pending["summaries"][1]["status"] == "pending"
    assert pending["summaries"][1]["source_snapshot_id"] == sources[second_id]
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert [item["content"] for item in fake_ollama.summary_calls[0]["dropped_messages"]] == ["SOURCE-B"]

    duplicate = await app_client.post("/api/session/summary/regenerate", json={
        "project": project,
        "save": save,
        "expected_revision": pending["revision"],
        "summary_id": second_id,
    })
    assert duplicate.status_code == 409, duplicate.text
    assert _detail_code(duplicate) == "summary_generation_pending"
    assert len(fake_ollama.summary_calls) == 1

    snapshot = json.loads(
        (get_session_store().history_dir(project) / sources[second_id]).read_text(encoding="utf-8")
    )
    assert snapshot["summary_id"] == second_id
    release.set()
    completed = await _wait_summary(project, save, second_id, "completed")
    first, second = completed["summaries"]
    assert first["text"] == "段一"
    assert second["id"] == second_id
    assert second["source_snapshot_id"] == sources[second_id]
    assert second["text"] == "B 的新总结"
    assert second["time"] == "第二日"


@pytest.mark.asyncio
async def test_missing_unlinked_and_mismatched_sources_never_fallback_or_call_model(
    app_client,
    fake_ollama,
    seed_project,
):
    project = "summary_missing_source"
    save, session, sources = await _seed_summaries(
        seed_project,
        project,
        [("段一", "SOURCE-A"), ("段二", "SOURCE-B")],
    )
    first_id, second_id = [item["id"] for item in session["summaries"]]
    target_path = get_session_store().history_dir(project) / sources[first_id]
    target_path.unlink()
    public = await app_client.get("/api/session", params={"project": project, "save": save})
    public_first = public.json()["summaries"][0]
    assert public_first["source_status"] == "missing"

    missing = await app_client.post("/api/session/summary/regenerate", json={
        "project": project,
        "save": save,
        "expected_revision": session["revision"],
        "summary_id": first_id,
    })
    assert missing.status_code == 409
    assert _detail_code(missing) == "summary_source_missing"
    assert fake_ollama.summary_calls == []

    current = get_session_store().read_sync(project, save)
    def unlink_source(state: dict, context) -> None:
        del context
        state["summaries"][0]["source_snapshot_id"] = None
    unlinked_session = await mutate_session(
        project,
        save,
        current["revision"],
        unlink_source,
    )
    unlinked = await app_client.post("/api/session/summary/regenerate", json={
        "project": project,
        "save": save,
        "expected_revision": unlinked_session.session["revision"],
        "summary_id": first_id,
    })
    assert unlinked.status_code == 409
    assert _detail_code(unlinked) == "summary_source_unlinked"
    assert fake_ollama.summary_calls == []

    source_path = get_session_store().history_dir(project) / sources[second_id]
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    payload.pop("summary_id")
    atomic_write(source_path, json.dumps(payload, ensure_ascii=False, indent=2))
    reverse_unlinked = await app_client.post("/api/session/summary/regenerate", json={
        "project": project,
        "save": save,
        "expected_revision": unlinked_session.session["revision"],
        "summary_id": second_id,
    })
    assert reverse_unlinked.status_code == 409
    assert _detail_code(reverse_unlinked) == "summary_source_mismatch"
    assert fake_ollama.summary_calls == []

    payload["summary_id"] = first_id
    atomic_write(source_path, json.dumps(payload, ensure_ascii=False, indent=2))
    mismatched = await app_client.post("/api/session/summary/regenerate", json={
        "project": project,
        "save": save,
        "expected_revision": unlinked_session.session["revision"],
        "summary_id": second_id,
    })
    assert mismatched.status_code == 409
    assert _detail_code(mismatched) == "summary_source_mismatch"
    assert fake_ollama.summary_calls == []


@pytest.mark.asyncio
async def test_empty_output_fails_then_retry_completes_without_losing_identity(
    app_client,
    fake_ollama,
    seed_project,
    monkeypatch,
):
    project = "summary_failure_retry"
    save, session, sources = await _seed_summaries(
        seed_project,
        project,
        [("旧有效正文", "SOURCE-RETRY")],
    )
    summary_id = session["summaries"][0]["id"]

    async def empty(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(fake_ollama, "summarize_once", empty)
    accepted = await app_client.post("/api/session/summary/regenerate", json={
        "project": project,
        "save": save,
        "expected_revision": session["revision"],
        "summary_id": summary_id,
    })
    assert accepted.status_code == 202
    failed = await _wait_summary(project, save, summary_id, "failed")
    failed_item = failed["summaries"][0]
    assert failed_item["id"] == summary_id
    assert failed_item["source_snapshot_id"] == sources[summary_id]
    assert failed_item["text"] == "旧有效正文"
    assert failed_item["content_status"] == "valid"
    assert "空总结" in failed_item["error"]

    async def success(*_args, **_kwargs):
        return "前情提要: 重试成功"

    monkeypatch.setattr(fake_ollama, "summarize_once", success)
    retried = await app_client.post("/api/session/summary/regenerate", json={
        "project": project,
        "save": save,
        "expected_revision": failed["revision"],
        "summary_id": summary_id,
    })
    assert retried.status_code == 202
    completed = await _wait_summary(project, save, summary_id, "completed")
    item = completed["summaries"][0]
    assert item["id"] == summary_id
    assert item["source_snapshot_id"] == sources[summary_id]
    assert item["text"] == "重试成功"
    assert item["generation_attempt"] == 2
    assert item["error"] is None


@pytest.mark.asyncio
async def test_stale_pending_after_restart_can_be_explicitly_retried(
    app_client,
    fake_ollama,
    seed_project,
    monkeypatch,
):
    project = "summary_stale_pending_retry"
    save, session, _sources = await _seed_summaries(
        seed_project,
        project,
        [("旧正文", "SOURCE-STALE")],
    )
    summary_id = session["summaries"][0]["id"]

    def make_stale(current: dict, context) -> None:
        del context
        current["summaries"][0].update({
            "status": "pending",
            "generation_id": str(uuid4()),
            "generation_attempt": 1,
        })

    stale = await mutate_session(project, save, session["revision"], make_stale)
    public = await app_client.get("/api/session", params={"project": project, "save": save})
    assert public.json()["summaries"][0]["generation_active"] is False

    async def success(*_args, **_kwargs):
        return "前情提要: 从中断任务恢复"

    monkeypatch.setattr(fake_ollama, "summarize_once", success)
    retried = await app_client.post("/api/session/summary/regenerate", json={
        "project": project,
        "save": save,
        "expected_revision": stale.session["revision"],
        "summary_id": summary_id,
    })
    assert retried.status_code == 202, retried.text
    completed = await _wait_summary(project, save, summary_id, "completed")
    assert completed["summaries"][0]["text"] == "从中断任务恢复"
    assert completed["summaries"][0]["generation_attempt"] == 2


@pytest.mark.asyncio
async def test_generation_result_waits_for_active_turn_then_merges_by_id(
    app_client,
    fake_ollama,
    seed_project,
    monkeypatch,
):
    project = "summary_waits_active_turn"
    save, session, _sources = await _seed_summaries(
        seed_project,
        project,
        [("旧正文", "SOURCE-WAIT")],
    )
    summary_id = session["summaries"][0]["id"]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return "前情提要: active turn 结束后写入"

    monkeypatch.setattr(fake_ollama, "summarize_once", blocked)
    accepted = await app_client.post("/api/session/summary/regenerate", json={
        "project": project,
        "save": save,
        "expected_revision": session["revision"],
        "summary_id": summary_id,
    })
    assert accepted.status_code == 202
    await asyncio.wait_for(entered.wait(), timeout=1)
    active_turns.register(project, save, "summary-test-turn")
    release.set()
    try:
        await asyncio.sleep(0.15)
        pending = get_session_store().read_sync(project, save)
        assert pending["summaries"][0]["status"] == "pending"
    finally:
        active_turns.unregister(project, save, "summary-test-turn")
    completed = await _wait_summary(project, save, summary_id, "completed")
    assert completed["summaries"][0]["text"] == "active turn 结束后写入"


@pytest.mark.asyncio
async def test_out_of_order_tasks_and_manual_edit_cannot_overwrite_other_generation(
    app_client,
    fake_ollama,
    seed_project,
    monkeypatch,
):
    project = "summary_out_of_order"
    save, session, _sources = await _seed_summaries(
        seed_project,
        project,
        [("A-old", "SOURCE-A"), ("B-old", "SOURCE-B")],
    )
    first_id, second_id = [item["id"] for item in session["summaries"]]
    entered = {"SOURCE-A": asyncio.Event(), "SOURCE-B": asyncio.Event()}
    release = {"SOURCE-A": asyncio.Event(), "SOURCE-B": asyncio.Event()}

    async def ordered(_model, dropped, **_kwargs):
        sentinel = dropped[0]["content"]
        entered[sentinel].set()
        await release[sentinel].wait()
        return f"前情提要: {sentinel}-new"

    monkeypatch.setattr(fake_ollama, "summarize_once", ordered)
    first_response = await app_client.post("/api/session/summary/regenerate", json={
        "project": project, "save": save,
        "expected_revision": session["revision"], "summary_id": first_id,
    })
    second_response = await app_client.post("/api/session/summary/regenerate", json={
        "project": project, "save": save,
        "expected_revision": first_response.json()["session"]["revision"],
        "summary_id": second_id,
    })
    assert second_response.status_code == 202
    await asyncio.wait_for(entered["SOURCE-A"].wait(), timeout=1)
    await asyncio.wait_for(entered["SOURCE-B"].wait(), timeout=1)

    release["SOURCE-B"].set()
    after_b = await _wait_summary(project, save, second_id, "completed")
    assert next(item for item in after_b["summaries"] if item["id"] == first_id)["status"] == "pending"
    assert next(item for item in after_b["summaries"] if item["id"] == second_id)["text"] == "SOURCE-B-new"

    edited = await app_client.patch("/api/session/summary", json={
        "project": project,
        "save": save,
        "expected_revision": after_b["revision"],
        "summary_id": first_id,
        "text": "人工内容优先",
    })
    assert edited.status_code == 200, edited.text
    edited_revision = edited.json()["session"]["revision"]
    release["SOURCE-A"].set()
    for _attempt in range(100):
        if not is_summary_generation_active(project, save, first_id):
            break
        await asyncio.sleep(0.01)
    final = get_session_store().read_sync(project, save)
    first = next(item for item in final["summaries"] if item["id"] == first_id)
    second = next(item for item in final["summaries"] if item["id"] == second_id)
    assert final["revision"] == edited_revision
    assert first["text"] == "人工内容优先"
    assert first["status"] == "completed"
    assert second["text"] == "SOURCE-B-new"
    await shutdown_summary_tasks()


def test_generated_summary_validation_rejects_empty_and_unbounded_fields():
    with pytest.raises(SummaryValidationError) as empty:
        validate_generated_summary("")
    assert empty.value.code == "summary_output_empty"
    with pytest.raises(SummaryValidationError) as long_text:
        validate_generated_summary("前情提要: " + "x" * 2001)
    assert long_text.value.code == "summary_field_too_long"


def test_import_rejects_duplicate_summary_identity_and_unsafe_source():
    duplicate = str(uuid4())
    base = {
        "session_id": "导入摘要",
        "message_history": [],
        "summaries": [
            {"id": duplicate, "text": "一"},
            {"id": duplicate, "text": "二"},
        ],
    }
    with pytest.raises(ValueError, match="摘要 id 不能重复"):
        validate_import_json(json.dumps(base, ensure_ascii=False))

    unsafe = deepcopy(base)
    unsafe["summaries"] = [{
        "id": str(uuid4()),
        "source_snapshot_id": "../other.trim.20260716_120000.json",
        "text": "一",
    }]
    with pytest.raises(ValueError, match="source_snapshot_id"):
        validate_import_json(json.dumps(unsafe, ensure_ascii=False))
