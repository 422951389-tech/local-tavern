"""存档管理路由。"""
from fastapi import APIRouter, HTTPException

from core.session_manager import (
    list_sessions,
    create_session,
    rename_session,
    delete_session,
    export_session,
    import_session,
    session_exists,
    save_session,
)
from routes.common import (
    SaveCreateRequest,
    SaveRenameRequest,
    SaveDeleteRequest,
    SaveImportRequest,
    SessionResetRequest,
    _norm_save,
    _norm_project,
    _initialize_session_from_profiles,
)

router = APIRouter()


@router.get("/api/sessions")
async def api_list_sessions(project: str = "默认项目"):
    project = _norm_project(project)
    return {"sessions": list_sessions(project)}


@router.post("/api/sessions")
async def api_create_session(req: SaveCreateRequest):
    project = _norm_project(req.project)
    try:
        s = await create_session(project, req.name)
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _initialize_session_from_profiles(s, project)
    await save_session(s, project, s["session_id"])
    return s


@router.post("/api/sessions/rename")
async def api_rename_session(req: SaveRenameRequest):
    project = _norm_project(req.project)
    try:
        return await rename_session(project, _norm_save(req.save), req.new_name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except FileExistsError as e:
        raise HTTPException(409, str(e))


@router.post("/api/sessions/delete")
async def api_delete_session(req: SaveDeleteRequest):
    project = _norm_project(req.project)
    try:
        return {"deleted": await delete_session(project, _norm_save(req.save))}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/sessions/export")
async def api_export_session(project: str = "默认项目", save: str = "默认存档"):
    save = _norm_save(save)
    project = _norm_project(project)
    if not session_exists(project, save):
        raise HTTPException(404, "存档不存在")
    return {"json_str": export_session(project, save)}


@router.post("/api/sessions/import")
async def api_import_session(req: SaveImportRequest):
    project = _norm_project(req.project)
    try:
        return await import_session(project, req.json_str, req.name)
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/session/reset")
async def api_reset_session(req: SessionResetRequest):
    from core.session_manager import reset_session
    project = _norm_project(req.project)
    save = _norm_save(req.save)
    new_session = reset_session(project, save)
    await _initialize_session_from_profiles(new_session, project)
    await save_session(new_session, project, save)
    return new_session
