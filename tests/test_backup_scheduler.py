from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.backup_scheduler import run_backup_maintenance_once
from tests.test_backup_store import backup_id, seed_backup_library


def test_first_maintenance_creates_scheduled_backup_and_drills_it(tmp_path):
    seeded = seed_backup_library(tmp_path)

    result = run_backup_maintenance_once(
        seeded["manager"],
        backup_interval=timedelta(hours=24),
        drill_interval=timedelta(days=7),
    )

    assert result["status"] == "completed"
    assert result["backup"]["kind"] == "scheduled"
    assert result["backup"]["reason"] == "scheduled_backup"
    assert result["drill"]["status"] == "passed"
    assert result["drill"]["backup_id"] == backup_id(result["backup"])
    assert len(seeded["manager"].list_backups()) == 1
    assert len(seeded["manager"].list_drills()) == 1


def test_fresh_backup_and_drill_satisfy_schedule_without_new_writes(tmp_path):
    seeded = seed_backup_library(tmp_path)
    created = seeded["manager"].create_backup(reason="fresh manual")
    drilled = seeded["manager"].drill(backup_id(created))

    result = run_backup_maintenance_once(
        seeded["manager"],
        backup_interval=timedelta(hours=24),
        drill_interval=timedelta(days=7),
        now=datetime.now(timezone.utc),
    )

    assert result["status"] == "completed"
    assert result["backup"] is None
    assert result["drill"] is None
    assert [item["backup_id"] for item in seeded["manager"].list_backups()] == [
        backup_id(created)
    ]
    assert [item["drill_id"] for item in seeded["manager"].list_drills()] == [
        drilled["drill_id"]
    ]


def test_due_schedule_never_applies_retention_plan(tmp_path, monkeypatch):
    seeded = seed_backup_library(tmp_path, retention_count=1)
    seeded["manager"].create_backup(reason="old one")

    def forbidden_retention(*_args, **_kwargs):
        raise AssertionError("调度器不得执行保留期删除")

    monkeypatch.setattr(seeded["manager"], "apply_retention", forbidden_retention)
    result = run_backup_maintenance_once(
        seeded["manager"],
        backup_interval=timedelta(hours=24),
        drill_interval=timedelta(days=7),
        now=datetime.now(timezone.utc) + timedelta(days=8),
    )

    assert result["backup"]["kind"] == "scheduled"
    assert result["drill"]["status"] == "passed"
    assert len(seeded["manager"].list_backups()) == 2


def test_pending_restore_pauses_scheduled_writes(tmp_path, monkeypatch):
    seeded = seed_backup_library(tmp_path)
    monkeypatch.setattr(
        seeded["manager"],
        "pending_restore_ids",
        lambda: ["00000000-0000-0000-0000-000000000001"],
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("恢复维护态不得创建备份或演练")

    monkeypatch.setattr(seeded["manager"], "list_backups", forbidden)
    monkeypatch.setattr(seeded["manager"], "create_backup", forbidden)
    monkeypatch.setattr(seeded["manager"], "drill", forbidden)

    result = run_backup_maintenance_once(
        seeded["manager"],
        backup_interval=timedelta(hours=24),
        drill_interval=timedelta(days=7),
    )

    assert result["status"] == "restore_pending"
    assert result["backup"] is None
    assert result["drill"] is None


@pytest.mark.parametrize(
    "backup_interval,drill_interval",
    (
        (timedelta(0), timedelta(days=1)),
        (timedelta(hours=1), timedelta(0)),
    ),
)
def test_schedule_rejects_non_positive_intervals(
    tmp_path,
    backup_interval,
    drill_interval,
):
    seeded = seed_backup_library(tmp_path)

    with pytest.raises(ValueError, match="周期必须大于零"):
        run_backup_maintenance_once(
            seeded["manager"],
            backup_interval=backup_interval,
            drill_interval=drill_interval,
        )
