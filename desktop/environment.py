"""桌面版资源目录与可写用户目录初始化。"""

from __future__ import annotations

import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path


APP_DIRECTORY_NAME = "LocalTavern"
_WRITABLE_PATH_ENVIRONMENTS = (
    "TAVERN_BASE_DIR",
    "TAVERN_DATA_DIR",
    "TAVERN_SETTINGS_PATH",
    "TAVERN_PROJECTS_DIR",
    "TAVERN_PROMPTS_DIR",
    "TAVERN_RECOVERY_DIR",
    "TAVERN_MIGRATIONS_DIR",
    "TAVERN_BACKUP_DIR",
    "TAVERN_LOG_DIR",
    "TAVERN_LOG_FILE",
    "TAVERN_PID_PATH",
    "TAVERN_STOP_REQUEST_PATH",
    "TAVERN_PROVIDER_DATA_DIR",
    "TAVERN_PROVIDER_CONFIG_PATH",
    "TAVERN_PROVIDER_SECRETS_PATH",
)


@dataclass(frozen=True, slots=True)
class DesktopPaths:
    resource_root: Path
    user_root: Path
    data_dir: Path
    projects_dir: Path
    prompts_dir: Path
    backups_dir: Path
    logs_dir: Path
    runtime_dir: Path


def default_user_root() -> Path:
    override = os.environ.get("TAVERN_DESKTOP_USER_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve(strict=False)
    configured_base = os.environ.get("TAVERN_BASE_DIR", "").strip()
    if configured_base:
        return Path(configured_base).expanduser().resolve(strict=False)
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if not local_app_data:
            local_app_data = str(Path.home() / "AppData" / "Local")
        return (Path(local_app_data) / APP_DIRECTORY_NAME).resolve(strict=False)
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local" / "share"
    return (base / "local-tavern").resolve(strict=False)


def build_paths(resource_root: Path, user_root: Path | None = None) -> DesktopPaths:
    resources = Path(resource_root).resolve(strict=False)
    writable = (user_root or default_user_root()).resolve(strict=False)
    data_dir = _configured_path("TAVERN_DATA_DIR", writable / "data")
    projects_dir = _configured_path("TAVERN_PROJECTS_DIR", data_dir / "projects")
    prompts_dir = _configured_path("TAVERN_PROMPTS_DIR", writable / "prompts")
    backups_dir = _configured_path("TAVERN_BACKUP_DIR", writable / "backups")
    logs_dir = _configured_path("TAVERN_LOG_DIR", writable / "logs")
    configured_pid = os.environ.get("TAVERN_PID_PATH", "").strip()
    runtime_dir = (
        Path(configured_pid).expanduser().resolve(strict=False).parent
        if configured_pid
        else writable / "runtime"
    )
    return DesktopPaths(
        resource_root=resources,
        user_root=writable,
        data_dir=data_dir,
        projects_dir=projects_dir,
        prompts_dir=prompts_dir,
        backups_dir=backups_dir,
        logs_dir=logs_dir,
        runtime_dir=runtime_dir,
    )


def prepare_desktop_environment(
    resource_root: Path,
    *,
    user_root: Path | None = None,
    legacy_root: Path | None = None,
    defer_storage_initialization: bool = False,
) -> DesktopPaths:
    """在导入 ``core.config`` 前固定所有可写路径。

    调用方预置的可写 ``TAVERN_*`` 路径始终优先。只读 Web 资源固定来自
    ``resource_root``，不能被旧环境变量改写。旧版目录绝不覆盖已有桌面数据。
    """
    if "core.config" in sys.modules:
        raise RuntimeError("必须在导入 core.config 前初始化桌面环境")

    paths = build_paths(resource_root, user_root)
    paths.user_root.mkdir(parents=True, exist_ok=True)
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)

    legacy = Path(legacy_root).resolve(strict=False) if legacy_root is not None else None
    if legacy is not None and legacy != paths.user_root:
        migrate_legacy_data(paths, legacy)
    if not defer_storage_initialization:
        ensure_storage_directories(paths)
        ensure_prompt_assets(paths)

    provider_data_raw = os.environ.get("TAVERN_PROVIDER_DATA_DIR", "").strip()
    provider_data_dir = (
        Path(provider_data_raw).expanduser()
        if provider_data_raw
        else paths.user_root
    )

    defaults = {
        "TAVERN_BASE_DIR": str(paths.user_root),
        "TAVERN_DATA_DIR": str(paths.data_dir),
        "TAVERN_SETTINGS_PATH": str(paths.data_dir / "settings.json"),
        "TAVERN_PROJECTS_DIR": str(paths.projects_dir),
        "TAVERN_PROMPTS_DIR": str(paths.prompts_dir),
        "TAVERN_RECOVERY_DIR": str(paths.data_dir / ".recovery"),
        "TAVERN_MIGRATIONS_DIR": str(paths.data_dir / ".migrations"),
        "TAVERN_BACKUP_DIR": str(paths.backups_dir),
        "TAVERN_LOG_DIR": str(paths.logs_dir),
        "TAVERN_LOG_FILE": str(paths.logs_dir / "tavern-desktop.log"),
        "TAVERN_PID_PATH": str(paths.runtime_dir / "desktop.pid"),
        "TAVERN_STOP_REQUEST_PATH": str(paths.runtime_dir / "desktop.stop.pid"),
        "TAVERN_PROVIDER_DATA_DIR": str(provider_data_dir),
        "TAVERN_PROVIDER_CONFIG_PATH": str(provider_data_dir / "providers.json"),
        "TAVERN_PROVIDER_SECRETS_PATH": str(
            provider_data_dir / "provider-secrets.json"
        ),
    }
    for name, value in defaults.items():
        if not os.environ.get(name, "").strip():
            os.environ[name] = value
    # 只读资源必须来自当前源码根或 PyInstaller _MEIPASS，禁止加载旧目录代码。
    os.environ["TAVERN_WEB_DIR"] = str(paths.resource_root / "web")
    # 桌面边界不接受外部服务环境覆盖：无监听器、固定回环虚拟 origin。
    os.environ["TAVERN_DESKTOP_MODE"] = "true"
    os.environ["TAVERN_ALLOW_REMOTE"] = "false"
    os.environ["TAVERN_HOST"] = "127.0.0.1"
    os.environ["TAVERN_PORT"] = "8765"
    return paths


