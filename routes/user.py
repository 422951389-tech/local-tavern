"""用户档案路由。"""

import asyncio

from fastapi import APIRouter, HTTPException, Request, Query

from core.async_utils import run_sync_critical
from core.character_loader import load_user_profile, save_user_profile
from core.destructive_service import DestructiveOperationError
from core.recovery_store import RecoveryConflict, RecoveryIntegrityError
from core.session_manager import RevisionConflict, delete_user_data, get_session_store
from routes.common import (
    _json_object,
    _norm_save,
    _norm_project,
    _raise_destructive_error,
    _raise_recovery_integrity,
    _raise_revision_conflict,
)

router = APIRouter()


@router.get("/api/user")
async def api_get_user(project: str = Query("默认项目")):
    project = _norm_project(project)
    return await asyncio.to_thread(load_user_profile, project)


@router.put("/api/user")
async def api_save_user(req: Request, project: str = Query("默认项目")):
    project = _norm_project(project)
    body = await _json_object(req)
    data = body.get("data", {})
    if not isinstance(data, dict):
        raise HTTPException(
            400,
            detail={
                "code": "invalid_request_body",
                "message": "用户档案 data 必须是 JSON 对象",
            },
        )
    try:
        project_lock = await get_session_store().project_lock(project)
        async with project_lock:
            await run_sync_critical(save_user_profile, project, data)
        return {"saved": True, "id": data.get("id", "user")}
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/api/user")
async def api_delete_user(
    project: str = Query("默认项目"),
    save: str = Query(None),
    expected_revision: int = Query(..., ge=0),
):
    project = _norm_project(project)
    save_id = _norm_save(save) if save else "默认存档"
    try:
        return await delete_user_data(
            project,
            save_id,
            expected_revision,
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
