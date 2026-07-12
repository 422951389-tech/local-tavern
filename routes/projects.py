"""项目路由。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.character_loader import get_project_dir, list_projects, ensure_project
from routes.common import _id_from_display_name, _norm_project

router = APIRouter()


class ProjectRequest(BaseModel):
    name: str


@router.get("/api/projects")
async def api_list_projects():
    return {"projects": list_projects()}


@router.post("/api/projects")
async def api_create_project(req: ProjectRequest):
    if not req.name or not req.name.strip():
        raise HTTPException(400, "项目名不能为空")
    name = _id_from_display_name(req.name, label="项目显示名")
    if name in list_projects():
        raise HTTPException(409, f"项目 ID {name} 已存在；请使用不会产生规范化碰撞的名称")
    d = ensure_project(name)
    return {"name": name, "path": str(d)}


@router.delete("/api/projects")
async def api_delete_project(req: ProjectRequest):
    import shutil
    name = _norm_project(req.name)
    projects = list_projects()
    if name not in projects:
        raise HTTPException(404, "项目不存在")
    if len(projects) <= 1:
        raise HTTPException(400, "至少保留 1 个项目")
    shutil.rmtree(get_project_dir(name))
    return {"deleted": name}
