from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from core.backup_store import (
    BackupConflict,
    BackupIntegrityError,
    BackupManager,
)
from core.library_lock import library_lock
from tests.data_guard import file_manifest
from tests.test_backup_restore import _mutate_all_roots
from tests.test_backup_store import (
    active_manifest,
    backup_id,
    seed_backup_library,
)


class SimulatedProcessExit(BaseException):
    """模拟不会进入常规 Exception 补偿分支的进程硬退出。"""


def _fresh_manager(seeded: dict) -> BackupManager:
    return BackupManager(
        seeded["backups_root"],
        seeded["projects_root"],
        seeded["prompts_dir"],
        seeded["settings_path"],
    )


@pytest.mark.parametrize("exit_point", ["mixed", "fully_installed"])
def test_new_manager_recovers_switching_journal_after_process_exit(
    tmp_path,
    monkeypatch,
    exit_point,
):
    seeded = seed_backup_library(tmp_path)
    backup_state = active_manifest(seeded)
    created = seeded["manager"].create_backup()
    identifier = backup_id(created)
    _mutate_all_roots(seeded)
    original_state = active_manifest(seeded)
    expected_current = seeded["manager"].dry_run(identifier)[
        "current_fingerprint"
    ]
    manager = seeded["manager"]
    original_replace = manager._replace_path

    def exit_after_durable_replace(source, target):
        original_replace(source, target)
        if exit_point == "mixed" and Path(source) == seeded["projects_root"]:
            raise SimulatedProcessExit("exit after moving first active root")
        if exit_point == "fully_installed" and Path(target) == seeded["settings_path"]:
            raise SimulatedProcessExit("exit after installing final active root")

    monkeypatch.setattr(manager, "_replace_path", exit_after_durable_replace)
    with pytest.raises(SimulatedProcessExit):
        manager.restore(
            identifier,
            expected_current_fingerprint=expected_current,
            confirm_conflicts=True,
        )

    fresh = _fresh_manager(seeded)
    journals = fresh.list_restore_journals()
    assert len(journals) == 1
    assert journals[0]["status"] == "switching"
    restore_id = journals[0]["restore_id"]
    query = fresh.get_restore_journal(restore_id)
    assert query["active_state"] == (
        "mixed" if exit_point == "mixed" else "backup"
    )
    with pytest.raises(BackupConflict) as blocked:
        fresh.create_backup(reason="must be blocked while mixed")
    assert blocked.value.code == "restore_recovery_required"

    recovered = fresh.recover_restore(restore_id)

    if exit_point == "mixed":
        assert recovered["action"] == "rollback"
        assert recovered["status"] == "rolled_back"
        assert active_manifest(seeded) == original_state
    else:
        assert recovered["action"] == "commit"
        assert recovered["status"] == "committed"
        assert active_manifest(seeded) == backup_state
    assert not list(tmp_path.rglob("*.restore-*.stage"))
    assert not list(tmp_path.rglob("*.restore-*.rollback"))


