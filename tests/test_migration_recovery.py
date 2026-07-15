from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

import core.migration_service as migration_module
from core.migration_service import LegacyMigrationService, MigrationConflict
from core.migration_service import MigrationIntegrityError, MigrationOperationError
from tests.data_guard import file_manifest
from tests.test_migration_service import seed_legacy_library


class SimulatedHardExit(BaseException):
    pass


def _new_service(seeded: dict) -> LegacyMigrationService:
    return LegacyMigrationService(
        seeded["data_root"],
        seeded["projects_root"],
        seeded["migrations_root"],
        seeded["backup_manager"],
        seeded["target_project"],
    )


def _crash_after_first_install(monkeypatch, service: LegacyMigrationService) -> None:
    original = service._install_staged_file
    crashed = False

    def install_then_crash(*args, **kwargs):
        nonlocal crashed
        original(*args, **kwargs)
        if not crashed:
            crashed = True
            raise SimulatedHardExit("injected hard exit")

    monkeypatch.setattr(service, "_install_staged_file", install_then_crash)


def _hard_exit(seeded: dict, monkeypatch) -> dict:
    service = seeded["service"]
    plan = service.plan()
    _crash_after_first_install(monkeypatch, service)
    with pytest.raises(SimulatedHardExit):
        service.apply(
            plan["plan_id"],
            expected_source_fingerprint=plan["source_fingerprint"],
            expected_target_fingerprint=plan["target_fingerprint"],
        )
    return plan


