"""进程存活与依赖就绪端点。"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.health import LIVE_PAYLOAD, readiness_report


router = APIRouter()


def _no_store(payload: dict, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/health/live")
async def health_live() -> JSONResponse:
    """只证明事件循环和路由存活，不触碰磁盘、依赖或 Ollama。"""
    return _no_store(dict(LIVE_PAYLOAD))


@router.get("/health/ready")
async def health_ready() -> JSONResponse:
    report = await readiness_report()
    return _no_store(
        report,
        status_code=200 if report["status"] == "ready" else 503,
    )
