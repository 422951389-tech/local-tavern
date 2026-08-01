"""项目路由。"""

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.character_loader import (
    ensure_project,
    list_characters,
    list_projects,
    load_worldbook,
)
from core.destructive_service import DestructiveOperationError
from core.recovery_store import RecoveryConflict, RecoveryIntegrityError
from core.session_manager import delete_project_data, list_sessions
from routes.common import (
    _id_from_display_name,
    _norm_project,
    _raise_destructive_error,
    _raise_recovery_integrity,
)

router = APIRouter()


class ProjectRequest(BaseModel):
    name: str


def _load_project_stats(project: str) -> dict:
    counts = {
        "characters": 0,
        "worldbook": 0,
        "saves": 0,
    }
    errors: list[str] = []
    for category, error_code, loader in (
        ("characters", "characters_unavailable", list_characters),
        ("worldbook", "worldbook_unavailable", load_worldbook),
        ("saves", "sessions_unavailable", list_sessions),
    ):
        try:
            entities = loader(project)
            counts[category] = len(entities)
            if category == "saves" and any(
                not isinstance(entity, dict) or entity.get("status") != "ready"
                for entity in entities
            ):
                counts[category] = 0
                errors.append(error_code)
        except Exception:
            # 聚合接口只公开安全的分类码；实体内容、路径和解析异常均不出域。
            errors.append(error_code)

    return {
        "project": project,
        **counts,
        "status": "partial" if errors else "ready",
        "errors": errors,
    }


@router.get("/api/projects")
async def api_list_projects():
    return {"projects": await asyncio.to_thread(list_projects)}


@router.get("/api/projects/stats")
async def api_project_stats():
    projects = await asyncio.to_thread(list_projects)

    semaphore = asyncio.Semaphore(8)

    async def load_stats(project: str) -> dict:
        async with semaphore:
            return await asyncio.to_thread(_load_project_stats, project)

    stats = await asyncio.gather(
        *(load_stats(project) for project in projects)
    )
    return {"stats": stats}


@router.post("/api/projects")
async def api_create_project(req: ProjectRequest):
    if not req.name or not req.name.strip():
        raise HTTPException(400, "项目名不能为空")
    name = _id_from_display_name(req.name, label="项目显示名")
    if name in await asyncio.to_thread(list_projects):
        raise HTTPException(
            409, f"项目 ID {name} 已存在；请使用不会产生规范化碰撞的名称"
        )
    await asyncio.to_thread(ensure_project, name)
    return {"name": name}


@router.delete("/api/projects")
async def api_delete_project(req: ProjectRequest):
    name = _norm_project(req.name)
    projects = await asyncio.to_thread(list_projects)
    if name not in projects:
        raise HTTPException(404, "项目不存在")
    if len(projects) <= 1:
        raise HTTPException(400, "至少保留 1 个项目")
    try:
        return await delete_project_data(name)
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
