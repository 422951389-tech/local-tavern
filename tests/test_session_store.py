import asyncio
import json
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from core import session_manager
from core.recovery_store import DataCorruptionError
from core.session_store import RevisionConflict, SessionStore, atomic_write


def _session(project: str, save: str) -> dict:
    return {
        "session_id": save,
        "name": save,
        "project": project,
        "revision": 0,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "message_history": [],
        "summaries": [],
        "summary_error": "",
    }


def _create_project(store: SessionStore, project: str) -> None:
    (store.projects_root / project).mkdir(parents=True, exist_ok=True)


@pytest.mark.asyncio
async def test_read_is_pure_and_does_not_create_directories(tmp_path):
    projects_root = tmp_path / "missing" / "projects"
    store = SessionStore(projects_root)
    assert await store.read("pure_project", "pure_save") is None
    assert not projects_root.exists()


@pytest.mark.asyncio
async def test_create_never_builds_a_partial_missing_project(tmp_path):
    store = SessionStore(tmp_path / "projects")
    with pytest.raises(FileNotFoundError, match="项目 missing_project 不存在"):
        await store.create(
            "missing_project",
            "save",
            _session("missing_project", "save"),
        )
    assert not (store.projects_root / "missing_project").exists()


@pytest.mark.asyncio
async def test_legacy_read_adds_stable_ids_only_in_memory(tmp_path):
    store = SessionStore(tmp_path / "projects")
    path = store.session_path("legacy_project", "legacy_save")
    legacy = _session("legacy_project", "legacy_save")
    legacy.pop("revision")
    legacy["message_history"] = [
        {"role": "user", "content": "legacy"},
        {"role": "assistant", "content": "reply"},
    ]
    atomic_write(path, json.dumps(legacy, ensure_ascii=False))
    before = path.read_bytes()

    first = await store.read("legacy_project", "legacy_save")
    second = await store.read("legacy_project", "legacy_save")

    assert first["revision"] == 0
    assert [item["id"] for item in first["message_history"]] == [
        item["id"] for item in second["message_history"]
    ]
    assert len(set(item["id"] for item in first["message_history"])) == 2
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_roleplay_defaults_are_added_only_in_memory_for_legacy_session(tmp_path):
    store = SessionStore(tmp_path / "projects")
    path = store.session_path("roleplay_legacy", "legacy_save")
    legacy = _session("roleplay_legacy", "legacy_save")
    legacy["characters_state"] = {
        "alpha": {"name": "阿尔法"},
    }
    atomic_write(path, json.dumps(legacy, ensure_ascii=False))
    before = path.read_bytes()

    loaded = await store.read("roleplay_legacy", "legacy_save")
    assert loaded["roleplay_policy"] == {"strict_muted_writeback": False}
    assert loaded["characters_state"]["alpha"]["remaining_silent_turns"] == 0
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_missing_legacy_fields_use_in_memory_defaults_without_writing(tmp_path):
    store = SessionStore(tmp_path / "projects")
    path = store.session_path("minimal_legacy", "minimal_save")
    legacy = {
        "name": "最小旧档",
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
    }
    atomic_write(path, json.dumps(legacy, ensure_ascii=False))
    before = path.read_bytes()

    loaded = await store.read("minimal_legacy", "minimal_save")

    assert loaded["project"] == "minimal_legacy"
    assert loaded["session_id"] == "minimal_save"
    assert loaded["revision"] == 0
    assert loaded["message_history"] == []
    assert loaded["characters_state"] == {}
    assert loaded["summaries"] == []
    assert loaded["scene_meta"] == {
        "location": "",
        "time": "",
        "weather": "",
        "main_quest": "",
        "current_scene": "",
        "next_goal": "",
    }
    assert loaded["manual_worldbook_ids"] == []
    assert loaded["roleplay_policy"] == {"strict_muted_writeback": False}
    assert loaded["relationship_edges"] == []
    assert loaded["current_provider"] == "ollama"
    assert loaded["current_model"] == ""
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_legal_nested_legacy_defaults_are_stable_and_read_only(tmp_path):
    store = SessionStore(tmp_path / "projects")
    path = store.session_path("nested_legacy", "nested_save")
    legacy = _session("nested_legacy", "nested_save")
    legacy.pop("revision")
    legacy["message_history"] = [
        {"role": "user", "content": "旧消息"},
        {"role": "assistant", "content": "旧回复"},
    ]
    legacy["characters_state"] = {"alpha": {"name": "阿尔法"}}
    legacy["summaries"] = [{"text": "旧摘要"}]
    atomic_write(path, json.dumps(legacy, ensure_ascii=False))
    before = path.read_bytes()

    first = await store.read("nested_legacy", "nested_save")
    second = await store.read("nested_legacy", "nested_save")

    assert first["revision"] == 0
    assert first["characters_state"]["alpha"]["remaining_silent_turns"] == 0
    assert first["summaries"][0]["status"] == "completed"
    assert first["summaries"][0]["id"] == second["summaries"][0]["id"]
    assert [item["id"] for item in first["message_history"]] == [
        item["id"] for item in second["message_history"]
    ]
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        pytest.param("project", 7, id="project-type"),
        pytest.param("project", "other_project", id="project-mismatch"),
        pytest.param("session_id", [], id="session-id-type"),
        pytest.param("session_id", "other_save", id="session-id-mismatch"),
        pytest.param("revision", True, id="revision-bool"),
        pytest.param("revision", "7", id="revision-string"),
        pytest.param("revision", -1, id="revision-negative"),
        pytest.param("message_history", {}, id="history-type"),
        pytest.param("characters_state", [], id="characters-state-type"),
        pytest.param("summaries", {}, id="summaries-type"),
        pytest.param("scene_meta", [], id="scene-meta-type"),
        pytest.param("manual_worldbook_ids", {}, id="manual-worldbook-type"),
        pytest.param("roleplay_policy", [], id="roleplay-policy-type"),
        pytest.param("relationship_edges", {}, id="relationship-edges-type"),
        pytest.param("current_provider", "", id="provider-empty"),
        pytest.param("current_provider", 3, id="provider-type"),
        pytest.param("current_model", [], id="model-type"),
    ],
)
@pytest.mark.asyncio
async def test_present_invalid_core_field_is_corrupt_and_read_only(
    tmp_path,
    field,
    bad_value,
):
    store = SessionStore(tmp_path / "projects")
    project = "invalid_core"
    save = "invalid_save"
    path = store.session_path(project, save)
    payload = _session(project, save)
    payload[field] = bad_value
    atomic_write(path, json.dumps(payload, ensure_ascii=False))
    before = path.read_bytes()

    with pytest.raises(DataCorruptionError) as captured:
        await store.read(project, save)

    assert captured.value.code == "data_corrupt"
    assert field in captured.value.reason
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        pytest.param("message_history", ["消息丢失"], id="history-item"),
        pytest.param(
            "message_history",
            [{"role": "system", "content": "越权消息"}],
            id="history-role",
        ),
        pytest.param(
            "message_history",
            [{"role": "user", "content": None}],
            id="history-content",
        ),
        pytest.param(
            "message_history",
            [{"id": "not-a-uuid", "role": "user", "content": "消息"}],
            id="history-id",
        ),
        pytest.param(
            "message_history",
            [
                {
                    "role": "assistant",
                    "content": "回复",
                    "context_diagnostics": [],
                }
            ],
            id="history-context-diagnostics",
        ),
        pytest.param(
            "message_history",
            [
                {
                    "role": "assistant",
                    "content": "回复",
                    "generation_telemetry": "unknown",
                }
            ],
            id="history-generation-telemetry",
        ),
        pytest.param(
            "message_history",
            [
                {
                    "role": "assistant",
                    "content": "回复",
                    "reply_alternatives": {},
                }
            ],
            id="history-reply-alternatives-type",
        ),
        pytest.param(
            "message_history",
            [
                {
                    "role": "assistant",
                    "content": "回复",
                    "reply_variant_id": "22222222-2222-4222-8222-222222222222",
                    "reply_variant_state": {
                        "scene_meta": {},
                        "characters_state": {},
                        "relationship_edges": [],
                    },
                    "reply_alternatives": [
                        {
                            "id": "33333333-3333-4333-8333-333333333333",
                            "content": "缺少状态快照",
                        }
                    ],
                }
            ],
            id="history-reply-alternative-state",
        ),
        pytest.param(
            "message_history",
            [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "role": "user",
                    "content": "消息一",
                },
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "role": "assistant",
                    "content": "消息二",
                },
            ],
            id="history-duplicate-id",
        ),
        pytest.param(
            "characters_state",
            {"alpha": "角色状态丢失"},
            id="character-item",
        ),
        pytest.param(
            "characters_state",
            {"alpha": {"remaining_silent_turns": "3"}},
            id="silent-turns-type",
        ),
        pytest.param(
            "characters_state",
            {"alpha": {"remaining_silent_turns": 1000}},
            id="silent-turns-range",
        ),
        pytest.param("summaries", ["摘要丢失"], id="summary-item"),
        pytest.param("summaries", [{"id": "bad"}], id="summary-id"),
        pytest.param(
            "summaries",
            [
                {"id": "22222222-2222-4222-8222-222222222222"},
                {"id": "22222222-2222-4222-8222-222222222222"},
            ],
            id="summary-duplicate-id",
        ),
        pytest.param("summaries", [{"status": "unknown"}], id="summary-status"),
        pytest.param("summaries", [{"facts": {}}], id="summary-facts"),
        pytest.param("summaries", [{"facts": [3]}], id="summary-facts-item"),
        pytest.param(
            "summaries",
            [{"kind": "memory_note", "character_id": 3}],
            id="memory-note-character",
        ),
        pytest.param(
            "summaries",
            [{"generation_attempt": -1}],
            id="summary-generation-attempt",
        ),
        pytest.param(
            "manual_worldbook_ids",
            ["valid_lore", 3],
            id="manual-worldbook-item",
        ),
        pytest.param(
            "manual_worldbook_ids",
            ["duplicate_lore", "duplicate_lore"],
            id="manual-worldbook-duplicate",
        ),
        pytest.param(
            "roleplay_policy",
            {"strict_muted_writeback": "true"},
            id="roleplay-policy-value",
        ),
        pytest.param(
            "relationship_edges",
            ["关系数据丢失"],
            id="relationship-edge-item",
        ),
    ],
)
@pytest.mark.asyncio
async def test_present_invalid_nested_core_field_is_corrupt_and_read_only(
    tmp_path,
    field,
    bad_value,
):
    store = SessionStore(tmp_path / "projects")
    project = "invalid_nested"
    save = "invalid_save"
    path = store.session_path(project, save)
    payload = _session(project, save)
    payload[field] = bad_value
    atomic_write(path, json.dumps(payload, ensure_ascii=False))
    before = path.read_bytes()

    with pytest.raises(DataCorruptionError) as captured:
        await store.read(project, save)

    assert captured.value.code == "data_corrupt"
    assert field in captured.value.reason
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_new_session_persists_explicit_roleplay_defaults(tmp_path):
    store = SessionStore(tmp_path / "projects")
    _create_project(store, "roleplay_new")
    session = _session("roleplay_new", "new_save")
    session["characters_state"] = {"alpha": {"name": "阿尔法"}}
    created = await store.create("roleplay_new", "new_save", session)

    assert created["roleplay_policy"] == {"strict_muted_writeback": False}
    assert created["characters_state"]["alpha"]["remaining_silent_turns"] == 0
    on_disk = json.loads(
        store.session_path("roleplay_new", "new_save").read_text(encoding="utf-8")
    )
    assert on_disk["roleplay_policy"] == {"strict_muted_writeback": False}
    assert on_disk["characters_state"]["alpha"]["remaining_silent_turns"] == 0


