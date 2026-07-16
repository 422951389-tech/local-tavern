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
import io
import json
import logging
import math
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

import yaml

from core.active_turns import assert_project_write_allowed
from core.config import DATA_DIR, PROJECTS_DIR
from core.library_lock import library_lock
from core.path_policy import (
    PathPolicyError,
    resolve_project_dir,
    resolve_under,
    validate_file_id,
)
from core.recovery_store import DataCorruptionError
from core.worldbook_policy import (
    ACTIVATIONS,
    MAX_WORLDBOOK_CONTENT_LENGTH,
    MAX_WORLDBOOK_KEYWORD_LENGTH,
    MAX_WORLDBOOK_KEYWORDS,
    MAX_WORLDBOOK_PRIORITY,
    MAX_WORLDBOOK_TITLE_LENGTH,
    MIN_WORLDBOOK_PRIORITY,
    WorldbookValidationError,
    normalize_worldbook_entry,
)

ROOT_DIR = PROJECTS_DIR
OLD_DATA_DIR = DATA_DIR  # 只读兼容别名；迁移必须通过 LegacyMigrationService 显式执行。
_YAML_WRITE_LOCK = threading.RLock()

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
                {"key": "aliases", "label": "角色别名（每行一个）", "type": "textarea", "rows": 3, "array": True},
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

# 世界书编辑器 schema：与服务端 normalize_worldbook_entry 共用同一组字段语义。
WORLD_BOOK_SCHEMA = {
    "id": "worldbook",
    "label": "世界书",
    "groups": [
        {
            "key": "_basic",
            "label": "基本信息",
            "builtin": True,
            "fields": [
                {
                    "key": "id",
                    "label": "稳定 ID（文件名）",
                    "type": "text",
                    "required": True,
                    "fixed": True,
                    "placeholder": "例如 qinglong_shanghui",
                },
                {
                    "key": "title",
                    "label": "标题",
                    "type": "text",
                    "maxLength": MAX_WORLDBOOK_TITLE_LENGTH,
                },
                {
                    "key": "enabled",
                    "label": "启用此条目",
                    "type": "checkbox",
                    "checkboxLabel": "允许此条目参与触发",
                },
                {
                    "key": "activation",
                    "label": "触发方式",
                    "type": "select",
                    "required": True,
                    "options": [
                        {"value": "always", "label": "常驻（always）"},
                        {"value": "keywords", "label": "关键词（keywords）"},
                        {"value": "manual", "label": "手动（manual）"},
                    ],
                },
                {
                    "key": "keywords",
                    "label": "触发关键词（每行一个）",
                    "type": "textarea",
                    "rows": 4,
                    "array": True,
                    "maxItems": MAX_WORLDBOOK_KEYWORDS,
                    "itemMaxLength": MAX_WORLDBOOK_KEYWORD_LENGTH,
                    "hint": "仅 activation=keywords 时参与匹配",
                },
                {
                    "key": "priority",
                    "label": "优先级",
                    "type": "number",
                    "min": MIN_WORLDBOOK_PRIORITY,
                    "max": MAX_WORLDBOOK_PRIORITY,
                },
                {
                    "key": "content",
                    "label": "设定内容",
                    "type": "textarea",
                    "rows": 8,
                    "maxLength": MAX_WORLDBOOK_CONTENT_LENGTH,
                },
            ],
        },
    ],
    "customGroup": {"key": "_custom", "label": "自定义事实字段"},
    "activationValues": sorted(ACTIVATIONS),
}


def _atomic_dump(path: Path, data: dict):
    """YAML 原子写：先 dump 到内存串，再走 tmp→rename，避免断电损坏设定层文件。"""
    from core.session_manager import atomic_write
    buf = io.StringIO()
    yaml.safe_dump(data, buf, allow_unicode=True, sort_keys=False)
    atomic_write(path, buf.getvalue())


@contextmanager
def yaml_write_transaction() -> Iterator[None]:
    """YAML 变更串行化，并纳入整库共享锁。"""
    with library_lock.shared():
        with _YAML_WRITE_LOCK:
            yield


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
    directory = resolve_under(get_project_dir(project), "worldbook")
    yaml_path = resolve_under(directory, f"{entry_id}.yaml")
    yml_path = resolve_under(directory, f"{entry_id}.yml")
    existing = [path for path in (yaml_path, yml_path) if path.is_file()]
    if len(existing) > 1:
        raise ValueError(f"世界书条目 {entry_id} 同时存在 .yaml 与 .yml")
    return existing[0] if existing else yaml_path


