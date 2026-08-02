"""Local Tavern — FastAPI 主程序

正式使用由独立桌面程序进程内加载；start.bat/core.launcher 仅保留浏览器开发模式。

v2: 项目+存档双层架构
  data/projects/<项目>/
    ├── characters/    角色卡（存档间共享）
    ├── worldbook/     世界设定（存档间共享）
    ├── user.yaml      用户档案
    └── saves/         存档（状态独立）
"""

import asyncio
import logging
from time import perf_counter
from uuid import uuid4
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.active_turns import ActiveTurnConflict, TurnMaintenanceConflict
from core.api_errors import (
    apply_security_headers,
    error_response,
    http_error_response,
    validation_error_detail,
)
from core.backup_store import BackupError
from core.backup_scheduler import run_backup_scheduler
from core.config import (
    BACKUP_DRILL_INTERVAL_DAYS,
    BACKUP_INTERVAL_HOURS,
    BACKUP_SCHEDULE_ENABLED,
    BACKUP_SCHEDULER_POLL_SECONDS,
    DESKTOP_MODE,
)
from core.ollama_client import get_client
from core.provider_registry import close_provider_registry, get_provider_registry
from core.process_guard import claim_pid_file, release_pid_file, wait_for_stop_request
from core.recovery_store import DataCorruptionError
from core.request_security import host_header_allowed
from routes import (
    backups,
    models,
    providers,
    projects,
    characters,
    user,
    worldbook,
    world_state,
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
    diagnostics,
    memory,
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
    process_metadata: dict | None,
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
        await run_cleanup("provider_registry", close_provider_registry)
        await run_cleanup("ollama_client", lambda: get_client().close())
    finally:
        if process_metadata is not None:
            try:
                await asyncio.to_thread(release_pid_file, process_metadata)
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
        else:
            logger.info("本地酒馆桌面运行时已完成 lifespan shutdown")
    if cancellation is not None:
        raise cancellation
    if release_error is not None:
        raise release_error


# ===== lifespan：替代已弃用的 @app.on_event("startup"/"shutdown") =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    process_metadata = (
        None if DESKTOP_MODE else await asyncio.to_thread(claim_pid_file)
    )
    stop_monitor = None
    turn_coordinator = None
    backup_scheduler = None
    try:
        if process_metadata is not None:
            stop_monitor = asyncio.create_task(wait_for_stop_request(process_metadata))
        pending_restores = await asyncio.to_thread(
            backups.get_backup_manager().pending_restore_ids
        )
        # Provider 配置和 DPAPI 凭据索引含磁盘读取，启动阶段也不阻塞事件循环。
        await asyncio.to_thread(get_provider_registry)
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
        if process_metadata is not None:
            logger.info("本地酒馆进程已登记 PID %s", process_metadata["pid"])
        else:
            logger.info("本地酒馆已进入无端口桌面模式")
        yield
    finally:
        await _cleanup_lifespan(
            process_metadata,
            stop_monitor=stop_monitor,
            backup_scheduler=backup_scheduler,
            turn_coordinator=turn_coordinator,
        )


app = FastAPI(title="Local Tavern", lifespan=lifespan)


@app.middleware("http")
async def add_correlation_context(request: Request, call_next):
    """为每个请求生成本地关联 ID；日志不记录查询值或请求正文。"""
    correlation_id = str(uuid4())
    request.state.correlation_id = correlation_id
    started = perf_counter()
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    if request.url.path.startswith("/api/"):
        logger.info(
            "api_request correlation_id=%s method=%s path=%s status=%s duration_ms=%s",
            correlation_id,
            request.method,
            request.url.path,
            response.status_code,
            max(0, round((perf_counter() - started) * 1000)),
        )
    return response


