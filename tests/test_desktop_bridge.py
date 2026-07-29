from __future__ import annotations

import asyncio
import base64
import inspect
import json
import re
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from desktop.asgi_bridge import AsgiBridge


ROOT = Path(__file__).resolve().parents[1]


def _body(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _event(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    assert isinstance(value, dict)
    return value


async def _call(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 2.0,
) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


def test_qt_bridge_uses_bridge_event_signal_without_overriding_qobject_event():
    source = (ROOT / "desktop" / "qt_bridge.py").read_text(encoding="utf-8")
    assert "bridgeEvent = Signal(str)" in source
    assert "self.bridgeEvent.emit(payload)" in source
    assert re.search(r"^\s*event\s*=\s*Signal\(", source, re.MULTILINE) is None


class _ProtocolApp:
    def __init__(self) -> None:
        self.startups = 0
        self.shutdowns = 0
        self.requests: list[dict[str, Any]] = []
        self.release_stream = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    self.startups += 1
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    self.shutdowns += 1
                    await send({"type": "lifespan.shutdown.complete"})
                    return

        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        self.requests.append({"scope": scope, "body": b"".join(chunks)})

        if scope["path"] == "/api/echo":
            payload = json.dumps(
                {
                    "method": scope["method"],
                    "query": scope["query_string"].decode("ascii"),
                    "body": b"".join(chunks).decode("utf-8"),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 201,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"x-asgi-bridge", b"desktop"),
                ],
            })
            await send({"type": "http.response.body", "body": payload})
            return

        if scope["path"] == "/api/stream":
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            })
            await send({
                "type": "http.response.body",
                "body": b"event: content\ndata: one\n\n",
                "more_body": True,
            })
            await self.release_stream.wait()
            await send({
                "type": "http.response.body",
                "body": b"event: terminal\ndata: completed\n\n",
                "more_body": False,
            })
            return

        if scope["path"] == "/api/cancel":
            try:
                await send({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                })
                await send({
                    "type": "http.response.body",
                    "body": b"event: content\ndata: started\n\n",
                    "more_body": True,
                })
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

        raise AssertionError(f"unexpected path: {scope['path']}")


class _NeverCompleteLifespanApp:
    def __init__(self, *, complete_startup: bool) -> None:
        self.complete_startup = complete_startup
        self.startup_seen = asyncio.Event()
        self.shutdown_seen = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def __call__(self, scope, receive, send) -> None:
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


@pytest.fixture
async def bridge_fixture():
    app = _ProtocolApp()
    events: list[dict[str, Any]] = []
    bridge = AsgiBridge(app, lambda value: events.append(_event(value)))
    await _call(bridge.startup())
    try:
        yield app, bridge, events
    finally:
        await _call(bridge.shutdown())


@pytest.mark.asyncio
async def test_desktop_bridge_serves_ordinary_asgi_request_without_tcp(
    monkeypatch,
    bridge_fixture,
):
    app, bridge, events = bridge_fixture

    def reject_bind(*_args, **_kwargs):
        raise AssertionError("桌面桥不得绑定 TCP 端口")

    with monkeypatch.context() as local_patch:
        local_patch.setattr(socket.socket, "bind", reject_bind)
        accepted = await _call(bridge.dispatch({
            "id": "ordinary",
            "method": "POST",
            "path": "/api/echo?source=desktop",
            "headers": [
                ["content-type", "application/json"],
                ["x-request-tag", "acceptance"],
            ],
            "body_base64": _body('{"text":"桌面"}'.encode("utf-8")),
        }))
    assert accepted is not False
    await _wait_until(lambda: any(
        event.get("request_id") == "ordinary" and event.get("type") == "complete"
        for event in events
    ))

    ordinary = [event for event in events if event.get("request_id") == "ordinary"]
    assert [event["type"] for event in ordinary] == [
        "response_start",
        "body",
        "complete",
    ]
    assert ordinary[0]["status"] == 201
    assert ["x-asgi-bridge", "desktop"] in ordinary[0]["headers"]
    response_body = base64.b64decode(ordinary[1]["body_base64"])
    assert json.loads(response_body) == {
        "method": "POST",
        "query": "source=desktop",
        "body": '{"text":"桌面"}',
    }
    request = app.requests[-1]
    assert request["scope"]["type"] == "http"
    assert request["scope"]["scheme"] == "http"
    assert request["scope"]["path"] == "/api/echo"
    assert request["body"] == '{"text":"桌面"}'.encode("utf-8")


@pytest.mark.asyncio
async def test_desktop_bridge_preserves_stream_chunks_and_terminal_order(
    bridge_fixture,
):
    app, bridge, events = bridge_fixture
    await _call(bridge.dispatch({
        "id": "stream",
        "method": "GET",
        "path": "/api/stream",
        "headers": [],
        "body_base64": "",
    }))
    await _wait_until(lambda: len([
        event for event in events
        if event.get("request_id") == "stream" and event.get("type") == "body"
    ]) == 1)
    assert not any(
        event.get("request_id") == "stream" and event.get("type") == "complete"
        for event in events
    )

    app.release_stream.set()
    await _wait_until(lambda: any(
        event.get("request_id") == "stream" and event.get("type") == "complete"
        for event in events
    ))
    stream = [event for event in events if event.get("request_id") == "stream"]
    assert [event["type"] for event in stream] == [
        "response_start",
        "body",
        "body",
        "complete",
    ]
    assert [base64.b64decode(event["body_base64"]) for event in stream[1:3]] == [
        b"event: content\ndata: one\n\n",
        b"event: terminal\ndata: completed\n\n",
    ]


