"""Local Tavern — FastAPI 主程序

启动：双击 start.bat，或由 core.launcher 统一校验后启动。

v2: 项目+存档双层架构
  data/projects/<项目>/
    ├── characters/    角色卡（存档间共享）
    ├── worldbook/     世界设定（存档间共享）
    ├── user.yaml      用户档案
    └── saves/         存档（状态独立）
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from core.active_turns import ActiveTurnConflict, TurnMaintenanceConflict
from core.backup_store import BackupError
from core.backup_scheduler import run_backup_scheduler
from core.config import (
    BACKUP_DRILL_INTERVAL_DAYS,
    BACKUP_INTERVAL_HOURS,
    BACKUP_SCHEDULE_ENABLED,
    BACKUP_SCHEDULER_POLL_SECONDS,
)
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
    roleplay,
    relationships,
    settings,
    sessions,
    search,
    chat,
    messages,
    migrations,
    prompts,
    recovery,
    health,
    static,
)

logger = logging.getLogger(__name__)


async def _cancel_task(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    # gather 把子任务自身的取消作为结果收口；当前 cleanup task 若被外部取消，
    # CancelledError 仍会向 run_cleanup 传播，确保其余资源继续关闭后再重抛。
    await asyncio.gather(task, return_exceptions=True)


async def _cleanup_lifespan(
    process_metadata: dict,
    *,
    stop_monitor: asyncio.Task | None,
    backup_scheduler: asyncio.Task | None,
    turn_coordinator,
) -> None:
    """逐项清理，任何单项失败都不能阻止 PID 释放。"""
    cancellation: asyncio.CancelledError | None = None

    async def run_cleanup(code: str, operation) -> None:
        nonlocal cancellation
        try:
            await operation()
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
            logger.warning(
                "lifespan_cleanup code=%s exception=CancelledError",
                code,
            )
        except Exception as exc:
            logger.error(
                "lifespan_cleanup code=%s exception=%s",
                code,
                type(exc).__name__,
            )

    release_error: Exception | None = None
    try:
        await run_cleanup("stop_monitor", lambda: _cancel_task(stop_monitor))
        await run_cleanup("backup_scheduler", lambda: _cancel_task(backup_scheduler))
        if turn_coordinator is not None:
            await run_cleanup("turn_coordinator", turn_coordinator.shutdown)
        await run_cleanup("chat_background", chat.shutdown_chat_background_tasks)
        await run_cleanup("ollama_client", lambda: get_client().close())
    finally:
        try:
            release_pid_file(process_metadata)
        except Exception as exc:
            release_error = exc
            logger.error(
                "lifespan_cleanup code=pid_release exception=%s",
                type(exc).__name__,
            )
        logger.info(
            "本地酒馆 PID %s 已完成 lifespan shutdown",
            process_metadata.get("pid"),
        )
    if cancellation is not None:
        raise cancellation
    if release_error is not None:
        raise release_error


# ===== lifespan：替代已弃用的 @app.on_event("startup"/"shutdown") =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    process_metadata = claim_pid_file()
    stop_monitor = None
    turn_coordinator = None
    backup_scheduler = None
    try:
        stop_monitor = asyncio.create_task(wait_for_stop_request(process_metadata))
        pending_restores = await asyncio.to_thread(
            backups.get_backup_manager().pending_restore_ids
        )
        if not pending_restores:
            from core.chat_turns import get_turn_coordinator

            turn_coordinator = get_turn_coordinator()
            await turn_coordinator.ensure_recovered()
        if BACKUP_SCHEDULE_ENABLED:
            from datetime import timedelta

            backup_scheduler = asyncio.create_task(
                run_backup_scheduler(
                    backups.get_backup_manager(),
                    backup_interval=timedelta(hours=BACKUP_INTERVAL_HOURS),
                    drill_interval=timedelta(days=BACKUP_DRILL_INTERVAL_DAYS),
                    poll_seconds=BACKUP_SCHEDULER_POLL_SECONDS,
                )
            )
        logger.info("本地酒馆进程已登记 PID %s", process_metadata["pid"])
        yield
    finally:
        await _cleanup_lifespan(
            process_metadata,
            stop_monitor=stop_monitor,
            backup_scheduler=backup_scheduler,
            turn_coordinator=turn_coordinator,
        )


app = FastAPI(title="Local Tavern", lifespan=lifespan)


def _utf8_safe_validation_value(value):
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, list):
        return [_utf8_safe_validation_value(item) for item in value]
    if isinstance(value, tuple):
        return [_utf8_safe_validation_value(item) for item in value]
    if isinstance(value, dict):
        return {
            _utf8_safe_validation_value(key): _utf8_safe_validation_value(item)
            for key, item in value.items()
        }
    return value


@app.exception_handler(RequestValidationError)
async def handle_request_validation(_request: Request, exc: RequestValidationError):
    """422 只返回定位与原因，不回显原始输入或异常上下文。"""
    errors = [
        {
            key: error[key]
            for key in ("type", "loc", "msg")
            if key in error
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"detail": _utf8_safe_validation_value(errors)},
    )


@app.exception_handler(ActiveTurnConflict)
async def handle_active_turn_conflict(_request: Request, exc: ActiveTurnConflict):
    return JSONResponse(status_code=409, content={"error": exc.as_detail()})


@app.exception_handler(TurnMaintenanceConflict)
async def handle_turn_maintenance(_request: Request, exc: TurnMaintenanceConflict):
    return JSONResponse(status_code=503, content={"error": exc.as_detail()})


@app.exception_handler(DataCorruptionError)
async def handle_data_corruption(_request: Request, exc: DataCorruptionError):
    """以稳定、脱敏的错误契约报告坏档，读取路径不执行隔离写入。"""
    detail = exc.as_detail()
    detail["message"] = "项目数据损坏，请先隔离原件后再恢复"
    detail.pop("reason", None)
    return JSONResponse(status_code=422, content={"error": detail})


@app.middleware("http")
async def enforce_restore_maintenance(request: Request, call_next):
    """整库恢复未收口时阻断其他写入，避免 mixed 状态产生新数据后被回滚。"""
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        from core.active_turns import maintenance_operation

        maintenance = maintenance_operation()
        if maintenance is not None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "turn_maintenance",
                        "message": "整库维护期间写入已暂停",
                        "operation": maintenance,
                    }
                },
            )
        parts = request.url.path.strip("/").split("/")
        is_recovery_action = (
            len(parts) == 5
            and parts[:3] == ["api", "backups", "restores"]
            and parts[4] == "recover"
        )
        if not is_recovery_action:
            try:
                pending = await asyncio.to_thread(
                    backups.get_backup_manager().pending_restore_ids
                )
            except (BackupError, OSError, ValueError):
                logger.exception("整库恢复维护态检测失败")
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "code": "restore_maintenance_check_failed",
                            "message": "整库恢复状态无法验证，写入已暂停",
                        }
                    },
                )
            if pending:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "code": "restore_maintenance_required",
                            "message": "存在未完成的整库恢复，写入已暂停",
                            "restore_ids": pending,
                        }
                    },
                )
            from core.chat_turns import get_turn_coordinator

            try:
                await get_turn_coordinator().ensure_recovered()
            except Exception:
                logger.exception("中断 turn 恢复失败")
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "code": "turn_recovery_failed",
                            "message": "中断聊天状态未完成恢复，写入已暂停",
                        }
                    },
                )
    return await call_next(request)


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
app.include_router(roleplay.router)
app.include_router(relationships.router)
app.include_router(settings.router)
app.include_router(sessions.router)
app.include_router(search.router)
app.include_router(chat.router)
app.include_router(messages.router)
app.include_router(migrations.router)
app.include_router(prompts.router)
app.include_router(recovery.router)
app.include_router(backups.router)
app.include_router(health.router)
app.include_router(static.router)


if __name__ == "__main__":
    from core.launcher import main as launcher_main

    raise SystemExit(launcher_main(["serve", "--workers", "1"]))
