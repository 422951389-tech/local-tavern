"""本地酒馆日志配置：前台控制台、隐藏模式轮转文件。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.config import LOG_BACKUP_COUNT, LOG_FILE, LOG_MAX_BYTES


_HANDLER_MARKER = "_local_tavern_handler"
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def configure_logging(
    *,
    hidden: bool = False,
    level: int = logging.INFO,
    logger: logging.Logger | None = None,
    log_file: Path = LOG_FILE,
    max_bytes: int = LOG_MAX_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
) -> logging.Handler:
    """幂等配置日志；隐藏模式只写 UTF-8 轮转文件。"""
    target = logger or logging.getLogger()
    existing = [
        handler
        for handler in target.handlers
        if getattr(handler, _HANDLER_MARKER, False)
    ]
    mode = "file" if hidden else "console"
    for handler in existing:
        if getattr(handler, "_local_tavern_mode", None) == mode:
            target.setLevel(level)
            return handler
        target.removeHandler(handler)
        handler.close()

    if hidden:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
    else:
        handler = logging.StreamHandler()

    setattr(handler, _HANDLER_MARKER, True)
    setattr(handler, "_local_tavern_mode", mode)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_FORMAT))
    target.addHandler(handler)
    target.setLevel(level)
    return handler


def reset_logging_for_tests(logger: logging.Logger) -> None:
    """仅供隔离测试移除本模块创建的 handler。"""
    for handler in list(logger.handlers):
        if not getattr(handler, _HANDLER_MARKER, False):
            continue
        logger.removeHandler(handler)
        handler.close()
