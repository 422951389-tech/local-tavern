from __future__ import annotations

import logging
from pathlib import Path

from core.logging_config import configure_logging, reset_logging_for_tests


def _isolated_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = False
    return logger


def test_hidden_logging_is_idempotent_utf8_and_rotates(tmp_path: Path):
    logger = _isolated_logger("tests.local_tavern.hidden")
    log_file = tmp_path / "logs" / "tavern.log"
    first = configure_logging(
        hidden=True,
        logger=logger,
        log_file=log_file,
        max_bytes=160,
        backup_count=2,
    )
    second = configure_logging(
        hidden=True,
        logger=logger,
        log_file=log_file,
        max_bytes=160,
        backup_count=2,
    )
    assert first is second
    assert len(logger.handlers) == 1

    for index in range(30):
        logger.info("轮转验证 %s 中文日志", index)
    first.flush()
    reset_logging_for_tests(logger)

    files = sorted(log_file.parent.glob("tavern.log*"))
    assert log_file in files
    assert len(files) <= 3
    assert not (log_file.parent / "tavern.log.3").exists()
    assert any("中文日志" in item.read_text(encoding="utf-8") for item in files)


def test_switching_modes_replaces_owned_handler(tmp_path: Path):
    logger = _isolated_logger("tests.local_tavern.switch")
    file_handler = configure_logging(
        hidden=True,
        logger=logger,
        log_file=tmp_path / "tavern.log",
        max_bytes=1024,
        backup_count=1,
    )
    console_handler = configure_logging(hidden=False, logger=logger)
    assert console_handler is not file_handler
    assert logger.handlers == [console_handler]
    reset_logging_for_tests(logger)
