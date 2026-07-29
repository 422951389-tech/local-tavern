"""不经 socket 调用现有 FastAPI ASGI 应用。"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from .protocol import (
    BridgeProtocolError,
    BridgeRequest,
    body_event,
    complete_event,
    error_event,
    response_start_event,
)


logger = logging.getLogger(__name__)
DEFAULT_LIFESPAN_STARTUP_TIMEOUT = 30.0
DEFAULT_LIFESPAN_SHUTDOWN_TIMEOUT = 30.0
DEFAULT_LIFESPAN_CANCEL_TIMEOUT = 2.0

_DROP_REQUEST_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "origin",
        "proxy-authorization",
        "proxy-connection",
        "referer",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


@dataclass(slots=True)
class _ResponseState:
    started: bool = False
    completed: bool = False
    terminal_sent: bool = False


class _LifespanSession:
    """最小 ASGI lifespan 驱动器，避免引入测试专用依赖。"""

    def __init__(
        self,
        app: Any,
        *,
        startup_timeout: float = DEFAULT_LIFESPAN_STARTUP_TIMEOUT,
        shutdown_timeout: float = DEFAULT_LIFESPAN_SHUTDOWN_TIMEOUT,
        cancel_timeout: float = DEFAULT_LIFESPAN_CANCEL_TIMEOUT,
    ) -> None:
        if startup_timeout <= 0 or shutdown_timeout <= 0 or cancel_timeout <= 0:
            raise ValueError("ASGI lifespan 超时必须大于 0")
        self._app = app
        self._startup_timeout = float(startup_timeout)
        self._shutdown_timeout = float(shutdown_timeout)
        self._cancel_timeout = float(cancel_timeout)
        self._receive_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._startup: asyncio.Future[None] | None = None
        self._shutdown: asyncio.Future[None] | None = None
        self._task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        if self._task is not None:
            return
        loop = asyncio.get_running_loop()
        self._startup = loop.create_future()
        self._shutdown = loop.create_future()

        async def receive() -> dict[str, Any]:
            return await self._receive_queue.get()

        async def send(message: dict[str, Any]) -> None:
            message_type = message.get("type")
            if message_type == "lifespan.startup.complete":
                if self._startup is not None and not self._startup.done():
                    self._startup.set_result(None)
            elif message_type == "lifespan.startup.failed":
                detail = str(message.get("message") or "ASGI lifespan startup 失败")
                if self._startup is not None and not self._startup.done():
                    self._startup.set_exception(RuntimeError(detail))
            elif message_type == "lifespan.shutdown.complete":
                if self._shutdown is not None and not self._shutdown.done():
                    self._shutdown.set_result(None)
            elif message_type == "lifespan.shutdown.failed":
                detail = str(message.get("message") or "ASGI lifespan shutdown 失败")
                if self._shutdown is not None and not self._shutdown.done():
                    self._shutdown.set_exception(RuntimeError(detail))

        async def run() -> None:
            try:
                await self._app(
                    {
                        "type": "lifespan",
                        "asgi": {"version": "3.0", "spec_version": "2.0"},
                        "state": {},
                    },
                    receive,
                    send,
                )
            except asyncio.CancelledError:
                for future in (self._startup, self._shutdown):
                    if future is not None and not future.done():
                        future.cancel()
                raise
            except BaseException as exc:
                for future in (self._startup, self._shutdown):
                    if future is not None and not future.done():
                        future.set_exception(exc)
                raise

        self._task = asyncio.create_task(run(), name="desktop-asgi-lifespan")
        await self._receive_queue.put({"type": "lifespan.startup"})
        try:
            async with asyncio.timeout(self._startup_timeout):
                await self._startup
        except TimeoutError as exc:
            await self._stop_failed_task()
            raise TimeoutError("ASGI lifespan startup 超时") from exc
        except BaseException:
            await self._stop_failed_task()
            raise

    async def shutdown(self) -> None:
        task = self._task
        if task is None:
            return
        try:
            async with asyncio.timeout(self._shutdown_timeout):
                if not task.done():
                    await self._receive_queue.put({"type": "lifespan.shutdown"})
                    if self._shutdown is not None:
                        await self._shutdown
                await asyncio.gather(task, return_exceptions=False)
        except TimeoutError as exc:
            await self._stop_failed_task()
            raise TimeoutError("ASGI lifespan shutdown 超时") from exc
        except BaseException:
            await self._stop_failed_task()
            raise
        else:
            self._task = None

    async def _stop_failed_task(self) -> None:
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            async with asyncio.timeout(self._cancel_timeout):
                await asyncio.gather(task, return_exceptions=True)
        except TimeoutError as exc:
            logger.error("desktop_lifespan_cancel_timeout")
            raise TimeoutError("ASGI lifespan 取消超时") from exc
        finally:
            for future in (self._startup, self._shutdown):
                if future is not None and not future.done():
                    future.cancel()
        self._task = None


class AsgiBridge:
    """在一个 asyncio loop 内管理 ASGI 生命周期和并发请求。

    ``emit_event`` 接收协议字典。调用方负责把它投递至 Qt 主线程；本类不依赖 Qt。
    """

    def __init__(
        self,
        app: Any,
        emit_event: Callable[[dict[str, Any]], None],
        *,
        manage_lifespan: bool = True,
        trusted_host: str | None = None,
        trusted_origin: str | None = None,
        lifespan_startup_timeout: float = DEFAULT_LIFESPAN_STARTUP_TIMEOUT,
        lifespan_shutdown_timeout: float = DEFAULT_LIFESPAN_SHUTDOWN_TIMEOUT,
        lifespan_cancel_timeout: float = DEFAULT_LIFESPAN_CANCEL_TIMEOUT,
    ) -> None:
        if not callable(app):
            raise TypeError("ASGI app 必须可调用")
        if not callable(emit_event):
            raise TypeError("emit_event 必须可调用")
        self._app = app
        self._event_sink = emit_event
        self._manage_lifespan = manage_lifespan
        self._trusted_host = trusted_host or _default_trusted_host()
        self._trusted_origin = (
            trusted_origin or f"http://{self._trusted_host}"
        ).rstrip("/")
        self._lifespan = (
            _LifespanSession(
                app,
                startup_timeout=lifespan_startup_timeout,
                shutdown_timeout=lifespan_shutdown_timeout,
                cancel_timeout=lifespan_cancel_timeout,
            )
            if manage_lifespan
            else None
        )
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._disconnects: dict[str, asyncio.Event] = {}
        self._started = False
        self._closing = False

    @property
    def active_request_count(self) -> int:
        return len(self._tasks)

    @property
    def started(self) -> bool:
        return self._started

    async def startup(self) -> None:
        if self._started:
            return
        if self._closing:
            raise RuntimeError("ASGI 桥正在关闭")
        if self._lifespan is not None:
            await self._lifespan.startup()
        self._started = True

    def dispatch(self, payload: object) -> bool:
        """校验并调度一个请求；必须在所属 asyncio loop 调用。"""
        try:
            request = BridgeRequest.from_payload(payload)
        except BridgeProtocolError as exc:
            self._emit(
                error_event(
                    exc.request_id,
                    exc.code,
                    exc.message,
                    status=exc.status,
                )
            )
            return False

        if not self._started or self._closing:
            self._emit(
                error_event(
                    request.request_id,
                    "desktop_not_running",
                    "桌面运行时尚未就绪或正在关闭",
                )
            )
            return False
        if request.request_id in self._tasks:
            self._emit(
                error_event(
                    request.request_id,
                    "duplicate_request_id",
                    "桌面请求 ID 正在使用",
                )
            )
            return False

        disconnect = asyncio.Event()
        task = asyncio.create_task(
            self._serve(request, disconnect),
            name=f"desktop-asgi-{request.request_id}",
        )
        self._tasks[request.request_id] = task
        self._disconnects[request.request_id] = disconnect
        task.add_done_callback(
            lambda completed, request_id=request.request_id: self._request_done(
                request_id, completed
            )
        )
        return True

    async def cancel(self, request_id: str) -> bool:
        task = self._tasks.get(request_id)
        if task is None:
            return False
        disconnect = self._disconnects.get(request_id)
        if disconnect is not None:
            disconnect.set()
        await asyncio.sleep(0)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def wait_for_idle(self) -> None:
        while self._tasks:
            tasks = tuple(self._tasks.values())
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)

    async def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            for disconnect in tuple(self._disconnects.values()):
                disconnect.set()
            tasks = tuple(self._tasks.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.clear()
            self._disconnects.clear()
            if self._lifespan is not None and self._started:
                await self._lifespan.shutdown()
        finally:
            self._started = False

    async def _serve(self, request: BridgeRequest, disconnect: asyncio.Event) -> None:
        response = _ResponseState()
        split = urlsplit(request.path)
        request_sent = False

        async def receive() -> dict[str, Any]:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {
                    "type": "http.request",
                    "body": request.body,
                    "more_body": False,
                }
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            message_type = message.get("type")
            if message_type == "http.response.start":
                if response.started:
                    raise RuntimeError("ASGI 响应重复开始")
                status = int(message.get("status", 500))
                headers = self._decode_response_headers(message.get("headers", ()))
                response.started = True
                self._emit(response_start_event(request.request_id, status, headers))
                return
            if message_type == "http.response.body":
                if not response.started:
                    raise RuntimeError("ASGI 响应体早于响应头")
                body = message.get("body", b"")
                if not isinstance(body, bytes):
                    raise RuntimeError("ASGI 响应体必须是 bytes")
                if body:
                    self._emit(body_event(request.request_id, body))
                if not bool(message.get("more_body", False)) and not response.completed:
                    response.completed = True
                    response.terminal_sent = True
                    self._emit(complete_event(request.request_id))
                return
            raise RuntimeError("ASGI 应用发送了不受支持的 HTTP 消息")

        scope = self._build_scope(request, split.path, split.query)
        try:
            await self._app(scope, receive, send)
            if not response.started:
                raise RuntimeError("ASGI 应用未开始响应")
            if not response.completed:
                response.completed = True
                response.terminal_sent = True
                self._emit(complete_event(request.request_id))
        except asyncio.CancelledError:
            if not response.terminal_sent:
                response.terminal_sent = True
                self._emit(
                    error_event(
                        request.request_id,
                        "request_aborted",
                        "桌面请求已取消",
                    )
                )
        except Exception as exc:  # noqa: BLE001 - ASGI 边界统一脱敏
            logger.error(
                "desktop_asgi_request_failed method=%s path=%s exception=%s",
                request.method,
                split.path,
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            if not response.terminal_sent:
                response.terminal_sent = True
                self._emit(
                    error_event(
                        request.request_id,
                        "desktop_request_failed",
                        "桌面内部请求失败",
                    )
                )
        finally:
            disconnect.set()

    def _build_scope(
        self,
        request: BridgeRequest,
        raw_path: str,
        query: str,
    ) -> dict[str, Any]:
        incoming: list[tuple[bytes, bytes]] = []
        for name, value in request.headers:
            if name in _DROP_REQUEST_HEADERS or name.startswith("proxy-"):
                continue
            incoming.append((name.encode("ascii"), value.encode("latin-1")))
        incoming.extend(
            [
                (b"host", self._trusted_host.encode("ascii")),
                (b"origin", self._trusted_origin.encode("ascii")),
                (b"referer", f"{self._trusted_origin}/".encode("ascii")),
                (b"content-length", str(len(request.body)).encode("ascii")),
            ]
        )
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": request.method,
            "scheme": "http",
            "path": unquote(raw_path, errors="strict"),
            "raw_path": raw_path.encode("utf-8", "surrogatepass"),
            "query_string": query.encode("ascii", "surrogateescape"),
            "root_path": "",
            "headers": incoming,
            "client": ("127.0.0.1", 0),
            "server": ("127.0.0.1", int(self._trusted_host.rsplit(":", 1)[1])),
            "state": {},
        }

    @staticmethod
    def _decode_response_headers(raw_headers: object) -> list[tuple[str, str]]:
        if not isinstance(raw_headers, (list, tuple)):
            raise RuntimeError("ASGI 响应头格式无效")
        result: list[tuple[str, str]] = []
        for item in raw_headers:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise RuntimeError("ASGI 响应头条目无效")
            name, value = item
            if not isinstance(name, bytes) or not isinstance(value, bytes):
                raise RuntimeError("ASGI 响应头必须为 bytes")
            result.append((name.decode("latin-1"), value.decode("latin-1")))
        return result

    def _request_done(self, request_id: str, task: asyncio.Task[None]) -> None:
        self._tasks.pop(request_id, None)
        self._disconnects.pop(request_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:  # _serve 自身已收口；这里只防回调泄漏
            logger.exception("desktop_asgi_task_unhandled request_id=%s", request_id)

    def _emit(self, event: dict[str, Any]) -> None:
        try:
            self._event_sink(event)
        except Exception:  # noqa: BLE001 - 页面关闭时事件接收端可先消失
            logger.exception("desktop_event_sink_failed type=%s", event.get("type"))


def _default_trusted_host() -> str:
    raw = os.environ.get("TAVERN_PORT", "").strip()
    try:
        port = int(raw) if raw else 8765
    except ValueError:
        port = 8765
    if not 1 <= port <= 65535:
        port = 8765
    return f"127.0.0.1:{port}"
