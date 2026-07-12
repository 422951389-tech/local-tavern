"""server.py 与路由模块共享的依赖与工具函数。"""
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel

from core.ollama_client import get_client
from core.character_loader import list_characters, load_user_profile
from core.config import DEFAULT_SAVE
from core.session_manager import (
    atomic_write,
    _saves_dir,
    DEFAULT_SAVE as _SESSION_DEFAULT_SAVE,
)
from core.path_policy import (
    PathPolicyError,
    display_name_to_id,
    resolve_under,
    validate_file_id,
)
# 单一事实源：DEFAULT_SAVE 从 config 导入，session_manager 中的同名常量 _SESSION_DEFAULT_SAVE 仅作内部用
del _SESSION_DEFAULT_SAVE

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    user_input: str
    model: Optional[str] = None
    project: str = "默认项目"
    save: str = DEFAULT_SAVE
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    num_predict: Optional[int] = None
    think: Optional[bool] = None


class SessionResetRequest(BaseModel):
    project: str = "默认项目"
    save: str = DEFAULT_SAVE


class SaveCreateRequest(BaseModel):
    project: str = "默认项目"
    name: str


class SaveRenameRequest(BaseModel):
    project: str = "默认项目"
    save: str
    new_name: str


class SaveDeleteRequest(BaseModel):
    project: str = "默认项目"
    save: str


class SaveImportRequest(BaseModel):
    project: str = "默认项目"
    json_str: str
    name: Optional[str] = None


class MessageAction(BaseModel):
    action: str
    index: Optional[int] = None
    content: Optional[str] = None
    in_prompt: Optional[bool] = None


# _saves_dir 已从 session_manager 导入，删除本模块重复定义（P1-6）


def _norm_save(save: Optional[str]) -> str:
    """严格校验引用既有存档时使用的稳定 ID。"""
    try:
        return validate_file_id(save if save is not None else DEFAULT_SAVE, label="存档 ID")
    except PathPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc


def _norm_project(project: Optional[str]) -> str:
    """严格校验引用既有项目时使用的稳定 ID。"""
    try:
        return validate_file_id(project if project is not None else "默认项目", label="项目 ID")
    except PathPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc


def _id_from_display_name(name: str, *, label: str) -> str:
    """新建/重命名入口专用：显示名生成 ID，引用入口不得调用。"""
    try:
        return display_name_to_id(name, label=label)
    except PathPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc


async def _initialize_session_from_profiles(session: dict, project: str):
    user_profile = load_user_profile(project)
    if user_profile.get("scene_meta"):
        session["scene_meta"].update(user_profile["scene_meta"])
    if user_profile.get("status"):
        session["user_status"].update(user_profile["status"])
    if user_profile.get("name"):
        session["user_status"]["name"] = user_profile["name"]
    if user_profile.get("identity"):
        session["user_status"]["identity"] = user_profile["identity"]

    for c in list_characters(project):
        if c.get("active", True):
            stats = c.get("initial_stats", {})
            session["characters_state"][c["id"]] = {
                "name": c.get("name", c["id"]),
                "affinity": stats.get("affinity", 0),
                "mood": stats.get("mood", ""),
                "inner_thought": "",
                "outfit": c.get("appearance", {}).get("outfit", ""),
                "posture": stats.get("posture", ""),
                "dialogue": "",
            }

    if not session.get("current_model"):
        models = await get_client().list_models()
        if models:
            for m in models:
                if "opus" in m.lower() or "35b" in m.lower():
                    session["current_model"] = m
                    break
            else:
                session["current_model"] = models[0]


async def _save_snapshot(session: dict, project: str):
    d = resolve_under(_saves_dir(project), ".history")
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        sid = validate_file_id(session.get("session_id", DEFAULT_SAVE), label="存档 ID")
        path = resolve_under(d, f"{sid}.{ts}.json")
    except PathPolicyError as exc:
        raise HTTPException(400, str(exc)) from exc
    session_copy = dict(session)
    session_copy["_snapshot_at"] = datetime.now().isoformat()
    atomic_write(path, json.dumps(session_copy, ensure_ascii=False, indent=2))


def apply_character_state(state: dict, parsed_char: dict):
    """把解析出的角色状态写回 session characters_state 的某个角色条目。

    C3：affinity 单轮变化钳制 ±10，防止模型跳变。
    """
    old_affinity = state.get("affinity", 0)
    new_affinity = parsed_char.get("affinity", old_affinity)
    delta = max(-10, min(10, new_affinity - old_affinity))
    state["affinity"] = old_affinity + delta

    if parsed_char.get("inner_thought"): state["inner_thought"] = parsed_char["inner_thought"]
    if parsed_char.get("outfit"): state["outfit"] = parsed_char["outfit"]
    if parsed_char.get("posture"): state["posture"] = parsed_char["posture"]
    if parsed_char.get("dialogue"): state["dialogue"] = parsed_char["dialogue"]