@pytest.mark.asyncio
async def test_revision_is_monotonic_and_stale_write_is_rejected(tmp_path):
    store = SessionStore(tmp_path / "projects")
    _create_project(store, "revision_project")
    created = await store.create(
        "revision_project",
        "revision_save",
        _session("revision_project", "revision_save"),
    )
    assert created["revision"] == 1

    def update(session, context):
        session["name"] = "updated"

    result = await store.mutate(
        "revision_project",
        "revision_save",
        1,
        update,
        initial_factory=lambda: _session("revision_project", "revision_save"),
    )
    assert result.session["revision"] == 2

    with pytest.raises(RevisionConflict) as captured:
        await store.mutate(
            "revision_project",
            "revision_save",
            1,
            update,
            initial_factory=lambda: _session("revision_project", "revision_save"),
        )
    assert captured.value.current == 2
    assert (await store.read("revision_project", "revision_save"))["revision"] == 2

    with pytest.raises(ValueError, match="非负整数"):
        await store.mutate(
            "revision_project",
            "revision_save",
            True,
            lambda session, context: None,
            initial_factory=lambda: _session("revision_project", "revision_save"),
        )


@pytest.mark.asyncio
async def test_one_hundred_concurrent_appends_have_no_duplicates_or_loss(tmp_path):
    store = SessionStore(tmp_path / "projects")
    _create_project(store, "race_project")
    await store.create(
        "race_project", "race_save", _session("race_project", "race_save")
    )

    async def append_unique(number: int) -> None:
        while True:
            current = await store.read("race_project", "race_save")
            expected = current["revision"]

            def append(session, context):
                session.setdefault("message_history", []).append(
                    {
                        "id": str(uuid4()),
                        "role": "user",
                        "content": f"message-{number}",
                        "pinned": False,
                        "in_prompt": True,
                    }
                )

            try:
                await store.mutate(
                    "race_project",
                    "race_save",
                    expected,
                    append,
                    initial_factory=lambda: _session("race_project", "race_save"),
                )
                return
            except RevisionConflict:
                await asyncio.sleep(0)

    await asyncio.gather(*(append_unique(number) for number in range(100)))
    final = await store.read("race_project", "race_save")
    history = final["message_history"]
    assert final["revision"] == 101
    assert len(history) == 100
    assert len({item["id"] for item in history}) == 100
    assert {item["content"] for item in history} == {
        f"message-{number}" for number in range(100)
    }


