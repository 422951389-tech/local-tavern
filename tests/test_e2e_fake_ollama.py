from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from tests.e2e_fake_ollama import (
    Control,
    E2E_NORMAL_REPLY,
    MODEL_NAME,
    create_server,
    read_control,
    stream_chunks,
)
from tests.fakes.fake_ollama import SUMMARY_REPLY


def _request(url: str, *, body: dict | None = None) -> tuple[int, bytes]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urlopen(request, timeout=2) as response:  # noqa: S310 - 回环测试服务
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def test_control_file_is_bounded_and_invalid_values_fall_back(tmp_path: Path):
    control_file = tmp_path / "control.json"
    assert read_control(control_file) == Control()
    control_file.write_text(json.dumps({
        "scenario": "slow",
        "delay_ms": 8_000,
        "hold_ms": -10,
    }), encoding="utf-8")
    assert read_control(control_file) == Control(
        scenario="slow",
        delay_ms=5_000,
        hold_ms=0,
    )
    control_file.write_text("[]", encoding="utf-8")
    assert read_control(control_file) == Control()


def test_stream_chunks_use_the_authoritative_affinity_fixture_text():
    normal = list(stream_chunks(Control()))
    assert normal[-1]["done"] is True
    assert normal[1]["message"]["content"] == E2E_NORMAL_REPLY
    slow = list(stream_chunks(Control(scenario="slow")))
    assert len(slow) == 2
    assert slow[-1]["done"] is False
    assert E2E_NORMAL_REPLY.startswith(slow[-1]["message"]["content"])


def test_http_server_serves_models_metadata_stream_summary_and_error(tmp_path: Path):
    control_file = tmp_path / "control.json"
    server = create_server("127.0.0.1", 0, control_file)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, raw = _request(f"{base}/api/tags")
        assert status == 200
        assert json.loads(raw)["models"][0]["name"] == MODEL_NAME

        status, raw = _request(f"{base}/api/show", body={"model": MODEL_NAME})
        assert status == 200
        assert json.loads(raw)["model_info"]["fake.context_length"] == 32_768

        status, raw = _request(f"{base}/api/chat", body={"stream": True})
        assert status == 200
        chunks = [json.loads(line) for line in raw.splitlines()]
        assert chunks[-1]["done"] is True
        assert chunks[1]["message"]["content"] == E2E_NORMAL_REPLY

        status, raw = _request(f"{base}/api/chat", body={"stream": False})
        assert status == 200
        assert json.loads(raw)["message"]["content"] == SUMMARY_REPLY

        control_file.write_text('{"scenario":"error"}', encoding="utf-8")
        status, raw = _request(f"{base}/api/chat", body={"stream": True})
        assert status == 503
        assert "上游错误" in json.loads(raw)["error"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_server_rejects_non_loopback_binding(tmp_path: Path):
    with pytest.raises(ValueError, match="回环"):
        create_server("0.0.0.0", 0, tmp_path / "control.json")
