from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tools.quality_gate import (
    GateFailure,
    GateRunner,
    GateStep,
    RealDataGuard,
    build_steps,
)


ROOT = Path(__file__).resolve().parents[1]


def _names(mode: str) -> list[str]:
    return [
        step.name
        for step in build_steps(mode, ROOT, sys.executable, "node", "npm")
    ]


def test_preflight_is_explicitly_smaller_than_the_release_gate():
    preflight = _names("preflight")
    release = _names("release")
    assert any(name.startswith("javascript-syntax:web/") for name in preflight)
    assert "javascript-syntax:tests/browser_e2e.mjs" in preflight
    assert "python-tests" in preflight
    assert "ruff" not in preflight
    assert "browser-e2e-axe" not in preflight
    assert "python-tests-coverage" in release
    assert "ruff" in release
    assert "browser-e2e-axe" in release
    assert release[-1] == "diff-check"


def test_real_data_guard_reports_exact_changed_path(tmp_path: Path):
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    logs = tmp_path / "logs"
    for directory in (data, backups, logs):
        directory.mkdir()
    target = data / "session.json"
    target.write_text("before", encoding="utf-8")
    guard = RealDataGuard(tmp_path)
    before = guard.capture()
    assert before["data"]["files"] == 1
    target.write_text("after", encoding="utf-8")
    with pytest.raises(GateFailure, match="session.json"):
        guard.verify()


def test_gate_runner_stops_at_the_first_failure():
    steps = [
        GateStep("pass", (sys.executable, "-c", "raise SystemExit(0)")),
        GateStep("fail", (sys.executable, "-c", "raise SystemExit(7)")),
        GateStep("unreached", (sys.executable, "-c", "raise SystemExit(0)")),
    ]
    results = GateRunner(ROOT).run(steps)
    assert [result.name for result in results] == ["pass", "fail"]
    assert results[-1].returncode == 7


def test_gate_runner_keep_going_collects_all_results():
    calls: list[list[str]] = []

    def executor(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1 if command[-1] == "fail" else 0)

    steps = [
        GateStep("first", ("tool", "fail")),
        GateStep("second", ("tool", "pass")),
    ]
    results = GateRunner(ROOT, executor=executor).run(steps, keep_going=True)
    assert [result.returncode for result in results] == [1, 0]
    assert len(calls) == 2
