"""角色卡与 schema 路由。"""
from fastapi import APIRouter, HTTPException, Request, Query

from core.character_loader import (
    list_characters,
    save_character,
    delete_character,
    CHARACTER_SCHEMA,
)

from core.session_manager import RevisionConflict, mutate_session
from routes.common import _norm_save, _norm_project, _raise_revision_conflict

router = APIRouter()


@router.get("/api/characters")
async def api_list_characters(project: str = Query("默认项目")):
    project = _norm_project(project)
    chars = list_characters(project)
    return {"characters": chars}


@router.get("/api/schema/character")
async def api_character_schema():
    """返回角色卡编辑器的字段定义（schema 单一事实源）。"""
    return CHARACTER_SCHEMA


@router.put("/api/characters/{char_id}")
async def api_save_character(char_id: str, req: Request, project: str = Query("默认项目")):
    project = _norm_project(project)
    body = await req.json()
    data = body.get("data", {})
    try:
        save_character(project, char_id, data)
        return {"saved": True, "id": char_id}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/api/characters/{char_id}")
async def api_delete_character(
    char_id: str,
    project: str = Query("默认项目"),
    save: str = Query(None),
    expected_revision: int = Query(..., ge=0),
):
    project = _norm_project(project)
    save_id = _norm_save(save) if save else "默认存档"

    def remove_character(session: dict, context) -> None:
        deleted = delete_character(project, char_id, session)
        if not deleted:
            raise HTTPException(404, f"角色卡不存在: {char_id}")

    try:
        mutation = await mutate_session(
            project,
            save_id,
            expected_revision,
            remove_character,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    return {"deleted": True, "id": char_id, "session": mutation.session}
