"""角色卡与世界书加载器 — 支持多项目

角色卡 = YAML 文件
世界书条目 = YAML 文件
用户档案 = YAML 文件

目录结构：
  data/projects/<项目名>/
    ├── characters/    角色卡
    ├── worldbook/     世界设定
    └── user.yaml      用户档案
"""
from pathlib import Path
from typing import Optional
import io
import json
import logging

logger = logging.getLogger(__name__)

import yaml

from core.config import DATA_DIR, PROJECTS_DIR
from core.path_policy import (
    PathPolicyError,
    resolve_project_dir,
    resolve_under,
    validate_file_id,
)

ROOT_DIR = PROJECTS_DIR
OLD_DATA_DIR = DATA_DIR  # 只读兼容别名；迁移必须通过 LegacyMigrationService 显式执行。

# 角色卡 schema：单一事实源，前后端共用。
# 前端编辑器从此 schema 渲染表单，避免前后端字段表漂移。
CHARACTER_SCHEMA = {
    "id": "character",
    "label": "角色卡",
    "groups": [
        {
            "key": "_basic",
            "label": "基础信息",
            "builtin": True,
            "fields": [
                {"key": "id", "label": "唯一 ID（文件名）", "type": "text", "required": True, "placeholder": "用英文/数字，如 elara", "fixed": True},
                {"key": "name", "label": "角色名", "type": "text"},
                {"key": "tagline", "label": "一句话定位", "type": "text"},
                {"key": "persona", "label": "详细人设", "type": "textarea", "rows": 6, "placeholder": "性格、背景、动机、说话方式..."},
            ],
        },
        {
            "key": "_looks",
            "label": "外貌",
            "builtin": True,
            "fields": [
                {"key": "appearance.hair", "label": "发型/发色", "type": "text"},
                {"key": "appearance.eyes", "label": "眼型/瞳色", "type": "text"},
                {"key": "appearance.outfit", "label": "穿着", "type": "text"},
                {"key": "appearance.features", "label": "其它特征", "type": "text"},
            ],
        },
        {
            "key": "_voice",
            "label": "风格与台词",
            "builtin": True,
            "fields": [
                {"key": "voice_tone", "label": "整体语调", "type": "text", "placeholder": "轻声细语 / 大大咧咧"},
                {"key": "speaking_style", "label": "说话方式", "type": "textarea", "rows": 4, "placeholder": '句末带"呢"，爱用省略号...'},
                {"key": "catchphrases", "label": "示范台词（每行一句）", "type": "textarea", "rows": 3, "array": True},
                {"key": "abilities", "label": "能力（每行一个）", "type": "textarea", "rows": 2, "array": True},
            ],
        },
        {
            "key": "_init",
            "label": "初始状态",
            "builtin": True,
            "fields": [
                {"key": "initial_stats.affinity", "label": "好感度 (0-100)", "type": "number", "min": 0, "max": 100},
                {"key": "initial_stats.mood", "label": "初始心情", "type": "text"},
                {"key": "initial_stats.posture", "label": "初始姿势", "type": "text"},
                {"key": "active", "label": "默认出场", "type": "checkbox", "checkboxLabel": "当前角色参与场景"},
            ],
        },
    ],
    "customGroup": {"key": "_custom", "label": "自定义字段"},
}


def _atomic_dump(path: Path, data: dict):
    """YAML 原子写：先 dump 到内存串，再走 tmp→rename，避免断电损坏设定层文件。"""
    from core.session_manager import atomic_write
    buf = io.StringIO()
    yaml.safe_dump(data, buf, allow_unicode=True, sort_keys=False)
    atomic_write(path, buf.getvalue())


# ========== 项目 ==========

def list_projects() -> list[str]:
    """列出所有项目目录"""
    if not ROOT_DIR.exists():
        return []
    projects = []
    for path in ROOT_DIR.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        try:
            project_id = validate_file_id(path.name, label="项目 ID")
            resolve_project_dir(ROOT_DIR, project_id)
            projects.append(project_id)
        except PathPolicyError:
            logger.warning("忽略不符合路径策略的项目目录: %s", path.name)
    return sorted(projects)


def get_project_dir(name: str) -> Path:
    return resolve_project_dir(ROOT_DIR, name)


def get_character_path(project: str, char_id: str) -> Path:
    char_id = _safe_id(char_id)
    return resolve_under(get_project_dir(project), "characters", f"{char_id}.yaml")


