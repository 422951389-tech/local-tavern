"""统一的文件 ID 与路径边界策略。

显示名可包含中文和空格；磁盘 ID 必须是规范化、可验证的单个路径组件。
所有外部输入形成路径前都通过本模块，并在 resolve 后确认仍位于指定根目录。
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path, PureWindowsPath
from typing import Iterable


MAX_ID_LENGTH = 80
MAX_DISPLAY_NAME_LENGTH = 200

_INVALID_WINDOWS_CHARS = set('\\/:*?"<>|%')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_SNAPSHOT_TIMESTAMP_RE = r"\d{8}_\d{6}"


class PathPolicyError(ValueError):
    """外部 ID 或路径不符合本地数据边界。"""


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise PathPolicyError(f"{label} 必须是字符串")
    if not value:
        raise PathPolicyError(f"{label} 不能为空")
    return value


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(ch).startswith("C") for ch in value)


def _is_windows_reserved(value: str) -> bool:
    stem = value.split(".", 1)[0].upper()
    return stem in _WINDOWS_RESERVED


def _reject_path_syntax(value: str, label: str) -> None:
    win_path = PureWindowsPath(value)
    if win_path.is_absolute() or bool(win_path.drive) or value.startswith(("/", "\\")):
        raise PathPolicyError(f"{label} 不能是绝对路径")
    if any(ch in value for ch in ("/", "\\")):
        raise PathPolicyError(f"{label} 不能包含路径分隔符")
    if value in {".", ".."}:
        raise PathPolicyError(f"{label} 不能是点段")


def validate_file_id(value: object, *, label: str = "文件 ID") -> str:
    """严格验证引用既有资源时使用的稳定磁盘 ID。"""
    text = _require_text(value, label)
    normalized = unicodedata.normalize("NFKC", text)
    if normalized != text:
        raise PathPolicyError(f"{label} 必须使用 Unicode 规范形式")
    if text != text.strip():
        raise PathPolicyError(f"{label} 不能包含首尾空白")
    if len(text) > MAX_ID_LENGTH:
        raise PathPolicyError(f"{label} 不能超过 {MAX_ID_LENGTH} 个字符")
    _reject_path_syntax(text, label)
    if text.startswith(".") or text.endswith("."):
        raise PathPolicyError(f"{label} 不能以点开头或结尾")
    if any(ch.isspace() for ch in text):
        raise PathPolicyError(f"{label} 不能包含空白字符")
    if _has_control_character(text):
        raise PathPolicyError(f"{label} 不能包含控制字符")
    invalid = next((ch for ch in text if ch in _INVALID_WINDOWS_CHARS), None)
    if invalid is not None:
        raise PathPolicyError(f"{label} 包含非法字符: {invalid}")
    if _is_windows_reserved(text):
        raise PathPolicyError(f"{label} 不能使用 Windows 保留名")
    return text


def display_name_to_id(
    value: object,
    *,
    label: str = "显示名",
    strip_suffixes: Iterable[str] = (".json", ".tmp"),
) -> str:
    """把新建资源的显示名转换为稳定 ID；路径语法直接拒绝。"""
    text = _require_text(value, label)
    text = unicodedata.normalize("NFKC", text).strip()
    if not text:
        raise PathPolicyError(f"{label} 不能为空")
    if len(text) > MAX_DISPLAY_NAME_LENGTH:
        raise PathPolicyError(f"{label} 不能超过 {MAX_DISPLAY_NAME_LENGTH} 个字符")
    _reject_path_syntax(text, label)
    if _has_control_character(text):
        raise PathPolicyError(f"{label} 不能包含控制字符")

    lowered = text.lower()
    for suffix in strip_suffixes:
        if lowered.endswith(suffix.lower()):
            text = text[: -len(suffix)]
            break

    candidate = re.sub(r'[\s:*?"<>|]+', "_", text)
    candidate = re.sub(r"_+", "_", candidate).strip("._")
    candidate = candidate[:MAX_ID_LENGTH].rstrip("._")
    if not candidate:
        raise PathPolicyError(f"{label} 无法生成有效文件 ID")
    return validate_file_id(candidate, label=f"{label}生成的文件 ID")


def resolve_under(root: Path, *parts: str) -> Path:
    """解析子路径，并强制结果仍在 root 内。"""
    root_resolved = Path(root).resolve(strict=False)
    candidate = root_resolved.joinpath(*parts).resolve(strict=False)
    if candidate == root_resolved or not candidate.is_relative_to(root_resolved):
        raise PathPolicyError("解析后的路径超出允许根目录")
    return candidate


def resolve_project_dir(projects_root: Path, project_id: object) -> Path:
    project = validate_file_id(project_id, label="项目 ID")
    return resolve_under(projects_root, project)


def resolve_saves_dir(projects_root: Path, project_id: object) -> Path:
    return resolve_under(resolve_project_dir(projects_root, project_id), "saves")


def resolve_session_path(projects_root: Path, project_id: object, save_id: object) -> Path:
    save = validate_file_id(save_id, label="存档 ID")
    return resolve_under(resolve_saves_dir(projects_root, project_id), f"{save}.json")


def validate_snapshot_filename(save_id: object, filename: object) -> tuple[str, str]:
    """验证快照 basename、所属存档和类型，返回 (文件名, 类型)。"""
    save = validate_file_id(save_id, label="存档 ID")
    name = _require_text(filename, "快照文件名")
    if unicodedata.normalize("NFKC", name) != name:
        raise PathPolicyError("快照文件名必须使用 Unicode 规范形式")
    _reject_path_syntax(name, "快照文件名")
    if PureWindowsPath(name).name != name:
        raise PathPolicyError("快照文件名必须是 basename")

    pattern = re.compile(
        rf"^{re.escape(save)}\.(?:(?P<kind>trim|reset)\.)?{_SNAPSHOT_TIMESTAMP_RE}\.json$"
    )
    match = pattern.fullmatch(name)
    if not match:
        raise PathPolicyError("快照文件名与当前存档、类型或时间格式不匹配")
    return name, match.group("kind") or "snapshot"


def resolve_snapshot_path(
    projects_root: Path,
    project_id: object,
    save_id: object,
    filename: object,
    *,
    allowed_types: Iterable[str] = ("snapshot", "reset", "trim"),
) -> tuple[Path, str]:
    name, kind = validate_snapshot_filename(save_id, filename)
    if kind not in set(allowed_types):
        raise PathPolicyError(f"不允许使用 {kind} 类型快照")
    history_root = resolve_under(resolve_saves_dir(projects_root, project_id), ".history")
    return resolve_under(history_root, name), kind
