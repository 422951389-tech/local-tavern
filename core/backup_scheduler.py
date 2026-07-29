"""全量备份与恢复演练的非破坏性周期调度。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from core.async_utils import run_sync_critical
from core.backup_store import BackupManager


logger = logging.getLogger(__name__)


def _aware_timestamp(value: object, *, field: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 时间无效") from exc
    if timestamp.tzinfo is None:
        raise ValueError(f"{field} 时间必须包含时区")
    return timestamp


def run_backup_maintenance_once(
    manager: BackupManager,
    *,
    backup_interval: timedelta,
    drill_interval: timedelta,
    now: datetime | None = None,
) -> dict:
    """执行一次到期检查；不会应用保留期计划，也不会删除备份。"""
    if backup_interval <= timedelta(0) or drill_interval <= timedelta(0):
        raise ValueError("备份与演练周期必须大于零")
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        raise ValueError("调度检查时间必须包含时区")

    pending = manager.pending_restore_ids()
    if pending:
        return {
            "checked_at": checked_at.isoformat(),
            "status": "restore_pending",
            "restore_ids": pending,
            "backup": None,
            "drill": None,
        }

    valid_backups = [
        item
        for item in manager.list_backups()
        if item.get("status") != "invalid"
    ]
    valid_backups.sort(key=lambda item: item["created_at"], reverse=True)
    latest_backup = valid_backups[0] if valid_backups else None
    backup_due = latest_backup is None or (
        checked_at
        - _aware_timestamp(latest_backup["created_at"], field="backup created_at")
        >= backup_interval
    )
    created = None
    if backup_due:
        created = manager.create_backup(
            reason="scheduled_backup",
            kind="scheduled",
        )
        latest_backup = created

    drills = [
        item
        for item in manager.list_drills()
        if item.get("status") == "passed"
    ]
    drills.sort(key=lambda item: item["completed_at"], reverse=True)
    latest_drill = drills[0] if drills else None
    drill_due = latest_drill is None or (
        checked_at
        - _aware_timestamp(latest_drill["completed_at"], field="drill completed_at")
        >= drill_interval
    )
    drilled = None
    if drill_due and latest_backup is not None:
        drilled = manager.drill(latest_backup["backup_id"])

    return {
        "checked_at": checked_at.isoformat(),
        "status": "completed",
        "restore_ids": [],
        "backup": created,
        "drill": drilled,
    }


async def run_backup_scheduler(
    manager: BackupManager,
    *,
    backup_interval: timedelta,
    drill_interval: timedelta,
    poll_seconds: int,
) -> None:
    """启动后立即检查，此后按轮询周期检查到期任务。"""
    if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, int) or poll_seconds < 1:
        raise ValueError("poll_seconds 必须是正整数")
    while True:
        try:
            result = await run_sync_critical(
                run_backup_maintenance_once,
                manager,
                backup_interval=backup_interval,
                drill_interval=drill_interval,
            )
            logger.info(
                "备份调度检查完成 status=%s backup=%s drill=%s",
                result["status"],
                bool(result["backup"]),
                bool(result["drill"]),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("备份调度检查失败")
        await asyncio.sleep(poll_seconds)
