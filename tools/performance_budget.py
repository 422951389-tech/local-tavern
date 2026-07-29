"""可重复的性能指标契约、预算检查与隔离后端冷启动探针。"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 1024 * 1024
MIN_SAMPLES = 3
MAX_SAMPLES = 50
BACKEND_CHILD_TIMEOUT_SECONDS = 30

EXIT_PASSED = 0
EXIT_BUDGET_EXCEEDED = 2
EXIT_INVALID_INPUT = 3
EXIT_PROBE_FAILED = 4

_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class MetricDefinition:
    budget_ms: float
    message_count: int | None = None


METRIC_DEFINITIONS: Mapping[str, MetricDefinition] = {
    "backend_startup_health_ms": MetricDefinition(5000.0),
    "browser_first_contentful_paint_ms": MetricDefinition(2000.0),
    "browser_first_screen_ready_ms": MetricDefinition(3000.0),
    "browser_render_1000_messages_ms": MetricDefinition(1500.0, message_count=1000),
}

SCOPE_METRICS: Mapping[str, frozenset[str]] = {
    "backend": frozenset({"backend_startup_health_ms"}),
    "browser": frozenset(
        {
            "browser_first_contentful_paint_ms",
            "browser_first_screen_ready_ms",
            "browser_render_1000_messages_ms",
        }
    ),
    "release": frozenset(METRIC_DEFINITIONS),
}


class PerformanceContractError(ValueError):
    """性能数据不符合稳定输入契约。"""


class PerformanceProbeError(RuntimeError):
    """性能探针没有完成受控场景。"""


def percentile_95(samples: Sequence[float]) -> float:
    """使用 nearest-rank 计算可复现的 P95，不依赖统计库插值实现。"""
    if not samples:
        raise PerformanceContractError("性能样本不能为空")
    ordered = sorted(float(value) for value in samples)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return round(ordered[index], 3)


def _require_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PerformanceContractError(f"{label} 必须是对象")
    return value


def _require_exact_keys(
    value: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str] | None = None,
    label: str,
) -> None:
    optional = optional or set()
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required - optional)
    if missing or unknown:
        raise PerformanceContractError(
            f"{label} 字段不符合契约（缺失={missing}，未知={unknown}）"
        )


def _validate_environment(value: object) -> dict[str, str]:
    raw = _require_object(value, "environment")
    required = {"os", "python", "node", "browser", "source_commit"}
    _require_exact_keys(raw, required=required, label="environment")
    result: dict[str, str] = {}
    for key in sorted(required):
        item = raw[key]
        if not isinstance(item, str) or not item.strip() or len(item) > 256:
            raise PerformanceContractError(f"environment.{key} 必须是非空短字符串")
        result[key] = item.strip()
    commit = result["source_commit"]
    if commit != "unavailable" and _COMMIT_RE.fullmatch(commit) is None:
        raise PerformanceContractError(
            "environment.source_commit 必须是 40 位 Git commit 或 unavailable"
        )
    return result


def _validate_samples(value: object, metric: str) -> list[float]:
    if not isinstance(value, list):
        raise PerformanceContractError(f"{metric}.samples 必须是数组")
    if not MIN_SAMPLES <= len(value) <= MAX_SAMPLES:
        raise PerformanceContractError(
            f"{metric}.samples 数量必须在 {MIN_SAMPLES} 到 {MAX_SAMPLES} 之间"
        )
    samples: list[float] = []
    for index, item in enumerate(value):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise PerformanceContractError(f"{metric}.samples[{index}] 必须是数字")
        number = float(item)
        if not math.isfinite(number) or number < 0 or number > 600_000:
            raise PerformanceContractError(
                f"{metric}.samples[{index}] 超出 0..600000ms"
            )
        samples.append(round(number, 3))
    return samples


def validate_document(document: object, *, scope: str) -> dict[str, object]:
    if scope not in SCOPE_METRICS:
        raise PerformanceContractError(f"未知性能检查范围：{scope}")
    raw = _require_object(document, "性能文档")
    _require_exact_keys(
        raw,
        required={"schema_version", "source", "environment", "warmup_runs", "metrics"},
        label="性能文档",
    )
    if raw["schema_version"] != SCHEMA_VERSION:
        raise PerformanceContractError(f"schema_version 必须为 {SCHEMA_VERSION}")
    source = raw["source"]
    if not isinstance(source, str) or not source.strip() or len(source) > 128:
        raise PerformanceContractError("source 必须是非空短字符串")
    warmup_runs = raw["warmup_runs"]
    if isinstance(warmup_runs, bool) or not isinstance(warmup_runs, int):
        raise PerformanceContractError("warmup_runs 必须是整数")
    if not 1 <= warmup_runs <= 20:
        raise PerformanceContractError("warmup_runs 必须在 1 到 20 之间")
    environment = _validate_environment(raw["environment"])
    raw_metrics = _require_object(raw["metrics"], "metrics")
    unknown_metrics = sorted(set(raw_metrics) - set(METRIC_DEFINITIONS))
    missing_metrics = sorted(SCOPE_METRICS[scope] - set(raw_metrics))
    if missing_metrics or unknown_metrics:
        raise PerformanceContractError(
            f"metrics 不符合检查范围（缺失={missing_metrics}，未知={unknown_metrics}）"
        )

    metrics: dict[str, dict[str, object]] = {}
    for name in sorted(raw_metrics):
        definition = METRIC_DEFINITIONS[name]
        metric = _require_object(raw_metrics[name], name)
        optional = {"message_count"} if definition.message_count is not None else set()
        _require_exact_keys(
            metric,
            required={"unit", "samples"},
            optional=optional,
            label=name,
        )
        if metric["unit"] != "ms":
            raise PerformanceContractError(f"{name}.unit 必须为 ms")
        if definition.message_count is not None:
            if metric.get("message_count") != definition.message_count:
                raise PerformanceContractError(
                    f"{name}.message_count 必须为 {definition.message_count}"
                )
        metrics[name] = {
            "unit": "ms",
            "samples": _validate_samples(metric["samples"], name),
            **(
                {"message_count": definition.message_count}
                if definition.message_count is not None
                else {}
            ),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "source": source.strip(),
        "environment": environment,
        "warmup_runs": warmup_runs,
        "metrics": metrics,
    }


def check_document(document: object, *, scope: str) -> dict[str, object]:
    validated = validate_document(document, scope=scope)
    rows: dict[str, dict[str, object]] = {}
    passed = True
    for name in sorted(SCOPE_METRICS[scope]):
        metric = validated["metrics"][name]
        p95_ms = percentile_95(metric["samples"])
        budget_ms = METRIC_DEFINITIONS[name].budget_ms
        within_budget = p95_ms <= budget_ms
        passed = passed and within_budget
        rows[name] = {
            "p95_ms": p95_ms,
            "budget_ms": budget_ms,
            "sample_count": len(metric["samples"]),
            "passed": within_budget,
        }
    return {
        "status": "passed" if passed else "failed",
        "error_code": None if passed else "performance_budget_exceeded",
        "scope": scope,
        "metrics": rows,
    }


def load_document(path: Path) -> object:
    try:
        target = path.resolve(strict=True)
        if target.stat().st_size > MAX_INPUT_BYTES:
            raise PerformanceContractError(f"性能输入超过 {MAX_INPUT_BYTES} 字节上限")
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PerformanceContractError("性能输入不是可读取的 UTF-8 JSON") from exc


def _command_version(command: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            list(command),
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    if completed.returncode != 0:
        return "unavailable"
    output = (completed.stdout or completed.stderr or "").strip().splitlines()
    return output[0][:256] if output else "unavailable"


def _source_commit(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    value = (completed.stdout or "").strip()
    return (
        value
        if completed.returncode == 0 and _COMMIT_RE.fullmatch(value)
        else "unavailable"
    )


def _backend_environment(root: Path, writable_root: Path) -> dict[str, str]:
    data = writable_root / "data"
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONUTF8": "1",
            "TAVERN_BASE_DIR": str(root),
            "TAVERN_DATA_DIR": str(data),
            "TAVERN_PROJECTS_DIR": str(data / "projects"),
            "TAVERN_WEB_DIR": str(root / "web"),
            "TAVERN_PROMPTS_DIR": str(root / "prompts"),
            "TAVERN_SETTINGS_PATH": str(data / "settings.json"),
            "TAVERN_RECOVERY_DIR": str(data / ".recovery"),
            "TAVERN_MIGRATIONS_DIR": str(data / ".migrations"),
            "TAVERN_BACKUP_DIR": str(writable_root / "backups"),
            "TAVERN_LOG_DIR": str(writable_root / "logs"),
            "TAVERN_LOG_FILE": str(writable_root / "logs" / "tavern.log"),
            "TAVERN_PID_PATH": str(writable_root / "tavern.pid"),
            "TAVERN_STOP_REQUEST_PATH": str(writable_root / "tavern.stop.pid"),
            "TAVERN_PROVIDER_DATA_DIR": str(writable_root / "provider-data"),
            "TAVERN_HOST": "127.0.0.1",
            "TAVERN_ALLOW_REMOTE": "false",
            "TAVERN_DESKTOP_MODE": "true",
            "TAVERN_BACKUP_SCHEDULE_ENABLED": "false",
            "TAVERN_OLLAMA_HOST": "http://127.0.0.1:1",
        }
    )
    return environment


def probe_backend(*, root: Path, python: str, repeat: int) -> dict[str, object]:
    if not MIN_SAMPLES <= repeat <= MAX_SAMPLES:
        raise PerformanceContractError(
            f"repeat 必须在 {MIN_SAMPLES} 到 {MAX_SAMPLES} 之间"
        )
    samples: list[float] = []
    with tempfile.TemporaryDirectory(prefix="local-tavern-performance-") as temporary:
        temporary_root = Path(temporary)
        for index in range(repeat + 1):
            writable_root = temporary_root / f"run-{index}"
            environment = _backend_environment(root, writable_root)
            environment["TAVERN_PERFORMANCE_START_NS"] = str(time.perf_counter_ns())
            try:
                completed = subprocess.run(
                    [python, str(Path(__file__).resolve()), "_backend-child"],
                    cwd=root,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=BACKEND_CHILD_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise PerformanceProbeError("后端冷启动探针未在时限内完成") from exc
            if completed.returncode != 0:
                raise PerformanceProbeError(
                    f"后端冷启动探针失败：退出码 {completed.returncode}"
                )
            lines = [
                line.strip()
                for line in (completed.stdout or "").splitlines()
                if line.strip()
            ]
            try:
                result = json.loads(lines[-1]) if lines else None
            except json.JSONDecodeError as exc:
                raise PerformanceProbeError("后端冷启动探针没有返回 JSON") from exc
            if (
                not isinstance(result, dict)
                or result.get("status") != "alive"
                or result.get("contract_version") != 1
                or isinstance(result.get("elapsed_ms"), bool)
                or not isinstance(result.get("elapsed_ms"), (int, float))
                or not 0
                <= float(result["elapsed_ms"])
                <= (BACKEND_CHILD_TIMEOUT_SECONDS * 1000)
            ):
                raise PerformanceProbeError("后端冷启动探针返回了错误的 health 契约")
            if index > 0:
                samples.append(round(float(result["elapsed_ms"]), 3))
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "tools.performance_budget:probe-backend",
        "environment": {
            "os": platform.platform(),
            "python": platform.python_version(),
            "node": _command_version(("node", "--version")),
            "browser": "not_applicable",
            "source_commit": _source_commit(root),
        },
        "warmup_runs": 1,
        "metrics": {
            "backend_startup_health_ms": {
                "unit": "ms",
                "samples": samples,
            },
        },
    }


async def _run_backend_child() -> dict[str, object]:
    import httpx

    from server import app

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
        ) as client:
            response = await client.get("/health/live")
            try:
                started_ns = int(os.environ["TAVERN_PERFORMANCE_START_NS"])
            except (KeyError, ValueError) as exc:
                raise PerformanceProbeError("探针缺少进程启动时钟") from exc
            elapsed_ms = round(
                (time.perf_counter_ns() - started_ns) / 1_000_000,
                3,
            )
    payload = response.json()
    if response.status_code != 200 or payload.get("status") != "alive":
        raise PerformanceProbeError("health/live 未满足存活契约")
    return {
        "status": payload["status"],
        "contract_version": payload["contract_version"],
        "elapsed_ms": elapsed_ms,
    }


def _write_document(path: Path, document: Mapping[str, object]) -> None:
    try:
        target = path.resolve(strict=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    except (OSError, UnicodeError) as exc:
        raise PerformanceProbeError("性能探针输出文件无法写入") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查本地酒馆性能预算")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check", help="校验已有性能数据")
    check_parser.add_argument("--input", type=Path, required=True)
    check_parser.add_argument(
        "--scope", choices=tuple(SCOPE_METRICS), default="release"
    )

    probe_parser = subparsers.add_parser(
        "probe-backend",
        help="隔离测量冷启动到 health/live 的耗时",
    )
    probe_parser.add_argument("--root", type=Path, default=ROOT)
    probe_parser.add_argument("--python", default=sys.executable)
    probe_parser.add_argument("--repeat", type=int, default=3)
    probe_parser.add_argument("--output", type=Path)

    subparsers.add_parser("_backend-child", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.command == "_backend-child":
        try:
            print(json.dumps(asyncio.run(_run_backend_child()), ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - 子进程仅返回稳定失败码
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error_code": "backend_probe_failed",
                        "exception": type(exc).__name__,
                    },
                    ensure_ascii=False,
                )
            )
            return EXIT_PROBE_FAILED
        return EXIT_PASSED

    try:
        if args.command == "check":
            document = load_document(args.input)
            report = check_document(document, scope=args.scope)
        else:
            document = probe_backend(
                root=args.root.resolve(),
                python=args.python,
                repeat=args.repeat,
            )
            if args.output is not None:
                _write_document(args.output, document)
            report = check_document(document, scope="backend")
    except PerformanceContractError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": "performance_input_invalid",
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_INVALID_INPUT
    except PerformanceProbeError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": "backend_probe_failed",
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return EXIT_PROBE_FAILED

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return EXIT_PASSED if report["status"] == "passed" else EXIT_BUDGET_EXCEEDED


if __name__ == "__main__":
    raise SystemExit(main())
