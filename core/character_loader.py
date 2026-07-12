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

from core.config import PROJECTS_DIR

ROOT_DIR = PROJECTS_DIR

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
    return sorted([p.name for p in ROOT_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")])


def get_project_dir(name: str) -> Path:
    return ROOT_DIR / name


def ensure_project(name: str) -> Path:
    """确保项目目录存在（含子目录），并保证有一个默认存档。

    默认存档用于：
    - list_sessions / delete_session 的"至少保留 1 个存档"逻辑能正确生效
    - 切项目后 loadOrCreateCurrentSave 不再因空目录创建幽灵存档
    """
    from core.session_manager import _empty_session, atomic_write, DEFAULT_SAVE
    d = get_project_dir(name)
    (d / "characters").mkdir(parents=True, exist_ok=True)
    (d / "worldbook").mkdir(parents=True, exist_ok=True)
    saves_dir = d / "saves"
    saves_dir.mkdir(parents=True, exist_ok=True)
    default_save_path = saves_dir / f"{DEFAULT_SAVE}.json"
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
    if not char_id:
        raise ValueError("id 不能为空")
    if char_id.startswith("_"):
        raise ValueError("id 不能以下划线开头")
    for ch in char_id:
        if ch in "\\/:*?\"<>|":
            raise ValueError(f"id 包含非法字符: {ch}")
    return char_id


# ========== 角色卡 ==========

def load_character(project: str, char_id: str) -> dict:
    path = get_project_dir(project) / "characters" / f"{char_id}.yaml"
    return load_yaml(path)


def list_characters(project: str) -> list[dict]:
    d = get_project_dir(project) / "characters"
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
    d = ensure_project(project) / "characters"
    path = d / f"{char_id}.yaml"
    _atomic_dump(path, data)
    return path


def delete_character(project: str, char_id: str, session: dict = None) -> bool:
    """删除角色卡 YAML，并同步清理 session 中的角色状态。

    session 由调用方通过 aload_session 获取并在操作后通过 save_session 持久化。
    这确保与 chat 等写入路径共用 per-save 锁，防止并发覆盖。
    """
    import logging
    logger = logging.getLogger(__name__)
    char_id = _safe_id(char_id)
    path = get_project_dir(project) / "characters" / f"{char_id}.yaml"
    deleted = False
    if path.exists():
        path.unlink()
        deleted = True
    # 同步清理会话中的角色状态
    if session is not None:
        states = session.get("characters_state", {})
        if char_id in states:
            del states[char_id]
    else:
        logger.warning("delete_character: 未传入 session，角色状态不会从存档中清理")
    return deleted


# ========== 世界书 ==========

def load_worldbook(project: str) -> list[dict]:
    d = get_project_dir(project) / "worldbook"
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
    d = ensure_project(project) / "worldbook"
    path = d / f"{entry_id}.yaml"
    _atomic_dump(path, data)
    return path


def delete_worldbook_entry(project: str, entry_id: str) -> bool:
    """删除世界书条目 YAML。"""
    entry_id = _safe_id(entry_id)
    path = get_project_dir(project) / "worldbook" / f"{entry_id}.yaml"
    if not path.exists():
        return False
    path.unlink()
    return True


# ========== 用户档案 ==========

def load_user_profile(project: str) -> dict:
    path = get_project_dir(project) / "user.yaml"
    return load_yaml(path)


def save_user_profile(project: str, data: dict) -> Path:
    d = ensure_project(project)
    path = d / "user.yaml"
    _atomic_dump(path, data)
    return path


def delete_user_profile(project: str, session: dict = None) -> bool:
    """删除用户档案 YAML，并同步清空 session 中的 user_status。

    session 由调用方通过 aload_session 获取并在操作后通过 save_session 持久化。
    """
    import logging
    logger = logging.getLogger(__name__)
    path = get_project_dir(project) / "user.yaml"
    deleted = False
    if path.exists():
        path.unlink()
        deleted = True
    if session is not None:
        session["user_status"] = {"name": "", "identity": "", "condition": "", "abilities": []}
    else:
        logger.warning("delete_user_profile: 未传入 session，user_status 不会从存档中清理")
    return deleted


# ========== 辅助 ==========

def get_active_character_ids(project: str) -> list[str]:
    return [c["id"] for c in list_characters(project) if c.get("active", True)]


# ========== 兼容旧数据迁移 ==========

OLD_DATA_DIR = Path("C:/local-tavern/data")

def needs_migration() -> bool:
    """检测是否有旧数据需要迁移"""
    return (OLD_DATA_DIR / "characters").exists() and not ROOT_DIR.exists()


def migrate_old_data() -> str:
    """将旧 data/{characters,worldbook,user,saves} 迁移到 data/projects/默认项目/"""
    import shutil
    project_name = "默认项目"
    dest = ensure_project(project_name)

    # 迁移角色卡
    old_chars = OLD_DATA_DIR / "characters"
    if old_chars.exists():
        for p in old_chars.glob("*.yaml"):
            shutil.copy2(p, dest / "characters" / p.name)

    # 迁移世界书
    old_wb = OLD_DATA_DIR / "worldbook"
    if old_wb.exists():
        for p in old_wb.glob("*.yaml"):
            shutil.copy2(p, dest / "worldbook" / p.name)

    # 迁移用户档案
    old_user = OLD_DATA_DIR / "user"
    if old_user.exists():
        for p in old_user.glob("*.yaml"):
            if not p.stem.startswith("_"):
                shutil.copy2(p, dest / "user.yaml")
                break

    # 迁移存档
    old_saves = OLD_DATA_DIR / "saves"
    if old_saves.exists():
        dest_saves = dest / "saves"
        for p in old_saves.glob("*.json"):
            if not p.stem.startswith("."):
                shutil.copy2(p, dest_saves / p.name)
        # 迁移历史快照
        old_hist = old_saves / ".history"
        if old_hist.exists():
            dest_hist = dest_saves / ".history"
            dest_hist.mkdir(exist_ok=True)
            for p in old_hist.iterdir():
                shutil.copy2(p, dest_hist / p.name)

    return project_name
