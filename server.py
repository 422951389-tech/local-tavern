"""Local Tavern — FastAPI 主程序

启动：uvicorn server:app --host 127.0.0.1 --port 8765
或双击 start.bat

v2: 项目+存档双层架构
  data/projects/<项目>/
    ├── characters/    角色卡（存档间共享）
    ├── worldbook/     世界设定（存档间共享）
    ├── user.yaml      用户档案
    └── saves/         存档（状态独立）
"""
import logging
import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.ollama_client import get_client
from core.process_guard import claim_pid_file, release_pid_file, wait_for_stop_request
from core.recovery_store import DataCorruptionError
from routes import (
    backups,
    models,
    projects,
    characters,
    user,
    worldbook,
    settings,
    sessions,
    chat,
    messages,
    migrations,
    prompts,
    recovery,
    static,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ===== lifespan：替代已弃用的 @app.on_event("startup"/"shutdown") =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    process_metadata = claim_pid_file()
    stop_monitor = asyncio.create_task(wait_for_stop_request(process_metadata))
    logger.info("本地酒馆进程已登记 PID %s", process_metadata["pid"])
    try:
        yield
    finally:
        # ---- shutdown ----
        stop_monitor.cancel()
        with suppress(asyncio.CancelledError):
            await stop_monitor
        try:
            await get_client().close()
        finally:
            release_pid_file(process_metadata)
            logger.info("本地酒馆 PID %s 已完成 lifespan shutdown", process_metadata["pid"])


app = FastAPI(title="Local Tavern", lifespan=lifespan)


@app.exception_handler(DataCorruptionError)
async def handle_data_corruption(_request: Request, exc: DataCorruptionError):
    """以稳定、脱敏的错误契约报告坏档，读取路径不执行隔离写入。"""
    detail = exc.as_detail()
    detail["message"] = "存档数据损坏，请先隔离原件后再恢复"
    detail.pop("reason", None)
    return JSONResponse(status_code=422, content={"error": detail})


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response

# 静态文件（需在 include_router 之前 mount，避免被路由覆盖）
static.mount_static(app)

# 注册路由
app.include_router(models.router)
app.include_router(projects.router)
app.include_router(characters.router)
app.include_router(user.router)
app.include_router(worldbook.router)
app.include_router(settings.router)
app.include_router(sessions.router)
app.include_router(chat.router)
app.include_router(messages.router)
app.include_router(migrations.router)
app.include_router(prompts.router)
app.include_router(recovery.router)
app.include_router(backups.router)
app.include_router(static.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, workers=1, log_level="info")
