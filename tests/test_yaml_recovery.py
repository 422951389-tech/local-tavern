from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core import config
from core import character_loader
from core.character_loader import load_yaml
from core.recovery_store import DataCorruptionError, RecoveryStore
from tests.data_guard import file_manifest


ENTITY_CASES = (
    ("character", "broken_character", "/api/characters"),
    ("user", "user", "/api/user"),
    ("worldbook", "broken_worldbook", "/api/worldbook"),
)


def _yaml_path(entity_type: str, project: str, entity_id: str) -> Path:
    project_dir = config.PROJECTS_DIR / project
    if entity_type == "character":
        return project_dir / "characters" / f"{entity_id}.yaml"
    if entity_type == "worldbook":
        return project_dir / "worldbook" / f"{entity_id}.yaml"
    assert entity_type == "user"
    return project_dir / "user.yaml"


def _write_yaml_bytes(
    entity_type: str,
    project: str,
    entity_id: str,
    content: bytes,
) -> Path:
    path = _yaml_path(entity_type, project, entity_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _api_error(response) -> dict:
    body = response.json()
    return body.get("error") or body.get("detail")


def test_yaml_corruption_fingerprint_uses_the_parsed_bytes(tmp_path, monkeypatch):
    path = tmp_path / "stable.yaml"
    parsed_payload = b"key: value\n"
    changed_payload = b"key: changed\n"
    path.write_bytes(parsed_payload)

    def change_source_then_fail(_text):
        path.write_bytes(changed_payload)
        raise character_loader.yaml.YAMLError("injected parse failure")

    monkeypatch.setattr(character_loader.yaml, "safe_load", change_source_then_fail)

    with pytest.raises(DataCorruptionError) as raised:
        load_yaml(
            path,
            entity_type="character",
            project="fingerprint_project",
            entity_id="stable",
        )

    assert raised.value.fingerprint == _sha256(parsed_payload)
    assert path.read_bytes() == changed_payload


@pytest.mark.parametrize(
    "payload,reason",
    (
        (b"loop: &loop [*loop]\n", "invalid_yaml"),
        (
            "".join(f"{'  ' * depth}level_{depth}:\n" for depth in range(70)).encode()
            + b" " * 140
            + b"value: end\n",
            "invalid_yaml",
        ),
        (b"value: " + b"x" * (2 * 1024 * 1024), "yaml_too_large"),
    ),
    ids=("cyclic-alias", "excessive-depth", "excessive-size"),
)
def test_yaml_resource_limits_reject_unbounded_payloads(tmp_path, payload, reason):
    path = tmp_path / "bounded.yaml"
    path.write_bytes(payload)

    with pytest.raises(DataCorruptionError) as raised:
        load_yaml(
            path,
            entity_type="worldbook",
            project="bounded_project",
            entity_id="bounded",
        )

    assert raised.value.reason == reason
    assert raised.value.fingerprint == _sha256(payload)


@pytest.mark.asyncio
async def test_invalid_yaml_disk_id_does_not_offer_unusable_quarantine(app_client):
    project = "yaml_invalid_disk_id"
    raw = b"broken: [\n"
    _write_yaml_bytes("character", project, "bad name", raw)

    response = await app_client.get("/api/characters", params={"project": project})

    assert response.status_code == 422, response.text
    error = _api_error(response)
    assert error["code"] == "data_corrupt"
    assert error["entity_id"] == "bad name"
    assert error["quarantine_available"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type,entity_id,endpoint", ENTITY_CASES)
@pytest.mark.parametrize(
    "content",
    (
        b"key: [unterminated\n",
        b"- list-item\n",
        b"\xff\xfe\x00",
    ),
)
async def test_yaml_get_reports_corruption_without_any_write(
    app_client,
    isolated_paths,
    entity_type,
    entity_id,
    endpoint,
    content,
):
    suffix = hashlib.sha256(entity_type.encode() + content).hexdigest()[:10]
    project = f"yaml_read_{suffix}"
    path = _write_yaml_bytes(entity_type, project, entity_id, content)
    before = file_manifest(isolated_paths["root"])
    recovery_existed = config.RECOVERY_DIR.exists()

    response = await app_client.get(endpoint, params={"project": project})

    assert response.status_code == 422, response.text
    error = _api_error(response)
    assert error["code"] == "data_corrupt"
    assert error["entity_type"] == entity_type
    assert error["project"] == project
    assert error["entity_id"] == entity_id
    assert error["fingerprint"] == _sha256(content)
    assert error["quarantine_available"] is True
    assert "reason" not in error
    assert str(path.resolve(strict=False)) not in response.text
    assert str(isolated_paths["root"]) not in response.text
    assert file_manifest(isolated_paths["root"]) == before
    assert config.RECOVERY_DIR.exists() is recovery_existed


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type,entity_id,endpoint", ENTITY_CASES)
async def test_yaml_quarantine_is_cas_idempotent_and_restore_is_journaled(
    app_client,
    entity_type,
    entity_id,
    endpoint,
):
    project = f"yaml_cycle_{entity_type}"
    raw = f"broken_{entity_type}: [\n".encode()
    source = _write_yaml_bytes(entity_type, project, entity_id, raw)
    request = {
        "entity_type": entity_type,
        "project": project,
        "entity_id": entity_id,
        "fingerprint": _sha256(raw),
    }

    first = await app_client.post("/api/recovery/quarantine", json=request)
    assert first.status_code == 200, first.text
    recovery = first.json()["recovery"]
    assert first.json()["deduplicated"] is False
    assert recovery["category"] == "quarantine"
    assert recovery["operation"] == "parse_failure"
    assert recovery["entity_type"] == entity_type
    assert recovery["project"] == project
    assert recovery["entity_id"] == entity_id
    assert recovery["metadata"] == {"fingerprint": _sha256(raw)}
    assert len(recovery["items"]) == 1
    assert not source.exists()

    item = recovery["items"][0]
    expected_relpath = source.relative_to(config.PROJECTS_DIR).as_posix()
    assert item["source_relpath"] == expected_relpath
    assert item["payload_relpath"] == f"payload/{expected_relpath}"
    entry_dir = config.RECOVERY_DIR / "quarantine" / recovery["recovery_id"]
    payload = entry_dir.joinpath(*item["payload_relpath"].split("/"))
    assert payload.read_bytes() == raw

    second = await app_client.post("/api/recovery/quarantine", json=request)
    assert second.status_code == 200, second.text
    assert second.json()["deduplicated"] is True
    assert second.json()["recovery"]["recovery_id"] == recovery["recovery_id"]

    restored = await app_client.post(
        f"/api/recovery/{recovery['recovery_id']}/restore",
        json={},
    )
    assert restored.status_code == 200, restored.text
    restored_body = restored.json()
    assert restored_body["restored"] is True
    assert restored_body["target_relpath"] == expected_relpath
    assert source.read_bytes() == raw
    restored_manifest = restored_body["recovery"]
    assert restored_manifest["status"] == "restored"
    assert restored_manifest["restore_journal"]["operation"] == "restore_quarantine"
    assert restored_manifest["restore_journal"]["target_relpath"] == expected_relpath
    assert restored_manifest["restore_journal"]["target_existed"] is False
    assert restored_manifest["restore_journal"]["completed_at"]

    still_corrupt = await app_client.get(endpoint, params={"project": project})
    assert still_corrupt.status_code == 422
    assert _api_error(still_corrupt)["fingerprint"] == _sha256(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_type,entity_id,_endpoint", ENTITY_CASES)
async def test_yaml_quarantine_rejects_changed_fingerprint_without_removal(
    app_client,
    entity_type,
    entity_id,
    _endpoint,
):
    project = f"yaml_cas_{entity_type}"
    original = b"broken: [\n"
    changed = b"changed: [\n"
    source = _write_yaml_bytes(entity_type, project, entity_id, original)
    source.write_bytes(changed)

    response = await app_client.post(
        "/api/recovery/quarantine",
        json={
            "entity_type": entity_type,
            "project": project,
            "entity_id": entity_id,
            "fingerprint": _sha256(original),
        },
    )

    assert response.status_code == 409, response.text
    assert _api_error(response)["code"] == "source_changed"
    assert source.read_bytes() == changed
    listed = await app_client.get(
        "/api/recovery/items",
        params={
            "category": "quarantine",
            "project": project,
            "entity_type": entity_type,
        },
    )
    assert listed.status_code == 200
    assert listed.json() == {"items": []}


def test_yaml_quarantine_unlink_failure_keeps_source_and_retry_is_idempotent(
    tmp_path,
    monkeypatch,
):
    projects_root = tmp_path / "data" / "projects"
    recovery_root = tmp_path / "data" / ".recovery"
    store = RecoveryStore(recovery_root, projects_root=projects_root)
    project = "yaml_unlink_retry"
    entity_id = "retry_character"
    raw = b"broken: [\n"
    source = projects_root / project / "characters" / f"{entity_id}.yaml"
    source.parent.mkdir(parents=True)
    source.write_bytes(raw)
    original_unlink = Path.unlink
    injected = False

    def fail_source_unlink(path, *args, **kwargs):
        nonlocal injected
        if Path(path) == source and not injected:
            injected = True
            raise OSError("injected unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_source_unlink)
    with pytest.raises(OSError, match="injected unlink failure"):
        store.quarantine_entity(
            entity_type="character",
            project=project,
            entity_id=entity_id,
            source_path=source,
            fingerprint=_sha256(raw),
        )

    assert source.read_bytes() == raw
    entries = store.list_entries(
        category="quarantine",
        project=project,
        entity_type="character",
    )
    assert len(entries) == 1

    manifest, deduplicated = store.quarantine_entity(
        entity_type="character",
        project=project,
        entity_id=entity_id,
        source_path=source,
        fingerprint=_sha256(raw),
    )
    assert deduplicated is True
    assert manifest["recovery_id"] == entries[0]["recovery_id"]
    assert not source.exists()


@pytest.mark.asyncio
async def test_yaml_restore_rejects_target_and_explicit_overwrite(app_client):
    project = "yaml_restore_conflict"
    entity_type = "character"
    entity_id = "conflicted_character"
    raw = b"broken: [\n"
    source = _write_yaml_bytes(entity_type, project, entity_id, raw)
    quarantined = await app_client.post(
        "/api/recovery/quarantine",
        json={
            "entity_type": entity_type,
            "project": project,
            "entity_id": entity_id,
            "fingerprint": _sha256(raw),
        },
    )
    assert quarantined.status_code == 200, quarantined.text
    recovery_id = quarantined.json()["recovery"]["recovery_id"]
    replacement = b"id: conflicted_character\nname: replacement\n"
    source.write_bytes(replacement)

    conflict = await app_client.post(
        f"/api/recovery/{recovery_id}/restore",
        json={},
    )
    assert conflict.status_code == 409, conflict.text
    assert _api_error(conflict)["code"] == "target_exists"
    assert source.read_bytes() == replacement

    overwrite = await app_client.post(
        f"/api/recovery/{recovery_id}/restore",
        json={"overwrite": True},
    )
    assert overwrite.status_code == 400, overwrite.text
    assert source.read_bytes() == replacement


@pytest.mark.asyncio
async def test_forged_yaml_manifest_cannot_cross_project(app_client):
    project = "yaml_manifest_owner"
    victim_project = "yaml_manifest_victim"
    entity_id = "owner_entry"
    raw = b"broken: [\n"
    source = _write_yaml_bytes("worldbook", project, entity_id, raw)
    quarantined = await app_client.post(
        "/api/recovery/quarantine",
        json={
            "entity_type": "worldbook",
            "project": project,
            "entity_id": entity_id,
            "fingerprint": _sha256(raw),
        },
    )
    assert quarantined.status_code == 200, quarantined.text
    recovery = quarantined.json()["recovery"]
    manifest_path = (
        config.RECOVERY_DIR
        / "quarantine"
        / recovery["recovery_id"]
        / "manifest.json"
    )
    original_manifest = manifest_path.read_text(encoding="utf-8")
    forged = json.loads(original_manifest)
    forged["project"] = victim_project
    manifest_path.write_text(json.dumps(forged, ensure_ascii=False), encoding="utf-8")

    try:
        response = await app_client.post(
            f"/api/recovery/{recovery['recovery_id']}/restore",
            json={},
        )

        assert response.status_code == 409, response.text
        assert _api_error(response)["code"] == "recovery_integrity_error"
        assert not source.exists()
        assert not _yaml_path("worldbook", victim_project, entity_id).exists()
    finally:
        manifest_path.write_text(original_manifest, encoding="utf-8")


@pytest.mark.asyncio
async def test_yaml_restore_failure_compensates_and_persists_journal(
    app_client,
    monkeypatch,
):
    project = "yaml_restore_compensation"
    entity_id = "compensated_entry"
    raw = b"broken: [\n"
    source = _write_yaml_bytes("worldbook", project, entity_id, raw)
    quarantined = await app_client.post(
        "/api/recovery/quarantine",
        json={
            "entity_type": "worldbook",
            "project": project,
            "entity_id": entity_id,
            "fingerprint": _sha256(raw),
        },
    )
    assert quarantined.status_code == 200, quarantined.text
    recovery = quarantined.json()["recovery"]
    recovery_id = recovery["recovery_id"]
    original_mark_restored = RecoveryStore.mark_restored

    def fail_mark_restored(self, verified):
        del self, verified
        raise OSError("injected mark failure")

    monkeypatch.setattr(RecoveryStore, "mark_restored", fail_mark_restored)
    with pytest.raises(OSError, match="injected mark failure"):
        await app_client.post(
            f"/api/recovery/{recovery_id}/restore",
            json={},
        )

    assert not source.exists()
    manifest_path = (
        config.RECOVERY_DIR / "quarantine" / recovery_id / "manifest.json"
    )
    failed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert failed["status"] == "complete"
    assert failed["restore_journal"]["phase"] == "failed"
    assert failed["restore_journal"]["error_code"] == "io_error"
    assert failed["restore_journal"]["compensated"] is True

    monkeypatch.setattr(RecoveryStore, "mark_restored", original_mark_restored)
    retried = await app_client.post(
        f"/api/recovery/{recovery_id}/restore",
        json={},
    )
    assert retried.status_code == 200, retried.text
    assert source.read_bytes() == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("target_state", ("absent", "exact"))
async def test_yaml_restore_resumes_interrupted_journal(
    app_client,
    target_state,
):
    project = f"yaml_resume_{target_state}"
    entity_id = "resumable_entry"
    raw = b"broken: [\n"
    source = _write_yaml_bytes("worldbook", project, entity_id, raw)
    quarantined = await app_client.post(
        "/api/recovery/quarantine",
        json={
            "entity_type": "worldbook",
            "project": project,
            "entity_id": entity_id,
            "fingerprint": _sha256(raw),
        },
    )
    assert quarantined.status_code == 200, quarantined.text
    recovery = quarantined.json()["recovery"]
    store = RecoveryStore(config.RECOVERY_DIR, projects_root=config.PROJECTS_DIR)
    verified = store.get_verified(recovery["recovery_id"])
    store.begin_restore(
        verified,
        {
            "operation": "restore_quarantine",
            "target_relpath": recovery["source_relpath"],
            "target_existed": False,
            "phase": "copying",
        },
    )
    if target_state == "exact":
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(raw)

    resumed = await app_client.post(
        f"/api/recovery/{recovery['recovery_id']}/restore",
        json={},
    )

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["resumed"] is True
    assert resumed.json()["recovery"]["status"] == "restored"
    assert source.read_bytes() == raw


@pytest.mark.asyncio
async def test_yaml_restore_rejects_mismatched_interrupted_target(app_client):
    project = "yaml_resume_mismatch"
    entity_id = "resumable_entry"
    raw = b"broken: [\n"
    source = _write_yaml_bytes("character", project, entity_id, raw)
    quarantined = await app_client.post(
        "/api/recovery/quarantine",
        json={
            "entity_type": "character",
            "project": project,
            "entity_id": entity_id,
            "fingerprint": _sha256(raw),
        },
    )
    assert quarantined.status_code == 200, quarantined.text
    recovery = quarantined.json()["recovery"]
    store = RecoveryStore(config.RECOVERY_DIR, projects_root=config.PROJECTS_DIR)
    store.begin_restore(
        store.get_verified(recovery["recovery_id"]),
        {
            "operation": "restore_quarantine",
            "target_relpath": recovery["source_relpath"],
            "target_existed": False,
            "phase": "copying",
        },
    )
    replacement = b"id: resumable_entry\nname: replacement\n"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(replacement)

    response = await app_client.post(
        f"/api/recovery/{recovery['recovery_id']}/restore",
        json={},
    )

    assert response.status_code == 409, response.text
    assert _api_error(response)["code"] == "target_exists"
    assert source.read_bytes() == replacement