@pytest.mark.asyncio
async def test_different_saves_write_in_parallel(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "projects")
    _create_project(store, "parallel_project")
    await store.create(
        "parallel_project", "save_a", _session("parallel_project", "save_a")
    )
    await store.create(
        "parallel_project", "save_b", _session("parallel_project", "save_b")
    )
    barrier = threading.Barrier(2, timeout=2)
    original_write = store._write_session_sync

    def synchronized_write(session, project, save_id):
        barrier.wait()
        original_write(session, project, save_id)

    monkeypatch.setattr(store, "_write_session_sync", synchronized_write)

    def update(session, context):
        session["current_model"] = "parallel"

    await asyncio.gather(
        store.mutate(
            "parallel_project",
            "save_a",
            1,
            update,
            initial_factory=lambda: _session("parallel_project", "save_a"),
        ),
        store.mutate(
            "parallel_project",
            "save_b",
            1,
            update,
            initial_factory=lambda: _session("parallel_project", "save_b"),
        ),
    )


@pytest.mark.asyncio
async def test_read_waits_for_same_save_atomic_write(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "projects")
    _create_project(store, "read_lock_project")
    await store.create(
        "read_lock_project",
        "read_lock_save",
        _session("read_lock_project", "read_lock_save"),
    )
    entered_write = threading.Event()
    release_write = threading.Event()
    original_write = store._write_session_sync

    def blocked_write(session, project, save_id):
        entered_write.set()
        if not release_write.wait(timeout=2):
            raise TimeoutError("test write release timed out")
        original_write(session, project, save_id)

    monkeypatch.setattr(store, "_write_session_sync", blocked_write)

    def update(session, _context):
        session["name"] = "updated"

    mutation = asyncio.create_task(
        store.mutate(
            "read_lock_project",
            "read_lock_save",
            1,
            update,
            initial_factory=lambda: _session(
                "read_lock_project",
                "read_lock_save",
            ),
        )
    )
    assert await asyncio.to_thread(entered_write.wait, 1)
    reading = asyncio.create_task(store.read("read_lock_project", "read_lock_save"))
    await asyncio.sleep(0.02)
    assert not reading.done()

    release_write.set()
    mutated, read_back = await asyncio.gather(mutation, reading)

    assert mutated.session["revision"] == 2
    assert read_back["revision"] == 2
    assert read_back["name"] == "updated"