def get_user_profile_path(project: str) -> Path:
    return resolve_under(get_project_dir(project), "user.yaml")


def ensure_project(name: str) -> Path:
    """确保项目目录存在（含子目录），并保证有一个默认存档。

    默认存档用于：
    - list_sessions / delete_session 的"至少保留 1 个存档"逻辑能正确生效
    - 切项目后 loadProjectContext 不再因空目录创建幽灵存档
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

_MAX_YAML_BYTES = 2 * 1024 * 1024
_MAX_YAML_DEPTH = 64
_MAX_YAML_NODES = 10_000


def _validate_yaml_tree(value: object) -> None:
    """限制 YAML 为有界、无环、JSON 兼容的数据树。"""
    visited_nodes = 0

    def visit(node: object, depth: int, ancestors: set[int]) -> None:
        nonlocal visited_nodes
        visited_nodes += 1
        if visited_nodes > _MAX_YAML_NODES:
            raise ValueError("YAML 节点数量超限")
        if depth > _MAX_YAML_DEPTH:
            raise ValueError("YAML 嵌套深度超限")
        if node is None or isinstance(node, (str, bool, int)):
            return
        if isinstance(node, float):
            if not math.isfinite(node):
                raise ValueError("YAML 浮点值必须有限")
            return
        if isinstance(node, (dict, list)):
            identity = id(node)
            if identity in ancestors:
                raise ValueError("YAML 数据存在循环引用")
            next_ancestors = {*ancestors, identity}
            if isinstance(node, dict):
                for key, item in node.items():
                    if not isinstance(key, str):
                        raise ValueError("YAML 对象键必须是字符串")
                    visit(item, depth + 1, next_ancestors)
            else:
                for item in node:
                    visit(item, depth + 1, next_ancestors)
            return
        raise ValueError("YAML 包含不支持的数据类型")

    visit(value, 0, set())

def load_yaml(
    path: Path,
    *,
    entity_type: str,
    project: str,
    entity_id: str,
) -> dict:
    """严格纯读 YAML；已存在的损坏文件绝不回写。"""
    if not path.exists():
        return {}
    try:
        validate_file_id(entity_id, label="条目 ID")
        quarantine_available = not entity_id.startswith("_")
    except PathPolicyError:
        quarantine_available = False
    try:
        payload = path.read_bytes()
        if len(payload) > _MAX_YAML_BYTES:
            raise DataCorruptionError.from_bytes(
                path,
                payload,
                entity_type=entity_type,
                project=project,
                entity_id=entity_id,
                reason="yaml_too_large",
                quarantine_available=quarantine_available,
            )
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DataCorruptionError.from_bytes(
            path,
            payload,
            entity_type=entity_type,
            project=project,
            entity_id=entity_id,
            reason="invalid_utf8",
            quarantine_available=quarantine_available,
        ) from exc
    try:
        data = yaml.safe_load(text)
        _validate_yaml_tree(data)
    except (yaml.YAMLError, RecursionError, ValueError) as exc:
        raise DataCorruptionError.from_bytes(
            path,
            payload,
            entity_type=entity_type,
            project=project,
            entity_id=entity_id,
            reason="invalid_yaml",
            quarantine_available=quarantine_available,
        ) from exc
    if not isinstance(data, dict):
        raise DataCorruptionError.from_bytes(
            path,
            payload,
            entity_type=entity_type,
            project=project,
            entity_id=entity_id,
            reason="top_level_not_object",
            quarantine_available=quarantine_available,
        )
    return data


def _safe_id(char_id: str) -> str:
    char_id = validate_file_id(char_id, label="条目 ID")
    if char_id.startswith("_"):
        raise ValueError("id 不能以下划线开头")
    return char_id


# ========== 角色卡 ==========

def load_character(project: str, char_id: str) -> dict:
    char_id = _safe_id(char_id)
    return load_yaml(
        get_character_path(project, char_id),
        entity_type="character",
        project=project,
        entity_id=char_id,
    )


def list_characters(project: str) -> list[dict]:
    d = resolve_under(get_project_dir(project), "characters")
    if not d.exists():
        return []
    chars = []
    for p in sorted(d.glob("*.yaml")):
        if p.stem.startswith("_") or p.stem.startswith("."):
            continue
        data = load_yaml(
            p,
            entity_type="character",
            project=project,
            entity_id=p.stem,
        )
        if data:
            chars.append(data)
    return chars


def save_character(project: str, char_id: str, data: dict) -> Path:
    char_id = _safe_id(char_id)
    if not isinstance(data, dict):
        raise ValueError("角色卡数据顶层必须是对象")
    data = dict(data)
    if data.get("id") and data["id"] != char_id:
        raise ValueError(f"文件 id({char_id}) 与内容 id({data['id']}) 不一致")
    data["id"] = char_id
    with yaml_write_transaction():
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
    project_dir = get_project_dir(project)
    if not project_dir.is_dir():
        raise FileNotFoundError(f"项目 {project} 不存在")
    d = resolve_under(project_dir, "worldbook")
    if not d.exists():
        return []
    entries = []
    paths = sorted(
        (*d.glob("*.yaml"), *d.glob("*.yml")),
        key=lambda path: (path.stem.casefold(), path.stem, path.suffix),
    )
    seen_stems: set[str] = set()
    for p in paths:
        if p.stem.startswith("_") or p.stem.startswith("."):
            continue
        if p.stem in seen_stems:
            payload = p.read_bytes()
            raise DataCorruptionError.from_bytes(
                p,
                payload,
                entity_type="worldbook",
                project=project,
                entity_id=p.stem,
                reason="worldbook_extension_collision",
                quarantine_available=False,
            )
        seen_stems.add(p.stem)
        data = load_yaml(
            p,
            entity_type="worldbook",
            project=project,
            entity_id=p.stem,
        )
        try:
            entries.append(normalize_worldbook_entry(data, p.stem))
        except WorldbookValidationError as exc:
            raise DataCorruptionError.from_bytes(
                p,
                p.read_bytes(),
                entity_type="worldbook",
                project=project,
                entity_id=p.stem,
                reason="worldbook_schema_invalid:" + ",".join(exc.violations),
            ) from exc
    return entries


def save_worldbook(project: str, entry_id: str, data: dict) -> Path:
    entry_id = _safe_id(entry_id)
    project_dir = get_project_dir(project)
    if not project_dir.is_dir():
        raise FileNotFoundError(f"项目 {project} 不存在")
    normalized = normalize_worldbook_entry(data, entry_id, strict=True)
    _validate_yaml_tree(normalized)
    serialized = yaml.safe_dump(normalized, allow_unicode=True, sort_keys=False)
    if len(serialized.encode("utf-8")) > _MAX_YAML_BYTES:
        raise WorldbookValidationError(["entry:serialized_too_large"])
    assert_project_write_allowed(project)
    with yaml_write_transaction():
        assert_project_write_allowed(project)
        path = get_worldbook_path(project, entry_id)
        _atomic_dump(path, normalized)
    return path


def delete_worldbook_entry(project: str, entry_id: str) -> bool:
    """禁止绕过统一 trash 事务直接删除。"""
    del project, entry_id
    raise RuntimeError("世界书删除必须通过破坏性操作服务执行")


# ========== 用户档案 ==========

def load_user_profile(project: str) -> dict:
    return load_yaml(
        get_user_profile_path(project),
        entity_type="user",
        project=project,
        entity_id="user",
    )


def save_user_profile(project: str, data: dict) -> Path:
    if not isinstance(data, dict):
        raise ValueError("用户档案数据顶层必须是对象")
    with yaml_write_transaction():
        d = ensure_project(project)
        path = resolve_under(d, "user.yaml")
        _atomic_dump(path, dict(data))
    return path


def delete_user_profile(project: str, session: dict = None) -> bool:
    """禁止绕过统一 trash 事务直接删除。"""
    del project, session
    raise RuntimeError("用户删除必须通过破坏性操作服务执行")


# ========== 辅助 ==========

def get_active_character_ids(project: str) -> list[str]:
    return [c["id"] for c in list_characters(project) if c.get("active", True)]
