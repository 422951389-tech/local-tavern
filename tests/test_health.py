from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from core import health
from core.ollama_client import OllamaClient


def _transport(payload=None, *, status: int = 200, exc: Exception | None = None):
    async def handler(request: httpx.Request) -> httpx.Response:
        if exc is not None:
            raise exc
        if isinstance(payload, bytes):
            return httpx.Response(status, content=payload, request=request)
        return httpx.Response(status, json=payload, request=request)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_live_is_constant_and_does_not_call_readiness(app_client, monkeypatch):
    async def forbidden():
        raise AssertionError("live 不得执行 readiness probe")

    from routes import health as health_route

    monkeypatch.setattr(health_route, "readiness_report", forbidden)
    response = await app_client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {
        "status": "alive",
        "service": "local-tavern",
        "contract_version": 1,
    }
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_ready_status_and_contract_are_fixed(app_client, monkeypatch):
    from routes import health as health_route

    report = {
        "status": "not_ready",
        "service": "local-tavern",
        "contract_version": 1,
        "checks": {
            "data": {"status": "ok"},
            "runtime": {"status": "ok"},
            "ollama": {"status": "error", "code": "timeout"},
            "maintenance": {"status": "ok"},
        },
    }

    async def fake_report():
        return report

    monkeypatch.setattr(health_route, "readiness_report", fake_report)
    response = await app_client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == report
    assert response.headers["cache-control"] == "no-store"


def test_data_probe_reads_writes_and_leaves_no_file(tmp_path: Path):
    before = list(tmp_path.iterdir())
    assert health.probe_data_directory(tmp_path) == {"status": "ok"}
    assert list(tmp_path.iterdir()) == before
    assert health.probe_data_directory(tmp_path / "missing") == {
        "status": "error",
        "code": "data_unavailable",
    }


def test_data_probe_fsync_failure_is_sanitized(tmp_path: Path, monkeypatch):
    def fail_fsync(_fd):
        raise OSError("SECRET_PATH_AND_CONTENT")

    monkeypatch.setattr(health.os, "fsync", fail_fsync)
    result = health.probe_data_directory(tmp_path)
    assert result == {"status": "error", "code": "data_io"}
    assert "SECRET" not in json.dumps(result)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transport", "expected"),
    [
        (_transport({"models": [{"name": "SECRET_MODEL"}]}), {"ok": True}),
        (_transport({"models": []}), {"ok": False, "code": "no_models"}),
        (_transport({"wrong": []}), {"ok": False, "code": "invalid_response"}),
        (_transport(b"not-json"), {"ok": False, "code": "invalid_response"}),
        (_transport({}, status=502), {"ok": False, "code": "bad_status"}),
    ],
)
async def test_ollama_probe_has_stable_codes(transport, expected):
    client = OllamaClient("http://127.0.0.1:11434", health_transport=transport)
    assert await client.probe_health() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (httpx.ReadTimeout("SECRET_TIMEOUT"), "timeout"),
        (httpx.ConnectError("SECRET_URL_AND_TOKEN"), "unreachable"),
    ],
)
async def test_ollama_probe_does_not_log_exception_text(exc, code, caplog):
    caplog.set_level("WARNING")
    client = OllamaClient(
        "http://127.0.0.1:11434",
        health_transport=_transport(exc=exc),
    )
    assert await client.probe_health() == {"ok": False, "code": code}
    assert "SECRET" not in caplog.text


@pytest.mark.asyncio
async def test_readiness_report_sanitizes_all_probe_failures():
    async def ollama_failure():
        raise RuntimeError("SECRET_MODEL_BODY")

    report = await health.readiness_report(
        data_probe=lambda: {"status": "ok"},
        runtime_probe=lambda: {"status": "ok"},
        maintenance_probe=lambda: {"status": "ok"},
        ollama_probe=ollama_failure,
    )
    assert report["status"] == "not_ready"
    assert report["checks"]["ollama"] == {
        "status": "error",
        "code": "unreachable",
    }
    assert "SECRET" not in json.dumps(report)


@pytest.mark.asyncio
async def test_readiness_report_sanitizes_sync_exceptions_and_malformed_results():
    async def ollama_ok():
        return {"ok": True, "model": "SECRET_MODEL"}

    def data_failure():
        raise RuntimeError("SECRET_DATA_PATH")

    report = await health.readiness_report(
        data_probe=data_failure,
        runtime_probe=lambda: {"status": "error", "code": "SECRET_VERSION"},
        maintenance_probe=lambda: {
            "status": "error",
            "code": "SECRET_RESTORE_ID",
        },
        ollama_probe=ollama_ok,
    )

    assert report["checks"] == {
        "data": {"status": "error", "code": "data_io"},
        "runtime": {"status": "error", "code": "lock_invalid"},
        "ollama": {"status": "ok"},
        "maintenance": {
            "status": "error",
            "code": "maintenance_unavailable",
        },
    }
    assert "SECRET" not in json.dumps(report)