def test_hard_exit_target_is_reconciled_and_new_manager_apply_resumes(tmp_path, monkeypatch):
    seeded = seed_legacy_library(tmp_path)
    plan = _hard_exit(seeded, monkeypatch)
    journal_path = seeded["migrations_root"] / f"{plan['plan_id']}.json"
    persisted = json.loads(journal_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "prepared"
    assert sum(item["state"] == "installing" for item in persisted["items"]) == 1
    assert all(item["state"] in {"pending", "installing"} for item in persisted["items"])
    assert len(seeded["backup_manager"].list_backups()) == 1
    installing = next(item for item in persisted["items"] if item["state"] == "installing")
    target = seeded["projects_root"] / Path(installing["target"])
    receipt = seeded["service"]._receipt_path(plan["plan_id"], installing["target"])
    assert receipt.is_file()
    assert target.is_file()
    assert target.samefile(receipt)

    resumed = _new_service(seeded).apply(
        plan["plan_id"],
        expected_source_fingerprint=plan["source_fingerprint"],
        expected_target_fingerprint=plan["target_fingerprint"],
    )

    assert resumed["applied"] is True
    assert resumed["status"] == "applied"
    assert resumed["migration_version"] == 2
    assert resumed["started_at"]
    assert resumed["completed_at"]
    assert resumed["errors"] == []
    assert resumed["input_hashes"] == {
        "source": plan["source_fingerprint"],
        "target": plan["target_fingerprint"],
        "plan": plan["plan_id"],
    }
    assert resumed["output_hashes"]["target"] == plan["result_target_fingerprint"]
    assert len(seeded["backup_manager"].list_backups()) == 1
    assert _new_service(seeded).get_migration(plan["plan_id"]) == resumed
    assert not seeded["service"]._receipt_root(plan["plan_id"]).exists()


def test_hard_exit_can_be_rolled_back_by_new_manager(tmp_path, monkeypatch):
    seeded = seed_legacy_library(tmp_path)
    plan = _hard_exit(seeded, monkeypatch)

    rolled_back = _new_service(seeded).recover(plan["plan_id"], action="rollback")

    assert rolled_back["applied"] is False
    assert rolled_back["status"] == "rolled_back"
    assert rolled_back["completed_at"]
    assert rolled_back["output_hashes"]["target"] == plan["target_fingerprint"]
    assert not seeded["target_dir"].exists()
    assert _new_service(seeded).plan()["target_fingerprint"] == plan["target_fingerprint"]


def test_rollback_refuses_changed_installed_file_without_deleting_it(tmp_path, monkeypatch):
    seeded = seed_legacy_library(tmp_path)
    plan = _hard_exit(seeded, monkeypatch)
    installed = next(path for path in seeded["target_dir"].rglob("*") if path.is_file())
    installed.write_text("外部替换内容", encoding="utf-8")
    before = file_manifest(seeded["projects_root"])

    service = _new_service(seeded)
    with pytest.raises(MigrationConflict) as raised:
        service.recover(plan["plan_id"], action="rollback")

    assert raised.value.code == "target_changed"
    assert file_manifest(seeded["projects_root"]) == before
    status = service.get_migration(plan["plan_id"])
    assert status["status"] == "needs_recovery"
    assert status["errors"][-1]["action"] == "rollback"


def test_rollback_does_not_claim_matching_file_without_install_intent(tmp_path, monkeypatch):
    seeded = seed_legacy_library(tmp_path)
    service = seeded["service"]
    plan = service.plan()

    def crash_before_copy(*args, **kwargs):
        raise SimulatedHardExit("injected before install intent")

    monkeypatch.setattr(service, "_copy_to_staging", crash_before_copy)
    with pytest.raises(SimulatedHardExit):
        service.apply(
            plan["plan_id"],
            expected_source_fingerprint=plan["source_fingerprint"],
            expected_target_fingerprint=plan["target_fingerprint"],
        )
    first = next(item for item in plan["items"] if item["action"] == "copy")
    source = seeded["data_root"] / Path(first["source"])
    target = seeded["projects_root"] / Path(first["target"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    before = file_manifest(seeded["projects_root"])

    recovered = _new_service(seeded)
    with pytest.raises(MigrationConflict) as raised:
        recovered.recover(plan["plan_id"], action="rollback")

    assert raised.value.code == "target_changed"
    assert file_manifest(seeded["projects_root"]) == before


def test_rollback_does_not_claim_matching_file_after_install_intent_before_publish(
    tmp_path,
    monkeypatch,
):
    seeded = seed_legacy_library(tmp_path)
    service = seeded["service"]
    plan = service.plan()

    def crash_before_publish(*args, **kwargs):
        raise SimulatedHardExit("injected after install intent")

    monkeypatch.setattr(service, "_install_staged_file", crash_before_publish)
    with pytest.raises(SimulatedHardExit):
        service.apply(
            plan["plan_id"],
            expected_source_fingerprint=plan["source_fingerprint"],
            expected_target_fingerprint=plan["target_fingerprint"],
        )
    journal_path = seeded["migrations_root"] / f"{plan['plan_id']}.json"
    persisted = json.loads(journal_path.read_text(encoding="utf-8"))
    installing = next(item for item in persisted["items"] if item["state"] == "installing")
    source = seeded["data_root"] / Path(installing["source"])
    target = seeded["projects_root"] / Path(installing["target"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    before = file_manifest(seeded["projects_root"])

    with pytest.raises(MigrationConflict) as raised:
        _new_service(seeded).recover(plan["plan_id"], action="rollback")

    assert raised.value.code == "target_changed"
    assert file_manifest(seeded["projects_root"]) == before


def test_resume_rejects_changed_source_and_preserves_partial_target(tmp_path, monkeypatch):
    seeded = seed_legacy_library(tmp_path)
    plan = _hard_exit(seeded, monkeypatch)
    source = seeded["data_root"] / "characters" / "勇者.yaml"
    source.write_text("id: 勇者\nname: 外部改动\n", encoding="utf-8")
    before = file_manifest(seeded["projects_root"])

    service = _new_service(seeded)
    with pytest.raises(MigrationConflict) as raised:
        service.recover(plan["plan_id"], action="resume")

    assert raised.value.code == "source_changed"
    assert file_manifest(seeded["projects_root"]) == before
    assert service.get_migration(plan["plan_id"])["status"] == "needs_recovery"


def test_existing_v1_applied_journal_is_read_without_rewrite(tmp_path):
    seeded = seed_legacy_library(tmp_path)
    plan = seeded["service"].plan()
    applied = seeded["service"].apply(
        plan["plan_id"],
        expected_source_fingerprint=plan["source_fingerprint"],
        expected_target_fingerprint=plan["target_fingerprint"],
    )
    journal_path = seeded["migrations_root"] / f"{plan['plan_id']}.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    legacy = {
        key: value
        for key, value in journal.items()
        if key not in {
            "migration_version",
            "started_at",
            "completed_at",
            "errors",
            "input_hashes",
            "output_hashes",
            "target_was_present",
            "input_target_directories",
            "backup_source_fingerprint",
            "plan_material",
        }
    }
    legacy["journal_version"] = 1
    legacy["items"] = [
        {key: value for key, value in item.items() if key != "state"}
        for item in legacy["items"]
    ]
    legacy["result"] = {
        key: applied[key]
        for key in ("applied", "migration_id", "plan_id", "backup_id", "copied", "skipped")
    }
    journal_path.write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    before = journal_path.read_bytes()

    status = _new_service(seeded).get_migration(plan["plan_id"])

    assert status["applied"] is True
    assert status["migration_version"] == 1
    assert status["started_at"] is None
    assert status["completed_at"] is None
    assert status["errors"] == []
    assert status["input_hashes"]["plan"] == plan["plan_id"]
    assert status["output_hashes"]["target"] == plan["result_target_fingerprint"]
    assert journal_path.read_bytes() == before


@pytest.mark.parametrize("operation", ["resume", "rollback", "apply"])
def test_v1_incomplete_journal_is_rejected_without_rewrite(tmp_path, operation):
    seeded = seed_legacy_library(tmp_path / operation)
    plan = seeded["service"].plan()
    seeded["service"].apply(
        plan["plan_id"],
        expected_source_fingerprint=plan["source_fingerprint"],
        expected_target_fingerprint=plan["target_fingerprint"],
    )
    journal_path = seeded["migrations_root"] / f"{plan['plan_id']}.json"
    current = json.loads(journal_path.read_text(encoding="utf-8"))
    legacy = {
        key: value
        for key, value in current.items()
        if key not in {
            "migration_version",
            "started_at",
            "completed_at",
            "errors",
            "input_hashes",
            "output_hashes",
            "target_was_present",
            "input_target_directories",
            "backup_source_fingerprint",
            "plan_material",
            "result",
        }
    }
    legacy["journal_version"] = 1
    legacy["status"] = "prepared"
    legacy["items"] = [
        {key: value for key, value in item.items() if key != "state"}
        for item in legacy["items"]
    ]
    journal_path.write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    before = journal_path.read_bytes()
    service = _new_service(seeded)

    with pytest.raises(MigrationOperationError) as raised:
        if operation == "apply":
            service.apply(
                plan["plan_id"],
                expected_source_fingerprint=plan["source_fingerprint"],
                expected_target_fingerprint=plan["target_fingerprint"],
            )
        else:
            service.recover(plan["plan_id"], action=operation)

    assert raised.value.code == "migration_recovery_unsupported"
    assert journal_path.read_bytes() == before


def test_plan_material_hash_tamper_is_rejected(tmp_path, monkeypatch):
    seeded = seed_legacy_library(tmp_path)
    plan = _hard_exit(seeded, monkeypatch)
    journal_path = seeded["migrations_root"] / f"{plan['plan_id']}.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["plan_material"]["items"][0]["target"] = (
        f"{seeded['target_project']}/characters/被篡改.yaml"
    )
    journal_path.write_text(
        json.dumps(journal, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(MigrationIntegrityError) as raised:
        _new_service(seeded).get_migration(plan["plan_id"])

    assert raised.value.code == "migration_journal_invalid"


def test_apply_holds_library_exclusive_lock_for_backup_and_install(tmp_path, monkeypatch):
    seeded = seed_legacy_library(tmp_path)
    plan = seeded["service"].plan()
    active = False

    class Guard:
        @contextmanager
        def exclusive(self):
            nonlocal active
            assert active is False
            active = True
            try:
                yield
            finally:
                active = False

    monkeypatch.setattr(migration_module, "library_lock", Guard())
    original_backup = seeded["backup_manager"].create_backup
    original_install = seeded["service"]._install_staged_file

    def checked_backup(*args, **kwargs):
        assert active is True
        return original_backup(*args, **kwargs)

    def checked_install(*args, **kwargs):
        assert active is True
        return original_install(*args, **kwargs)

    monkeypatch.setattr(seeded["backup_manager"], "create_backup", checked_backup)
    monkeypatch.setattr(seeded["service"], "_install_staged_file", checked_install)

    seeded["service"].apply(
        plan["plan_id"],
        expected_source_fingerprint=plan["source_fingerprint"],
        expected_target_fingerprint=plan["target_fingerprint"],
    )

    assert active is False
