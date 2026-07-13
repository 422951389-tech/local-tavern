from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
import yaml

import core.destructive_service as destructive_module
from core.destructive_service import (
    DestructiveOperationError,
    DestructiveRecoveryRequired,
    DestructiveService,
)
from core.recovery_store import RecoveryStore
from core.session_store import SessionStore
from tests.data_guard import file_manifest


CHARACTER_ID = "service_character"
WORLD_ID = "service_world"
SAVE_IDS = ("active", "side_a", "side_b")


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


async def _seed_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    project: str = "service_project",
    keeper: bool = False,
) -> dict:
    projects_root = tmp_path / "data" / "projects"
    recovery_root = tmp_path / "data" / ".recovery"
    project_dir = projects_root / project
    character_path = project_dir / "characters" / f"{CHARACTER_ID}.yaml"
    user_path = project_dir / "user.yaml"
    world_path = project_dir / "worldbook" / f"{WORLD_ID}.yaml"

    _write_yaml(
        character_path,
        {"id": CHARACTER_ID, "name": "故障注入角色", "active": True},
    )
    _write_yaml(
        user_path,
        {"id": "user", "name": "故障注入用户", "status": "正常"},
    )
    _write_yaml(
        world_path,
        {"id": WORLD_ID, "title": "故障注入世界书", "content": "正文"},
    )
    if keeper:
        keeper_path = projects_root / "keeper_project" / "README.txt"
        keeper_path.parent.mkdir(parents=True, exist_ok=True)
        keeper_path.write_text("至少保留一个项目", encoding="utf-8")

    recovery = RecoveryStore(recovery_root, projects_root=projects_root)
    sessions = SessionStore(projects_root, recovery_store=recovery)
    service = DestructiveService(sessions)
    created: dict[str, dict] = {}
    for index, save_id in enumerate(SAVE_IDS):
        created[save_id] = await sessions.create(
            project,
            save_id,
            {
                "name": save_id,
                "characters_state": {
                    CHARACTER_ID: {"name": "故障注入角色", "affinity": index},
                },
                "user_status": {
                    "name": "故障注入用户",
                    "identity": f"身份-{index}",
                    "condition": "正常",
                    "abilities": ["测试"],
                },
                "message_history": [],
            },
        )
    return {
        "project": project,
        "projects_root": projects_root,
        "recovery_root": recovery_root,
        "project_dir": project_dir,
        "character_path": character_path,
        "user_path": user_path,
        "world_path": world_path,
        "recovery": recovery,
        "sessions": sessions,
        "service": service,
        "created": created,
    }


def _snapshot_sources(projects_root: Path, paths: list[Path]) -> dict[str, bytes]:
    return {
        path.relative_to(projects_root).as_posix(): path.read_bytes()
        for path in paths
    }


