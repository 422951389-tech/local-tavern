import asyncio
import json
import threading

import pytest

from core import active_turns
from core.character_loader import (
    ensure_project,
    save_character,
    save_user_profile,
    save_worldbook,
)
from core.destructive_service import _run_with_library_exclusive
from core.library_lock import library_lock
from core.session_store import SessionStore, atomic_write


def _session(project: str, save: str) -> dict:
    return {
        "session_id": save,
        "name": save,
        "project": project,
        "revision": 0,
        "created_at": "2026-01-01T00:00:00+08:00",
        "updated_at": "2026-01-01T00:00:00+08:00",
        "message_history": [],
        "summaries": [],
        "summary_error": "",
    }


@pytest.fixture(autouse=True)
def _clear_maintenance_state():
    active_turns.clear_for_testing()
    yield
    active_turns.clear_for_testing()


def test_active_api_read_and_maintenance_are_mutually_exclusive():
    active_turns.register_api_read()
    try:
        with pytest.raises(active_turns.TurnMaintenanceConflict) as raised:
            active_turns.begin_maintenance("backup_restore")
        assert raised.value.operation == "active_api_reads"
    finally:
        active_turns.unregister_api_read()

    token = active_turns.begin_maintenance("backup_restore")
    try:
        with pytest.raises(active_turns.TurnMaintenanceConflict) as raised:
            active_turns.register_api_read()
        assert raised.value.operation == "backup_restore"
    finally:
        active_turns.end_maintenance(token)


def test_project_generation_only_rejects_stale_writes_for_changed_project():
    with active_turns.request_maintenance_generation_context():
        active_turns.advance_project_write_generation(
            "changed_project",
            "delete_character",
        )
        with pytest.raises(active_turns.TurnMaintenanceConflict) as raised:
            active_turns.assert_project_write_allowed("changed_project")
        assert raised.value.operation == "delete_character"
        active_turns.assert_project_write_allowed("other_project")

    with active_turns.request_maintenance_generation_context():
        active_turns.assert_project_write_allowed("changed_project")


@pytest.mark.parametrize(
    "writer",
    [
        pytest.param(
            lambda: save_character("deleted_project", "alpha", {"name": "阿尔法"}),
            id="character",
        ),
        pytest.param(
            lambda: save_user_profile("deleted_project", {"name": "用户"}),
            id="user",
        ),
        pytest.param(
            lambda: save_worldbook(
                "deleted_project",
                "entry",
                {
                    "title": "条目",
                    "content": "正文",
                    "enabled": True,
                    "activation": "always",
                    "keywords": [],
                    "priority": 0,
                },
            ),
            id="worldbook",
        ),
    ],
)
def test_entity_writes_never_recreate_a_missing_project(tmp_path, monkeypatch, writer):
    from core import character_loader

    projects_root = tmp_path / "projects"
    monkeypatch.setattr(character_loader, "ROOT_DIR", projects_root)

    with pytest.raises(FileNotFoundError, match="项目 deleted_project 不存在"):
        writer()
    assert not (projects_root / "deleted_project").exists()


@pytest.mark.asyncio
async def test_stale_session_write_is_rejected_after_maintenance_boundary(
    tmp_path,
    monkeypatch,
):
    project = "barrier_project"
    save = "barrier_save"
    store = SessionStore(tmp_path / "projects")
    project_dir = store.projects_root / project
    project_dir.mkdir(parents=True)
    path = store.session_path(project, save)
    atomic_write(path, json.dumps(_session(project, save), ensure_ascii=False))
    before = path.read_bytes()

    attempted_shared_lock = threading.Event()
    original_acquire_shared = library_lock.acquire_shared

    def acquire_shared_after_signal():
        attempted_shared_lock.set()
        original_acquire_shared()

    monkeypatch.setattr(
        library_lock,
        "acquire_shared",
        acquire_shared_after_signal,
    )

    with library_lock.exclusive():
        with active_turns.request_maintenance_generation_context():
            task = asyncio.create_task(
                store.mutate(
                    project,
                    save,
                    0,
                    lambda session, _context: session.update(name="stale"),
                    initial_factory=lambda: _session(project, save),
                )
            )
        assert await asyncio.to_thread(attempted_shared_lock.wait, 1)
        token = active_turns.begin_maintenance("backup_restore")
        active_turns.end_maintenance(token)

    with pytest.raises(active_turns.TurnMaintenanceConflict) as raised:
        await task
    assert raised.value.operation == "backup_restore"
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_stale_yaml_write_is_rejected_after_maintenance_boundary(
    tmp_path,
    monkeypatch,
):
    from core import character_loader

    monkeypatch.setattr(character_loader, "ROOT_DIR", tmp_path / "projects")
    ensure_project("yaml_project")
    original_path = save_user_profile("yaml_project", {"name": "original"})
    before = original_path.read_bytes()

    attempted_shared_lock = threading.Event()
    original_acquire_shared = library_lock.acquire_shared

    def acquire_shared_after_signal():
        attempted_shared_lock.set()
        original_acquire_shared()

    monkeypatch.setattr(
        library_lock,
        "acquire_shared",
        acquire_shared_after_signal,
    )

    with library_lock.exclusive():
        with active_turns.request_maintenance_generation_context():
            task = asyncio.create_task(
                asyncio.to_thread(
                    save_user_profile,
                    "yaml_project",
                    {"name": "stale"},
                )
            )
        assert await asyncio.to_thread(attempted_shared_lock.wait, 1)
        token = active_turns.begin_maintenance("backup_restore")
        active_turns.end_maintenance(token)

    with pytest.raises(active_turns.TurnMaintenanceConflict):
        await task
    assert original_path.read_bytes() == before


def test_maintenance_owner_can_write_inside_exclusive_operation():
    with active_turns.request_maintenance_generation_context():
        token = active_turns.begin_maintenance("backup_restore")
        try:
            with library_lock.shared_write():
                active_turns.assert_global_write_allowed()
        finally:
            active_turns.end_maintenance(token)


@pytest.mark.asyncio
async def test_stale_exclusive_destructive_write_is_rejected_after_maintenance(
    monkeypatch,
):
    attempted_exclusive_lock = threading.Event()
    callback_ran: list[str] = []
    original_acquire_exclusive = library_lock.acquire_exclusive

    def acquire_exclusive_after_signal():
        attempted_exclusive_lock.set()
        original_acquire_exclusive()

    monkeypatch.setattr(
        library_lock,
        "acquire_exclusive",
        acquire_exclusive_after_signal,
    )

    with library_lock.shared():
        with active_turns.request_maintenance_generation_context():
            task = asyncio.create_task(
                asyncio.to_thread(
                    _run_with_library_exclusive,
                    lambda: callback_ran.append("stale-write"),
                )
            )
        assert await asyncio.to_thread(attempted_exclusive_lock.wait, 1)
        token = active_turns.begin_maintenance("backup_restore")
        active_turns.end_maintenance(token)

    with pytest.raises(active_turns.TurnMaintenanceConflict):
        await task
    assert callback_ran == []
