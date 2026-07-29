"""稳定且脱敏的存活/就绪检查。"""
from __future__ import annotations

import asyncio
import os
import secrets
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from core.config import DATA_DIR


LIVE_PAYLOAD = {
    "status": "alive",
    "service": "local-tavern",
    "contract_version": 1,
}
_OLLAMA_CODES = {
    "timeout",
    "unreachable",
    "bad_status",
    "invalid_response",
    "no_models",
}
_DATA_CODES = {
    "data_unavailable",
    "data_not_writable",
    "data_readback",
    "data_io",
}
_RUNTIME_CODES = {
    "python_version",
    "lock_invalid",
    "package_missing",
    "package_version",
}
_MAINTENANCE_CODES = {
    "maintenance_active",
    "restore_pending",
    "maintenance_unavailable",
}
_PROVIDER_CODES = {"provider_config_unavailable"}


def _ok() -> dict[str, str]:
    return {"status": "ok"}


def _error(code: str) -> dict[str, str]:
    return {"status": "error", "code": code}


def probe_data_directory(data_dir: Path = DATA_DIR) -> dict[str, str]:
    """创建自身匿名临时文件，验证目录真实读写与 fsync。"""
    root = Path(data_dir)
    if not root.exists() or not root.is_dir():
        return _error("data_unavailable")
    nonce = secrets.token_bytes(32)
    try:
        with tempfile.TemporaryFile(mode="w+b", dir=root) as probe:
            probe.write(nonce)
            probe.flush()
            os.fsync(probe.fileno())
            probe.seek(0)
            if probe.read() != nonce:
                return _error("data_readback")
    except PermissionError:
        return _error("data_not_writable")
    except OSError:
        return _error("data_io")
    return _ok()


def probe_runtime() -> dict[str, str]:
    """把运行时校验结果压缩为固定代码，不暴露包名或版本。"""
    try:
        from core.runtime_validation import validate_runtime

        result = validate_runtime()
    except Exception:
        return _error("lock_invalid")
    if isinstance(result, dict) and result.get("ok") is True:
        return _ok()
    code = result.get("code") if isinstance(result, dict) else None
    return _error(code if code in _RUNTIME_CODES else "lock_invalid")


def probe_maintenance() -> dict[str, str]:
    """检查进程维护态与未完成恢复，只公开有限状态码。"""
    try:
        from core.active_turns import maintenance_operation
        from routes.backups import get_backup_manager

        if maintenance_operation() is not None:
            return _error("maintenance_active")
        if get_backup_manager().pending_restore_ids():
            return _error("restore_pending")
    except Exception:
        return _error("maintenance_unavailable")
    return _ok()


def probe_provider_registry() -> dict[str, object]:
    """只检查 Provider 配置可读性；不向任何云端发请求。"""
    try:
        from core.provider_registry import get_provider_registry

        providers = get_provider_registry().list_configs()
        cloud_configured = any(
            isinstance(item, dict)
            and item.get("kind") != "ollama"
            and item.get("has_credential") is True
            for item in providers
        )
    except Exception:
        return _error("provider_config_unavailable")
    return {"status": "ok", "cloud_configured": cloud_configured}


def _normalize_ollama_result(value: object) -> dict[str, str]:
    if isinstance(value, dict) and value.get("ok") is True:
        return _ok()
    code = value.get("code") if isinstance(value, dict) else None
    return _error(code if code in _OLLAMA_CODES else "invalid_response")


def _normalize_sync_result(
    value: object,
    *,
    allowed_codes: set[str],
    fallback_code: str,
) -> dict[str, str]:
    """只接受内部固定结果，防止异常 probe 把原文带进 health 响应。"""
    if isinstance(value, dict) and value.get("status") == "ok":
        return _ok()
    code = value.get("code") if isinstance(value, dict) else None
    return _error(code if code in allowed_codes else fallback_code)


def _run_sync_probe(
    probe: Callable[[], object],
    *,
    allowed_codes: set[str],
    fallback_code: str,
) -> dict[str, str]:
    try:
        value = probe()
    except Exception:
        return _error(fallback_code)
    return _normalize_sync_result(
        value,
        allowed_codes=allowed_codes,
        fallback_code=fallback_code,
    )


def _run_provider_probe(probe: Callable[[], object]) -> object:
    try:
        return probe()
    except Exception:
        return _error("provider_config_unavailable")


async def readiness_report(
    *,
    data_probe: Callable[[], dict[str, str]] = probe_data_directory,
    runtime_probe: Callable[[], dict[str, str]] = probe_runtime,
    maintenance_probe: Callable[[], dict[str, str]] = probe_maintenance,
    provider_probe: Callable[[], dict[str, object]] = probe_provider_registry,
    ollama_probe: Callable[[], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, object]:
    """检查本地基础设施和至少一个推理入口；不自动请求云端。"""
    if ollama_probe is None:
        from core.ollama_client import get_client

        ollama_probe = get_client().probe_health

    data, runtime, maintenance, provider_result = await asyncio.gather(
        asyncio.to_thread(
            _run_sync_probe,
            data_probe,
            allowed_codes=_DATA_CODES,
            fallback_code="data_io",
        ),
        asyncio.to_thread(
            _run_sync_probe,
            runtime_probe,
            allowed_codes=_RUNTIME_CODES,
            fallback_code="lock_invalid",
        ),
        asyncio.to_thread(
            _run_sync_probe,
            maintenance_probe,
            allowed_codes=_MAINTENANCE_CODES,
            fallback_code="maintenance_unavailable",
        ),
        asyncio.to_thread(_run_provider_probe, provider_probe),
    )
    if (
        isinstance(provider_result, dict)
        and provider_result.get("status") == "ok"
        and isinstance(provider_result.get("cloud_configured"), bool)
    ):
        providers = {
            "status": "ok",
            "cloud_configured": provider_result["cloud_configured"],
            "connectivity": "not_probed",
        }
    else:
        code = (
            provider_result.get("code")
            if isinstance(provider_result, dict)
            else None
        )
        providers = _error(
            code if code in _PROVIDER_CODES else "provider_config_unavailable"
        )
    try:
        ollama = _normalize_ollama_result(await ollama_probe())
    except Exception:
        ollama = _error("unreachable")

    checks = {
        "data": data,
        "runtime": runtime,
        "ollama": ollama,
        "providers": providers,
        "maintenance": maintenance,
    }
    essentials_ready = all(
        checks[name] == _ok()
        for name in ("data", "runtime", "maintenance")
    ) and providers.get("status") == "ok"
    inference_ready = (
        ollama == _ok()
        or providers.get("cloud_configured") is True
    )
    if ollama == _ok():
        inference = {
            "status": "ok",
            "source": "ollama",
            "connectivity": "verified",
        }
    elif providers.get("cloud_configured") is True:
        inference = {
            "status": "ok",
            "source": "cloud_configuration",
            "connectivity": "not_probed",
        }
    else:
        inference = _error("inference_unavailable")
    checks["inference"] = inference
    ready = essentials_ready and inference_ready
    return {
        "status": "ready" if ready else "not_ready",
        "service": "local-tavern",
        "contract_version": 2,
        "readiness_scope": "configuration",
        "checks": checks,
    }
