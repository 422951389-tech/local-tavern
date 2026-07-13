"""世界书路由。"""
from fastapi import APIRouter, HTTPException, Request, Query

from core.character_loader import load_worldbook, save_worldbook
from core.destructive_service import DestructiveOperationError
from core.recovery_store import RecoveryConflict, RecoveryIntegrityError
from core.session_manager import delete_worldbook_data
from routes.common import (
    _norm_project,
    _raise_destructive_error,
    _raise_recovery_integrity,
)

router = APIRouter()


@router.get("/api/worldbook")
async def api_list_worldbook(project: str = Query("默认项目")):
    project = _norm_project(project)
    return {"entries": load_worldbook(project)}


@router.put("/api/worldbook/{entry_id}")
async def api_save_worldbook(entry_id: str, req: Request, project: str = Query("默认项目")):
    project = _norm_project(project)
    body = await req.json()
    data = body.get("data", {})
    try:
        save_worldbook(project, entry_id, data)
        return {"saved": True, "id": entry_id}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/api/worldbook/{entry_id}")
async def api_delete_worldbook(entry_id: str, project: str = Query("默认项目")):
    project = _norm_project(project)
    try:
        return await delete_worldbook_data(project, entry_id)
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
