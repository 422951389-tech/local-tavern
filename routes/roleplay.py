"""存档级角色沉默倒计时与角色扮演策略路由。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from core.path_policy import PathPolicyError, validate_file_id
from core.session_manager import RevisionConflict, mutate_session
from routes.common import (
    ExpectedRevision,
    _norm_project,
    _norm_save,
    _raise_revision_conflict,
)


router = APIRouter()
SilentTurns = Annotated[StrictInt, Field(ge=0, le=999)]


class CharacterSilenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: StrictStr
    save: StrictStr
    expected_revision: ExpectedRevision
    remaining_silent_turns: SilentTurns


class RoleplayPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: StrictStr
    save: StrictStr
    expected_revision: ExpectedRevision
    strict_muted_writeback: StrictBool


def _norm_character_id(character_id: str) -> str:
    try:
        return validate_file_id(character_id, label="角色 ID")
    except PathPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/api/session/characters/{character_id}/silence")
async def api_patch_character_silence(
    character_id: str,
    req: CharacterSilenceRequest,
):
    project = _norm_project(req.project)
    save = _norm_save(req.save)
    character_id = _norm_character_id(character_id)

    def apply_silence(session: dict, context) -> None:
        if not context.existed:
            raise FileNotFoundError(f"存档 {save} 不存在")
        states = session.get("characters_state")
        if not isinstance(states, dict) or character_id not in states:
            raise FileNotFoundError(
                f"角色 {character_id} 不属于存档 {save}"
            )
        state = states.get(character_id)
        if not isinstance(state, dict):
            raise FileNotFoundError(
                f"角色 {character_id} 不属于存档 {save}"
            )
        state["remaining_silent_turns"] = req.remaining_silent_turns

    try:
        result = await mutate_session(
            project,
            save,
            req.expected_revision,
            apply_silence,
        )
        return {"session": result.session}
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.patch("/api/session/roleplay-policy")
async def api_patch_roleplay_policy(req: RoleplayPolicyRequest):
    project = _norm_project(req.project)
    save = _norm_save(req.save)

    def apply_policy(session: dict, context) -> None:
        if not context.existed:
            raise FileNotFoundError(f"存档 {save} 不存在")
        raw_policy = session.get("roleplay_policy")
        policy = dict(raw_policy) if isinstance(raw_policy, dict) else {}
        policy["strict_muted_writeback"] = req.strict_muted_writeback
        session["roleplay_policy"] = policy

    try:
        result = await mutate_session(
            project,
            save,
            req.expected_revision,
            apply_policy,
        )
        return {"session": result.session}
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
