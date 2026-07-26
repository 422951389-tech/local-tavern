from __future__ import annotations

import json
from uuid import uuid4

import pytest
import yaml

from core import active_turns
from core.character_loader import ensure_project
from core.import_validation import validate_import_json


def _seed_session(isolated_paths, project: str, states: dict) -> object:
    ensure_project(project)
    path = isolated_paths["projects"] / project / "saves" / "默认存档.json"
    session = json.loads(path.read_text(encoding="utf-8"))
    session["characters_state"] = states
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_legacy_character_defaults_schema_and_strict_save(
    app_client,
    isolated_paths,
):
    project = "roleplay_character"
    ensure_project(project)
    path = isolated_paths["projects"] / project / "characters" / "legacy.yaml"
    legacy = {
        "id": "legacy",
        "name": "旧角色",
        "aliases": ["旧名"],
        "custom_fact": {"kept": True},
    }
    path.write_text(yaml.safe_dump(legacy, allow_unicode=True), encoding="utf-8")
    before = path.read_bytes()

    listed = await app_client.get("/api/characters", params={"project": project})
    assert listed.status_code == 200, listed.text
    assert listed.json()["characters"][0]["chattiness"] == 50
    assert path.read_bytes() == before

    schema_response = await app_client.get("/api/schema/character")
    fields = {
        field["key"]: field
        for group in schema_response.json()["groups"]
        for field in group["fields"]
    }
    assert fields["chattiness"] == {
        "key": "chattiness",
        "label": "发言倾向 (0-100)",
        "type": "number",
        "min": 0,
        "max": 100,
        "default": 50,
        "hint": "仅作为群聊提示权重，不强制角色每轮发言",
    }

    saved = await app_client.put(
        "/api/characters/legacy",
        params={"project": project},
        json={"data": legacy},
    )
    assert saved.status_code == 200, saved.text
    on_disk = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert on_disk["chattiness"] == 50
    assert on_disk["custom_fact"] == {"kept": True}

    valid = await app_client.put(
        "/api/characters/legacy",
        params={"project": project},
        json={"data": {**on_disk, "chattiness": 100}},
    )
    assert valid.status_code == 200, valid.text
    before_invalid = path.read_bytes()
    for invalid in (True, "50", 50.0, -1, 101):
        response = await app_client.put(
            "/api/characters/legacy",
            params={"project": project},
            json={"data": {**on_disk, "chattiness": invalid}},
        )
        assert response.status_code == 400, response.text
        assert path.read_bytes() == before_invalid


