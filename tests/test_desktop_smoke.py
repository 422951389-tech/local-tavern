from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from desktop import main as desktop_main
from tools import desktop_smoke
from tools.desktop_smoke import (
    DesktopSmokeError,
    EXPECTED_RESULT,
    _descendant_pids,
    _listening_pids,
    _validate_result,
    run_smoke,
)


ROOT = Path(__file__).resolve().parents[1]


def _result(pid: int, **overrides) -> dict:
    return {"pid": pid, **EXPECTED_RESULT, **overrides}


def test_cold_product_timeout_contract_has_internal_and_external_margin(
    monkeypatch,
    tmp_path,
    capsys,
):
    assert desktop_main.SMOKE_TIMEOUT_MS == 60_000
    executable = tmp_path / "LocalTavern.exe"
    executable.write_bytes(b"fixture")
    captured: dict[str, object] = {}

    def fake_run_smoke(target, *, timeout_seconds, hold_ms):
        captured.update(
            target=target,
            timeout_seconds=timeout_seconds,
            hold_ms=hold_ms,
        )
        return {"status": "passed"}

    monkeypatch.setattr(desktop_smoke, "run_smoke", fake_run_smoke)

    assert desktop_smoke.main(["--executable", str(executable)]) == 0
    assert captured == {
        "target": executable,
        "timeout_seconds": 120.0,
        "hold_ms": 4000,
    }
    assert '"status": "passed"' in capsys.readouterr().out


def test_netstat_parser_returns_only_tcp_listeners(monkeypatch):
    output = """
      TCP    127.0.0.1:8765       0.0.0.0:0       LISTENING       1234
      TCP    127.0.0.1:50000      127.0.0.1:443   ESTABLISHED     2345
      TCP    [::1]:9000           [::]:0          LISTENING       3456
      UDP    0.0.0.0:5353         *:*                              4567
      TCP    invalid              row             LISTENING       not-a-pid
    """
    monkeypatch.setattr(
        desktop_smoke.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output),
    )
    assert _listening_pids() == {1234, 3456}