def ensure_storage_directories(paths: DesktopPaths) -> None:
    paths.backups_dir.mkdir(parents=True, exist_ok=True)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.projects_dir.mkdir(parents=True, exist_ok=True)


def ensure_prompt_assets(paths: DesktopPaths) -> None:
    """在可写目录中补齐打包默认提示词，不覆盖迁移或用户已有文件。"""
    paths.prompts_dir.mkdir(parents=True, exist_ok=True)
    _merge_missing_tree(paths.resource_root / "prompts", paths.prompts_dir)


def migrate_legacy_data(paths: DesktopPaths, legacy_root: Path) -> dict[str, bool]:
    """迁移旧版数据；已有目标优先，缺失提示词逐文件补齐。"""
    legacy = Path(legacy_root).resolve(strict=False)
    if legacy == paths.user_root:
        return {"data": False, "backups": False}
    if not _copy_tree_if_absent(legacy / "prompts", paths.prompts_dir):
        _merge_missing_tree(legacy / "prompts", paths.prompts_dir)
    return {
        "data": _copy_tree_if_absent(legacy / "data", paths.data_dir),
        "backups": _copy_tree_if_absent(legacy / "backups", paths.backups_dir),
    }


def paths_touch_root(paths: DesktopPaths, root: Path) -> bool:
    """判断桌面所有已解析或显式可写路径是否落入指定根目录。"""
    candidates = {
        paths.user_root,
        paths.data_dir,
        paths.projects_dir,
        paths.prompts_dir,
        paths.backups_dir,
        paths.logs_dir,
        paths.runtime_dir,
    }
    for name in _WRITABLE_PATH_ENVIRONMENTS:
        raw = os.environ.get(name, "").strip()
        if raw:
            candidates.add(Path(raw).expanduser())
    resolved_root = Path(root).resolve(strict=False)
    return any(_is_within(candidate, resolved_root) for candidate in candidates)


def _configured_path(name: str, fallback: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return (
        Path(raw).expanduser().resolve(strict=False)
        if raw
        else Path(fallback).resolve(strict=False)
    )


def _copy_tree_if_absent(source: Path, target: Path) -> bool:
    if target.exists() or not source.is_dir():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.desktop-import-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, temporary, copy_function=shutil.copy2)
        temporary.replace(target)
        return True
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def _merge_missing_tree(source: Path, target: Path) -> bool:
    if not source.is_dir():
        return False
    changed = False
    for source_path in source.rglob("*"):
        relative = source_path.relative_to(source)
        target_path = target / relative
        if source_path.is_dir():
            changed = changed or not target_path.exists()
            target_path.mkdir(parents=True, exist_ok=True)
        elif source_path.is_file() and not target_path.exists():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            changed = True
    return changed


def _is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(root)
        return True
    except (OSError, ValueError):
        return False
