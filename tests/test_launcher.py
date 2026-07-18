from __future__ import annotations

import json
import logging
import socket
from logging.handlers import RotatingFileHandler

import pytest

from core import launcher


class _Response:
    def __init__(self, payload: object, *, status: int = 200):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int):
        return self._body[:limit]


def test_browser_url_never_uses_wildcard_address():
    assert launcher.service_url("0.0.0.0", 8765) == "http://127.0.0.1:8765"
    assert launcher.service_url("::", 9000) == "http://127.0.0.1:9000"
    assert launcher.service_url("localhost", 8765) == "http://localhost:8765"


def test_health_gate_requires_exact_marker_and_health_path():
    requested: list[str] = []

    def exact(request, *, timeout):
        requested.append(request.full_url)
        assert timeout == 1.0
        return _Response(dict(launcher.LIVE_MARKER))

    assert launcher.health_marker_matches("http://127.0.0.1:8765", opener=exact)
    assert requested == ["http://127.0.0.1:8765/health/live"]

    def foreign(_request, *, timeout):
        return _Response({"status": "ok", "service": "foreign"})

    assert not launcher.health_marker_matches("http://127.0.0.1:8765", opener=foreign)

    def unexpected_failure(_request, *, timeout):
        raise RuntimeError("SECRET_RESPONSE")

    assert not launcher.health_marker_matches(
        "http://127.0.0.1:8765",
        opener=unexpected_failure,
    )


def test_browser_opens_once_only_after_live_marker():
    probes = iter([False, False, True])
    opened: list[str] = []
    assert launcher.open_browser_when_live(
        "http://127.0.0.1:8765",
        timeout_seconds=1,
        probe=lambda _url: next(probes),
        browser_open=opened.append,
    )
    assert opened == ["http://127.0.0.1:8765"]


def test_browser_failure_is_sanitized(caplog):
    caplog.set_level("ERROR")

    def fail_open(_url):
        raise RuntimeError("SECRET_BROWSER_COMMAND")

    assert not launcher.open_browser_when_live(
        "http://127.0.0.1:8765",
        timeout_seconds=1,
        probe=lambda _url: True,
        browser_open=fail_open,
    )
    assert "SECRET" not in caplog.text


def test_prebound_socket_blocks_a_competing_listener():
    first = launcher.bind_socket("127.0.0.1", 0)
    try:
        port = first.getsockname()[1]
        first.listen(1)
        with pytest.raises(OSError):
            second = launcher.bind_socket("127.0.0.1", port)
            second.close()
    finally:
        first.close()


def test_foreign_listener_never_opens_browser(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(launcher, "bind_socket", lambda *_args: (_ for _ in ()).throw(OSError("busy")))
    monkeypatch.setattr(launcher, "_existing_instance_is_owned", lambda _config: False)
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    assert launcher.serve(hidden=False, open_browser=True) == 3
    assert opened == []


def test_exact_existing_instance_must_also_pass_live_marker(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(launcher, "bind_socket", lambda *_args: (_ for _ in ()).throw(OSError("busy")))
    monkeypatch.setattr(launcher, "_existing_instance_is_owned", lambda _config: True)
    monkeypatch.setattr(launcher, "health_marker_matches", lambda _url: True)
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    assert launcher.serve(hidden=False, open_browser=True) == 0
    assert len(opened) == 1


def test_launcher_rejects_multiple_workers_before_start():
    with pytest.raises(SystemExit) as caught:
        launcher.main(["serve", "--workers", "2"])
    assert caught.value.code == 2


def test_hidden_environment_failure_is_written_to_bootstrap_log(
    monkeypatch,
    tmp_path,
):
    from core import logging_config

    log_path = tmp_path / "hidden" / "startup.log"
    monkeypatch.setenv("TAVERN_LOG_FILE", str(log_path))

    def fail_logging(**_kwargs):
        raise OSError("SECRET_LOG_PATH")

    monkeypatch.setattr(logging_config, "configure_logging", fail_logging)
    assert launcher.serve(hidden=True, open_browser=False) == 2
    text = log_path.read_text(encoding="utf-8")
    assert "startup_failed code=environment exception=OSError" in text
    assert "SECRET" not in text


def test_bootstrap_log_falls_back_to_project_log_and_rotates(monkeypatch, tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("TAVERN_LOG_FILE", str(blocked / "startup.log"))
    handler = launcher._bootstrap_hidden_log(tmp_path)
    try:
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 1024 * 1024
        assert handler.backupCount == 2
        assert handler.baseFilename == str((tmp_path / "logs" / "tavern.log").resolve())
    finally:
        launcher._remove_handler(handler)


def test_uvicorn_lifespan_startup_failure_returns_nonzero(monkeypatch):
    import uvicorn

    class FakeServer:
        def __init__(self, _config):
            self.started = False

        def run(self, *, sockets):
            assert len(sockets) == 1

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    assert launcher.serve(hidden=False, open_browser=False) == 4

    for handler in list(logging.getLogger().handlers):
        if getattr(handler, "_local_tavern_handler", False):
            logging.getLogger().removeHandler(handler)
            handler.close()