@app.middleware("http")
async def enforce_trusted_host(request: Request, call_next):
    """本地模式只接受回环 Host，阻断 Host 欺骗与 DNS rebinding 写入口。"""
    if not host_header_allowed(request.headers.get("host")):
        return error_response(
            400,
            "host_not_allowed",
            "请求 Host 不在服务允许范围内",
            {},
        )
    return await call_next(request)


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(_request: Request, exc: StarletteHTTPException):
    return http_error_response(
        exc.status_code,
        exc.detail,
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def handle_request_validation(_request: Request, exc: RequestValidationError):
    """422 只返回定位与原因，不回显原始输入或异常上下文。"""
    return http_error_response(422, validation_error_detail(exc.errors()))


@app.exception_handler(ActiveTurnConflict)
async def handle_active_turn_conflict(_request: Request, exc: ActiveTurnConflict):
    return http_error_response(409, exc.as_detail())


@app.exception_handler(TurnMaintenanceConflict)
async def handle_turn_maintenance(_request: Request, exc: TurnMaintenanceConflict):
    return http_error_response(503, exc.as_detail())


@app.exception_handler(DataCorruptionError)
async def handle_data_corruption(_request: Request, exc: DataCorruptionError):
    """以稳定、脱敏的错误契约报告坏档，读取路径不执行隔离写入。"""
    detail = exc.as_detail()
    detail["message"] = "项目数据损坏，请先隔离原件后再恢复"
    detail.pop("reason", None)
    return http_error_response(422, detail)


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception):
    """未分类异常只在服务端记录，响应不暴露路径或异常文本。"""
    logger.error(
        "api_unhandled correlation_id=%s method=%s path=%s exception=%s",
        getattr(request.state, "correlation_id", "unavailable"),
        request.method,
        request.url.path,
        type(exc).__name__,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return error_response(
        500,
        "internal_error",
        "服务内部错误",
        {},
    )


@app.middleware("http")
async def enforce_restore_maintenance(request: Request, call_next):
    """维护期间冻结业务读写，避免响应暴露跨根 mixed 状态。"""
    from core.active_turns import (
        TurnMaintenanceConflict,
        maintenance_operation,
        register_api_read,
        unregister_api_read,
    )

    is_api_read = (
        request.method in {"GET", "HEAD"}
        and request.url.path.startswith("/api/")
    )
    if is_api_read:
        try:
            register_api_read()
        except TurnMaintenanceConflict as exc:
            return error_response(
                503,
                "turn_maintenance",
                "整库维护期间读取已暂停",
                {"operation": exc.operation},
            )
        try:
            return await call_next(request)
        finally:
            unregister_api_read()

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        maintenance = maintenance_operation()
        if maintenance is not None:
            return error_response(
                503,
                "turn_maintenance",
                "整库维护期间写入已暂停",
                {"operation": maintenance},
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
                return error_response(
                    503,
                    "restore_maintenance_check_failed",
                    "整库恢复状态无法验证，写入已暂停",
                    {},
                )
            if pending:
                return error_response(
                    503,
                    "restore_maintenance_required",
                    "存在未完成的整库恢复，写入已暂停",
                    {"restore_ids": pending},
                )
            from core.chat_turns import get_turn_coordinator

            try:
                await get_turn_coordinator().ensure_recovered()
            except Exception:
                logger.exception("中断 turn 恢复失败")
                return error_response(
                    503,
                    "turn_recovery_failed",
                    "中断聊天状态未完成恢复，写入已暂停",
                    {},
                )
    return await call_next(request)


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    return apply_security_headers(response)


@app.middleware("http")
async def bind_request_maintenance_generation(request, call_next):
    """让写事务识别请求进入后跨越的整库恢复边界。"""

    from core.active_turns import request_maintenance_generation_context

    with request_maintenance_generation_context():
        return await call_next(request)


# 静态文件（需在 include_router 之前 mount，避免被路由覆盖）
static.mount_static(app)

# 注册路由
app.include_router(providers.router)
app.include_router(models.router)
app.include_router(projects.router)
app.include_router(characters.router)
app.include_router(user.router)
app.include_router(worldbook.router)
app.include_router(world_state.router)
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
app.include_router(diagnostics.router)
app.include_router(memory.router)
app.include_router(static.router)


if __name__ == "__main__":
    from core.launcher import main as launcher_main

    raise SystemExit(launcher_main(["serve", "--workers", "1"]))
