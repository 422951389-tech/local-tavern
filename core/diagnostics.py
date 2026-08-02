"""本地诊断快照与脱敏支持包。

本模块只输出固定白名单字段。不得返回文件路径、Provider 地址、凭据、
项目/存档身份、Prompt、角色/世界书内容或对话正文。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
import platform
import re
import sys
from pathlib import Path

from core.config import BASE_DIR, DATA_DIR, DESKTOP_MODE, LOG_FILE


_LOG_HEADER = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[^ ]*)\s+"
    r"\[(?P<level>[A-Z]+)]\s+(?P<component>[A-Za-z0-9_.-]+):"
)
_CODE = re.compile(r"(?:error_)?code=(?P<code>[a-z][a-z0-9_]{1,63})")
_SAFE_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})


def _release_manifest_path(base_dir: Path = BASE_DIR) -> Path:
    if getattr(sys, "frozen", False):
        # onedir 发行结构：release/LocalTavern/LocalTavern.exe，清单位于
        # release/release-manifest.json。仅解析固定父级，不扫描任意路径。
        return Path(sys.executable).resolve().parent.parent / "release-manifest.json"
    return Path(base_dir) / "release" / "release-manifest.json"


def _release_manifest_summary(base_dir: Path = BASE_DIR) -> dict[str, object]:
    path = _release_manifest_path(base_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unavailable"}
    if not isinstance(payload, dict):
        return {"status": "invalid"}
    result: dict[str, object] = {
        "status": "available",
        "schema_version": payload.get("schema_version"),
        "application": payload.get("application"),
        "packaging": payload.get("packaging"),
        "file_count": payload.get("file_count"),
        "total_bytes": payload.get("total_bytes"),
    }
    provenance = payload.get("source")
    if isinstance(provenance, dict):
        result["source"] = {
            "commit": provenance.get("commit"),
            "dirty": provenance.get("dirty"),
        }
    return result


def turn_store_summary(data_dir: Path = DATA_DIR) -> dict[str, object]:
    root = Path(data_dir) / ".chat-turns"
    if not root.is_dir():
        return {
            "status": "ok",
            "total": 0,
            "active": 0,
            "terminal": 0,
            "invalid": 0,
            "event_log_bytes": 0,
        }
    counts: Counter[str] = Counter()
    event_log_bytes = 0
    for path in root.iterdir():
        if not path.is_dir():
            continue
        try:
            meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            counts["invalid"] += 1
            continue
        if not isinstance(meta, dict):
            counts["invalid"] += 1
            continue
        status = meta.get("status")
        if status in {"pending", "streaming"}:
            counts["active"] += 1
        elif status in {"completed", "cancelled", "failed"}:
            counts["terminal"] += 1
        else:
            counts["invalid"] += 1
        size = meta.get("event_log_bytes", 0)
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            event_log_bytes += size
    total = counts["active"] + counts["terminal"] + counts["invalid"]
    return {
        "status": "ok",
        "total": total,
        "active": counts["active"],
        "terminal": counts["terminal"],
        "invalid": counts["invalid"],
        "event_log_bytes": event_log_bytes,
    }


def recent_error_codes(
    log_file: Path = LOG_FILE,
    *,
    max_read_bytes: int = 512 * 1024,
    max_items: int = 20,
) -> list[dict[str, str]]:
    """从日志尾部提取固定元数据，不返回原始消息。"""

    path = Path(log_file)
    if not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - max_read_bytes))
            text = handle.read(max_read_bytes).decode("utf-8", errors="replace")
    except OSError:
        return []
    result: list[dict[str, str]] = []
    for line in reversed(text.splitlines()):
        header = _LOG_HEADER.match(line)
        if header is None or header.group("level") not in _SAFE_LEVELS:
            continue
        code_match = _CODE.search(line)
        result.append({
            "timestamp": header.group("timestamp")[:32],
            "level": header.group("level"),
            "component": header.group("component")[:80],
            "code": code_match.group("code") if code_match else "unclassified_error",
        })
        if len(result) >= max_items:
            break
    result.reverse()
    return result


def local_diagnostics() -> dict[str, object]:
    renderer = os.environ.get("TAVERN_DESKTOP_RENDERER_ACTIVE", "").casefold()
    if renderer not in {"software", "hardware"}:
        renderer = "not_applicable" if not DESKTOP_MODE else "software"
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "application": {
            "name": "Local Tavern",
            "runtime_mode": "desktop" if DESKTOP_MODE else "local_development",
            "transport": "in_process_asgi" if DESKTOP_MODE else "loopback_http",
        },
        "runtime": {
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "desktop_rendering": {
            "mode": renderer,
            "gpu_acceleration": renderer == "hardware",
            "restart_required_after_change": True,
        },
        "release": _release_manifest_summary(),
        "turns": turn_store_summary(),
        "recent_errors": recent_error_codes(),
    }


__all__ = ["local_diagnostics", "recent_error_codes", "turn_store_summary"]
