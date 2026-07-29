"""脱敏诊断中心与支持包 API。"""
from __future__ import annotations

import asyncio
from copy import deepcopy

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.diagnostics import local_diagnostics
from core.health import readiness_report
from core.provider_registry import get_provider_registry
from routes.backups import get_backup_manager


router = APIRouter()


def _provider_summary() -> list[dict[str, object]]:
    result = []
    for item in get_provider_registry().list_configs():
        if not isinstance(item, dict):
            continue
        models = item.get("models")
        result.append({
            "id": item.get("provider_id"),
            "name": item.get("name"),
            "kind": item.get("kind"),
            "has_credential": item.get("has_credential") is True,
            "model_count": len(models) if isinstance(models, list) else 0,
            "context_limit": item.get("context_limit"),
        })
    return result


def _backup_summary() -> dict[str, object]:
    manager = get_backup_manager()
    backups = manager.list_backups()
    valid = [item for item in backups if item.get("status") != "invalid"]
    invalid = [item for item in backups if item.get("status") == "invalid"]
    latest = valid[0] if valid else None
    latest_summary = None
    if isinstance(latest, dict):
        latest_summary = {
            "backup_id": latest.get("backup_id"),
            "kind": latest.get("kind"),
            "created_at": latest.get("created_at"),
            "file_count": latest.get("file_count"),
            "total_size": latest.get("total_size"),
            "expired": latest.get("expired") is True,
        }
    return {
        "total": len(backups),
        "valid": len(valid),
        "invalid": len(invalid),
        "pending_restores": len(manager.pending_restore_ids()),
        "latest": latest_summary,
    }


async def _snapshot() -> dict[str, object]:
    local, health, providers, backups = await asyncio.gather(
        asyncio.to_thread(local_diagnostics),
        readiness_report(),
        asyncio.to_thread(_provider_summary),
        asyncio.to_thread(_backup_summary),
    )
    result = deepcopy(local)
    result["health"] = health
    result["providers"] = providers
    result["backups"] = backups
    return result


@router.get("/api/diagnostics")
async def api_diagnostics():
    return await _snapshot()


@router.get("/api/diagnostics/support-bundle")
async def api_support_bundle():
    return JSONResponse(
        await _snapshot(),
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": (
                'attachment; filename="local-tavern-support-bundle.json"'
            ),
        },
    )
