"""世界书路由。"""
from fastapi import APIRouter, HTTPException, Request, Query

from core.character_loader import load_worldbook, save_worldbook, delete_worldbook_entry
from routes.common import _norm_project

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
        deleted = delete_worldbook_entry(project, entry_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not deleted:
        raise HTTPException(404, f"世界书条目不存在: {entry_id}")
    return {"deleted": True, "id": entry_id}
