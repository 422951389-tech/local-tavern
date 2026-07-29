"""角色卡与 schema 路由。"""

import asyncio

from fastapi import APIRouter, HTTPException, Request, Query

from core.async_utils import run_sync_critical
from core.character_loader import (
    list_characters,
    save_character,
    CHARACTER_SCHEMA,
)

from core.destructive_service import DestructiveOperationError
from core.recovery_store import RecoveryConflict, RecoveryIntegrityError
from core.session_manager import RevisionConflict, delete_character_data, get_session_store
from routes.common import (
    _json_object,
    _norm_save,
    _norm_project,
    _raise_destructive_error,
    _raise_recovery_integrity,
    _raise_revision_conflict,
)

router = APIRouter()


@router.get("/api/characters")
async def api_list_characters(project: str = Query("默认项目")):
    project = _norm_project(project)
    chars = await asyncio.to_thread(list_characters, project)
    return {"characters": chars}


@router.get("/api/schema/character")
async def api_character_schema():
    """返回角色卡编辑器的字段定义（schema 单一事实源）。"""
    return CHARACTER_SCHEMA


@router.put("/api/characters/{char_id}")
async def api_save_character(
    char_id: str, req: Request, project: str = Query("默认项目")
):
    project = _norm_project(project)
    body = await _json_object(req)
    data = body.get("data", {})
    if not isinstance(data, dict):
        raise HTTPException(
            400,
            detail={
                "code": "invalid_request_body",
                "message": "角色卡 data 必须是 JSON 对象",
            },
        )
    try:
        project_lock = await get_session_store().project_lock(project)
        async with project_lock:
            await run_sync_critical(save_character, project, char_id, data)
        return {"saved": True, "id": char_id}
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/api/characters/{char_id}")
async def api_delete_character(
    char_id: str,
    project: str = Query("默认项目"),
    save: str = Query(None),
    expected_revision: int = Query(..., ge=0),
):
    project = _norm_project(project)
    save_id = _norm_save(save) if save else "默认存档"
    try:
        return await delete_character_data(
            project,
            char_id,
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
