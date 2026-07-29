"""可编辑的长期记忆便签；复用摘要 Prompt 来源。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request

from core.session_manager import RevisionConflict, aload_session, mutate_session
from core.summary_lifecycle import SummaryValidationError, validated_summary_patch
from routes.common import (
    _expected_revision,
    _json_object,
    _norm_project,
    _norm_save,
    _raise_revision_conflict,
)


router = APIRouter()
MAX_MEMORY_NOTES = 100
_IDENTITY_FIELDS = frozenset({"project", "save", "expected_revision"})
_CONTENT_FIELDS = frozenset({"text", "time", "facts", "relations"})
_CREATE_FIELDS = _IDENTITY_FIELDS | _CONTENT_FIELDS | {"character_id"}


def _note_id(value: object) -> str:
    if not isinstance(value, str):
        raise HTTPException(400, detail={
            "code": "memory_note_id_invalid",
            "message": "记忆便签 ID 必须是 UUID",
        })
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(400, detail={
            "code": "memory_note_id_invalid",
            "message": "记忆便签 ID 必须是 UUID",
        }) from exc


def _notes(session: dict) -> list[dict]:
    return [
        item
        for item in session.get("summaries", [])
        if isinstance(item, dict) and item.get("kind") == "memory_note"
    ]


def _find_note(session: dict, note_id: str) -> dict:
    note = next((item for item in _notes(session) if item.get("id") == note_id), None)
    if note is None:
        raise HTTPException(404, detail={
            "code": "memory_note_not_found",
            "message": "记忆便签不存在",
        })
    return note


def _validate_character_id(session: dict, value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value not in session.get("characters_state", {}):
        raise HTTPException(422, detail={
            "code": "memory_note_character_invalid",
            "message": "便签角色不属于当前存档",
        })
    return value


def _reject_unknown(body: dict, allowed: frozenset[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise HTTPException(400, detail={
            "code": "memory_note_request_invalid",
            "message": f"记忆便签请求包含未知字段：{', '.join(unknown)}",
        })


@router.get("/api/memory-notes")
async def api_list_memory_notes(
    project: str = Query("默认项目"),
    save: str = Query("默认存档"),
):
    normalized_project = _norm_project(project)
    normalized_save = _norm_save(save)
    session = await aload_session(normalized_project, normalized_save)
    return {
        "revision": session.get("revision", 0),
        "notes": deepcopy(_notes(session)),
    }


@router.post("/api/memory-notes")
async def api_create_memory_note(req: Request):
    body = await _json_object(
        req,
        object_error_code="memory_note_request_invalid",
        object_error_message="记忆便签请求体必须是 JSON 对象",
    )
    _reject_unknown(body, _CREATE_FIELDS)
    project = _norm_project(body.get("project", "默认项目"))
    save = _norm_save(body.get("save", "默认存档"))
    expected_revision = _expected_revision(body)

    def create_note(session: dict, context) -> dict:
        del context
        if len(_notes(session)) >= MAX_MEMORY_NOTES:
            raise HTTPException(409, detail={
                "code": "memory_note_limit_reached",
                "message": f"每个存档最多保存 {MAX_MEMORY_NOTES} 条记忆便签",
            })
        content = validated_summary_patch(
            {key: body[key] for key in _CONTENT_FIELDS if key in body},
            {"text": "", "time": "", "facts": [], "relations": []},
        )
        now = datetime.now().astimezone().isoformat()
        note = {
            "id": str(uuid4()),
            "kind": "memory_note",
            "character_id": _validate_character_id(session, body.get("character_id")),
            "source_snapshot_id": None,
            "status": "completed",
            "content_status": "valid",
            "generation_attempt": 0,
            **content,
            "created_at": now,
            "edited_at": now,
            "error": None,
        }
        session.setdefault("summaries", []).append(note)
        return deepcopy(note)

    try:
        mutation = await mutate_session(
            project,
            save,
            expected_revision,
            create_note,
        )
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    except SummaryValidationError as exc:
        raise HTTPException(422, detail=exc.as_detail()) from exc
    return {"note": mutation.value, "session": mutation.session}


@router.patch("/api/memory-notes/{note_id}")
async def api_patch_memory_note(note_id: str, req: Request):
    identifier = _note_id(note_id)
    body = await _json_object(
        req,
        object_error_code="memory_note_request_invalid",
        object_error_message="记忆便签请求体必须是 JSON 对象",
    )
    _reject_unknown(body, _CREATE_FIELDS)
    project = _norm_project(body.get("project", "默认项目"))
    save = _norm_save(body.get("save", "默认存档"))
    expected_revision = _expected_revision(body)

    def patch_note(session: dict, context) -> dict:
        del context
        note = _find_note(session, identifier)
        provided_content = {key: body[key] for key in _CONTENT_FIELDS if key in body}
        if provided_content:
            note.update(validated_summary_patch(provided_content, note))
        if "character_id" in body:
            character_id = _validate_character_id(session, body["character_id"])
            if character_id == note.get("character_id") and not provided_content:
                raise SummaryValidationError(
                    "summary_no_changes",
                    "记忆便签内容没有变化",
                )
            note["character_id"] = character_id
        elif not provided_content:
            raise SummaryValidationError(
                "summary_patch_empty",
                "记忆便签编辑至少需要一个内容字段",
            )
        note["edited_at"] = datetime.now().astimezone().isoformat()
        return deepcopy(note)

    try:
        mutation = await mutate_session(
            project,
            save,
            expected_revision,
            patch_note,
        )
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    except SummaryValidationError as exc:
        raise HTTPException(422, detail=exc.as_detail()) from exc
    return {"note": mutation.value, "session": mutation.session}


@router.delete("/api/memory-notes/{note_id}")
async def api_delete_memory_note(note_id: str, req: Request):
    identifier = _note_id(note_id)
    body = await _json_object(
        req,
        object_error_code="memory_note_request_invalid",
        object_error_message="记忆便签请求体必须是 JSON 对象",
    )
    _reject_unknown(body, _IDENTITY_FIELDS)
    project = _norm_project(body.get("project", "默认项目"))
    save = _norm_save(body.get("save", "默认存档"))
    expected_revision = _expected_revision(body)

    def delete_note(session: dict, context) -> dict:
        del context
        note = deepcopy(_find_note(session, identifier))
        session["summaries"] = [
            item
            for item in session.get("summaries", [])
            if item.get("id") != identifier
        ]
        return note

    try:
        mutation = await mutate_session(
            project,
            save,
            expected_revision,
            delete_note,
        )
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    return {"deleted": mutation.value, "session": mutation.session}
