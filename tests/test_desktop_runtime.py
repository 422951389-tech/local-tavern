from __future__ import annotations

import ast
import base64
import json
import threading
import time
from pathlib import Path
from typing import Any

from desktop.runtime import AsyncioRuntime


ROOT = Path(__file__).resolve().parents[1]


class _RuntimeApp:
    def __init__(self) -> None:
        self.cancel_started = threading.Event()
        self.cancel_finished = threading.Event()

    async def __call__(self, scope, receive, send) -> None:
        assert scope["type"] == "http"
        request = await receive()
        assert request["type"] == "http.request"
        if scope["path"] == "/api/runtime":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({
                "type": "http.response.body",
                "body": json.dumps({"thread": threading.current_thread().name}).encode(),
            })
            return

        if scope["path"] == "/api/runtime/cancel":
            import asyncio

            try:
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                })
                await send({
                    "type": "http.response.body",
                    "body": b"event: started\n\n",
                    "more_body": True,
                })
                self.cancel_started.set()
                await asyncio.Event().wait()
            finally:
                self.cancel_finished.set()
            return

        raise AssertionError(f"unexpected path: {scope['path']}")


class _NeverCompleteLifespanApp:
    def __init__(self, *, complete_startup: bool) -> None:
        self.complete_startup = complete_startup
        self.startup_seen = threading.Event()
        self.shutdown_seen = threading.Event()
        self.cancelled = threading.Event()

    async def __call__(self, scope, receive, send) -> None:
        import asyncio

        assert scope["type"] == "lifespan"
        try:
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    self.startup_seen.set()
                    if self.complete_startup:
                        await send({"type": "lifespan.startup.complete"})
                    else:
                        await asyncio.Event().wait()
                elif event["type"] == "lifespan.shutdown":
                    self.shutdown_seen.set()
                    await asyncio.Event().wait()
        finally:
            self.cancelled.set()


def _wait_for(
    predicate,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("等待桌面运行时状态超时")
        time.sleep(0.005)


def _payload(request_id: str, path: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "method": "GET",
        "path": path,
        "headers": [],
        "body_base64": base64.b64encode(b"").decode("ascii"),
    }


def test_asyncio_runtime_serves_bridge_and_stops_its_only_background_thread():
    app = _RuntimeApp()
    events: list[dict[str, Any]] = []
    event_ready = threading.Event()

    def receive_event(event: dict[str, Any]) -> None:
        events.append(event)
        if event.get("request_id") == "runtime" and event.get("type") == "complete":
            event_ready.set()

    runtime = AsyncioRuntime(app, receive_event, manage_lifespan=False)
    runtime.start(timeout=2)
    try:
        assert runtime.running
        assert runtime.dispatch(_payload("runtime", "/api/runtime"))
        assert event_ready.wait(2)
        idle = runtime.wait_for_idle()
        assert idle is not None
        idle.result(timeout=2)
        assert runtime.active_request_count == 0

        request_events = [event for event in events if event.get("request_id") == "runtime"]
        assert [event["type"] for event in request_events] == [
            "response_start",
            "body",
            "complete",
        ]
        body = json.loads(base64.b64decode(request_events[1]["body_base64"]))
        assert body == {"thread": "local-tavern-asgi"}
    finally:
        runtime.shutdown(timeout=2)

    assert not runtime.running
    assert runtime.active_request_count == 0
    assert not any(
        thread.name == "local-tavern-asgi" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_asyncio_runtime_cancel_and_window_shutdown_leave_no_active_request():
    app = _RuntimeApp()
    events: list[dict[str, Any]] = []
    runtime = AsyncioRuntime(app, events.append, manage_lifespan=False)
    runtime.start(timeout=2)
    try:
        assert runtime.dispatch(_payload("cancel", "/api/runtime/cancel"))
        assert app.cancel_started.wait(2)
        future = runtime.cancel("cancel")
        assert future is not None
        assert future.result(timeout=2)
        assert app.cancel_finished.wait(2)
        _wait_for(lambda: runtime.active_request_count == 0)
    finally:
        runtime.shutdown(timeout=2)

    terminal = [
        event for event in events
        if event.get("request_id") == "cancel"
        and event.get("type") in {"complete", "error"}
    ]
    assert len(terminal) == 1
    assert terminal[0]["type"] == "error"
    assert terminal[0]["code"] == "request_aborted"
    assert not runtime.running
    assert runtime.active_request_count == 0


def test_runtime_external_start_timeout_aborts_and_joins_background_thread():
    app = _NeverCompleteLifespanApp(complete_startup=False)
    runtime = AsyncioRuntime(
        app,
        lifespan_startup_timeout=30,
        lifespan_shutdown_timeout=0.1,
    )

    started = time.monotonic()
    try:
        try:
            runtime.start(timeout=0.03)
        except TimeoutError as exc:
            assert "启动超时" in str(exc)
        else:
            raise AssertionError("永不 complete 的 lifespan 必须触发启动超时")
    finally:
        runtime.shutdown(timeout=0.2)

    assert time.monotonic() - started < 2
    assert app.startup_seen.is_set()
    assert app.cancelled.wait(1)
    assert not runtime.running
    assert not any(
        thread.name == "local-tavern-asgi" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_runtime_internal_lifespan_startup_timeout_stops_thread():
    app = _NeverCompleteLifespanApp(complete_startup=False)
    runtime = AsyncioRuntime(
        app,
        lifespan_startup_timeout=0.03,
        lifespan_shutdown_timeout=0.1,
    )

    try:
        runtime.start(timeout=1)
    except RuntimeError as exc:
        assert "启动失败" in str(exc)
        assert isinstance(exc.__cause__, TimeoutError)
    else:
        raise AssertionError("lifespan 内部超时必须传递到 runtime.start")

    assert app.cancelled.wait(1)
    assert not runtime.running
    assert not any(
        thread.name == "local-tavern-asgi" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_runtime_internal_lifespan_shutdown_timeout_stops_thread():
    app = _NeverCompleteLifespanApp(complete_startup=True)
    runtime = AsyncioRuntime(
        app,
        lifespan_startup_timeout=0.1,
        lifespan_shutdown_timeout=0.03,
    )
    runtime.start(timeout=1)

    try:
        runtime.shutdown(timeout=1)
    except RuntimeError as exc:
        assert "关闭失败" in str(exc)
        assert isinstance(exc.__cause__, TimeoutError)
    else:
        raise AssertionError("lifespan 内部关闭超时必须传递到 runtime.shutdown")

    assert app.shutdown_seen.is_set()
    assert app.cancelled.wait(1)
    assert not runtime.running
    assert not any(
        thread.name == "local-tavern-asgi" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_desktop_runtime_sources_do_not_import_or_create_network_servers():
    prohibited_modules = {
        "gunicorn",
        "http",
        "hypercorn",
        "socket",
        "socketserver",
        "uvicorn",
        "waitress",
    }
    prohibited_calls = {"bind", "create_server", "start_server", "serve"}
    inspected = []
    for path in sorted((ROOT / "desktop").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        inspected.append(path.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(
                    alias.name.split(".", 1)[0] in prohibited_modules
                    for alias in node.names
                ), path.name
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".", 1)[0] not in prohibited_modules, path.name
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in prohibited_calls, (
                    path.name,
                    node.func.attr,
                    node.lineno,
                )
        assert "core.launcher" not in source
    assert {"asgi_bridge.py", "runtime.py"} <= set(inspected)
