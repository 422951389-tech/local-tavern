"""无需 Qt 的桌面核心自检：协议、分块、取消、lifespan、线程和资源边界。"""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import threading
from pathlib import Path
from typing import Any

from .asgi_bridge import AsgiBridge
from .resources import ResourceError, read_asset, resolve_web_asset, resolve_web_root
from .runtime import AsyncioRuntime


class _FakeAsgiApp:
    def __init__(self) -> None:
        self.startups = 0
        self.shutdowns = 0
        self.disconnects = 0

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    self.startups += 1
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    self.shutdowns += 1
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return

        request = await receive()
        assert request["type"] == "http.request"
        assert dict(scope["headers"])[b"host"] == b"127.0.0.1:8765"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        if scope["path"] == "/api/ping":
            await send(
                {
                    "type": "http.response.body",
                    "body": json.dumps({"ok": True}).encode("utf-8"),
                    "more_body": False,
                }
            )
            return
        if scope["path"] == "/api/chunks":
            for chunk in (b"one", b"two"):
                await send(
                    {
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": True,
                    }
                )
                await asyncio.sleep(0)
            disconnected = await receive()
            assert disconnected["type"] == "http.disconnect"
            self.disconnects += 1
            return
        await send({"type": "http.response.body", "body": b"", "more_body": False})


def _request(request_id: str, path: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "method": "GET",
        "path": path,
        "headers": [["accept", "application/json"]],
        "body_base64": "",
    }


async def _check_bridge() -> dict[str, int]:
    app = _FakeAsgiApp()
    events: list[dict[str, Any]] = []
    bridge = AsgiBridge(app, events.append)
    await bridge.startup()
    assert bridge.dispatch(_request("normal", "/api/ping"))
    await bridge.wait_for_idle()
    normal = [event for event in events if event["request_id"] == "normal"]
    assert [event["type"] for event in normal] == ["response_start", "body", "complete"]
    body = base64.b64decode(normal[1]["body_base64"])
    assert json.loads(body) == {"ok": True}

    assert bridge.dispatch(_request("stream", "/api/chunks"))
    for _ in range(100):
        if len([event for event in events if event["request_id"] == "stream"]) >= 3:
            break
        await asyncio.sleep(0.001)
    assert await bridge.cancel("stream")
    await bridge.wait_for_idle()
    stream = [event for event in events if event["request_id"] == "stream"]
    assert [event["type"] for event in stream[:3]] == ["response_start", "body", "body"]
    assert stream[-1]["type"] in {"complete", "error"}

    assert not bridge.dispatch(_request("bad", "https://example.com/api"))
    assert events[-1]["type"] == "error"
    await bridge.shutdown()
    assert bridge.active_request_count == 0
    assert app.startups == 1 and app.shutdowns == 1
    return {"events": len(events), "disconnects": app.disconnects}


def _check_runtime() -> dict[str, int]:
    app = _FakeAsgiApp()
    events: list[dict[str, Any]] = []
    lock = threading.Lock()

    def collect(event: dict[str, Any]) -> None:
        with lock:
            events.append(event)

    runtime = AsyncioRuntime(app, collect)
    runtime.start(timeout=5)
    assert runtime.dispatch(_request("threaded", "/api/ping"))
    idle = runtime.wait_for_idle()
    assert idle is not None
    idle.result(timeout=5)
    runtime.shutdown(timeout=5)
    assert not runtime.running
    with lock:
        terminal = [event for event in events if event["request_id"] == "threaded"]
    assert terminal[-1]["type"] == "complete"
    assert app.startups == 1 and app.shutdowns == 1
    return {"events": len(events)}


def _check_resources() -> dict[str, int]:
    with tempfile.TemporaryDirectory(prefix="tavern-desktop-selfcheck-") as temporary:
        root = Path(temporary)
        web = root / "web"
        web.mkdir()
        (web / "index.html").write_text(
            '<!doctype html><html><head><meta charset="UTF-8"></head><body></body></html>',
            encoding="utf-8",
        )
        (web / "app.mjs").write_text("export {};", encoding="utf-8")
        assert resolve_web_root(root) == web.resolve()
        assert resolve_web_asset("/", web).name == "index.html"
        assert resolve_web_asset("/static/app.mjs", web).name == "app.mjs"
        injected = read_asset(web / "index.html").decode("utf-8")
        for marker in ("Content-Security-Policy", "nosniff", "no-store"):
            assert marker in injected
        rejected = 0
        for candidate in (
            "/static/../index.html",
            "/static/%2e%2e/index.html",
            "/other/file.js",
            "https://example.com/static/app.mjs",
        ):
            try:
                resolve_web_asset(candidate, web)
            except ResourceError:
                rejected += 1
        assert rejected == 4
        return {"rejected_paths": rejected}


def main() -> int:
    result = {
        "bridge": asyncio.run(_check_bridge()),
        "runtime": _check_runtime(),
        "resources": _check_resources(),
        "status": "passed",
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
