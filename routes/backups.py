"""全量备份列表、创建、预检与恢复 API。"""
from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from core.async_utils import await_critical, run_sync_critical
from core import config
from core.backup_store import (
    BackupConflict,
    BackupError,
    BackupIntegrityError,
    BackupManager,
    BackupOperationError,
)


router = APIRouter()
Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class BackupCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=500)


class BackupRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_current_fingerprint: Fingerprint
    confirm_conflicts: StrictBool = False


class RetentionApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_fingerprint: Fingerprint
    confirm: StrictBool


async def _validated_body(req: Request, model_type):
    try:
        payload = await req.json()
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是对象")
        return model_type.model_validate(payload)
    except (ValueError, TypeError, ValidationError):
        raise HTTPException(
            400,
            detail={"code": "invalid_request", "message": "备份请求体无效"},
        ) from None


def get_backup_manager() -> BackupManager:
    return BackupManager(
        config.BACKUPS_DIR,
        config.PROJECTS_DIR,
        config.PROMPTS_DIR,
        config.SETTINGS_PATH,
        config.BACKUP_RETENTION_DAYS,
        config.BACKUP_RETENTION_COUNT,
    )


def _raise_backup_error(exc: Exception) -> None:
    if isinstance(exc, BackupConflict):
        raise HTTPException(409, detail=exc.as_detail()) from exc
    if isinstance(exc, BackupIntegrityError):
        raise HTTPException(422, detail=exc.as_detail()) from exc
    if isinstance(exc, BackupOperationError):
        raise HTTPException(500, detail=exc.as_detail()) from exc
    raise exc


def _raise_not_found(exc: FileNotFoundError) -> None:
    raise HTTPException(
        404,
        detail={"code": "not_found", "message": str(exc)},
    ) from exc


def _raise_bad_request(exc: ValueError) -> None:
    raise HTTPException(
        400,
        detail={"code": "invalid_request", "message": str(exc)},
    ) from exc


async def _run_restore_and_reconcile(callback, *args, **kwargs):
    """恢复线程与 turn 对账在同一维护窗口内完整收口。"""

    from core.chat_turns import get_turn_coordinator

    async def operation():
        result = await asyncio.to_thread(callback, *args, **kwargs)
        await get_turn_coordinator().reconcile_after_restore()
        return result

    return await await_critical(operation())


@router.get("/api/backups")
async def api_list_backups():
    try:
        backups = await asyncio.to_thread(get_backup_manager().list_backups)
    except (BackupConflict, BackupIntegrityError, BackupOperationError) as exc:
        _raise_backup_error(exc)
    return {"backups": backups}


@router.get("/api/backups/restores")
async def api_list_restore_journals():
    try:
        restores = await asyncio.to_thread(
            get_backup_manager().list_restore_journals,
        )
    except BackupError as exc:
        _raise_backup_error(exc)
    return {"restores": restores}


@router.get("/api/backups/restores/{restore_id}")
async def api_get_restore_journal(restore_id: str):
    try:
        return await asyncio.to_thread(
            get_backup_manager().get_restore_journal,
            restore_id,
        )
    except BackupError as exc:
        _raise_backup_error(exc)
    except FileNotFoundError as exc:
        _raise_not_found(exc)
    except ValueError as exc:
        _raise_bad_request(exc)


@router.post("/api/backups/restores/{restore_id}/recover")
async def api_recover_restore(restore_id: str):
    from core.active_turns import begin_maintenance, end_maintenance

    maintenance_token = begin_maintenance("backup_restore_recovery")
    try:
        try:
            result = await _run_restore_and_reconcile(
                get_backup_manager().recover_restore,
                restore_id,
            )
        except BackupError as exc:
            _raise_backup_error(exc)
        except FileNotFoundError as exc:
            _raise_not_found(exc)
        except ValueError as exc:
            _raise_bad_request(exc)
        return result
    finally:
        end_maintenance(maintenance_token)


@router.get("/api/backups/retention/plan")
async def api_plan_backup_retention():
    try:
        return await asyncio.to_thread(
            get_backup_manager().plan_retention,
        )
    except BackupError as exc:
        _raise_backup_error(exc)


@router.post("/api/backups/retention/apply")
async def api_apply_backup_retention(req: Request):
    body = await _validated_body(req, RetentionApplyRequest)
    try:
        return await run_sync_critical(
            get_backup_manager().apply_retention,
            plan_fingerprint=body.plan_fingerprint,
            confirm=body.confirm,
        )
    except BackupError as exc:
        _raise_backup_error(exc)
    except ValueError as exc:
        _raise_bad_request(exc)


@router.get("/api/backups/drills")
async def api_list_backup_drills():
    try:
        drills = await asyncio.to_thread(get_backup_manager().list_drills)
    except BackupError as exc:
        _raise_backup_error(exc)
    return {"drills": drills}


@router.get("/api/backups/drills/{drill_id}")
async def api_get_backup_drill(drill_id: str):
    try:
        return await asyncio.to_thread(
            get_backup_manager().get_drill,
            drill_id,
        )
    except BackupError as exc:
        _raise_backup_error(exc)
    except FileNotFoundError as exc:
        _raise_not_found(exc)
    except ValueError as exc:
        _raise_bad_request(exc)


@router.post("/api/backups")
async def api_create_backup(req: Request):
    body = await _validated_body(req, BackupCreateRequest)
    try:
        backup = await run_sync_critical(
            get_backup_manager().create_backup,
            body.reason,
        )
    except BackupError as exc:
        _raise_backup_error(exc)
    except ValueError as exc:
        _raise_bad_request(exc)
    return {"backup": backup}


@router.post("/api/backups/{backup_id}/dry-run")
async def api_backup_dry_run(backup_id: str):
    try:
        return await asyncio.to_thread(
            get_backup_manager().dry_run,
            backup_id,
        )
    except BackupError as exc:
        _raise_backup_error(exc)
    except FileNotFoundError as exc:
        _raise_not_found(exc)
    except ValueError as exc:
        _raise_bad_request(exc)


@router.post("/api/backups/{backup_id}/drill")
async def api_run_backup_drill(backup_id: str):
    try:
        return await run_sync_critical(
            get_backup_manager().drill,
            backup_id,
        )
    except BackupError as exc:
        _raise_backup_error(exc)
    except FileNotFoundError as exc:
        _raise_not_found(exc)
    except ValueError as exc:
        _raise_bad_request(exc)


@router.post("/api/backups/{backup_id}/restore")
async def api_restore_backup(backup_id: str, req: Request):
    from core.active_turns import begin_maintenance, end_maintenance

    body = await _validated_body(req, BackupRestoreRequest)
    maintenance_token = begin_maintenance("backup_restore")
    try:
        try:
            result = await _run_restore_and_reconcile(
                get_backup_manager().restore,
                backup_id,
                expected_current_fingerprint=body.expected_current_fingerprint,
                confirm_conflicts=body.confirm_conflicts,
            )
        except BackupError as exc:
            _raise_backup_error(exc)
        except FileNotFoundError as exc:
            _raise_not_found(exc)
        except ValueError as exc:
            _raise_bad_request(exc)
        return result
    finally:
        end_maintenance(maintenance_token)