def _assert_manifest_payloads(
    recovery: RecoveryStore,
    recovery_id: str,
    expected: dict[str, bytes],
) -> dict:
    verified = recovery.get_verified(recovery_id)
    manifest = verified.manifest
    items = {item["source_relpath"]: item for item in manifest["items"]}
    assert set(items) == set(expected)
    for source_relpath, original in expected.items():
        item = items[source_relpath]
        assert item["size"] == len(original)
        assert item["sha256"] == hashlib.sha256(original).hexdigest()
        assert recovery.payload_path(verified, item).read_bytes() == original
    return manifest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_type", "operation"),
    [
        ("character", "delete_character"),
        ("user", "delete_user"),
        ("worldbook", "delete_worldbook"),
        ("project", "delete_project"),
    ],
)
async def test_trash_manifest_is_exact_and_each_entity_restores(
    tmp_path,
    monkeypatch,
    entity_type,
    operation,
):
    seeded = await _seed_service(
        tmp_path,
        monkeypatch,
        keeper=entity_type == "project",
    )
    service = seeded["service"]
    sessions = seeded["sessions"]
    project = seeded["project"]
    save_paths = [sessions.session_path(project, save_id) for save_id in SAVE_IDS]

    if entity_type == "character":
        source_paths = [seeded["character_path"], *save_paths]
        expected = _snapshot_sources(seeded["projects_root"], source_paths)
        result = await service.delete_character(
            project,
            CHARACTER_ID,
            SAVE_IDS[0],
            seeded["created"][SAVE_IDS[0]]["revision"],
        )
        assert result["affected_saves"] == list(SAVE_IDS)
    elif entity_type == "user":
        source_paths = [seeded["user_path"], *save_paths]
        expected = _snapshot_sources(seeded["projects_root"], source_paths)
        result = await service.delete_user(
            project,
            SAVE_IDS[0],
            seeded["created"][SAVE_IDS[0]]["revision"],
        )
        assert result["affected_saves"] == list(SAVE_IDS)
    elif entity_type == "worldbook":
        source_paths = [seeded["world_path"]]
        expected = _snapshot_sources(seeded["projects_root"], source_paths)
        saves_before = file_manifest(seeded["project_dir"] / "saves")
        result = await service.delete_worldbook(project, WORLD_ID)
        assert result["affected_saves"] == []
        assert file_manifest(seeded["project_dir"] / "saves") == saves_before
    else:
        source_paths = sorted(
            (path for path in seeded["project_dir"].rglob("*") if path.is_file()),
            key=lambda path: path.as_posix(),
        )
        expected = _snapshot_sources(seeded["projects_root"], source_paths)
        project_before = file_manifest(seeded["project_dir"])
        result = await service.delete_project(project)

    manifest = _assert_manifest_payloads(
        seeded["recovery"],
        result["recovery_id"],
        expected,
    )
    assert manifest["category"] == "trash"
    assert manifest["entity_type"] == entity_type
    assert manifest["operation"] == operation

    restored = await service.restore_trash(result["recovery_id"])
    assert restored == {
        "restored": True,
        "recovery_id": result["recovery_id"],
        "undo_recovery_id": None,
        "entity_type": entity_type,
        "project": project,
        "entity_id": project
        if entity_type == "project"
        else (CHARACTER_ID if entity_type == "character" else WORLD_ID if entity_type == "worldbook" else "user"),
    }
    if entity_type == "project":
        assert file_manifest(seeded["project_dir"]) == project_before
    elif entity_type == "character":
        for save_id in SAVE_IDS:
            assert CHARACTER_ID in (await sessions.read(project, save_id))["characters_state"]
    elif entity_type == "user":
        for save_id in SAVE_IDS:
            assert (await sessions.read(project, save_id))["user_status"]["name"] == "故障注入用户"


@pytest.mark.asyncio
async def test_nth_session_write_failure_fully_compensates(tmp_path, monkeypatch):
    seeded = await _seed_service(tmp_path, monkeypatch)
    project_before = file_manifest(seeded["project_dir"])
    original_write = seeded["sessions"]._write_session_sync
    calls = 0

    def fail_second_write(session, project, save_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected session write failure")
        return original_write(session, project, save_id)

    monkeypatch.setattr(
        seeded["sessions"],
        "_write_session_sync",
        fail_second_write,
    )
    with pytest.raises(DestructiveOperationError) as raised:
        await seeded["service"].delete_character(
            seeded["project"],
            CHARACTER_ID,
            SAVE_IDS[0],
            seeded["created"][SAVE_IDS[0]]["revision"],
        )

    assert type(raised.value) is DestructiveOperationError
    assert raised.value.code == "delete_failed"
    assert raised.value.needs_recovery is False
    assert calls == 2
    assert file_manifest(seeded["project_dir"]) == project_before


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["yaml", "project"])
async def test_primary_move_failure_fully_compensates(
    tmp_path,
    monkeypatch,
    target_kind,
):
    seeded = await _seed_service(
        tmp_path,
        monkeypatch,
        keeper=target_kind == "project",
    )
    project_before = file_manifest(seeded["project_dir"])
    failed_source = (
        seeded["character_path"]
        if target_kind == "yaml"
        else seeded["project_dir"]
    )
    original_replace = destructive_module.os.replace

    def fail_primary_move(source, target):
        if Path(source) == failed_source:
            raise OSError("injected primary move failure")
        return original_replace(source, target)

    monkeypatch.setattr(destructive_module.os, "replace", fail_primary_move)
    with pytest.raises(DestructiveOperationError) as raised:
        if target_kind == "yaml":
            await seeded["service"].delete_character(
                seeded["project"],
                CHARACTER_ID,
                SAVE_IDS[0],
                seeded["created"][SAVE_IDS[0]]["revision"],
            )
        else:
            await seeded["service"].delete_project(seeded["project"])

    assert type(raised.value) is DestructiveOperationError
    assert raised.value.code == "delete_failed"
    assert raised.value.needs_recovery is False
    assert file_manifest(seeded["project_dir"]) == project_before