@pytest.mark.asyncio
async def test_snapshot_names_do_not_collide_within_one_second(tmp_path):
    store = SessionStore(tmp_path / "projects")
    _create_project(store, "snapshot_project")
    await store.create(
        "snapshot_project",
        "snapshot_save",
        _session("snapshot_project", "snapshot_save"),
    )
    names = []
    for _ in range(20):
        _, path = await store.snapshot("snapshot_project", "snapshot_save", 1)
        names.append(path.name)
    assert len(names) == len(set(names)) == 20


@pytest.mark.asyncio
async def test_rename_and_delete_are_revision_guarded_transactions(tmp_path):
    store = SessionStore(tmp_path / "projects")
    _create_project(store, "lifecycle_project")
    source = await store.create(
        "lifecycle_project",
        "source_save",
        _session("lifecycle_project", "source_save"),
    )
    await store.create(
        "lifecycle_project",
        "keeper_save",
        _session("lifecycle_project", "keeper_save"),
    )
    _, old_snapshot = await store.snapshot(
        "lifecycle_project",
        "source_save",
        source["revision"],
    )

    renamed = await store.rename(
        "lifecycle_project",
        "source_save",
        "renamed_save",
        "重命名存档",
        source["revision"],
    )
    assert renamed["session_id"] == "renamed_save"
    assert renamed["name"] == "重命名存档"
    assert renamed["revision"] == source["revision"] + 1
    assert not store.session_path("lifecycle_project", "source_save").exists()
    assert store.session_path("lifecycle_project", "renamed_save").exists()
    assert not old_snapshot.exists()
    assert any(
        path.name.startswith("renamed_save.")
        for path in store.history_dir("lifecycle_project").glob("*.json")
    )

    with pytest.raises(RevisionConflict):
        await store.delete(
            "lifecycle_project",
            "renamed_save",
            source["revision"],
        )
    assert store.session_path("lifecycle_project", "renamed_save").exists()

    assert await store.delete(
        "lifecycle_project",
        "renamed_save",
        renamed["revision"],
    )
    with pytest.raises(ValueError, match="至少保留 1 个存档"):
        await store.delete(
            "lifecycle_project",
            "keeper_save",
            1,
        )