def get_worldbook_path(project: str, entry_id: str) -> Path:
    entry_id = _safe_id(entry_id)
    return resolve_under(get_project_dir(project), "worldbook", f"{entry_id}.yaml")


def get_user_profile_path(project: str) -> Path:
    return resolve_under(get_project_dir(project), "user.yaml")


def ensure_project(name: str) -> Path:
    """确保项目目录存在（含子目录），并保证有一个默认存档。

    默认存档用于：
    - list_sessions / delete_session 的"至少保留 1 个存档"逻辑能正确生效
    - 切项目后 loadOrCreateCurrentSave 不再因空目录创建幽灵存档
    """
    from core.session_manager import _empty_session, atomic_write, DEFAULT_SAVE
    d = get_project_dir(name)
    resolve_under(d, "characters").mkdir(parents=True, exist_ok=True)
    resolve_under(d, "worldbook").mkdir(parents=True, exist_ok=True)
    saves_dir = resolve_under(d, "saves")
    saves_dir.mkdir(parents=True, exist_ok=True)
    default_save_path = resolve_under(saves_dir, f"{DEFAULT_SAVE}.json")
    if not default_save_path.exists():
        atomic_write(default_save_path, json.dumps(_empty_session(DEFAULT_SAVE, name), ensure_ascii=False, indent=2))
    return d


# ========== YAML 工具 ==========

def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _safe_id(char_id: str) -> str:
    char_id = validate_file_id(char_id, label="条目 ID")
    if char_id.startswith("_"):
        raise ValueError("id 不能以下划线开头")
    return char_id


# ========== 角色卡 ==========

def load_character(project: str, char_id: str) -> dict:
    return load_yaml(get_character_path(project, char_id))


def list_characters(project: str) -> list[dict]:
    d = resolve_under(get_project_dir(project), "characters")
    if not d.exists():
        return []
    chars = []
    for p in d.glob("*.yaml"):
        if p.stem.startswith("_") or p.stem.startswith("."):
            continue
        data = load_yaml(p)
        if data:
            chars.append(data)
    return chars


def save_character(project: str, char_id: str, data: dict) -> Path:
    char_id = _safe_id(char_id)
    if data.get("id") and data["id"] != char_id:
        raise ValueError(f"文件 id({char_id}) 与内容 id({data['id']}) 不一致")
    data["id"] = char_id
    d = resolve_under(ensure_project(project), "characters")
    path = resolve_under(d, f"{char_id}.yaml")
    _atomic_dump(path, data)
    return path


def delete_character(project: str, char_id: str, session: dict = None) -> bool:
    """禁止绕过统一 trash 事务直接删除。"""
    del project, char_id, session
    raise RuntimeError("角色删除必须通过破坏性操作服务执行")


# ========== 世界书 ==========

def load_worldbook(project: str) -> list[dict]:
    d = resolve_under(get_project_dir(project), "worldbook")
    if not d.exists():
        return []
    entries = []
    for p in d.glob("*.yaml"):
        if p.stem.startswith("_") or p.stem.startswith("."):
            continue
        data = load_yaml(p)
        if data:
            entries.append(data)
    return entries


def save_worldbook(project: str, entry_id: str, data: dict) -> Path:
    entry_id = _safe_id(entry_id)
    if data.get("id") and data["id"] != entry_id:
        raise ValueError(f"文件 id({entry_id}) 与内容 id({data['id']}) 不一致")
    data["id"] = entry_id
    d = resolve_under(ensure_project(project), "worldbook")
    path = resolve_under(d, f"{entry_id}.yaml")
    _atomic_dump(path, data)
    return path


def delete_worldbook_entry(project: str, entry_id: str) -> bool:
    """禁止绕过统一 trash 事务直接删除。"""
    del project, entry_id
    raise RuntimeError("世界书删除必须通过破坏性操作服务执行")


# ========== 用户档案 ==========

def load_user_profile(project: str) -> dict:
    return load_yaml(get_user_profile_path(project))


def save_user_profile(project: str, data: dict) -> Path:
    d = ensure_project(project)
    path = resolve_under(d, "user.yaml")
    _atomic_dump(path, data)
    return path


def delete_user_profile(project: str, session: dict = None) -> bool:
    """禁止绕过统一 trash 事务直接删除。"""
    del project, session
    raise RuntimeError("用户删除必须通过破坏性操作服务执行")


# ========== 辅助 ==========

def get_active_character_ids(project: str) -> list[str]:
    return [c["id"] for c in list_characters(project) if c.get("active", True)]
