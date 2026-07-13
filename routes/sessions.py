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
    new_session,
    reset_session,
    RevisionConflict,
)
from routes.common import (
    SaveCreateRequest,
    SaveRenameRequest,
    SaveDeleteRequest,
    SaveImportRequest,
    SessionResetRequest,
    _norm_save,
    _norm_project,
    _id_from_display_name,
    _initialize_session_from_profiles,
    _raise_revision_conflict,
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
        save_id = _id_from_display_name(req.name, label="存档显示名")
        initial = new_session(project, save_id)
        initial["name"] = req.name.strip()
        await _initialize_session_from_profiles(initial, project)
        s = await create_session(project, req.name, initial)
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return s


@router.post("/api/sessions/rename")
async def api_rename_session(req: SaveRenameRequest):
    project = _norm_project(req.project)
    try:
        return await rename_session(
            project,
            _norm_save(req.save),
            req.new_name,
            req.expected_revision,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)


@router.post("/api/sessions/delete")
async def api_delete_session(req: SaveDeleteRequest):
    project = _norm_project(req.project)
    try:
        return await delete_session(
            project,
            _norm_save(req.save),
            req.expected_revision,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)


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
    project = _norm_project(req.project)
    save = _norm_save(req.save)
    replacement = new_session(project, save)
    await _initialize_session_from_profiles(replacement, project)
    try:
        return await reset_session(
            project,
            save,
            req.expected_revision,
            replacement,
        )
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
