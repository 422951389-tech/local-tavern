from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import core.recovery_store as recovery_module
from core.recovery_store import (
    RecoveryConflict,
    RecoveryIntegrityError,
    RecoveryStore,
)
from core.session_store import SessionStore


def _write_source(projects_root: Path, relative: str, content: bytes) -> Path:
    path = projects_root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _store(tmp_path: Path) -> tuple[RecoveryStore, Path, Path]:
    projects_root = tmp_path / "data" / "projects"
    recovery_root = tmp_path / "data" / ".recovery"
    return (
        RecoveryStore(
            recovery_root,
            projects_root=projects_root,
            retention_days=30,
        ),
        projects_root,
        recovery_root,
    )


def _single_manifest_path(recovery_root: Path, category: str) -> Path:
    manifests = list((recovery_root / category).glob("*/manifest.json"))
    assert len(manifests) == 1
    return manifests[0]


def test_recovery_reads_are_pure_when_root_does_not_exist(tmp_path):
    store, _, recovery_root = _store(tmp_path)

    assert store.list_entries() == []
    assert not recovery_root.exists()


def test_checkpoint_manifest_covers_every_payload_with_size_and_sha256(tmp_path):
    store, projects_root, recovery_root = _store(tmp_path)
    session = _write_source(
        projects_root,
        "project_a/saves/save_a.json",
        b'{"revision": 7, "message_history": []}',
    )
    history = _write_source(
        projects_root,
        "project_a/saves/.history/save_a.snapshot.json",
        b'{"snapshot": true}',
    )

    manifest = store.create_session_entry(
        category="checkpoint",
        operation="reset",
        project="project_a",
        save_id="save_a",
        session_path=session,
        source_revision=7,
        history_paths=[history],
        metadata={"reason": "test"},
    )

    assert manifest["manifest_version"] == 1
    assert manifest["category"] == "checkpoint"
    assert manifest["operation"] == "reset"
    assert manifest["entity_type"] == "session"
    assert manifest["project"] == "project_a"
    assert manifest["entity_id"] == "save_a"
    assert manifest["source_revision"] == 7
    assert manifest["status"] == "complete"
    assert manifest["restored_at"] is None
    assert manifest["metadata"] == {"reason": "test"}
    assert len(manifest["items"]) == 2

    manifest_path = _single_manifest_path(recovery_root, "checkpoint")
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert on_disk == manifest
    entry_dir = manifest_path.parent
    for item in manifest["items"]:
        payload = entry_dir.joinpath(*item["payload_relpath"].split("/"))
        assert payload.is_file()
        assert payload.stat().st_size == item["size"]
        assert hashlib.sha256(payload.read_bytes()).hexdigest() == item["sha256"]
        source = projects_root.joinpath(*item["source_relpath"].split("/"))
        assert payload.read_bytes() == source.read_bytes()

    verified = store.get_verified(manifest["recovery_id"])
    assert verified.manifest == manifest


def test_quarantine_is_deduplicated_and_rejects_changed_fingerprint(tmp_path):
    store, projects_root, _ = _store(tmp_path)
    source = _write_source(
        projects_root,
        "project_q/saves/broken.json",
        b'{"broken":',
    )
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()

    first, first_deduplicated = store.quarantine_session(
        project="project_q",
        save_id="broken",
        session_path=source,
        fingerprint=fingerprint,
    )
    second, second_deduplicated = store.quarantine_session(
        project="project_q",
        save_id="broken",
        session_path=source,
        fingerprint=fingerprint.upper(),
    )

    assert first_deduplicated is False
    assert second_deduplicated is True
    assert second["recovery_id"] == first["recovery_id"]
    assert store.list_entries(category="quarantine", project="project_q") == [first]

    source.write_bytes(b'{"changed":')
    with pytest.raises(RecoveryConflict) as exc_info:
        store.quarantine_session(
            project="project_q",
            save_id="broken",
            session_path=source,
            fingerprint=fingerprint,
        )
    assert exc_info.value.code == "source_changed"
    assert len(store.list_entries(category="quarantine", project="project_q")) == 1


