"""项目路由。"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.character_loader import list_projects, ensure_project
from routes.common import ROOT_DIR, _norm_project

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
    name = _norm_project(req.name)
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
    shutil.rmtree(ROOT_DIR / name)
    return {"deleted": name}
