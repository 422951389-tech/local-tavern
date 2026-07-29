"""首次迁移前只读检测旧版 Uvicorn 服务，防止并发复制活动数据。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LegacyService:
    pid: int
    port: int
    project_root: Path


def detect_verified_legacy_service(legacy_root: Path) -> LegacyService | None:
    """仅当 PID、命令行、项目根与监听端口全部吻合时返回。"""
    root = Path(legacy_root).resolve(strict=False)
    pid_path = root / "tavern.pid"
    try:
        if pid_path.stat().st_size > 16 * 1024:
            return None
        metadata = json.loads(pid_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict):
        return None
    pid = metadata.get("pid")
    port = metadata.get("port")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
    ):
        return None

    try:
        recorded_root = Path(metadata.get("project_root", "")).resolve(strict=False)
    except (OSError, TypeError, ValueError):
        return None
    if recorded_root != root:
        return None

    # 在桌面环境变量已固定后导入，避免 core.config 提前锁定错误目录。
    # 不复用 metadata_matches_process：它会把 metadata.port 与当前桌面 PORT
    # 比较，从而漏掉使用非默认端口运行的旧服务。
    from core.process_guard import (
        _listening_pids,
        _pid_is_running,
        _process_command_line,
    )

    if not _pid_is_running(pid):
        return None
    command_line = _process_command_line(pid)
    if not _command_matches_legacy_tavern(command_line):
        return None
    if pid not in _listening_pids(port):
        return None
    return LegacyService(pid=pid, port=port, project_root=root)


def _command_matches_legacy_tavern(command_line: str) -> bool:
    command = str(command_line).casefold().replace("\\", "/")
    module_launcher = "core" + ".launcher"
    return (
        ("uvicorn" in command and "server:app" in command)
        or (
            (module_launcher in command or "core/launcher.py" in command)
            and " serve" in command
        )
        or command.endswith("server.py")
        or " server.py " in command
    )