@pytest.mark.asyncio
async def test_project_delete_moves_directory_without_source_unlink_or_rmtree(
    tmp_path,
    monkeypatch,
):
    seeded = await _seed_service(tmp_path, monkeypatch, keeper=True)
    project_dir = seeded["project_dir"].resolve(strict=False)
    original_replace = destructive_module.os.replace
    original_unlink = Path.unlink
    original_rmtree = shutil.rmtree
    moves: list[tuple[Path, Path]] = []
    destructive_calls: list[Path] = []

    def track_replace(source, target):
        moves.append((Path(source), Path(target)))
        return original_replace(source, target)

    def track_unlink(path, *args, **kwargs):
        resolved = Path(path).resolve(strict=False)
        if resolved == project_dir or resolved.is_relative_to(project_dir):
            destructive_calls.append(resolved)
        return original_unlink(path, *args, **kwargs)

    def track_rmtree(path, *args, **kwargs):
        resolved = Path(path).resolve(strict=False)
        if resolved == project_dir or resolved.is_relative_to(project_dir):
            destructive_calls.append(resolved)
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(destructive_module.os, "replace", track_replace)
    monkeypatch.setattr(Path, "unlink", track_unlink)
    monkeypatch.setattr(shutil, "rmtree", track_rmtree)

    result = await seeded["service"].delete_project(seeded["project"])
    verified = seeded["recovery"].get_verified(result["recovery_id"])
    tombstone = seeded["recovery"].tombstone_path(verified)

    assert (seeded["project_dir"], tombstone) in moves
    assert destructive_calls == []
    assert not seeded["project_dir"].exists()
    assert tombstone.is_dir()


@pytest.mark.asyncio
async def test_compensation_failure_marks_manifest_needs_recovery(
    tmp_path,
    monkeypatch,
):
    seeded = await _seed_service(tmp_path, monkeypatch)
    original_write = seeded["sessions"]._write_session_sync
    calls = 0

    def fail_second_write(session, project, save_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected session write failure")
        return original_write(session, project, save_id)

    def fail_compensation(_verified, _source_relpath):
        raise OSError("injected compensation failure")

    monkeypatch.setattr(
        seeded["sessions"],
        "_write_session_sync",
        fail_second_write,
    )
    monkeypatch.setattr(
        seeded["recovery"],
        "restore_item_exact",
        fail_compensation,
    )

    with pytest.raises(DestructiveRecoveryRequired) as raised:
        await seeded["service"].delete_character(
            seeded["project"],
            CHARACTER_ID,
            SAVE_IDS[0],
            seeded["created"][SAVE_IDS[0]]["revision"],
        )

    assert raised.value.code == "needs_recovery"
    assert raised.value.needs_recovery is True
    assert raised.value.recovery_id
    manifest = seeded["recovery"].get_verified(raised.value.recovery_id).manifest
    assert manifest["metadata"]["needs_recovery"] is True
    assert manifest["metadata"]["recovery_stage"] == "delete_compensation"
    assert manifest["metadata"]["recovery_error"] == "compensation_failed"
