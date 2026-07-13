"""全量备份列表、创建、预检与恢复 API。"""
from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from core import config
from core.backup_store import (
    BackupConflict,
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


async def _validated_body(req: Request, model_type):
    try:
        payload = await req.json()
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是对象")
        return model_type.model_validate(payload)
    except (ValueError, TypeError, ValidationError):
        raise HTTPException(400, "备份请求体无效") from None


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


@router.get("/api/backups")
async def api_list_backups():
    try:
        backups = await asyncio.to_thread(get_backup_manager().list_backups)
    except (BackupConflict, BackupIntegrityError, BackupOperationError) as exc:
        _raise_backup_error(exc)
    return {"backups": backups}


@router.post("/api/backups")
async def api_create_backup(req: Request):
    body = await _validated_body(req, BackupCreateRequest)
    try:
        backup = await asyncio.to_thread(
            get_backup_manager().create_backup,
            body.reason,
        )
    except (BackupConflict, BackupIntegrityError, BackupOperationError) as exc:
        _raise_backup_error(exc)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"backup": backup}


@router.post("/api/backups/{backup_id}/dry-run")
async def api_backup_dry_run(backup_id: str):
    try:
        return await asyncio.to_thread(
            get_backup_manager().dry_run,
            backup_id,
        )
    except (BackupConflict, BackupIntegrityError, BackupOperationError) as exc:
        _raise_backup_error(exc)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/backups/{backup_id}/restore")
async def api_restore_backup(backup_id: str, req: Request):
    body = await _validated_body(req, BackupRestoreRequest)
    try:
        return await asyncio.to_thread(
            get_backup_manager().restore,
            backup_id,
            expected_current_fingerprint=body.expected_current_fingerprint,
            confirm_conflicts=body.confirm_conflicts,
        )
    except (BackupConflict, BackupIntegrityError, BackupOperationError) as exc:
        _raise_backup_error(exc)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
