"""存档级角色—角色关系边的读取与人工编辑 API。"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from core.relationship_edges import (
    MAX_EVIDENCE_PER_EDGE,
    MAX_RELATION_TYPE_LENGTH,
    RelationshipEdgeConflict,
    RelationshipEdgeError,
    RelationshipEdgeNotFound,
    delete_relationship_edge,
    normalize_relation_type,
    upsert_relationship_edge,
    validate_relationship_edges,
)
from core.session_manager import RevisionConflict, get_session_store, mutate_session
from routes.common import ExpectedRevision, _norm_project, _norm_save, _raise_revision_conflict


router = APIRouter()
RelationshipStrength = Annotated[StrictInt, Field(ge=0, le=100)]
RelationshipType = Annotated[
    StrictStr,
    Field(min_length=1, max_length=MAX_RELATION_TYPE_LENGTH),
]


class RelationshipKeyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_character_id: StrictStr
    target_character_id: StrictStr
    relation_type: RelationshipType

    @field_validator("relation_type", mode="before")
    @classmethod
    def validate_relation_type(cls, value: object) -> str:
        try:
            return normalize_relation_type(value)
        except RelationshipEdgeError as exc:
            raise ValueError(str(exc)) from exc


class RelationshipEdgeInput(RelationshipKeyInput):
    strength: RelationshipStrength
    evidence_message_ids: Annotated[
        list[StrictStr],
        Field(min_length=1, max_length=MAX_EVIDENCE_PER_EDGE),
    ]


class RelationshipUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: StrictStr
    save: StrictStr
    expected_revision: ExpectedRevision
    edge: RelationshipEdgeInput
    original_key: RelationshipKeyInput | None = None


class RelationshipDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: StrictStr
    save: StrictStr
    expected_revision: ExpectedRevision
    key: RelationshipKeyInput


def _raise_relationship_error(exc: RelationshipEdgeError) -> None:
    if isinstance(exc, RelationshipEdgeNotFound):
        status = 404
    elif isinstance(exc, RelationshipEdgeConflict):
        status = 409
    else:
        status = 422
    raise HTTPException(status, detail=exc.as_detail()) from exc


@router.get("/api/session/relationships")
async def api_get_relationships(
    project: str = "默认项目",
    save: str = "默认存档",
):
    project = _norm_project(project)
    save = _norm_save(save)
    session = await get_session_store().read(project, save)
    if session is None:
        raise HTTPException(
            404,
            detail={"code": "session_not_found", "message": f"存档 {save} 不存在"},
        )
    try:
        edges = validate_relationship_edges(session)
    except RelationshipEdgeError as exc:
        _raise_relationship_error(exc)
    return {
        "project": project,
        "save": save,
        "revision": session["revision"],
        "edges": edges,
    }


@router.put("/api/session/relationships")
async def api_put_relationship(req: RelationshipUpsertRequest):
    project = _norm_project(req.project)
    save = _norm_save(req.save)
    edge_input = req.edge.model_dump(mode="python")
    edge_input["updated_at"] = datetime.now().astimezone().isoformat()
    original_key = (
        req.original_key.model_dump(mode="python")
        if req.original_key is not None
        else None
    )

    def apply_relationship(session: dict, context) -> dict:
        if not context.existed:
            raise FileNotFoundError(f"存档 {save} 不存在")
        return upsert_relationship_edge(
            session,
            edge_input,
            original_key=original_key,
        )

    try:
        result = await mutate_session(
            project,
            save,
            req.expected_revision,
            apply_relationship,
        )
        return {"edge": result.value, "session": result.session}
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    except FileNotFoundError as exc:
        raise HTTPException(
            404,
            detail={"code": "session_not_found", "message": str(exc)},
        ) from exc
    except RelationshipEdgeError as exc:
        _raise_relationship_error(exc)


@router.delete("/api/session/relationships")
async def api_delete_relationship(req: RelationshipDeleteRequest):
    project = _norm_project(req.project)
    save = _norm_save(req.save)
    key = req.key.model_dump(mode="python")

    def apply_delete(session: dict, context) -> dict:
        if not context.existed:
            raise FileNotFoundError(f"存档 {save} 不存在")
        return delete_relationship_edge(session, key)

    try:
        result = await mutate_session(
            project,
            save,
            req.expected_revision,
            apply_delete,
        )
        return {"deleted": True, "edge": result.value, "session": result.session}
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    except FileNotFoundError as exc:
        raise HTTPException(
            404,
            detail={"code": "session_not_found", "message": str(exc)},
        ) from exc
    except RelationshipEdgeError as exc:
        _raise_relationship_error(exc)