def test_recovery_rebuilds_missing_rollback_from_pre_restore_backup(
    tmp_path,
    monkeypatch,
):
    seeded = seed_backup_library(tmp_path)
    created = seeded["manager"].create_backup()
    identifier = backup_id(created)
    _mutate_all_roots(seeded)
    original_state = active_manifest(seeded)
    expected_current = seeded["manager"].dry_run(identifier)["current_fingerprint"]
    manager = seeded["manager"]
    original_replace = manager._replace_path

    def exit_after_first_move(source, target):
        original_replace(source, target)
        if Path(source) == seeded["projects_root"]:
            raise SimulatedProcessExit("exit after moving projects")

    monkeypatch.setattr(manager, "_replace_path", exit_after_first_move)
    with pytest.raises(SimulatedProcessExit):
        manager.restore(
            identifier,
            expected_current_fingerprint=expected_current,
            confirm_conflicts=True,
        )

    journal = _fresh_manager(seeded).list_restore_journals()[0]
    restore_id = journal["restore_id"]
    rollback = seeded["projects_root"].parent / (
        f".{seeded['projects_root'].name}.restore-{restore_id}.rollback"
    )
    assert rollback.is_dir()
    for path in sorted(rollback.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    rollback.rmdir()

    recovered = _fresh_manager(seeded).recover_restore(restore_id)

    assert recovered["action"] == "rollback"
    assert recovered["recovered_from_pre_restore"] is True
    assert active_manifest(seeded) == original_state
    assert not list(tmp_path.rglob("*.restore-*.stage"))
    assert not list(tmp_path.rglob("*.restore-*.rollback"))


def test_restore_journal_paths_are_strictly_validated(tmp_path, monkeypatch):
    seeded = seed_backup_library(tmp_path)
    created = seeded["manager"].create_backup()
    identifier = backup_id(created)
    _mutate_all_roots(seeded)
    current = seeded["manager"].dry_run(identifier)["current_fingerprint"]
    manager = seeded["manager"]
    original_replace = manager._replace_path

    def exit_after_first_move(source, target):
        original_replace(source, target)
        if Path(source) == seeded["projects_root"]:
            raise SimulatedProcessExit()

    monkeypatch.setattr(manager, "_replace_path", exit_after_first_move)
    with pytest.raises(SimulatedProcessExit):
        manager.restore(
            identifier,
            expected_current_fingerprint=current,
            confirm_conflicts=True,
        )
    journal_path = next((seeded["backups_root"] / ".journals").glob("*.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["roots"][0]["stage"] = str(tmp_path / "outside.stage")
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    with pytest.raises(BackupIntegrityError):
        _fresh_manager(seeded).get_restore_journal(journal["restore_id"])


def test_only_exact_atomic_journal_temps_are_ignored(tmp_path):
    seeded = seed_backup_library(tmp_path)
    journal_root = seeded["backups_root"] / ".journals"
    journal_root.mkdir(parents=True)
    restore_id = "00000000-0000-0000-0000-000000000001"
    exact_temp = journal_root / f".{restore_id}.json.random.tmp"
    exact_temp.write_text("partial", encoding="utf-8")

    assert seeded["manager"].pending_restore_ids() == []

    unknown = journal_root / "unrelated.tmp"
    unknown.write_text("unknown", encoding="utf-8")
    with pytest.raises(BackupIntegrityError, match="非法条目"):
        seeded["manager"].pending_restore_ids()


@pytest.mark.parametrize("field", ("had_current", "desired"))
def test_restore_journal_root_booleans_are_bound_to_verified_backups(
    tmp_path,
    monkeypatch,
    field,
):
    seeded = seed_backup_library(tmp_path)
    created = seeded["manager"].create_backup()
    identifier = backup_id(created)
    _mutate_all_roots(seeded)
    current = seeded["manager"].dry_run(identifier)["current_fingerprint"]
    manager = seeded["manager"]
    original_replace = manager._replace_path

    def exit_after_first_move(source, target):
        original_replace(source, target)
        if Path(source) == seeded["projects_root"]:
            raise SimulatedProcessExit()

    monkeypatch.setattr(manager, "_replace_path", exit_after_first_move)
    with pytest.raises(SimulatedProcessExit):
        manager.restore(
            identifier,
            expected_current_fingerprint=current,
            confirm_conflicts=True,
        )
    journal_path = next((seeded["backups_root"] / ".journals").glob("*.json"))
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["roots"][0][field] = not journal["roots"][0][field]
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    before = file_manifest(tmp_path)

    with pytest.raises(BackupIntegrityError, match="根状态"):
        _fresh_manager(seeded).recover_restore(journal["restore_id"])

    assert file_manifest(tmp_path) == before


def test_backup_root_layout_rejects_drill_workspace_overlap(tmp_path):
    projects = tmp_path / "projects"
    prompts = tmp_path / "prompts"
    settings = tmp_path / "settings.json"

    with pytest.raises(ValueError, match="不能重叠"):
        BackupManager(
            projects / "backups",
            projects,
            prompts,
            settings,
        )


def test_retention_plan_is_read_only_and_apply_audits_before_each_delete(
    tmp_path,
    monkeypatch,
):
    seeded = seed_backup_library(tmp_path, retention_count=1)
    first = seeded["manager"].create_backup(reason="first")
    second = seeded["manager"].create_backup(reason="second")
    before_plan = file_manifest(seeded["backups_root"])

    plan = seeded["manager"].plan_retention()
    repeated = seeded["manager"].plan_retention()

    assert plan["plan_fingerprint"] == repeated["plan_fingerprint"]
    assert plan["delete_count"] == 1
    assert plan["deletions"][0]["backup_id"] == backup_id(first)
    assert file_manifest(seeded["backups_root"]) == before_plan
    with pytest.raises(BackupConflict) as unconfirmed:
        seeded["manager"].apply_retention(
            plan_fingerprint=plan["plan_fingerprint"],
            confirm=False,
        )
    assert unconfirmed.value.code == "confirmation_required"

    original_delete = seeded["manager"]._delete_backup_path

    def assert_intent_exists_before_delete(path):
        intents = list(
            (seeded["backups_root"] / ".retention-audit").glob("*-intent.json")
        )
        assert len(intents) == 1
        record = json.loads(intents[0].read_text(encoding="utf-8"))
        assert record["action"] == "delete_intent"
        assert record["backup_id"] == backup_id(first)
        original_delete(path)

    monkeypatch.setattr(
        seeded["manager"],
        "_delete_backup_path",
        assert_intent_exists_before_delete,
    )
    result = seeded["manager"].apply_retention(
        plan_fingerprint=plan["plan_fingerprint"],
        confirm=True,
    )

    assert result["deleted_backup_ids"] == [backup_id(first)]
    assert {item["backup_id"] for item in seeded["manager"].list_backups()} == {
        backup_id(second)
    }
    audit_records = list(
        (seeded["backups_root"] / ".retention-audit").glob("*.json")
    )
    assert len(audit_records) == 2


def test_retention_apply_rejects_stale_plan_without_deleting(tmp_path):
    seeded = seed_backup_library(tmp_path, retention_count=1)
    first = seeded["manager"].create_backup(reason="first")
    seeded["manager"].create_backup(reason="second")
    plan = seeded["manager"].plan_retention()
    seeded["manager"].create_backup(reason="plan changed")
    before = file_manifest(seeded["backups_root"])

    with pytest.raises(BackupConflict) as changed:
        seeded["manager"].apply_retention(
            plan_fingerprint=plan["plan_fingerprint"],
            confirm=True,
        )

    assert changed.value.code == "retention_plan_changed"
    assert file_manifest(seeded["backups_root"]) == before
    assert any(
        item["backup_id"] == backup_id(first)
        for item in seeded["manager"].list_backups()
    )


def test_drill_unpacks_to_temporary_workspace_and_persists_result(tmp_path):
    seeded = seed_backup_library(tmp_path)
    created = seeded["manager"].create_backup(reason="drill")
    identifier = backup_id(created)
    active_before = active_manifest(seeded)
    archive_before = {
        path.name: path.read_bytes()
        for path in seeded["backups_root"].glob("*.zip")
    }

    result = seeded["manager"].drill(identifier)

    assert result["status"] == "passed"
    assert result["workspace_cleaned"] is True
    assert result["checked_file_count"] > 0
    assert active_manifest(seeded) == active_before
    assert {
        path.name: path.read_bytes()
        for path in seeded["backups_root"].glob("*.zip")
    } == archive_before
    assert seeded["manager"].get_drill(result["drill_id"]) == result
    assert seeded["manager"].list_drills() == [result]


def test_mutating_backup_operations_enter_library_exclusive_lock(
    tmp_path,
    monkeypatch,
):
    seeded = seed_backup_library(tmp_path)
    entered = 0
    original_exclusive = library_lock.exclusive

    @contextmanager
    def observed_exclusive():
        nonlocal entered
        entered += 1
        with original_exclusive():
            yield

    monkeypatch.setattr(library_lock, "exclusive", observed_exclusive)

    created = seeded["manager"].create_backup()
    assert entered == 1
    entered = 0
    seeded["manager"].drill(backup_id(created))
    assert entered == 1
    entered = 0
    plan = seeded["manager"].plan_retention()
    seeded["manager"].apply_retention(
        plan_fingerprint=plan["plan_fingerprint"],
        confirm=True,
    )
    assert entered == 1
    entered = 0
    dry_run = seeded["manager"].dry_run(backup_id(created))
    seeded["manager"].restore(
        backup_id(created),
        expected_current_fingerprint=dry_run["current_fingerprint"],
        confirm_conflicts=False,
    )
    # restore 外层和其内部 pre_restore 备份均必须持独占锁。
    assert entered == 2
