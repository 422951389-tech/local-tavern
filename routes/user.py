"""用户档案路由。"""
from fastapi import APIRouter, HTTPException, Request, Query

from core.character_loader import load_user_profile, save_user_profile, delete_user_profile
from core.session_manager import aload_session, save_session
from routes.common import _norm_save, _norm_project

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
async def api_delete_user(project: str = Query("默认项目"), save: str = Query(None)):
    project = _norm_project(project)
    save_id = _norm_save(save) if save else "默认存档"
    session = await aload_session(project, save_id)
    deleted = delete_user_profile(project, session)
    if not deleted:
        raise HTTPException(404, "用户档案不存在")
    await save_session(session, project, save_id)
    return {"deleted": True}
