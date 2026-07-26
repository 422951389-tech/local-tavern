from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from core import config, session_manager
from tests.data_guard import file_manifest


CHARACTER_ID = "destructive_character"
WORLD_ID = "destructive_world"
SAVE_IDS = (
    "destructive_active",
    "destructive_side_a",
    "destructive_side_b",
)


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


async def _seed_project(project: str, *, saves: tuple[str, ...] = SAVE_IDS) -> dict:
    project_dir = config.PROJECTS_DIR / project
    _write_yaml(
        project_dir / "characters" / f"{CHARACTER_ID}.yaml",
        {
            "id": CHARACTER_ID,
            "name": "待删除角色",
            "active": True,
        },
    )
    _write_yaml(
        project_dir / "user.yaml",
        {
            "id": "user",
            "name": "待删除用户",
            "status": {"condition": "正文不得出现在错误响应"},
        },
    )
    _write_yaml(
        project_dir / "worldbook" / f"{WORLD_ID}.yaml",
        {
            "id": WORLD_ID,
            "title": "待删除世界书",
            "content": "世界书秘密正文",
        },
    )
    created: dict[str, dict] = {}
    for index, save in enumerate(saves):
        session = session_manager.new_session(project, save)
        session["characters_state"][CHARACTER_ID] = {
            "name": "待删除角色",
            "affinity": 20 + index,
        }
        session["user_status"] = {
            "name": "待删除用户",
            "identity": f"身份-{index}",
            "condition": "正常",
            "abilities": ["测试"],
        }
        session_manager.append_history(session, "user", f"存档正文-{save}")
        created[save] = await session_manager.create_session(project, save, session)
    return {
        "project_dir": project_dir,
        "character_path": project_dir / "characters" / f"{CHARACTER_ID}.yaml",
        "user_path": project_dir / "user.yaml",
        "world_path": project_dir / "worldbook" / f"{WORLD_ID}.yaml",
        "sessions": created,
    }


def _error(response) -> dict:
    body = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert isinstance(error, dict), body
    return error


def _assert_private_error(response, *, forbidden_text: str = "") -> None:
    assert str(config.PROJECTS_DIR) not in response.text
    assert str(config.DATA_DIR) not in response.text
    if forbidden_text:
        assert forbidden_text not in response.text


