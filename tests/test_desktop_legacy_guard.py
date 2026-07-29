from __future__ import annotations

import json
from pathlib import Path

import pytest

from desktop.legacy_guard import (
    _command_matches_legacy_tavern,
    detect_verified_legacy_service,
)


def _write_metadata(root: Path, **overrides) -> dict:
    metadata = {
        "pid": 4312,
        "port": 54321,
        "project_root": str(root.resolve()),
        **overrides,
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "tavern.pid").write_text(json.dumps(metadata), encoding="utf-8")
    return metadata


def test_legacy_guard_verifies_the_metadata_port_independently_of_desktop_port(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "legacy"
    metadata = _write_metadata(root)
    observed_ports: list[int] = []
    monkeypatch.setenv("TAVERN_PORT", "8765")

    import core.process_guard as process_guard

    monkeypatch.setattr(process_guard, "_pid_is_running", lambda pid: pid == metadata["pid"])
    monkeypatch.setattr(
        process_guard,
        "_process_command_line",
        lambda pid: f"python -m uvicorn server:app --port {metadata['port']}",
    )

    def listening_pids(port: int) -> set[int]:
        observed_ports.append(port)
        return {metadata["pid"]}

    monkeypatch.setattr(process_guard, "_listening_pids", listening_pids)

    service = detect_verified_legacy_service(root)

    assert service is not None
    assert service.pid == metadata["pid"]
    assert service.port == metadata["port"]
    assert service.project_root == root.resolve()
    assert observed_ports == [metadata["port"]]


@pytest.mark.parametrize(
    ("pid_running", "command_line", "listener_pids"),
    [
        (False, "python -m uvicorn server:app", {4312}),
        (True, "python unrelated.py", {4312}),
        (True, "python -m core.launcher serve", {9999}),
    ],
)
def test_legacy_guard_requires_pid_command_and_listener_to_match(
    monkeypatch,
    tmp_path,
    pid_running,
    command_line,
    listener_pids,
):
    root = tmp_path / "legacy"
    _write_metadata(root)

    import core.process_guard as process_guard

    monkeypatch.setattr(process_guard, "_pid_is_running", lambda _pid: pid_running)
    monkeypatch.setattr(process_guard, "_process_command_line", lambda _pid: command_line)
    monkeypatch.setattr(process_guard, "_listening_pids", lambda _port: listener_pids)

    assert detect_verified_legacy_service(root) is None


def test_legacy_guard_rejects_mismatched_recorded_root_before_process_checks(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "legacy"
    _write_metadata(root, project_root=str(tmp_path / "other"))

    import core.process_guard as process_guard

    def fail(*_args, **_kwargs):
        raise AssertionError("根目录不匹配时不得读取进程状态")

    monkeypatch.setattr(process_guard, "_pid_is_running", fail)
    monkeypatch.setattr(process_guard, "_process_command_line", fail)
    monkeypatch.setattr(process_guard, "_listening_pids", fail)

    assert detect_verified_legacy_service(root) is None


@pytest.mark.parametrize(
    "command_line",
    [
        "python -m uvicorn server:app --port 9000",
        "python -m core.launcher serve --hidden",
        "python C:/local-tavern/server.py",
    ],
)
def test_legacy_guard_accepts_only_known_tavern_server_commands(command_line):
    assert _command_matches_legacy_tavern(command_line)
    assert not _command_matches_legacy_tavern("python unrelated.py")
