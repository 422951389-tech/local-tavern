"""存档级世界发现与变化台账 API。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from core.character_loader import load_worldbook
from core.session_manager import (
    RevisionConflict,
    aload_session,
    get_session_store,
    mutate_session,
)
from core.world_state import (
    WORLD_CHANGE_CATEGORIES,
    WorldStateValidationError,
    add_world_change,
    normalize_world_state,
    remove_world_change,
    set_discovered_world_entries,
    update_world_change,
)
from routes.common import (
    ExpectedRevision,
    _norm_project,
    _norm_save,
    _raise_revision_conflict,
)


router = APIRouter()


class DiscoveriesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    project: StrictStr
    save: StrictStr
    expected_revision: ExpectedRevision
    entry_ids: list[StrictStr] = Field(max_length=100)


class WorldChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    project: StrictStr
    save: StrictStr
    expected_revision: ExpectedRevision
    category: StrictStr
    title: StrictStr = Field(min_length=1, max_length=200)
    detail: StrictStr = Field(default="", max_length=2_000)
    status: StrictStr = "active"
    related_entry_ids: list[StrictStr] = Field(default_factory=list, max_length=20)
    evidence_message_ids: list[StrictStr] = Field(default_factory=list, max_length=20)


class RemoveWorldChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    project: StrictStr
    save: StrictStr
    expected_revision: ExpectedRevision


def _raise_validation(exc: WorldStateValidationError) -> None:
    raise HTTPException(422, detail=exc.as_detail()) from exc


def _require_existing(context, save: str) -> None:
    if not context.existed:
        raise FileNotFoundError(f"存档 {save} 不存在")


def _validate_change_links(
    session: dict,
    *,
    known_entry_ids: set[str],
    related_entry_ids: list[str],
    evidence_message_ids: list[str],
) -> None:
    if set(related_entry_ids) - known_entry_ids:
        raise WorldStateValidationError(["related_entry_ids:item_unknown"])
    message_ids = {
        item.get("id")
        for item in session.get("message_history", [])
        if isinstance(item, dict)
    }
    if set(evidence_message_ids) - message_ids:
        raise WorldStateValidationError(["evidence_message_ids:item_unknown"])


@router.get("/api/session/world-state")
async def api_get_world_state(
    project: str = Query("默认项目"),
    save: str = Query("默认存档"),
):
    project = _norm_project(project)
    save = _norm_save(save)
    session = await aload_session(project, save)
    return {
        "revision": session.get("revision", 0),
        "world_state": normalize_world_state(session.get("world_state")),
    }


@router.put("/api/session/world-state/discoveries")
async def api_put_world_discoveries(req: DiscoveriesRequest):
    project = _norm_project(req.project)
    save = _norm_save(req.save)
    project_lock = await get_session_store().project_lock(project)
    try:
        async with project_lock:
            entries = await load_worldbook_async(project)
            known_ids = {entry.get("id") for entry in entries}
            if set(req.entry_ids) - known_ids:
                raise WorldStateValidationError(["discovered_entry_ids:item_unknown"])

            def mutate(session: dict, context) -> None:
                _require_existing(context, save)
                session["world_state"] = set_discovered_world_entries(
                    session.get("world_state"),
                    list(req.entry_ids),
                )

            result = await mutate_session(project, save, req.expected_revision, mutate)
        return {"session": result.session}
    except WorldStateValidationError as exc:
        _raise_validation(exc)
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


async def load_worldbook_async(project: str) -> list[dict]:
    import asyncio

    return await asyncio.to_thread(load_worldbook, project)


@router.post("/api/session/world-state/changes")
async def api_add_world_change(req: WorldChangeRequest):
    project = _norm_project(req.project)
    save = _norm_save(req.save)
    if req.category not in WORLD_CHANGE_CATEGORIES:
        _raise_validation(WorldStateValidationError(["category:invalid"]))
    try:
        entries = await load_worldbook_async(project)
        known_entry_ids = {entry.get("id") for entry in entries}

        def mutate(session: dict, context) -> None:
            _require_existing(context, save)
            _validate_change_links(
                session,
                known_entry_ids=known_entry_ids,
                related_entry_ids=list(req.related_entry_ids),
                evidence_message_ids=list(req.evidence_message_ids),
            )
            session["world_state"] = add_world_change(
                session.get("world_state"),
                category=req.category,
                title=req.title,
                detail=req.detail,
                status=req.status,
                related_entry_ids=list(req.related_entry_ids),
                evidence_message_ids=list(req.evidence_message_ids),
            )

        result = await mutate_session(project, save, req.expected_revision, mutate)
        return {"session": result.session}
    except WorldStateValidationError as exc:
        _raise_validation(exc)
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.patch("/api/session/world-state/changes/{change_id}")
async def api_update_world_change(change_id: str, req: WorldChangeRequest):
    project = _norm_project(req.project)
    save = _norm_save(req.save)
    if req.category not in WORLD_CHANGE_CATEGORIES:
        _raise_validation(WorldStateValidationError(["category:invalid"]))
    try:
        entries = await load_worldbook_async(project)
        known_entry_ids = {entry.get("id") for entry in entries}

        def mutate(session: dict, context) -> None:
            _require_existing(context, save)
            _validate_change_links(
                session,
                known_entry_ids=known_entry_ids,
                related_entry_ids=list(req.related_entry_ids),
                evidence_message_ids=list(req.evidence_message_ids),
            )
            session["world_state"] = update_world_change(
                session.get("world_state"),
                change_id,
                category=req.category,
                title=req.title,
                detail=req.detail,
                status=req.status,
                related_entry_ids=list(req.related_entry_ids),
                evidence_message_ids=list(req.evidence_message_ids),
            )

        result = await mutate_session(project, save, req.expected_revision, mutate)
        return {"session": result.session}
    except WorldStateValidationError as exc:
        _raise_validation(exc)
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/api/session/world-state/changes/{change_id}")
async def api_remove_world_change(change_id: str, req: RemoveWorldChangeRequest):
    project = _norm_project(req.project)
    save = _norm_save(req.save)
    try:
        def mutate(session: dict, context) -> None:
            _require_existing(context, save)
            session["world_state"] = remove_world_change(
                session.get("world_state"),
                change_id,
            )

        result = await mutate_session(project, save, req.expected_revision, mutate)
        return {"session": result.session}
    except WorldStateValidationError as exc:
        _raise_validation(exc)
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
