"""恢复点、坏档隔离与 trash 恢复 API。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from core.session_manager import (
    RevisionConflict,
    list_recovery_items,
    quarantine_session,
    quarantine_yaml_entity,
    restore_recovery_item,
)
from core.destructive_service import DestructiveOperationError
from core.recovery_store import RecoveryConflict, RecoveryIntegrityError
from routes.common import (
    QuarantineRequest,
    RecoveryRestoreRequest,
    _norm_project,
    _norm_save,
    _raise_destructive_error,
    _raise_recovery_integrity,
    _raise_revision_conflict,
)


router = APIRouter()
_ENTITY_TYPES = {"session", "project", "character", "user", "worldbook"}


@router.get("/api/recovery/items")
async def api_list_recovery_items(
    category: str | None = Query(None),
    project: str | None = Query(None),
    entity_type: str | None = Query(None),
):
    normalized_project = _norm_project(project) if project is not None else None
    if entity_type is not None and entity_type not in _ENTITY_TYPES:
        raise HTTPException(400, "无效 entity_type")
    try:
        items = await list_recovery_items(
            category=category,
            project=normalized_project,
            entity_type=entity_type,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"items": items}


@router.post("/api/recovery/quarantine")
async def api_quarantine_corrupt(req: QuarantineRequest):
    if req.entity_type not in {"session", "character", "user", "worldbook"}:
        raise HTTPException(400, "不支持隔离该 entity_type")
    try:
        project = _norm_project(req.project)
        if req.entity_type == "session":
            return await quarantine_session(
                project,
                _norm_save(req.entity_id),
                req.fingerprint,
            )
        return await quarantine_yaml_entity(
            req.entity_type,
            project,
            req.entity_id,
            req.fingerprint,
        )
    except RecoveryConflict as exc:
        raise HTTPException(409, detail=exc.as_detail()) from exc
    except RecoveryIntegrityError as exc:
        _raise_recovery_integrity(exc)
    except DestructiveOperationError as exc:
        _raise_destructive_error(exc)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/recovery/{recovery_id}/restore")
async def api_restore_recovery(
    recovery_id: str,
    req: RecoveryRestoreRequest,
):
    try:
        return await restore_recovery_item(
            recovery_id,
            expected_revision=req.expected_revision,
            overwrite=req.overwrite,
        )
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    except RecoveryConflict as exc:
        raise HTTPException(409, detail=exc.as_detail()) from exc
    except RecoveryIntegrityError as exc:
        _raise_recovery_integrity(exc)
    except DestructiveOperationError as exc:
        _raise_destructive_error(exc)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
