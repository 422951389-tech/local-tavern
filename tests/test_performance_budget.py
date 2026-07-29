from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import performance_budget
from tools.performance_budget import (
    EXIT_BUDGET_EXCEEDED,
    EXIT_INVALID_INPUT,
    PerformanceContractError,
    check_document,
    percentile_95,
    probe_backend,
    validate_document,
)


COMMIT = "a" * 40


def _document(metrics: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source": "test-browser-probe",
        "environment": {
            "os": "Windows-test",
            "python": "3.12.10",
            "node": "v24.15.0",
            "browser": "Edge 149",
            "source_commit": COMMIT,
        },
        "warmup_runs": 1,
        "metrics": metrics,
    }


def _metric(*samples: float, message_count: int | None = None) -> dict[str, object]:
    value: dict[str, object] = {"unit": "ms", "samples": list(samples)}
    if message_count is not None:
        value["message_count"] = message_count
    return value


def _browser_document() -> dict[str, object]:
    return _document(
        {
            "browser_first_contentful_paint_ms": _metric(500, 700, 600),
            "browser_first_screen_ready_ms": _metric(900, 1000, 1100),
            "browser_render_1000_messages_ms": _metric(
                800,
                900,
                1000,
                message_count=1000,
            ),
        }
    )


def test_nearest_rank_p95_is_deterministic():
    assert percentile_95([50, 10, 20, 30, 40]) == 50.0
    assert percentile_95([1, 2, 3]) == 3.0


def test_browser_contract_covers_first_screen_and_exactly_1000_messages():
    validated = validate_document(_browser_document(), scope="browser")
    assert set(validated["metrics"]) == {
        "browser_first_contentful_paint_ms",
        "browser_first_screen_ready_ms",
        "browser_render_1000_messages_ms",
    }
    assert (
        validated["metrics"]["browser_render_1000_messages_ms"]["message_count"] == 1000
    )

    invalid = _browser_document()
    invalid["metrics"]["browser_render_1000_messages_ms"]["message_count"] = 999
    with pytest.raises(PerformanceContractError, match="必须为 1000"):
        validate_document(invalid, scope="browser")


def test_contract_rejects_missing_unknown_and_non_finite_metrics():
    missing = _browser_document()
    missing["metrics"].pop("browser_first_screen_ready_ms")
    with pytest.raises(PerformanceContractError, match="缺失"):
        validate_document(missing, scope="browser")

    unknown = _browser_document()
    unknown["metrics"]["browser_typo_ms"] = _metric(1, 2, 3)
    with pytest.raises(PerformanceContractError, match="未知"):
        validate_document(unknown, scope="browser")

    invalid = _browser_document()
    invalid["metrics"]["browser_first_contentful_paint_ms"]["samples"][1] = float("nan")
    with pytest.raises(PerformanceContractError, match="超出"):
        validate_document(invalid, scope="browser")


def test_budget_report_uses_stable_failure_code():
    document = _document(
        {
            "backend_startup_health_ms": _metric(4999, 5000, 5001),
        }
    )
    report = check_document(document, scope="backend")
    assert report["status"] == "failed"
    assert report["error_code"] == "performance_budget_exceeded"
    assert report["metrics"]["backend_startup_health_ms"] == {
        "p95_ms": 5001.0,
        "budget_ms": 5000.0,
        "sample_count": 3,
        "passed": False,
    }


def test_cli_exit_codes_distinguish_invalid_input_and_budget_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    over_budget = tmp_path / "over-budget.json"
    over_budget.write_text(
        json.dumps(
            _document(
                {
                    "backend_startup_health_ms": _metric(6000, 6100, 6200),
                }
            )
        ),
        encoding="utf-8",
    )
    assert (
        performance_budget.main(
            [
                "check",
                "--input",
                str(over_budget),
                "--scope",
                "backend",
            ]
        )
        == EXIT_BUDGET_EXCEEDED
    )
    assert (
        json.loads(capsys.readouterr().out)["error_code"]
        == "performance_budget_exceeded"
    )

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}", encoding="utf-8")
    assert (
        performance_budget.main(
            [
                "check",
                "--input",
                str(invalid),
                "--scope",
                "backend",
            ]
        )
        == EXIT_INVALID_INPUT
    )
    assert (
        json.loads(capsys.readouterr().out)["error_code"] == "performance_input_invalid"
    )

    assert (
        performance_budget.main(
            [
                "check",
                "--input",
                str(tmp_path / "missing.json"),
                "--scope",
                "backend",
            ]
        )
        == EXIT_INVALID_INPUT
    )
    assert (
        json.loads(capsys.readouterr().out)["error_code"] == "performance_input_invalid"
    )


def test_backend_probe_uses_cold_isolated_children_and_discards_warmup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    child_environments: list[dict[str, str]] = []

    def run(command, **kwargs):
        command = [str(item) for item in command]
        if command[-1] == "_backend-child":
            child_environments.append(kwargs["env"])
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "status": "alive",
                        "contract_version": 1,
                        "elapsed_ms": 100 + len(child_environments),
                    }
                )
                + "\n",
                stderr="",
            )
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(
                command, 0, stdout=f"{COMMIT}\n", stderr=""
            )
        if command[:2] == ["node", "--version"]:
            return subprocess.CompletedProcess(
                command, 0, stdout="v24.15.0\n", stderr=""
            )
        raise AssertionError(command)

    monkeypatch.setattr(performance_budget.subprocess, "run", run)
    document = probe_backend(root=tmp_path, python=sys.executable, repeat=3)

    samples = document["metrics"]["backend_startup_health_ms"]["samples"]
    assert len(samples) == 3
    assert len(child_environments) == 4
    assert document["warmup_runs"] == 1
    assert document["environment"]["source_commit"] == COMMIT
    for environment in child_environments:
        data_dir = Path(environment["TAVERN_DATA_DIR"])
        assert environment["TAVERN_DESKTOP_MODE"] == "true"
        assert environment["TAVERN_ALLOW_REMOTE"] == "false"
        assert int(environment["TAVERN_PERFORMANCE_START_NS"]) > 0
        assert data_dir != tmp_path / "data"
        assert "local-tavern-performance-" in str(data_dir)
