"""仅用于端到端测试的回环 Ollama HTTP 替身。"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

from tests.fakes.fake_ollama import NORMAL_REPLY, SUMMARY_REPLY


MODEL_NAME = "fake-model:latest"
DEFAULT_CONTROL = {
    "scenario": "normal",
    "delay_ms": 0,
    "hold_ms": 30_000,
}
SCENARIOS = frozenset({"normal", "slow", "error", "eof"})


@dataclass(frozen=True)
class Control:
    scenario: str = "normal"
    delay_ms: int = 0
    hold_ms: int = 30_000


def _bounded_integer(value: object, *, default: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(max(value, 0), maximum)


def read_control(path: Path) -> Control:
    """读取每次请求的场景；缺失或损坏文件回落到可重现默认值。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    scenario = raw.get("scenario", DEFAULT_CONTROL["scenario"])
    if scenario not in SCENARIOS:
        scenario = DEFAULT_CONTROL["scenario"]
    return Control(
        scenario=scenario,
        delay_ms=_bounded_integer(
            raw.get("delay_ms"),
            default=DEFAULT_CONTROL["delay_ms"],
            maximum=5_000,
        ),
        hold_ms=_bounded_integer(
            raw.get("hold_ms"),
            default=DEFAULT_CONTROL["hold_ms"],
            maximum=120_000,
        ),
    )


def stream_chunks(control: Control) -> Iterator[dict]:
    """生成 Ollama NDJSON 块，内容只复用现有 fake 响应。"""
    if control.scenario == "slow":
        yield {
            "message": {"role": "assistant", "thinking": "测试思考", "content": ""},
            "done": False,
        }
        yield {
            "message": {"role": "assistant", "thinking": "", "content": NORMAL_REPLY[:32]},
            "done": False,
        }
        return
    if control.scenario == "eof":
        yield {
            "message": {"role": "assistant", "thinking": "", "content": NORMAL_REPLY[:32]},
            "done": False,
        }
        return
    yield {
        "message": {"role": "assistant", "thinking": "测试思考", "content": ""},
        "done": False,
    }
    yield {
        "message": {"role": "assistant", "thinking": "", "content": NORMAL_REPLY},
        "done": False,
    }
    yield {"message": {"role": "assistant", "thinking": "", "content": ""}, "done": True}


class FakeOllamaServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, server_address: tuple[str, int], control_file: Path):
        self.control_file = control_file
        super().__init__(server_address, FakeOllamaHandler)


class FakeOllamaHandler(BaseHTTPRequestHandler):
    server: FakeOllamaServer

    def log_message(self, _format: str, *args: object) -> None:
        del args

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _json(self, status: int, value: object) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/healthz":
            self._json(200, {"ok": True})
            return
        if self.path == "/api/tags":
            self._json(200, {"models": [{"name": MODEL_NAME}]})
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        body = self._read_json()
        if self.path == "/api/show":
            self._json(200, {
                "model_info": {
                    "general.architecture": "fake",
                    "fake.context_length": 32_768,
                },
                "parameters": "num_ctx 32768\n",
            })
            return
        if self.path != "/api/chat":
            self._json(404, {"error": "not_found"})
            return

        control = read_control(self.server.control_file)
        if control.scenario == "error":
            self._json(503, {"error": "fake Ollama 上游错误"})
            return
        if body.get("stream") is False:
            self._json(200, {
                "message": {"role": "assistant", "content": SUMMARY_REPLY},
                "done": True,
            })
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for chunk in stream_chunks(control):
                self.wfile.write(json.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\n")
                self.wfile.flush()
                if control.delay_ms:
                    time.sleep(control.delay_ms / 1_000)
            if control.scenario == "slow" and control.hold_ms:
                time.sleep(control.hold_ms / 1_000)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def create_server(host: str, port: int, control_file: Path) -> FakeOllamaServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("fake Ollama 只允许绑定回环地址")
    return FakeOllamaServer((host, port), control_file.resolve())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动回环 fake Ollama 测试服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--control-file", type=Path, required=True)
    args = parser.parse_args(argv)
    server = create_server(args.host, args.port, args.control_file)
    print(json.dumps({
        "ready": True,
        "host": args.host,
        "port": server.server_address[1],
        "model": MODEL_NAME,
    }, ensure_ascii=False), flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