@pytest.mark.asyncio
async def test_character_affinity_save_is_strict_bounded_and_zero_write(
    app_client,
    isolated_paths,
):
    project = "roleplay_affinity"
    ensure_project(project)
    character_dir = isolated_paths["projects"] / project / "characters"
    path = character_dir / "affinity.yaml"
    base = {"id": "affinity", "name": "边界角色"}

    for affinity in (0, 50.5, 100):
        response = await app_client.put(
            "/api/characters/affinity",
            params={"project": project},
            json={
                "data": {
                    **base,
                    "initial_stats": {"affinity": affinity},
                }
            },
        )
        assert response.status_code == 200, response.text
        assert yaml.safe_load(path.read_text(encoding="utf-8"))[
            "initial_stats"
        ]["affinity"] == affinity

    def snapshot_character_dir() -> dict[str, bytes]:
        return {
            item.name: item.read_bytes()
            for item in character_dir.iterdir()
            if item.is_file()
        }

    before_invalid = snapshot_character_dir()
    invalid_affinities = (
        True,
        "50",
        None,
        -1,
        101,
        float("nan"),
        float("inf"),
        float("-inf"),
    )
    for affinity in invalid_affinities:
        payload = {
            "data": {
                **base,
                "initial_stats": {"affinity": affinity},
            }
        }
        response = await app_client.put(
            "/api/characters/affinity",
            params={"project": project},
            content=json.dumps(payload, ensure_ascii=False, allow_nan=True),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 400, response.text
        assert snapshot_character_dir() == before_invalid

    for initial_stats in (True, "invalid", [], None):
        response = await app_client.put(
            "/api/characters/affinity",
            params={"project": project},
            json={"data": {**base, "initial_stats": initial_stats}},
        )
        assert response.status_code == 400, response.text
        assert snapshot_character_dir() == before_invalid


def test_import_roleplay_fields_are_strict_and_bounded():
    base = {
        "session_id": "roleplay_import",
        "message_history": [],
        "characters_state": {"alpha": {"name": "阿尔法"}},
    }
    defaults = validate_import_json(json.dumps(base, ensure_ascii=False))
    assert defaults["characters_state"]["alpha"]["remaining_silent_turns"] == 0
    assert defaults["roleplay_policy"] == {"strict_muted_writeback": False}

    valid = validate_import_json(json.dumps({
        **base,
        "characters_state": {
            "alpha": {"remaining_silent_turns": 999},
            "beta": {"remaining_silent_turns": 0},
        },
        "roleplay_policy": {"strict_muted_writeback": True},
    }))
    assert valid["characters_state"]["alpha"]["remaining_silent_turns"] == 999
    assert valid["roleplay_policy"]["strict_muted_writeback"] is True

    for invalid in (True, "1", 1.0, -1, 1000):
        with pytest.raises(ValueError, match="remaining_silent_turns"):
            validate_import_json(json.dumps({
                **base,
                "characters_state": {
                    "alpha": {"remaining_silent_turns": invalid},
                },
            }))
    for invalid in (0, 1, "true", None):
        with pytest.raises(ValueError, match="strict_muted_writeback"):
            validate_import_json(json.dumps({
                **base,
                "roleplay_policy": {"strict_muted_writeback": invalid},
            }))


@pytest.mark.asyncio
async def test_roleplay_patch_success_boundaries_and_cas_are_atomic(
    app_client,
    isolated_paths,
):
    project = "roleplay_patch_success"
    path = _seed_session(
        isolated_paths,
        project,
        {"alpha": {"name": "阿尔法", "remaining_silent_turns": 0}},
    )

    silence = await app_client.patch(
        "/api/session/characters/alpha/silence",
        json={
            "project": project,
            "save": "默认存档",
            "expected_revision": 0,
            "remaining_silent_turns": 999,
        },
    )
    assert silence.status_code == 200, silence.text
    assert silence.json()["session"]["revision"] == 1
    assert silence.json()["session"]["characters_state"]["alpha"][
        "remaining_silent_turns"
    ] == 999

    policy = await app_client.patch(
        "/api/session/roleplay-policy",
        json={
            "project": project,
            "save": "默认存档",
            "expected_revision": 1,
            "strict_muted_writeback": True,
        },
    )
    assert policy.status_code == 200, policy.text
    assert policy.json()["session"]["revision"] == 2
    assert policy.json()["session"]["roleplay_policy"] == {
        "strict_muted_writeback": True,
    }

    zero = await app_client.patch(
        "/api/session/characters/alpha/silence",
        json={
            "project": project,
            "save": "默认存档",
            "expected_revision": 2,
            "remaining_silent_turns": 0,
        },
    )
    assert zero.status_code == 200, zero.text
    assert zero.json()["session"]["revision"] == 3

    for url, body in (
        (
            "/api/session/characters/alpha/silence",
            {"remaining_silent_turns": 1},
        ),
        (
            "/api/session/roleplay-policy",
            {"strict_muted_writeback": False},
        ),
    ):
        before = path.read_bytes()
        response = await app_client.patch(url, json={
            "project": project,
            "save": "默认存档",
            "expected_revision": 2,
            **body,
        })
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "revision_conflict"
        assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_roleplay_patch_rejects_invalid_scope_active_turn_and_maintenance(
    app_client,
    isolated_paths,
):
    project = "roleplay_patch_guards"
    path = _seed_session(
        isolated_paths,
        project,
        {"alpha": {"name": "阿尔法", "remaining_silent_turns": 2}},
    )

    invalid_requests = (
        (
            "/api/session/characters/alpha/silence",
            {"remaining_silent_turns": True},
        ),
        (
            "/api/session/characters/alpha/silence",
            {"remaining_silent_turns": 1000},
        ),
        (
            "/api/session/roleplay-policy",
            {"strict_muted_writeback": "true"},
        ),
    )
    for url, body in invalid_requests:
        before = path.read_bytes()
        response = await app_client.patch(url, json={
            "project": project,
            "save": "默认存档",
            "expected_revision": 0,
            **body,
        })
        assert response.status_code == 422, response.text
        assert path.read_bytes() == before

    for url, body, expected_status in (
        (
            "/api/session/characters/missing/silence",
            {"remaining_silent_turns": 1},
            404,
        ),
        (
            "/api/session/characters/bad%20id/silence",
            {"remaining_silent_turns": 1},
            400,
        ),
    ):
        before = path.read_bytes()
        response = await app_client.patch(url, json={
            "project": project,
            "save": "默认存档",
            "expected_revision": 0,
            **body,
        })
        assert response.status_code == expected_status, response.text
        assert path.read_bytes() == before

    other_project = "roleplay_patch_other_project"
    other_path = _seed_session(
        isolated_paths,
        other_project,
        {"beta": {"name": "贝塔", "remaining_silent_turns": 0}},
    )
    other_before = other_path.read_bytes()
    cross_project = await app_client.patch(
        "/api/session/characters/alpha/silence",
        json={
            "project": other_project,
            "save": "默认存档",
            "expected_revision": 0,
            "remaining_silent_turns": 1,
        },
    )
    assert cross_project.status_code == 404, cross_project.text
    assert other_path.read_bytes() == other_before

    missing_save = await app_client.patch(
        "/api/session/roleplay-policy",
        json={
            "project": project,
            "save": "missing_save",
            "expected_revision": 0,
            "strict_muted_writeback": True,
        },
    )
    assert missing_save.status_code == 404, missing_save.text
    assert not (path.parent / "missing_save.json").exists()

    turn_id = str(uuid4())
    active_turns.register(project, "默认存档", turn_id)
    try:
        for url, body in (
            (
                "/api/session/characters/alpha/silence",
                {"remaining_silent_turns": 1},
            ),
            (
                "/api/session/roleplay-policy",
                {"strict_muted_writeback": True},
            ),
        ):
            before = path.read_bytes()
            response = await app_client.patch(url, json={
                "project": project,
                "save": "默认存档",
                "expected_revision": 0,
                **body,
            })
            assert response.status_code == 409, response.text
            assert response.json()["error"]["code"] == "active_turn_conflict"
            assert path.read_bytes() == before
    finally:
        active_turns.unregister(project, "默认存档", turn_id)

    token = active_turns.begin_maintenance("roleplay-test")
    try:
        before = path.read_bytes()
        blocked = await app_client.patch(
            "/api/session/roleplay-policy",
            json={
                "project": project,
                "save": "默认存档",
                "expected_revision": 0,
                "strict_muted_writeback": True,
            },
        )
        assert blocked.status_code == 503, blocked.text
        assert blocked.json()["error"]["code"] == "turn_maintenance"
        assert path.read_bytes() == before
    finally:
        active_turns.end_maintenance(token)