def test_atomic_write_failure_keeps_previous_complete_file(tmp_path, monkeypatch):
    path = tmp_path / "save.json"
    atomic_write(path, '{"version": 1}')

    def fail_replace(source: Path, target: Path):
        raise OSError("fault injection")

    monkeypatch.setattr("core.session_store.os.replace", fail_replace)
    with pytest.raises(OSError, match="fault injection"):
        atomic_write(path, '{"version": 2}')

    assert json.loads(path.read_text(encoding="utf-8")) == {"version": 1}
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_cancelled_mutation_keeps_save_lock_until_worker_finishes(
    tmp_path,
    monkeypatch,
):
    store = SessionStore(tmp_path / "projects")
    project = "cancel_project"
    save = "cancel_save"
    (store.projects_root / project).mkdir(parents=True)
    created = await store.create(project, save, _session(project, save))

    write_started = threading.Event()
    release_write = threading.Event()
    original_write = store._write_session_sync

    def delayed_write(session, target_project, target_save):
        if session.get("marker") == "first":
            write_started.set()
            assert release_write.wait(5)
        return original_write(session, target_project, target_save)

    monkeypatch.setattr(store, "_write_session_sync", delayed_write)

    first = asyncio.create_task(
        store.mutate(
            project,
            save,
            created["revision"],
            lambda session, _context: session.update(marker="first"),
            initial_factory=lambda: _session(project, save),
        )
    )
    assert await asyncio.to_thread(write_started.wait, 1)
    first.cancel()
    second = asyncio.create_task(
        store.mutate(
            project,
            save,
            created["revision"],
            lambda session, _context: session.update(marker="second"),
            initial_factory=lambda: _session(project, save),
        )
    )
    await asyncio.sleep(0.05)
    first.cancel()
    await asyncio.sleep(0.05)
    assert not second.done(), "取消不能在持久化线程结束前释放 save lock"

    release_write.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    with pytest.raises(RevisionConflict):
        await second

    final = await store.read(project, save)
    assert final["marker"] == "first"
    assert final["revision"] == created["revision"] + 1


def test_session_manager_explicitly_reexports_compatibility_primitives():
    assert session_manager.RevisionConflict is RevisionConflict
    assert session_manager.atomic_write is atomic_write
