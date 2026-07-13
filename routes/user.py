"""用户档案路由。"""
from fastapi import APIRouter, HTTPException, Request, Query

from core.character_loader import load_user_profile, save_user_profile
from core.destructive_service import DestructiveOperationError
from core.recovery_store import RecoveryConflict, RecoveryIntegrityError
from core.session_manager import RevisionConflict, delete_user_data
from routes.common import (
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
    return load_user_profile(project)


@router.put("/api/user")
async def api_save_user(req: Request, project: str = Query("默认项目")):
    project = _norm_project(project)
    body = await req.json()
    data = body.get("data", {})
    try:
        save_user_profile(project, data)
        return {"saved": True, "id": data.get("id", "user")}
    except ValueError as e:
        raise HTTPException(400, str(e))


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