def test_manifest_write_failure_keeps_source_and_leaves_no_complete_entry(
    tmp_path,
    monkeypatch,
):
    store, projects_root, recovery_root = _store(tmp_path)
    original = b'{"revision": 2}'
    source = _write_source(
        projects_root,
        "project_f/saves/save_f.json",
        original,
    )

    def fail_manifest_write(path: Path, text: str) -> None:
        del path, text
        raise OSError("injected manifest failure")

    monkeypatch.setattr(recovery_module, "_atomic_write_text", fail_manifest_write)

    with pytest.raises(OSError, match="injected manifest failure"):
        store.create_session_entry(
            category="checkpoint",
            operation="reset",
            project="project_f",
            save_id="save_f",
            session_path=source,
            source_revision=2,
        )

    assert source.read_bytes() == original
    assert not list((recovery_root / "checkpoint").glob("*/manifest.json"))
    assert not list((recovery_root / ".staging").glob("*"))


def test_final_directory_replace_failure_is_cleaned_without_touching_source(
    tmp_path,
    monkeypatch,
):
    store, projects_root, recovery_root = _store(tmp_path)
    original = b'{"revision": 3}'
    source = _write_source(
        projects_root,
        "project_r/saves/save_r.json",
        original,
    )
    real_replace = recovery_module.os.replace

    def fail_final_replace(source_path, target_path):
        source_path = Path(source_path)
        target_path = Path(target_path)
        if source_path.parent.name == ".staging" and target_path.parent.name == "checkpoint":
            raise OSError("injected final replace failure")
        return real_replace(source_path, target_path)

    monkeypatch.setattr(recovery_module.os, "replace", fail_final_replace)

    with pytest.raises(OSError, match="injected final replace failure"):
        store.create_session_entry(
            category="checkpoint",
            operation="rename",
            project="project_r",
            save_id="save_r",
            session_path=source,
            source_revision=3,
        )

    assert source.read_bytes() == original
    assert not list((recovery_root / "checkpoint").glob("*/manifest.json"))
    assert not list((recovery_root / ".staging").glob("*"))


def test_forged_manifest_path_and_tampered_payload_are_rejected(tmp_path):
    store, projects_root, recovery_root = _store(tmp_path)
    source = _write_source(
        projects_root,
        "project_i/saves/save_i.json",
        b'{"revision": 4}',
    )
    first = store.create_session_entry(
        category="trash",
        operation="delete",
        project="project_i",
        save_id="save_i",
        session_path=source,
        source_revision=4,
    )
    first_manifest_path = _single_manifest_path(recovery_root, "trash")
    forged = json.loads(first_manifest_path.read_text(encoding="utf-8"))
    forged["source_relpath"] = "../../outside.json"
    forged["items"][0]["source_relpath"] = "../../outside.json"
    first_manifest_path.write_text(
        json.dumps(forged, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(RecoveryIntegrityError, match="越界路径"):
        store.get_verified(first["recovery_id"])

    second_source = _write_source(
        projects_root,
        "project_i/saves/save_j.json",
        b'{"revision": 5}',
    )
    second = store.create_session_entry(
        category="trash",
        operation="delete",
        project="project_i",
        save_id="save_j",
        session_path=second_source,
        source_revision=5,
    )
    second_verified = store.get_verified(second["recovery_id"])
    payload = store.payload_path(second_verified, second["items"][0])
    payload.write_bytes(payload.read_bytes() + b"tampered")

    with pytest.raises(RecoveryIntegrityError, match="大小不匹配|哈希不匹配"):
        store.get_verified(second["recovery_id"])


@pytest.mark.asyncio
async def test_damaged_trash_payload_cannot_restore_or_create_partial_target(tmp_path):
    recovery, projects_root, _ = _store(tmp_path)
    sessions = SessionStore(projects_root, recovery_store=recovery)
    (projects_root / "project_t").mkdir(parents=True, exist_ok=True)
    target = {
        "session_id": "target",
        "name": "target",
        "project": "project_t",
        "revision": 0,
        "message_history": [],
    }
    keeper = {
        "session_id": "keeper",
        "name": "keeper",
        "project": "project_t",
        "revision": 0,
        "message_history": [],
    }
    created = await sessions.create("project_t", "target", target)
    await sessions.create("project_t", "keeper", keeper)
    deleted = await sessions.delete(
        "project_t",
        "target",
        created["revision"],
    )
    assert deleted is not None
    assert not sessions.session_path("project_t", "target").exists()

    verified = recovery.get_verified(deleted.recovery_id)
    payload = recovery.payload_path(verified, verified.manifest["items"][0])
    payload.write_bytes(payload.read_bytes() + b"corrupt")

    with pytest.raises(RecoveryIntegrityError, match="大小不匹配|哈希不匹配"):
        await sessions.restore_recovery(deleted.recovery_id)

    assert not sessions.session_path("project_t", "target").exists()
    assert sessions.session_path("project_t", "keeper").is_file()