@pytest.mark.asyncio
async def test_desktop_bridge_cancel_emits_one_error_terminal_and_releases_task(
    bridge_fixture,
):
    app, bridge, events = bridge_fixture
    await _call(bridge.dispatch({
        "id": "cancel-me",
        "method": "GET",
        "path": "/api/cancel",
        "headers": [],
        "body_base64": "",
    }))
    await _wait_until(lambda: any(
        event.get("request_id") == "cancel-me" and event.get("type") == "body"
        for event in events
    ))

    cancelled = await _call(bridge.cancel("cancel-me"))
    assert cancelled is not False
    await _wait_until(app.cancelled.is_set)
    await _wait_until(lambda: bridge.active_request_count == 0)

    terminal = [
        event for event in events
        if event.get("request_id") == "cancel-me"
        and event.get("type") in {"complete", "error"}
    ]
    assert len(terminal) == 1
    assert terminal[0]["type"] == "error"
    assert terminal[0]["code"] == "request_aborted"


@pytest.mark.asyncio
async def test_desktop_bridge_shutdown_cancels_requests_and_closes_lifespan():
    app = _ProtocolApp()
    events: list[dict[str, Any]] = []
    bridge = AsgiBridge(app, lambda value: events.append(_event(value)))
    await _call(bridge.startup())
    assert app.startups == 1

    await _call(bridge.dispatch({
        "id": "shutdown-active",
        "method": "GET",
        "path": "/api/cancel",
        "headers": [],
        "body_base64": "",
    }))
    await _wait_until(lambda: any(
        event.get("request_id") == "shutdown-active"
        and event.get("type") == "body"
        for event in events
    ))
    await _call(bridge.shutdown())

    assert app.cancelled.is_set()
    assert bridge.active_request_count == 0
    assert app.shutdowns == 1
    terminal = [
        event for event in events
        if event.get("request_id") == "shutdown-active"
        and event.get("type") in {"complete", "error"}
    ]
    assert len(terminal) == 1
    assert terminal[0]["type"] == "error"


@pytest.mark.asyncio
async def test_lifespan_startup_timeout_cancels_never_completing_app_task():
    app = _NeverCompleteLifespanApp(complete_startup=False)
    bridge = AsgiBridge(
        app,
        lambda _event: None,
        lifespan_startup_timeout=0.02,
        lifespan_shutdown_timeout=0.02,
    )

    with pytest.raises(TimeoutError, match="lifespan startup"):
        await bridge.startup()

    assert app.startup_seen.is_set()
    assert app.cancelled.is_set()
    assert bridge.started is False
    assert not any(
        task.get_name() == "desktop-asgi-lifespan" and not task.done()
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_lifespan_shutdown_timeout_cancels_never_completing_app_task():
    app = _NeverCompleteLifespanApp(complete_startup=True)
    bridge = AsgiBridge(
        app,
        lambda _event: None,
        lifespan_startup_timeout=0.1,
        lifespan_shutdown_timeout=0.02,
    )
    await bridge.startup()

    with pytest.raises(TimeoutError, match="lifespan shutdown"):
        await bridge.shutdown()

    assert app.shutdown_seen.is_set()
    assert app.cancelled.is_set()
    assert bridge.started is False
    assert not any(
        task.get_name() == "desktop-asgi-lifespan" and not task.done()
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_desktop_bridge_rejects_duplicate_ids_and_invalid_external_paths(
    bridge_fixture,
):
    _app, bridge, _events = bridge_fixture
    payload = {
        "id": "duplicate",
        "method": "GET",
        "path": "/api/cancel",
        "headers": [],
        "body_base64": "",
    }
    assert await _call(bridge.dispatch(payload)) is not False
    assert await _call(bridge.dispatch(payload)) is False
    assert await _call(bridge.cancel("duplicate")) is not False
    await _wait_until(lambda: bridge.active_request_count == 0)

    for index, path in enumerate((
        "https://example.com/api",
        "//example.com/api",
        "file:///C:/secret.txt",
        "/../secret.txt",
    )):
        rejected = await _call(bridge.dispatch({
            "id": f"invalid-path-{index}",
            "method": "GET",
            "path": path,
            "headers": [],
            "body_base64": "",
        }))
        assert rejected is False


@pytest.mark.asyncio
async def test_default_desktop_bridge_reaches_real_app_health_without_http_server():
    import server

    events: list[dict[str, Any]] = []
    bridge = AsgiBridge(server.app, events.append, manage_lifespan=False)
    await bridge.startup()
    try:
        assert bridge.dispatch({
            "id": "real-health",
            "method": "GET",
            "path": "/health/live",
            "headers": [],
            "body_base64": "",
        })
        await _wait_until(lambda: any(
            event.get("request_id") == "real-health"
            and event.get("type") == "complete"
            for event in events
        ))
    finally:
        await bridge.shutdown()

    response = [event for event in events if event.get("request_id") == "real-health"]
    assert [event["type"] for event in response] == [
        "response_start",
        "body",
        "complete",
    ]
    assert response[0]["status"] == 200
    assert json.loads(base64.b64decode(response[1]["body_base64"])) == {
        "status": "alive",
        "service": "local-tavern",
        "contract_version": 1,
    }