async def _recovery_items(
    app_client,
    *,
    project: str,
    entity_type: str,
) -> list[dict]:
    response = await app_client.get(
        "/api/recovery/items",
        params={
            "category": "trash",
            "project": project,
            "entity_type": entity_type,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _assert_restore_envelope(
    body: dict,
    *,
    recovery_id: str,
    entity_type: str,
    project: str,
    entity_id: str,
) -> None:
    assert body["restored"] is True
    assert body["recovery_id"] == recovery_id
    assert body["undo_recovery_id"] is None
    assert body["entity_type"] == entity_type
    assert body["project"] == project
    assert body["entity_id"] == entity_id


@pytest.mark.asyncio
async def test_character_delete_updates_three_saves_once_and_restores(
    app_client,
    isolated_paths,
):
    project = "destructive_api_character"
    seeded = await _seed_project(project)
    real_before = file_manifest(isolated_paths["real_data"])

    response = await app_client.delete(
        f"/api/characters/{CHARACTER_ID}",
        params={
            "project": project,
            "save": SAVE_IDS[0],
            "expected_revision": seeded["sessions"][SAVE_IDS[0]]["revision"],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] is True
    assert body["id"] == CHARACTER_ID
    assert body["recovery_id"]
    assert set(body["affected_saves"]) == set(SAVE_IDS)
    assert body["session"]["session_id"] == SAVE_IDS[0]
    assert not seeded["character_path"].exists()
    for save in SAVE_IDS:
        current = session_manager.load_session(project, save)
        assert current["revision"] == seeded["sessions"][save]["revision"] + 1
        assert CHARACTER_ID not in current["characters_state"]

    items = await _recovery_items(
        app_client,
        project=project,
        entity_type="character",
    )
    assert [item["recovery_id"] for item in items] == [body["recovery_id"]]
    assert items[0]["operation"] == "delete_character"

    restored = await app_client.post(
        f"/api/recovery/{body['recovery_id']}/restore",
        json={},
    )
    assert restored.status_code == 200, restored.text
    _assert_restore_envelope(
        restored.json(),
        recovery_id=body["recovery_id"],
        entity_type="character",
        project=project,
        entity_id=CHARACTER_ID,
    )
    assert seeded["character_path"].is_file()
    for save in SAVE_IDS:
        assert CHARACTER_ID in session_manager.load_session(project, save)[
            "characters_state"
        ]
    assert file_manifest(isolated_paths["real_data"]) == real_before


@pytest.mark.asyncio
async def test_user_delete_cleans_every_save_and_restores(app_client):
    project = "destructive_api_user"
    seeded = await _seed_project(project)

    response = await app_client.delete(
        "/api/user",
        params={
            "project": project,
            "save": SAVE_IDS[0],
            "expected_revision": seeded["sessions"][SAVE_IDS[0]]["revision"],
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] is True
    assert body["recovery_id"]
    assert set(body["affected_saves"]) == set(SAVE_IDS)
    assert body["session"]["session_id"] == SAVE_IDS[0]
    assert not seeded["user_path"].exists()
    for save in SAVE_IDS:
        current = session_manager.load_session(project, save)
        assert current["revision"] == seeded["sessions"][save]["revision"] + 1
        assert current["user_status"] == {
            "name": "",
            "identity": "",
            "condition": "",
            "abilities": [],
        }

    restored = await app_client.post(
        f"/api/recovery/{body['recovery_id']}/restore",
        json={},
    )
    assert restored.status_code == 200, restored.text
    _assert_restore_envelope(
        restored.json(),
        recovery_id=body["recovery_id"],
        entity_type="user",
        project=project,
        entity_id="user",
    )
    assert seeded["user_path"].is_file()
    for save in SAVE_IDS:
        assert session_manager.load_session(project, save)["user_status"]["name"] == (
            "待删除用户"
        )


@pytest.mark.asyncio
async def test_worldbook_delete_only_moves_yaml_to_trash(app_client):
    project = "destructive_api_worldbook"
    seeded = await _seed_project(project)
    session_hashes = {
        save: hashlib.sha256(
            (seeded["project_dir"] / "saves" / f"{save}.json").read_bytes()
        ).hexdigest()
        for save in SAVE_IDS
    }

    response = await app_client.delete(
        f"/api/worldbook/{WORLD_ID}",
        params={"project": project},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] is True
    assert body["id"] == WORLD_ID
    assert body["recovery_id"]
    assert body["affected_saves"] == []
    assert not seeded["world_path"].exists()
    for save in SAVE_IDS:
        path = seeded["project_dir"] / "saves" / f"{save}.json"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == session_hashes[save]

    manifest = (await _recovery_items(
        app_client,
        project=project,
        entity_type="worldbook",
    ))[0]
    assert manifest["operation"] == "delete_worldbook"
    assert len(manifest["items"]) == 1

    restored = await app_client.post(
        f"/api/recovery/{body['recovery_id']}/restore",
        json={},
    )
    assert restored.status_code == 200, restored.text
    _assert_restore_envelope(
        restored.json(),
        recovery_id=body["recovery_id"],
        entity_type="worldbook",
        project=project,
        entity_id=WORLD_ID,
    )
    assert seeded["world_path"].is_file()


@pytest.mark.asyncio
async def test_project_delete_and_restore_round_trip(app_client):
    project = "destructive_api_project"
    seeded = await _seed_project(project)
    await _seed_project("destructive_api_project_keeper", saves=("keeper",))
    before = file_manifest(seeded["project_dir"])

    response = await app_client.request(
        "DELETE",
        "/api/projects",
        json={"name": project},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] is True
    assert body["recovery_id"]
    assert not seeded["project_dir"].exists()

    restored = await app_client.post(
        f"/api/recovery/{body['recovery_id']}/restore",
        json={},
    )
    assert restored.status_code == 200, restored.text
    _assert_restore_envelope(
        restored.json(),
        recovery_id=body["recovery_id"],
        entity_type="project",
        project=project,
        entity_id=project,
    )
    assert file_manifest(seeded["project_dir"]) == before


@pytest.mark.asyncio
async def test_restore_rejects_when_any_affected_save_changed_and_writes_nothing(
    app_client,
):
    project = "destructive_api_restore_conflict"
    seeded = await _seed_project(project)
    deleted = await app_client.delete(
        f"/api/characters/{CHARACTER_ID}",
        params={
            "project": project,
            "save": SAVE_IDS[0],
            "expected_revision": seeded["sessions"][SAVE_IDS[0]]["revision"],
        },
    )
    assert deleted.status_code == 200, deleted.text
    recovery_id = deleted.json()["recovery_id"]
    changed_save = SAVE_IDS[1]
    changed = session_manager.load_session(project, changed_save)
    switched = await app_client.post(
        "/api/model/switch",
        json={
            "project": project,
            "save": changed_save,
            "model": "fake-model:latest",
            "expected_revision": changed["revision"],
        },
    )
    assert switched.status_code == 200, switched.text
    before = file_manifest(config.DATA_DIR)

    response = await app_client.post(
        f"/api/recovery/{recovery_id}/restore",
        json={},
    )

    assert response.status_code == 409, response.text
    assert _error(response)["code"] in {
        "revision_conflict",
        "target_changed",
        "recovery_conflict",
    }
    _assert_private_error(response, forbidden_text="存档正文")
    assert file_manifest(config.DATA_DIR) == before


@pytest.mark.asyncio
async def test_stale_active_revision_aborts_character_delete_before_any_write(
    app_client,
):
    project = "destructive_api_stale"
    seeded = await _seed_project(project)
    before = file_manifest(config.DATA_DIR)
    active_revision = seeded["sessions"][SAVE_IDS[0]]["revision"]

    response = await app_client.delete(
        f"/api/characters/{CHARACTER_ID}",
        params={
            "project": project,
            "save": SAVE_IDS[0],
            "expected_revision": active_revision - 1,
        },
    )

    assert response.status_code == 409, response.text
    assert _error(response)["code"] == "revision_conflict"
    _assert_private_error(response, forbidden_text="存档正文")
    assert file_manifest(config.DATA_DIR) == before


@pytest.mark.asyncio
async def test_corrupt_sibling_save_aborts_delete_before_recovery_or_mutation(app_client):
    project = "destructive_api_corrupt"
    seeded = await _seed_project(project)
    corrupt_path = seeded["project_dir"] / "saves" / f"{SAVE_IDS[2]}.json"
    corrupt_body = b'{"secret_corrupt_body":'
    corrupt_path.write_bytes(corrupt_body)
    before = file_manifest(config.DATA_DIR)

    response = await app_client.delete(
        f"/api/characters/{CHARACTER_ID}",
        params={
            "project": project,
            "save": SAVE_IDS[0],
            "expected_revision": seeded["sessions"][SAVE_IDS[0]]["revision"],
        },
    )

    assert response.status_code == 422, response.text
    assert _error(response)["code"] == "data_corrupt"
    _assert_private_error(response, forbidden_text="secret_corrupt_body")
    assert file_manifest(config.DATA_DIR) == before
    assert seeded["character_path"].is_file()
