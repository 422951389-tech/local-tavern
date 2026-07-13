from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core import config
from tests.data_guard import file_manifest


def _write_corrupt_save(
    project: str,
    save: str,
    content: bytes = b'{"broken":',
) -> Path:
    path = config.PROJECTS_DIR / project / "saves" / f"{save}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _error(response) -> dict:
    body = response.json()
    assert set(body) == {"error"}, body
    return body["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        b'{"broken":',
        b"[]",
        b"\xff\xfe\x00",
    ],
)
async def test_corrupt_session_get_is_structured_pure_and_path_safe(
    app_client,
    isolated_paths,
    content,
):
    suffix = hashlib.sha256(content).hexdigest()[:8]
    project = f"corrupt_get_{suffix}"
    save = f"broken_{suffix}"
    path = _write_corrupt_save(project, save, content)
    before = file_manifest(isolated_paths["root"])
    real_before = file_manifest(isolated_paths["real_data"])
    recovery_root = config.RECOVERY_DIR
    recovery_existed = recovery_root.exists()

    response = await app_client.get(
        "/api/session",
        params={"project": project, "save": save},
    )

    assert response.status_code == 422, response.text
    error = _error(response)
    assert error["code"] == "data_corrupt"
    assert isinstance(error["message"], str) and error["message"]
    assert error["entity_type"] == "session"
    assert error["project"] == project
    assert error["entity_id"] == save
    assert error["fingerprint"] == _fingerprint(content)
    assert error["quarantine_available"] is True
    assert str(path.resolve(strict=False)) not in response.text
    assert str(isolated_paths["root"]) not in response.text
    assert file_manifest(isolated_paths["root"]) == before
    assert file_manifest(isolated_paths["real_data"]) == real_before
    assert recovery_root.exists() is recovery_existed


@pytest.mark.asyncio
async def test_session_list_surfaces_corrupt_entry_without_writing(app_client, isolated_paths):
    project = "corrupt_list_project"
    save = "corrupt_list_save"
    raw = b'{"list_broken":'
    _write_corrupt_save(project, save, raw)
    before = file_manifest(isolated_paths["root"])

    response = await app_client.get("/api/sessions", params={"project": project})

    assert response.status_code == 200, response.text
    sessions = response.json()["sessions"]
    corrupt = next(item for item in sessions if item["session_id"] == save)
    assert corrupt["status"] == "corrupt"
    assert corrupt["error_code"] == "data_corrupt"
    assert corrupt["fingerprint"] == _fingerprint(raw)
    assert corrupt["quarantine_available"] is True
    assert file_manifest(isolated_paths["root"]) == before


@pytest.mark.asyncio
async def test_quarantine_is_explicit_idempotent_and_removes_bad_source(app_client):
    project = "corrupt_quarantine_project"
    save = "corrupt_quarantine_save"
    raw = b'{"quarantine_broken":'
    path = _write_corrupt_save(project, save, raw)
    fingerprint = _fingerprint(raw)
    request = {
        "entity_type": "session",
        "project": project,
        "entity_id": save,
        "fingerprint": fingerprint,
    }

    first = await app_client.post("/api/recovery/quarantine", json=request)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert set(first_body) >= {"recovery", "deduplicated"}
    assert first_body["deduplicated"] is False
    recovery = first_body["recovery"]
    assert recovery["category"] == "quarantine"
    assert recovery["entity_type"] == "session"
    assert recovery["project"] == project
    assert recovery["entity_id"] == save
    assert recovery["metadata"]["fingerprint"] == fingerprint
    assert not path.exists()

    item = recovery["items"][0]
    entry_dir = config.RECOVERY_DIR / "quarantine" / recovery["recovery_id"]
    payload = entry_dir.joinpath(*item["payload_relpath"].split("/"))
    assert payload.read_bytes() == raw
    assert item["size"] == len(raw)
    assert item["sha256"] == fingerprint

    second = await app_client.post("/api/recovery/quarantine", json=request)
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["deduplicated"] is True
    assert second_body["recovery"]["recovery_id"] == recovery["recovery_id"]

    listed = await app_client.get(
        "/api/recovery/items",
        params={
            "category": "quarantine",
            "project": project,
            "entity_type": "session",
        },
    )
    assert listed.status_code == 200, listed.text
    assert [item["recovery_id"] for item in listed.json()["items"]] == [
        recovery["recovery_id"]
    ]


@pytest.mark.asyncio
async def test_changed_fingerprint_rejects_quarantine_without_recovery_write(app_client):
    project = "corrupt_changed_project"
    save = "corrupt_changed_save"
    original = b'{"first":'
    changed = b'{"second":'
    path = _write_corrupt_save(project, save, original)

    detected = await app_client.get(
        "/api/session",
        params={"project": project, "save": save},
    )
    assert detected.status_code == 422, detected.text
    old_fingerprint = _error(detected)["fingerprint"]
    path.write_bytes(changed)

    response = await app_client.post(
        "/api/recovery/quarantine",
        json={
            "entity_type": "session",
            "project": project,
            "entity_id": save,
            "fingerprint": old_fingerprint,
        },
    )

    assert response.status_code == 409, response.text
    error = response.json().get("error") or response.json().get("detail")
    assert error["code"] == "source_changed"
    assert path.read_bytes() == changed
    listed = await app_client.get(
        "/api/recovery/items",
        params={"category": "quarantine", "project": project},
    )
    assert listed.status_code == 200, listed.text
    assert listed.json() == {"items": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "quarantine_payload",
    [
        {
            "entity_type": "session",
            "project": "corrupt_invalid_project",
            "entity_id": "corrupt_invalid_save",
            "fingerprint": "not-a-sha256",
        },
        {
            "entity_type": "character",
            "project": "corrupt_invalid_project",
            "entity_id": "corrupt_invalid_save",
            "fingerprint": "0" * 64,
        },
        {
            "entity_type": "session",
            "project": "../outside",
            "entity_id": "corrupt_invalid_save",
            "fingerprint": "0" * 64,
        },
        {
            "entity_type": "session",
            "project": "corrupt_invalid_project",
            "entity_id": "../outside",
            "fingerprint": "0" * 64,
        },
    ],
)
async def test_invalid_quarantine_identity_is_rejected_without_writes(
    app_client,
    isolated_paths,
    quarantine_payload,
):
    before = file_manifest(isolated_paths["root"])

    response = await app_client.post(
        "/api/recovery/quarantine",
        json=quarantine_payload,
    )

    assert response.status_code in {400, 422}
    assert file_manifest(isolated_paths["root"]) == before


@pytest.mark.asyncio
async def test_quarantine_payload_cannot_be_restored_as_a_valid_session(app_client):
    project = "corrupt_no_restore_project"
    save = "corrupt_no_restore_save"
    raw = b'{"cannot_restore":'
    _write_corrupt_save(project, save, raw)
    quarantined = await app_client.post(
        "/api/recovery/quarantine",
        json={
            "entity_type": "session",
            "project": project,
            "entity_id": save,
            "fingerprint": _fingerprint(raw),
        },
    )
    assert quarantined.status_code == 200, quarantined.text
    recovery_id = quarantined.json()["recovery"]["recovery_id"]

    response = await app_client.post(
        f"/api/recovery/{recovery_id}/restore",
        json={},
    )

    assert response.status_code in {400, 409, 422}, response.text
    assert not (config.PROJECTS_DIR / project / "saves" / f"{save}.json").exists()
