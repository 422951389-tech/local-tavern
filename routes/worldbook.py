"""世界书 Schema、CRUD 与存档级手动选择路由。"""
import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from core.character_loader import WORLD_BOOK_SCHEMA, load_worldbook, save_worldbook
from core.destructive_service import DestructiveOperationError
from core.recovery_store import RecoveryConflict, RecoveryIntegrityError
from core.path_policy import PathPolicyError, validate_file_id
from core.session_manager import (
    RevisionConflict,
    delete_worldbook_data,
    get_session_store,
    mutate_session,
)
from core.worldbook_policy import (
    MAX_MANUAL_WORLDBOOK_IDS,
    WorldbookValidationError,
)
from routes.common import (
    ExpectedRevision,
    _norm_save,
    _norm_project,
    _raise_destructive_error,
    _raise_recovery_integrity,
    _raise_revision_conflict,
)

router = APIRouter()


class WorldbookSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: dict[str, Any]


class ManualWorldbookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: StrictStr
    save: StrictStr
    expected_revision: ExpectedRevision
    entry_ids: list[StrictStr] = Field(max_length=MAX_MANUAL_WORLDBOOK_IDS)

    @field_validator("entry_ids")
    @classmethod
    def validate_entry_ids(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for entry_id in value:
            try:
                validated = validate_file_id(entry_id, label="手动世界书条目 ID")
            except PathPolicyError as exc:
                raise ValueError(str(exc)) from exc
            if validated in seen:
                raise ValueError("手动世界书条目 ID 不能重复")
            seen.add(validated)
            normalized.append(validated)
        return sorted(normalized)


def _raise_worldbook_validation(exc: WorldbookValidationError) -> None:
    raise HTTPException(422, detail=exc.as_detail()) from exc


def _validate_manual_selection(
    entries: list[dict],
    entry_ids: list[str],
    *,
    existing_ids: list[str],
) -> None:
    by_id = {entry["id"]: entry for entry in entries}
    requested = set(entry_ids)
    dormant = set(existing_ids)
    violations: list[str] = []
    # 已在当前 Session 中的引用可以休眠：条目删除、禁用或改模式后仍可
    # 原样保留；只有新增的无效引用才被严格拒绝。
    new_ids = requested - dormant
    if new_ids - set(by_id):
        violations.append("manual_worldbook_ids:item_unknown")
    if any(
        entry_id in by_id and by_id[entry_id].get("activation") != "manual"
        for entry_id in new_ids
    ):
        violations.append("manual_worldbook_ids:item_not_manual")
    if any(
        entry_id in by_id and by_id[entry_id].get("enabled") is not True
        for entry_id in new_ids
    ):
        violations.append("manual_worldbook_ids:item_disabled")
    if violations:
        raise WorldbookValidationError(violations)


@router.get("/api/worldbook")
async def api_list_worldbook(project: str = Query("默认项目")):
    project = _norm_project(project)
    try:
        return {"entries": await asyncio.to_thread(load_worldbook, project)}
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/api/schema/worldbook")
async def api_worldbook_schema():
    return WORLD_BOOK_SCHEMA


@router.put("/api/worldbook/{entry_id}")
async def api_save_worldbook(
    entry_id: str,
    req: WorldbookSaveRequest,
    project: str = Query("默认项目"),
):
    project = _norm_project(project)
    project_lock = await get_session_store().project_lock(project)
    try:
        async with project_lock:
            await asyncio.to_thread(save_worldbook, project, entry_id, req.data)
        return {"saved": True, "id": entry_id}
    except WorldbookValidationError as exc:
        _raise_worldbook_validation(exc)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/api/session/worldbook/manual")
async def api_patch_manual_worldbook(req: ManualWorldbookRequest):
    project = _norm_project(req.project)
    save = _norm_save(req.save)
    project_lock = await get_session_store().project_lock(project)
    try:
        async with project_lock:
            entries = await asyncio.to_thread(load_worldbook, project)

            def apply_selection(session: dict, context) -> None:
                if not context.existed:
                    raise FileNotFoundError(f"存档 {save} 不存在")
                _validate_manual_selection(
                    entries,
                    req.entry_ids,
                    existing_ids=session.get("manual_worldbook_ids", []),
                )
                session["manual_worldbook_ids"] = list(req.entry_ids)

            result = await mutate_session(
                project,
                save,
                req.expected_revision,
                apply_selection,
            )
        return {"session": result.session}
    except WorldbookValidationError as exc:
        _raise_worldbook_validation(exc)
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/api/worldbook/{entry_id}")
async def api_delete_worldbook(entry_id: str, project: str = Query("默认项目")):
    project = _norm_project(project)
    try:
        return await delete_worldbook_data(project, entry_id)
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
