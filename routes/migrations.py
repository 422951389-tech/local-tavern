"""旧版平铺数据迁移的计划、执行与查询 API。"""
from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core import config
from core.backup_store import BackupManager
from core.migration_service import (
    LegacyMigrationService,
    MigrationConflict,
    MigrationIntegrityError,
    MigrationOperationError,
)


router = APIRouter()
Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class MigrationPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_project: str = Field(min_length=1, max_length=80)


class MigrationApplyRequest(MigrationPlanRequest):
    expected_source_fingerprint: Fingerprint
    expected_target_fingerprint: Fingerprint


async def _validated_body(req: Request, model_type):
    try:
        payload = await req.json()
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是对象")
        return model_type.model_validate(payload)
    except (ValueError, TypeError, ValidationError):
        raise HTTPException(400, "迁移请求体无效") from None


def get_migration_service(target_project: str = "默认项目") -> LegacyMigrationService:
    backup_manager = BackupManager(
        config.BACKUPS_DIR,
        config.PROJECTS_DIR,
        config.PROMPTS_DIR,
        config.SETTINGS_PATH,
        config.BACKUP_RETENTION_DAYS,
        config.BACKUP_RETENTION_COUNT,
    )
    migrations_root = getattr(config, "MIGRATIONS_DIR", config.DATA_DIR / ".migrations")
    return LegacyMigrationService(
        config.DATA_DIR,
        config.PROJECTS_DIR,
        migrations_root,
        backup_manager,
        target_project,
    )


def _raise_migration_error(exc: Exception) -> None:
    if isinstance(exc, MigrationConflict):
        raise HTTPException(409, detail=exc.as_detail()) from exc
    if isinstance(exc, MigrationIntegrityError):
        raise HTTPException(422, detail=exc.as_detail()) from exc
    if isinstance(exc, MigrationOperationError):
        raise HTTPException(500, detail=exc.as_detail()) from exc
    raise exc


@router.post("/api/migrations/legacy/plan")
async def api_plan_legacy_migration(req: Request):
    body = await _validated_body(req, MigrationPlanRequest)
    try:
        plan = await asyncio.to_thread(
            get_migration_service(body.target_project).plan,
        )
    except (MigrationConflict, MigrationIntegrityError, MigrationOperationError) as exc:
        _raise_migration_error(exc)
    except ValueError as exc:
        raise HTTPException(400, "目标项目无效") from exc
    return {"plan": plan}


@router.post("/api/migrations/legacy/{plan_id}/apply")
async def api_apply_legacy_migration(plan_id: str, req: Request):
    body = await _validated_body(req, MigrationApplyRequest)
    try:
        migration = await asyncio.to_thread(
            get_migration_service(body.target_project).apply,
            plan_id,
            expected_source_fingerprint=body.expected_source_fingerprint,
            expected_target_fingerprint=body.expected_target_fingerprint,
        )
    except (MigrationConflict, MigrationIntegrityError, MigrationOperationError) as exc:
        _raise_migration_error(exc)
    except FileNotFoundError as exc:
        raise HTTPException(404, "迁移记录不存在") from exc
    except ValueError as exc:
        raise HTTPException(400, "迁移请求参数无效") from exc
    return {"migration": migration}


@router.get("/api/migrations/{migration_id}")
async def api_get_migration(migration_id: str):
    try:
        migration = await asyncio.to_thread(
            get_migration_service().get_migration,
            migration_id,
        )
    except (MigrationConflict, MigrationIntegrityError, MigrationOperationError) as exc:
        _raise_migration_error(exc)
    except FileNotFoundError as exc:
        raise HTTPException(404, "迁移记录不存在") from exc
    except ValueError as exc:
        raise HTTPException(400, "migration_id 无效") from exc
    return {"migration": migration}
