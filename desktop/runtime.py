"""Qt 主线程之外的 asyncio/ASGI 生命周期宿主。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Callable
from typing import Any

from .asgi_bridge import AsgiBridge
from .protocol import error_event


logger = logging.getLogger(__name__)


class AsyncioRuntime:
    """在唯一后台线程中运行 ASGI app，不创建网络监听器。"""

    def __init__(
        self,
        app: Any,
        emit_event: Callable[[dict[str, Any]], None] | None = None,
        *,
        manage_lifespan: bool = True,
        trusted_host: str | None = None,
        trusted_origin: str | None = None,
        lifespan_startup_timeout: float = 30.0,
        lifespan_shutdown_timeout: float = 30.0,
        lifespan_cancel_timeout: float = 2.0,
    ) -> None:
        if (
            lifespan_startup_timeout <= 0
            or lifespan_shutdown_timeout <= 0
            or lifespan_cancel_timeout <= 0
        ):
            raise ValueError("ASGI lifespan 超时必须大于 0")
        self._app = app
        self._event_sink = emit_event or (lambda _event: None)
        self._manage_lifespan = manage_lifespan
        self._trusted_host = trusted_host
        self._trusted_origin = trusted_origin
        self._lifespan_startup_timeout = float(lifespan_startup_timeout)
        self._lifespan_shutdown_timeout = float(lifespan_shutdown_timeout)
        self._lifespan_cancel_timeout = float(lifespan_cancel_timeout)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._bridge: AsgiBridge | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._thread_error: BaseException | None = None
        self._startup_succeeded = False
        self._abort_requested = threading.Event()
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive() and self._ready.is_set())

    @property
    def active_request_count(self) -> int:
        bridge = self._bridge
        return bridge.active_request_count if bridge is not None else 0

    def set_event_sink(self, sink: Callable[[dict[str, Any]], None]) -> None:
        if not callable(sink):
            raise TypeError("事件接收端必须可调用")
        with self._lock:
            self._event_sink = sink

    def start(self, *, timeout: float = 30.0) -> None:
        wait_timeout = max(0.1, float(timeout))
        with self._lock:
            if self.running:
                return
            if self._thread is not None:
                raise RuntimeError("桌面运行时不能重复启动")
            self._ready.clear()
            self._stopped.clear()
            self._abort_requested.clear()
            self._thread_error = None
            self._startup_succeeded = False
            self._thread = threading.Thread(
                target=self._thread_main,
                name="local-tavern-asgi",
                daemon=False,
            )
            thread = self._thread
            thread.start()
        if not self._ready.wait(timeout=wait_timeout):
            self._abort_and_join(thread, timeout=self._abort_timeout(wait_timeout))
            raise TimeoutError("桌面 ASGI 运行时启动超时")
        if self._thread_error is not None:
            thread.join(timeout=self._abort_timeout(wait_timeout))
            if thread.is_alive():
                self._abort_and_join(thread, timeout=self._abort_timeout(wait_timeout))
            else:
                self._clear_thread_reference(thread)
            raise RuntimeError("桌面 ASGI 运行时启动失败") from self._thread_error

    def dispatch(self, payload: object) -> bool:
        loop = self._loop
        bridge = self._bridge
        if loop is None or bridge is None or not self.running:
            request_id = payload.get("id", "") if isinstance(payload, dict) else ""
            self._relay_event(
                error_event(
                    request_id if isinstance(request_id, str) else "",
                    "desktop_not_running",
                    "桌面运行时尚未就绪",
                )
            )
            return False
        loop.call_soon_threadsafe(bridge.dispatch, payload)
        return True

    def cancel(self, request_id: str) -> concurrent.futures.Future[bool] | None:
        loop = self._loop
        bridge = self._bridge
        if loop is None or bridge is None or not self.running:
            return None
        return asyncio.run_coroutine_threadsafe(bridge.cancel(request_id), loop)

    def wait_for_idle(self) -> concurrent.futures.Future[None] | None:
        loop = self._loop
        bridge = self._bridge
        if loop is None or bridge is None or not self.running:
            return None
        return asyncio.run_coroutine_threadsafe(bridge.wait_for_idle(), loop)

    def shutdown(self, *, timeout: float = 30.0) -> None:
        wait_timeout = max(0.1, float(timeout))
        with self._lock:
            thread = self._thread
            loop = self._loop
            stop_event = self._stop_event
            if thread is None:
                return
            if loop is not None and stop_event is not None and not loop.is_closed():
                loop.call_soon_threadsafe(stop_event.set)
        if thread is threading.current_thread():
            raise RuntimeError("不能从 ASGI 线程关闭桌面运行时")
        thread.join(timeout=wait_timeout)
        if thread.is_alive():
            self._abort_and_join(thread, timeout=self._abort_timeout(wait_timeout))
            raise TimeoutError("桌面 ASGI 运行时关闭超时，后台线程已中止")
        error = self._thread_error if self._startup_succeeded else None
        self._clear_thread_reference(thread)
        if error is not None:
            raise RuntimeError("桌面 ASGI 运行时关闭失败") from error

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as exc:  # noqa: BLE001 - 跨线程传递启动/关闭失败
            aborted = self._abort_requested.is_set() and isinstance(
                exc,
                asyncio.CancelledError,
            )
            if not aborted:
                self._thread_error = exc
                logger.error(
                    "desktop_runtime_failed exception=%s",
                    type(exc).__name__,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
        finally:
            self._ready.set()
            self._stopped.set()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._run_task = asyncio.current_task()
        self._stop_event = asyncio.Event()
        if self._abort_requested.is_set():
            raise asyncio.CancelledError
        bridge = AsgiBridge(
            self._app,
            self._relay_event,
            manage_lifespan=self._manage_lifespan,
            trusted_host=self._trusted_host,
            trusted_origin=self._trusted_origin,
            lifespan_startup_timeout=self._lifespan_startup_timeout,
            lifespan_shutdown_timeout=self._lifespan_shutdown_timeout,
            lifespan_cancel_timeout=self._lifespan_cancel_timeout,
        )
        self._bridge = bridge
        try:
            await bridge.startup()
            self._startup_succeeded = True
            self._ready.set()
            await self._stop_event.wait()
        finally:
            try:
                await bridge.shutdown()
            finally:
                self._bridge = None
                self._stop_event = None
                self._run_task = None
                self._loop = None

    def _abort_timeout(self, requested: float) -> float:
        return max(
            self._lifespan_cancel_timeout + 0.5,
            1.0,
            min(5.0, requested),
        )

    def _abort_and_join(self, thread: threading.Thread, *, timeout: float) -> None:
        self._abort_requested.set()
        loop = self._loop
        run_task = self._run_task
        if loop is not None and run_task is not None and not loop.is_closed():
            def cancel_runner() -> None:
                if not run_task.done():
                    run_task.cancel()

            loop.call_soon_threadsafe(cancel_runner)
        thread.join(timeout=max(0.1, timeout))
        if thread.is_alive():
            raise TimeoutError("桌面 ASGI 运行时中止失败，后台线程仍在运行")
        self._clear_thread_reference(thread)

    def _clear_thread_reference(self, thread: threading.Thread) -> None:
        with self._lock:
            if self._thread is thread:
                self._thread = None

    def _relay_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            sink = self._event_sink
        try:
            sink(event)
        except Exception:  # noqa: BLE001 - Qt 退出时接收端会先销毁
            logger.exception("desktop_runtime_event_failed type=%s", event.get("type"))
