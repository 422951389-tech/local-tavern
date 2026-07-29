from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import quality_gate
from tools.quality_gate import (
    COVERAGE_FAIL_UNDER,
    GATE_EXECUTION_ERROR_RETURN_CODE,
    GateFailure,
    GateRunner,
    GateStep,
    RealDataGuard,
    StepResult,
    build_steps,
)


ROOT = Path(__file__).resolve().parents[1]


def _names(mode: str) -> list[str]:
    return [
        step.name
        for step in build_steps(
            mode,
            ROOT,
            sys.executable,
            "node",
            "npm",
            "desktop-python" if mode == "release" else None,
        )
    ]


def test_preflight_is_explicitly_smaller_than_the_release_gate():
    preflight = _names("preflight")
    release = _names("release")
    assert any(name.startswith("javascript-syntax:web/") for name in preflight)
    assert "javascript-syntax:tests/browser_e2e.mjs" in preflight
    assert "python-tests" in preflight
    assert "node-dependency-locks" in preflight
    assert "desktop-environment" not in preflight
    assert "desktop-pip-check" not in preflight
    assert "desktop-build" not in preflight
    assert "desktop-smoke" not in preflight
    assert "ruff" not in preflight
    assert "browser-e2e-axe" not in preflight
    assert "browser-performance-budget" not in preflight
    assert "desktop-journey" not in preflight
    assert "python-tests-coverage" in release
    assert "backend-performance-budget" in release
    assert "ruff" in release
    assert "browser-e2e-axe" in release
    assert "browser-performance-budget" in release
    assert "desktop-environment" in release
    assert "desktop-pip-check" in release
    assert "desktop-build" in release
    assert "desktop-smoke" in release
    assert "desktop-journey" in release
    assert release.index("desktop-environment") < release.index("pip-check")
    assert release.index("desktop-pip-check") < release.index("pip-check")
    assert release.index("browser-e2e-axe") < release.index("desktop-build")
    assert release.index("browser-e2e-axe") < release.index(
        "browser-performance-budget"
    )
    assert release.index("coverage-report") < release.index(
        "backend-performance-budget"
    )
    assert release.index("desktop-build") < release.index("desktop-smoke")
    assert release.index("desktop-smoke") < release.index("desktop-journey")
    assert release[-1] == "diff-check"


def test_release_desktop_steps_use_locked_builder_and_frozen_executable_smoke():
    desktop_python = r"C:\locked-desktop\python.exe"
    steps = build_steps(
        "release",
        ROOT,
        sys.executable,
        "node",
        "npm",
        desktop_python,
    )
    by_name = {step.name: step.command for step in steps}
    assert by_name["desktop-environment"] == (
        desktop_python,
        "tools/dependency_locks.py",
        "--environment",
        "desktop",
    )
    assert by_name["desktop-pip-check"] == (
        desktop_python,
        "-m",
        "pip",
        "check",
    )
    assert by_name["desktop-build"] == (
        desktop_python,
        "tools/build_desktop.py",
        "--clean",
    )
    assert by_name["desktop-smoke"] == (
        sys.executable,
        "tools/desktop_smoke.py",
        "--executable",
        "release/LocalTavern/LocalTavern.exe",
        "--timeout",
        "120",
        "--hold-ms",
        "5000",
    )
    assert by_name["desktop-journey"] == (
        sys.executable,
        "tools/desktop_journey_smoke.py",
        "--executable",
        "release/LocalTavern/LocalTavern.exe",
        "--artifacts-dir",
        "artifacts/desktop-journey",
        "--timeout",
        "150",
        "--hold-ms",
        "3000",
    )
    assert "desktop" in by_name["python-compile"]
    assert by_name["coverage-report"] == (
        sys.executable,
        "-m",
        "coverage",
        "report",
        f"--fail-under={COVERAGE_FAIL_UNDER}",
    )
    assert by_name["backend-performance-budget"] == (
        sys.executable,
        "tools/performance_budget.py",
        "probe-backend",
        "--repeat",
        "3",
    )
    assert by_name["browser-performance-budget"] == (
        sys.executable,
        "tools/performance_budget.py",
        "check",
        "--input",
        "artifacts/browser-performance.json",
        "--scope",
        "browser",
    )


def test_release_gate_refuses_to_claim_readiness_without_desktop_builder():
    with pytest.raises(GateFailure, match="桌面构建 Python"):
        build_steps("release", ROOT, sys.executable, "node", "npm", None)


def test_release_browser_gate_pins_tools_and_rejects_module_overrides():
    source = (ROOT / "tests" / "browser_e2e.mjs").read_text(encoding="utf-8")
    assert "EXPECTED_PLAYWRIGHT_VERSION = '1.61.1'" in source
    assert "EXPECTED_AXE_VERSION = '4.12.1'" in source
    assert "EXPECTED_BROWSER_VERSION = '149.0.7827.55'" in source
    assert "发布门禁止本地模块覆盖" in source


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
    with pytest.raises(GateFailure, match="session.json") as failure:
        guard.verify()
    assert failure.value.code == "real_data_guard_changed"


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


def test_gate_runner_maps_missing_executable_to_stable_return_code(capsys):
    def executor(_command, **_kwargs):
        raise OSError("SECRET LOCAL PATH")

    results = GateRunner(ROOT, executor=executor).run(
        [GateStep("missing-tool", ("missing-tool",))]
    )
    assert results[0].returncode == GATE_EXECUTION_ERROR_RETURN_CODE
    assert "SECRET" not in capsys.readouterr().out


def test_main_reports_stable_gate_step_failure_code(monkeypatch, capsys):
    class Guard:
        def __init__(self, _root):
            pass

        def capture(self):
            return {"data": {"files": 0, "sha256": "EMPTY"}}

        def verify(self):
            return {"data": {"files": 0, "sha256": "EMPTY"}}

    monkeypatch.setattr(quality_gate, "RealDataGuard", Guard)
    monkeypatch.setattr(quality_gate, "_executable", lambda value, _label: value)
    monkeypatch.setattr(
        quality_gate,
        "build_steps",
        lambda *_args, **_kwargs: [GateStep("fixed-failure", ("tool",))],
    )
    monkeypatch.setattr(
        quality_gate.GateRunner,
        "run",
        lambda *_args, **_kwargs: [StepResult("fixed-failure", ("tool",), 37, 0.1)],
    )

    assert quality_gate.main(["--preflight", "--root", str(ROOT)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "gate_step_failed"
    assert payload["failed_step"] == "fixed-failure"


def test_main_preserves_stable_configuration_failure_code(monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise GateFailure("fixture", code="fixed_configuration_failure")

    monkeypatch.setattr(quality_gate, "_executable", fail)
    assert quality_gate.main(["--preflight", "--root", str(ROOT)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "fixed_configuration_failure"
