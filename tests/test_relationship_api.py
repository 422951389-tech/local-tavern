from __future__ import annotations

import asyncio
import json
from uuid import UUID, uuid4

import pytest

from core import active_turns
from core.character_loader import ensure_project, save_character
from core.message_commands import RegenerationWouldRewriteHistory
from core.session_manager import get_session_store


def _message(index: int, *, pinned: bool = False) -> dict:
    return {
        "id": str(UUID(int=index + 1)),
        "role": "user" if index % 2 == 0 else "assistant",
        "content": f"证据消息 {index}",
        "pinned": pinned,
        "in_prompt": True,
    }


def _edge(evidence: list[str], relation_type: str = "盟友") -> dict:
    return {
        "source_character_id": "alpha",
        "target_character_id": "beta",
        "relation_type": relation_type,
        "strength": 70,
        "evidence_message_ids": evidence,
        "updated_at": "2026-07-22T12:00:00+08:00",
    }


def _seed_session(isolated_paths, project: str, *, save: str = "默认存档", revision: int = 0) -> tuple[object, list[dict]]:
    ensure_project(project)
    path = isolated_paths["projects"] / project / "saves" / f"{save}.json"
    if not path.exists():
        source = isolated_paths["projects"] / project / "saves" / "默认存档.json"
        payload = json.loads(source.read_text(encoding="utf-8"))
        payload["session_id"] = save
        payload["name"] = save
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
    messages = [_message(index) for index in range(4)]
    payload.update({
        "session_id": save,
        "project": project,
        "revision": revision,
        "characters_state": {
            "alpha": {"name": "阿尔法"},
            "beta": {"name": "贝塔"},
            "gamma": {"name": "伽马"},
        },
        "message_history": messages,
        "relationship_edges": [],
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, messages


@pytest.mark.asyncio
async def test_relationship_api_crud_key_edit_and_revision(app_client, isolated_paths):
    project = "relationship_api_crud"
    path, messages = _seed_session(isolated_paths, project)
    create = await app_client.put("/api/session/relationships", json={
        "project": project,
        "save": "默认存档",
        "expected_revision": 0,
        "edge": {
            "source_character_id": "alpha",
            "target_character_id": "beta",
            "relation_type": "  Ｓｔｒａße  ",
            "strength": 0,
            "evidence_message_ids": [messages[0]["id"]],
        },
    })
    assert create.status_code == 200, create.text
    created = create.json()
    assert created["session"]["revision"] == 1
    assert created["edge"]["relation_type"] == "Straße"
    assert created["edge"]["strength"] == 0
    assert created["edge"]["updated_at"]

    get_response = await app_client.get(
        "/api/session/relationships",
        params={"project": project, "save": "默认存档"},
    )
    assert get_response.status_code == 200, get_response.text
    assert get_response.json()["edges"] == [created["edge"]]

    update = await app_client.put("/api/session/relationships", json={
        "project": project,
        "save": "默认存档",
        "expected_revision": 1,
        "original_key": {
            "source_character_id": "alpha",
            "target_character_id": "beta",
            "relation_type": "STRASSE",
        },
        "edge": {
            "source_character_id": "beta",
            "target_character_id": "alpha",
            "relation_type": "挚友",
            "strength": 100,
            "evidence_message_ids": [messages[1]["id"], messages[0]["id"]],
        },
    })
    assert update.status_code == 200, update.text
    assert update.json()["session"]["revision"] == 2
    assert update.json()["edge"]["source_character_id"] == "beta"

    before_stale = path.read_bytes()
    stale = await app_client.request("DELETE", "/api/session/relationships", json={
        "project": project,
        "save": "默认存档",
        "expected_revision": 1,
        "key": {
            "source_character_id": "beta",
            "target_character_id": "alpha",
            "relation_type": "挚友",
        },
    })
    assert stale.status_code == 409, stale.text
    assert path.read_bytes() == before_stale

    deleted = await app_client.request("DELETE", "/api/session/relationships", json={
        "project": project,
        "save": "默认存档",
        "expected_revision": 2,
        "key": {
            "source_character_id": "beta",
            "target_character_id": "alpha",
            "relation_type": "挚友",
        },
    })
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["session"]["relationship_edges"] == []
    assert deleted.json()["session"]["revision"] == 3


@pytest.mark.asyncio
async def test_legacy_relationship_read_is_empty_and_does_not_write_disk(app_client, isolated_paths):
    project = "relationship_legacy_read"
    path, _messages = _seed_session(isolated_paths, project)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("relationship_edges", None)
    payload["summaries"] = [{"relations": ["不得迁移的文本关系"]}]
    payload["characters_state"]["alpha"].update({"affinity": 100, "mood": "亲密"})
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    before = path.read_bytes()

    response = await app_client.get(
        "/api/session/relationships",
        params={"project": project, "save": "默认存档"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["edges"] == []
    assert path.read_bytes() == before

    main_response = await app_client.get(
        "/api/session",
        params={"project": project, "save": "默认存档"},
    )
    assert main_response.status_code == 200, main_response.text
    main_session = main_response.json().get("session", main_response.json())
    assert main_session["relationship_edges"] == []
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_relationship_api_rejects_invalid_scope_and_active_turn(app_client, isolated_paths):
    project = "relationship_api_guards"
    path, messages = _seed_session(isolated_paths, project)
    base = {
        "project": project,
        "save": "默认存档",
        "expected_revision": 0,
        "edge": {
            "source_character_id": "alpha",
            "target_character_id": "beta",
            "relation_type": "盟友",
            "strength": 70,
            "evidence_message_ids": [messages[0]["id"]],
        },
    }
    for patch in (
        {"strength": True},
        {"source_character_id": "alpha", "target_character_id": "alpha"},
        {"source_character_id": "missing"},
        {"evidence_message_ids": [str(uuid4())]},
    ):
        before = path.read_bytes()
        body = {**base, "edge": {**base["edge"], **patch}}
        response = await app_client.put("/api/session/relationships", json=body)
        assert response.status_code == 422, response.text
        assert path.read_bytes() == before

    turn_id = str(uuid4())
    active_turns.register(project, "默认存档", turn_id)
    try:
        blocked = await app_client.put("/api/session/relationships", json=base)
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["error"]["code"] == "active_turn_conflict"
    finally:
        active_turns.unregister(project, "默认存档", turn_id)

    missing = await app_client.get(
        "/api/session/relationships",
        params={"project": project, "save": "missing"},
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_relationship_api_rejects_unicode_controls_and_concurrent_revision(app_client, isolated_paths):
    project = "relationship_api_unicode_concurrency"
    path, messages = _seed_session(isolated_paths, project)
    base = {
        "project": project,
        "save": "默认存档",
        "expected_revision": 0,
        "edge": {
            "source_character_id": "alpha",
            "target_character_id": "beta",
            "relation_type": "盟友",
            "strength": 70,
            "evidence_message_ids": [messages[0]["id"]],
        },
    }
    for unsafe in ("盟\u202e友", "盟\u200b友", "A\u0085B", "\ud800", "\udfff"):
        before = path.read_bytes()
        response = await app_client.put(
            "/api/session/relationships",
            json={**base, "edge": {**base["edge"], "relation_type": unsafe}},
        )
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["code"] == "request_validation_failed"
        for issue in error["details"]["issues"]:
            assert set(issue) <= {"type", "loc", "msg"}
            assert "input" not in issue
        assert path.read_bytes() == before

    first, second = await asyncio.gather(
        app_client.put("/api/session/relationships", json=base),
        app_client.put("/api/session/relationships", json={
            **base,
            "edge": {**base["edge"], "relation_type": "竞争"},
        }),
    )
    assert sorted((first.status_code, second.status_code)) == [200, 409]
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["revision"] == 1
    assert len(stored["relationship_edges"]) == 1


@pytest.mark.asyncio
async def test_relationship_get_rejects_unsafe_legacy_type_without_encoding_500(app_client, isolated_paths):
    project = "relationship_api_unsafe_legacy"
    path, messages = _seed_session(isolated_paths, project)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["relationship_edges"] = [_edge([messages[0]["id"]], "\ud800")]
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    before = path.read_bytes()

    response = await app_client.get(
        "/api/session/relationships",
        params={"project": project, "save": "默认存档"},
    )
    assert response.status_code == 422, response.text
    assert "\\ud800" not in response.text.lower()

    main_response = await app_client.get(
        "/api/session",
        params={"project": project, "save": "默认存档"},
    )
    assert main_response.status_code == 422, main_response.text
    assert "\\ud800" not in main_response.text.lower()
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_message_delete_and_truncate_reconcile_relationship_evidence(app_client, isolated_paths):
    project = "relationship_message_lifecycle"
    path, messages = _seed_session(isolated_paths, project)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["relationship_edges"] = [_edge([messages[0]["id"], messages[1]["id"]])]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    first = await app_client.patch(
        "/api/session",
        params={"project": project, "save": "默认存档"},
        json={"action": "delete", "message_id": messages[0]["id"], "expected_revision": 0},
    )
    assert first.status_code == 200, first.text
    assert first.json()["relationship_edges"][0]["evidence_message_ids"] == [messages[1]["id"]]

    second = await app_client.patch(
        "/api/session",
        params={"project": project, "save": "默认存档"},
        json={"action": "truncate", "message_id": messages[1]["id"], "expected_revision": 1},
    )
    assert second.status_code == 200, second.text
    assert second.json()["relationship_edges"] == []


@pytest.mark.asyncio
async def test_historical_regenerate_rejection_preserves_relationship_evidence(
    isolated_paths,
):
    project = "relationship_regenerate"
    path, messages = _seed_session(isolated_paths, project)
    messages[0].update({"role": "user", "content": "重新生成源", "turn_id": "old"})
    messages[1].update({"role": "assistant", "turn_id": "old"})
    messages[3]["pinned"] = True
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["message_history"] = messages
    payload["relationship_edges"] = [_edge([messages[1]["id"], messages[3]["id"]])]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    before = path.read_bytes()
    store = get_session_store()
    turn_id = str(uuid4())
    try:
        with pytest.raises(RegenerationWouldRewriteHistory):
            await store.accept_regenerated_chat_turn(
                project,
                "默认存档",
                0,
                turn_id=turn_id,
                target_message_id=messages[1]["id"],
                expected_user_input="重新生成源",
                created_at="2026-07-22T12:00:00+08:00",
            )
        assert path.read_bytes() == before
    finally:
        active_turns.unregister(project, "默认存档", turn_id)


@pytest.mark.asyncio
async def test_character_delete_cleans_incident_edges_across_saves(isolated_paths):
    project = "relationship_character_delete"
    ensure_project(project)
    save_character(project, "alpha", {"id": "alpha", "name": "阿尔法", "active": True})
    save_character(project, "beta", {"id": "beta", "name": "贝塔", "active": True})
    first_path, first_messages = _seed_session(isolated_paths, project)
    second_path, second_messages = _seed_session(isolated_paths, project, save="第二存档")
    for path, messages in ((first_path, first_messages), (second_path, second_messages)):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["relationship_edges"] = [
            _edge([messages[0]["id"]], "盟友"),
            {
                **_edge([messages[1]["id"]], "竞争"),
                "source_character_id": "beta",
                "target_character_id": "gamma",
            },
        ]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    from core.destructive_service import DestructiveService

    result = await DestructiveService(get_session_store()).delete_character(
        project,
        "alpha",
        "默认存档",
        0,
    )
    assert result["affected_saves"] == ["第二存档", "默认存档"]
    for path in (first_path, second_path):
        session = json.loads(path.read_text(encoding="utf-8"))
        assert "alpha" not in session["characters_state"]
        assert [edge["relation_type"] for edge in session["relationship_edges"]] == ["竞争"]


@pytest.mark.asyncio
async def test_relationship_edges_never_enter_prompt_or_model_writeback(
    app_client,
    fake_ollama,
    isolated_paths,
):
    project = "relationship_prompt_isolation"
    path, messages = _seed_session(isolated_paths, project)
    sentinel = "RELATION_SENTINEL_DO_NOT_PROMPT"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["relationship_edges"] = [_edge([messages[0]["id"]], sentinel)]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    fake_ollama.configure("normal", block_before_first=True)

    created = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": "默认存档",
        "user_input": "验证关系隔离",
        "model": "fake-model:latest",
        "expected_revision": 0,
    })
    assert created.status_code == 202, created.text
    await asyncio.wait_for(fake_ollama.entered.wait(), timeout=1)
    prompt_payload = json.dumps(fake_ollama.chat_calls[-1]["messages"], ensure_ascii=False)
    assert sentinel not in prompt_payload

    fake_ollama.release.set()
    turn_id = created.json()["turn_id"]
    for _ in range(100):
        turn = (await app_client.get(f"/api/chat/turns/{turn_id}")).json()
        if turn["status"] in {"completed", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.01)
    assert turn["status"] == "completed"
    session = (await app_client.get("/api/session", params={
        "project": project,
        "save": "默认存档",
    })).json()
    assert session["relationship_edges"][0]["relation_type"] == sentinel
