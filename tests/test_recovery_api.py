from __future__ import annotations

import hashlib
import json
from urllib.parse import quote

import pytest

from core import config, recovery_store, session_manager
from tests.data_guard import file_manifest


async def _seed_session(project: str, save: str, content: str = "原始消息") -> dict:
    session = session_manager.new_session(project, save)
    session_manager.append_history(session, "user", content)
    return await session_manager.create_session(project, save, session)


async def _snapshot(app_client, project: str, save: str, revision: int) -> str:
    response = await app_client.patch(
        "/api/session",
        params={"project": project, "save": save},
        json={"action": "snapshot", "expected_revision": revision},
    )
    assert response.status_code == 200, response.text
    return response.json()["filename"]


async def _items(app_client, *, project: str, category: str | None = None) -> list[dict]:
    params = {"project": project, "entity_type": "session"}
    if category is not None:
        params["category"] = category
    response = await app_client.get("/api/recovery/items", params=params)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def _find(items: list[dict], recovery_id: str) -> dict:
    return next(item for item in items if item.get("recovery_id") == recovery_id)


@pytest.mark.asyncio
async def test_recovery_list_is_pure_and_filtered(app_client, isolated_paths):
    project = "recovery_list_empty"
    before = file_manifest(isolated_paths["root"])

    response = await app_client.get(
        "/api/recovery/items",
        params={
            "project": project,
            "category": "checkpoint",
            "entity_type": "session",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"items": []}
    assert file_manifest(isolated_paths["root"]) == before


@pytest.mark.asyncio
async def test_reset_checkpoint_can_be_restored_and_creates_undo(
    app_client,
):
    project = "recovery_reset_project"
    save = "recovery_reset_save"
    created = await _seed_session(project, save, "重置前必须保留")

    reset = await app_client.post(
        "/api/session/reset",
        json={
            "project": project,
            "save": save,
            "expected_revision": created["revision"],
        },
    )
    assert reset.status_code == 200, reset.text
    reset_body = reset.json()
    assert set(reset_body) >= {"session", "recovery_id"}
    assert reset_body["recovery_id"]
    assert reset_body["session"]["message_history"] == []

    checkpoint = _find(
        await _items(app_client, project=project, category="checkpoint"),
        reset_body["recovery_id"],
    )
    assert checkpoint["operation"] == "reset"
    assert checkpoint["source_revision"] == created["revision"]

    restored = await app_client.post(
        f"/api/recovery/{reset_body['recovery_id']}/restore",
        json={"expected_revision": reset_body["session"]["revision"]},
    )
    assert restored.status_code == 200, restored.text
    restored_body = restored.json()
    assert restored_body["restored"] is True
    assert restored_body["recovery_id"] == reset_body["recovery_id"]
    assert restored_body["undo_recovery_id"]
    assert restored_body["undo_recovery_id"] != reset_body["recovery_id"]
    assert restored_body["session"]["message_history"][0]["content"] == "重置前必须保留"

    items = await _items(app_client, project=project, category="checkpoint")
    undo = _find(items, restored_body["undo_recovery_id"])
    assert undo["operation"] == "restore_undo"
    assert undo["metadata"]["restored_from"] == reset_body["recovery_id"]


@pytest.mark.asyncio
async def test_checkpoint_restore_rejects_stale_revision_without_mutation(app_client):
    project = "recovery_stale_project"
    save = "recovery_stale_save"
    created = await _seed_session(project, save)
    reset = await app_client.post(
        "/api/session/reset",
        json={
            "project": project,
            "save": save,
            "expected_revision": created["revision"],
        },
    )
    assert reset.status_code == 200, reset.text
    reset_body = reset.json()

    switched = await app_client.post(
        "/api/model/switch",
        json={
            "project": project,
            "save": save,
            "model": "fake-model:latest",
            "expected_revision": reset_body["session"]["revision"],
        },
    )
    assert switched.status_code == 200, switched.text
    current = switched.json()["session"]
    path = config.PROJECTS_DIR / project / "saves" / f"{save}.json"
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    stale = await app_client.post(
        f"/api/recovery/{reset_body['recovery_id']}/restore",
        json={"expected_revision": reset_body["session"]["revision"]},
    )

    assert stale.status_code == 409, stale.text
    error = stale.json()["error"]
    assert error["code"] == "revision_conflict"
    assert error["details"]["current_revision"] == current["revision"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.asyncio
async def test_rename_checkpoint_contains_main_save_and_owned_history(app_client):
    project = "recovery_rename_project"
    old_save = "recovery_rename_old"
    new_save = "recovery_rename_new"
    created = await _seed_session(project, old_save)
    await _snapshot(app_client, project, old_save, created["revision"])

    renamed = await app_client.post(
        "/api/sessions/rename",
        json={
            "project": project,
            "save": old_save,
            "new_name": new_save,
            "expected_revision": created["revision"],
        },
    )
    assert renamed.status_code == 200, renamed.text
    body = renamed.json()
    assert body["session"]["session_id"] == new_save
    assert body["recovery_id"]

    manifest = _find(
        await _items(app_client, project=project, category="checkpoint"),
        body["recovery_id"],
    )
    assert manifest["operation"] == "rename"
    assert manifest["metadata"]["renamed_to"] == new_save
    source_paths = {item["source_relpath"] for item in manifest["items"]}
    assert f"{project}/saves/{old_save}.json" in source_paths
    assert any(
        path.startswith(f"{project}/saves/.history/{old_save}.")
        for path in source_paths
    )


@pytest.mark.asyncio
async def test_rename_checkpoint_restore_reverses_main_and_history_ownership(app_client):
    project = "recovery_rename_undo_project"
    old_save = "recovery_rename_undo_old"
    new_save = "recovery_rename_undo_new"
    created = await _seed_session(project, old_save, "重命名前内容")
    snapshot_filename = await _snapshot(
        app_client,
        project,
        old_save,
        created["revision"],
    )

    renamed = await app_client.post(
        "/api/sessions/rename",
        json={
            "project": project,
            "save": old_save,
            "new_name": new_save,
            "expected_revision": created["revision"],
        },
    )
    assert renamed.status_code == 200, renamed.text
    renamed_body = renamed.json()
    recovery_id = renamed_body["recovery_id"]
    renamed_revision = renamed_body["session"]["revision"]
    saves_dir = config.PROJECTS_DIR / project / "saves"
    history_dir = saves_dir / ".history"
    old_path = saves_dir / f"{old_save}.json"
    new_path = saves_dir / f"{new_save}.json"
    renamed_history = history_dir / (
        new_save + snapshot_filename[len(old_save):]
    )
    assert not old_path.exists()
    assert new_path.is_file()
    assert not (history_dir / snapshot_filename).exists()
    assert renamed_history.is_file()

    restored = await app_client.post(
        f"/api/recovery/{recovery_id}/restore",
        json={"expected_revision": renamed_revision},
    )

    assert restored.status_code == 200, restored.text
    restored_body = restored.json()
    assert restored_body["restored"] is True
    assert restored_body["recovery_id"] == recovery_id
    assert restored_body["session"]["session_id"] == old_save
    assert restored_body["session"]["message_history"][0]["content"] == "重命名前内容"
    assert old_path.is_file()
    assert not new_path.exists()
    assert (history_dir / snapshot_filename).is_file()
    assert not renamed_history.exists()
    assert all(
        not path.name.startswith(f"{new_save}.")
        for path in history_dir.iterdir()
        if path.is_file()
    )


@pytest.mark.asyncio
async def test_snapshot_restore_creates_checkpoint_before_replacing_session(app_client):
    project = "recovery_snapshot_project"
    save = "recovery_snapshot_save"
    created = await _seed_session(project, save, "快照原文")
    message_id = created["message_history"][0]["id"]
    filename = await _snapshot(app_client, project, save, created["revision"])

    edited = await app_client.patch(
        "/api/session",
        params={"project": project, "save": save},
        json={
            "action": "edit",
            "message_id": message_id,
            "content": "恢复前当前内容",
            "expected_revision": created["revision"],
        },
    )
    assert edited.status_code == 200, edited.text

    restored = await app_client.post(
        "/api/session/restore",
        json={
            "project": project,
            "save": save,
            "filename": filename,
            "expected_revision": edited.json()["revision"],
        },
    )
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["recovery_id"]
    assert body["session"]["message_history"][0]["content"] == "快照原文"

    checkpoint = _find(
        await _items(app_client, project=project, category="checkpoint"),
        body["recovery_id"],
    )
    assert checkpoint["operation"] == "restore"
    assert checkpoint["metadata"]["snapshot_filename"] == filename
    primary = checkpoint["items"][0]
    entry_dir = config.RECOVERY_DIR / "checkpoint" / body["recovery_id"]
    payload = entry_dir.joinpath(*primary["payload_relpath"].split("/"))
    saved_before_restore = json.loads(payload.read_text(encoding="utf-8"))
    assert saved_before_restore["message_history"][0]["content"] == "恢复前当前内容"


@pytest.mark.asyncio
async def test_session_delete_moves_main_and_history_to_trash_then_restores(app_client):
    project = "recovery_trash_project"
    save = "recovery_trash_target"
    keeper = "recovery_trash_keeper"
    created = await _seed_session(project, save, "删除后恢复")
    await _seed_session(project, keeper, "保留存档")
    filename = await _snapshot(app_client, project, save, created["revision"])

    deleted = await app_client.post(
        "/api/sessions/delete",
        json={
            "project": project,
            "save": save,
            "expected_revision": created["revision"],
        },
    )
    assert deleted.status_code == 200, deleted.text
    deleted_body = deleted.json()
    assert deleted_body["deleted"] is True
    assert deleted_body["recovery_id"]
    save_path = config.PROJECTS_DIR / project / "saves" / f"{save}.json"
    history_path = config.PROJECTS_DIR / project / "saves" / ".history" / filename
    assert not save_path.exists()
    assert not history_path.exists()

    trash = _find(
        await _items(app_client, project=project, category="trash"),
        deleted_body["recovery_id"],
    )
    assert trash["operation"] == "delete"
    assert len(trash["items"]) >= 2
    assert trash["expires_at"]

    restored = await app_client.post(
        f"/api/recovery/{deleted_body['recovery_id']}/restore",
        json={},
    )
    assert restored.status_code == 200, restored.text
    restored_body = restored.json()
    assert restored_body["restored"] is True
    assert restored_body["recovery_id"] == deleted_body["recovery_id"]
    assert restored_body.get("undo_recovery_id") is None
    assert restored_body["session"]["message_history"][0]["content"] == "删除后恢复"
    assert save_path.is_file()
    assert history_path.is_file()


@pytest.mark.asyncio
async def test_trash_restore_mark_failure_rolls_back_and_remains_retryable(
    app_client,
    monkeypatch,
):
    project = "recovery_trash_retry_project"
    save = "recovery_trash_retry_target"
    created = await _seed_session(project, save, "恢复提交失败后可重试")
    await _seed_session(project, "recovery_trash_retry_keeper", "保留")
    snapshot_filename = await _snapshot(
        app_client,
        project,
        save,
        created["revision"],
    )
    deleted = await app_client.post(
        "/api/sessions/delete",
        json={
            "project": project,
            "save": save,
            "expected_revision": created["revision"],
        },
    )
    assert deleted.status_code == 200, deleted.text
    recovery_id = deleted.json()["recovery_id"]
    recovery = session_manager.get_session_store().recovery_store
    verified_before = recovery.get_verified(recovery_id)
    manifest_path = verified_before.entry_dir / "manifest.json"
    manifest_before = manifest_path.read_bytes()
    payload_before = {
        item["payload_relpath"]: recovery.payload_path(verified_before, item).read_bytes()
        for item in verified_before.manifest["items"]
    }
    saves_dir = config.PROJECTS_DIR / project / "saves"
    save_path = saves_dir / f"{save}.json"
    history_path = saves_dir / ".history" / snapshot_filename
    assert not save_path.exists()
    assert not history_path.exists()

    original_mark_restored = recovery_store.RecoveryStore.mark_restored

    def fail_mark_restored(self, verified):
        del self, verified
        raise OSError("injected mark_restored failure")

    monkeypatch.setattr(
        recovery_store.RecoveryStore,
        "mark_restored",
        fail_mark_restored,
    )

    with pytest.raises(OSError, match="injected mark_restored failure"):
        await app_client.post(
            f"/api/recovery/{recovery_id}/restore",
            json={},
        )

    assert not save_path.exists()
    assert not history_path.exists()
    assert manifest_path.read_bytes() == manifest_before
    verified_after_failure = recovery.get_verified(recovery_id)
    assert verified_after_failure.manifest["status"] == "complete"
    assert {
        item["payload_relpath"]: recovery.payload_path(
            verified_after_failure,
            item,
        ).read_bytes()
        for item in verified_after_failure.manifest["items"]
    } == payload_before

    monkeypatch.setattr(
        recovery_store.RecoveryStore,
        "mark_restored",
        original_mark_restored,
    )
    retried = await app_client.post(
        f"/api/recovery/{recovery_id}/restore",
        json={},
    )

    assert retried.status_code == 200, retried.text
    retried_body = retried.json()
    assert retried_body["restored"] is True
    assert retried_body["session"]["message_history"][0]["content"] == "恢复提交失败后可重试"
    assert save_path.is_file()
    assert history_path.is_file()
    assert recovery.get_verified(recovery_id).manifest["status"] == "restored"


@pytest.mark.asyncio
async def test_trash_restore_conflict_and_forged_recovery_id_do_not_write(app_client):
    project = "recovery_conflict_project"
    save = "recovery_conflict_target"
    created = await _seed_session(project, save, "待删除")
    await _seed_session(project, "recovery_conflict_keeper", "保留")
    deleted = await app_client.post(
        "/api/sessions/delete",
        json={
            "project": project,
            "save": save,
            "expected_revision": created["revision"],
        },
    )
    assert deleted.status_code == 200, deleted.text
    recovery_id = deleted.json()["recovery_id"]
    replacement = await _seed_session(project, save, "冲突目标不得覆盖")
    save_path = config.PROJECTS_DIR / project / "saves" / f"{save}.json"
    before = hashlib.sha256(save_path.read_bytes()).hexdigest()

    conflict = await app_client.post(
        f"/api/recovery/{recovery_id}/restore",
        json={"overwrite": False},
    )
    assert conflict.status_code == 409, conflict.text
    error = conflict.json()["error"]
    assert error["code"] == "target_exists"
    assert hashlib.sha256(save_path.read_bytes()).hexdigest() == before
    assert session_manager.load_session(project, save)["revision"] == replacement["revision"]

    all_before = file_manifest(config.DATA_DIR)
    forged_id = quote("../outside", safe="")
    forged = await app_client.post(
        f"/api/recovery/{forged_id}/restore",
        json={},
    )
    assert forged.status_code in {400, 404, 422}
    assert file_manifest(config.DATA_DIR) == all_before


@pytest.mark.asyncio
async def test_checkpoint_creation_failure_aborts_reset_before_snapshot_or_write(
    app_client,
    monkeypatch,
):
    project = "recovery_checkpoint_fault_project"
    save = "recovery_checkpoint_fault_save"
    created = await _seed_session(project, save, "故障后仍存在")
    save_path = config.PROJECTS_DIR / project / "saves" / f"{save}.json"
    history_dir = save_path.parent / ".history"
    before = save_path.read_bytes()
    snapshots_before = set(history_dir.glob("*")) if history_dir.exists() else set()

    def fail_checkpoint(*args, **kwargs):
        del args, kwargs
        raise OSError("injected checkpoint failure")

    monkeypatch.setattr(recovery_store.RecoveryStore, "create_session_entry", fail_checkpoint)

    with pytest.raises(OSError, match="injected checkpoint failure"):
        await app_client.post(
            "/api/session/reset",
            json={
                "project": project,
                "save": save,
                "expected_revision": created["revision"],
            },
        )

    assert save_path.read_bytes() == before
    snapshots_after = set(history_dir.glob("*")) if history_dir.exists() else set()
    assert snapshots_after == snapshots_before