def test_netstat_failure_is_not_treated_as_no_listener(monkeypatch):
    monkeypatch.setattr(
        desktop_smoke.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    with pytest.raises(DesktopSmokeError, match="无法读取 Windows TCP 监听表"):
        _listening_pids()


def test_descendant_pid_walk_is_recursive_and_excludes_unrelated_processes(monkeypatch):
    monkeypatch.setattr(
        desktop_smoke,
        "_process_parent_map",
        lambda: {
            200: 100,
            300: 200,
            400: 300,
            500: 999,
            600: 100,
        },
    )
    assert _descendant_pids(100) == {200, 300, 400, 600}


def test_smoke_result_contract_is_exact_and_pid_bound():
    _validate_result(_result(1234), 1234)
    with pytest.raises(DesktopSmokeError, match="PID"):
        _validate_result(_result(1234), 9999)
    with pytest.raises(DesktopSmokeError, match="api_live"):
        _validate_result(_result(1234, api_live=False), 1234)


def test_gui_smoke_probes_health_through_desktop_transport_not_native_fetch():
    source = (ROOT / "desktop" / "main.py").read_text(encoding="utf-8")
    assert "globalThis.TavernDesktopTransport" in source
    assert "transport.fetch('/health/live'" in source
    assert re.search(r"(?<![\w.])fetch\s*\(\s*['\"]\/health\/live", source) is None


def test_gui_smoke_reports_via_locked_console_marker_not_page_title():
    main_source = (ROOT / "desktop" / "main.py").read_text(encoding="utf-8")
    window_source = (ROOT / "desktop" / "window.py").read_text(encoding="utf-8")
    assert "console.info(prefix + encoded)" in main_source
    assert "page.consoleMessage.connect" in main_source
    assert "titleChanged.connect" not in main_source
    assert "consoleMessage = Signal(str)" in window_source
    assert "self.consoleMessage.emit(str(message))" in window_source
    unknown = _result(1234)
    unknown["secret"] = "must-not-pass"
    with pytest.raises(DesktopSmokeError, match="未知或缺失字段"):
        _validate_result(unknown, 1234)
    missing = _result(1234)
    missing.pop("transport")
    with pytest.raises(DesktopSmokeError):
        _validate_result(missing, 1234)


class _FakeProcess:
    def __init__(self, command, **kwargs) -> None:
        self.command = command
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_real_smoke_launch_contract_uses_isolated_paths_and_cleans_process(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("TAVERN_PROJECTS_DIR", "C:/Users/zcw/real-projects")
    monkeypatch.setenv("TAVERN_UNRELATED_TEST_LEAK", "must-be-removed")
    executable = tmp_path / "release" / "LocalTavern.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    processes = []

    def popen(command, **kwargs):
        process = _FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(desktop_smoke.subprocess, "Popen", popen)
    monkeypatch.setattr(
        desktop_smoke,
        "_read_result",
        lambda _path, process, _deadline: _result(process.pid),
    )
    monkeypatch.setattr(desktop_smoke, "_listening_pids", lambda: set())
    monkeypatch.setattr(desktop_smoke, "_descendant_pids", lambda _pid: {5252, 6262})
    monkeypatch.setattr(desktop_smoke, "_process_parent_map", lambda: {})
    monkeypatch.setattr(desktop_smoke.time, "sleep", lambda _seconds: None)

    result = run_smoke(executable, timeout_seconds=5, hold_ms=1500)

    assert result == {
        "status": "passed",
        "pid": 4242,
        "runtime": "in_process_asgi",
        "transport": "qwebchannel",
        "scheme": "tavern://app",
        "tcp_listener_samples": [[], [], []],
        "process_exited": True,
    }
    assert len(processes) == 1
    process = processes[0]
    assert process.command[0] == str(executable.resolve())
    assert process.command[1] == "--smoke-test"
    assert Path(process.command[2]).is_absolute()
    assert process.command[3:] == ["--smoke-hold-ms", "1500"]
    assert process.kwargs["cwd"] == executable.parent.resolve()
    environment = process.kwargs["env"]
    isolated_root = Path(environment["TAVERN_BASE_DIR"])
    assert isolated_root.name.startswith("local-tavern-desktop-smoke-")
    for name in (
        "TAVERN_DESKTOP_USER_ROOT",
        "TAVERN_DATA_DIR",
        "TAVERN_SETTINGS_PATH",
        "TAVERN_PROJECTS_DIR",
        "TAVERN_PROMPTS_DIR",
        "TAVERN_RECOVERY_DIR",
        "TAVERN_MIGRATIONS_DIR",
        "TAVERN_BACKUP_DIR",
        "TAVERN_LOG_DIR",
        "TAVERN_LOG_FILE",
        "TAVERN_PID_PATH",
        "TAVERN_STOP_REQUEST_PATH",
        "TAVERN_PROVIDER_DATA_DIR",
        "TAVERN_PROVIDER_CONFIG_PATH",
        "TAVERN_PROVIDER_SECRETS_PATH",
    ):
        assert Path(environment[name]).is_relative_to(isolated_root)
    assert "TAVERN_UNRELATED_TEST_LEAK" not in environment
    assert environment["TAVERN_OLLAMA_HOST"] == "http://127.0.0.1:1"
    assert environment["TAVERN_BACKUP_SCHEDULE_ENABLED"] == "false"
    assert process.returncode == 0
    assert not process.terminated
    assert not process.killed


def test_smoke_detects_listener_and_always_terminates_process(monkeypatch, tmp_path):
    executable = tmp_path / "LocalTavern.exe"
    executable.write_bytes(b"fixture")
    process = _FakeProcess([], cwd=tmp_path)
    monkeypatch.setattr(desktop_smoke.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        desktop_smoke,
        "_read_result",
        lambda _path, launched, _deadline: _result(launched.pid),
    )
    child_pid = 5252
    monkeypatch.setattr(desktop_smoke, "_descendant_pids", lambda _pid: {child_pid})
    monkeypatch.setattr(desktop_smoke, "_listening_pids", lambda: {child_pid})
    monkeypatch.setattr(desktop_smoke, "_process_parent_map", lambda: {})

    with pytest.raises(DesktopSmokeError, match="进程树开启了 TCP 监听端口"):
        run_smoke(executable, timeout_seconds=5, hold_ms=1500)
    assert process.terminated
    assert process.returncode == -15


def test_smoke_rejects_non_executable_target(tmp_path):
    target = tmp_path / "LocalTavern.bin"
    target.write_bytes(b"fixture")
    with pytest.raises(DesktopSmokeError, match="必须是 .exe"):
        run_smoke(target, timeout_seconds=5, hold_ms=1500)
