"""本地酒馆的可重现质量门编排器。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.data_guard import file_manifest, manifest_diff, manifest_digest  # noqa: E402


Executor = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class GateStep:
    name: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class StepResult:
    name: str
    command: tuple[str, ...]
    returncode: int
    duration_seconds: float


class GateFailure(RuntimeError):
    pass


class RealDataGuard:
    """确保质量门不会改动仓库中的真实数据、备份与日志。"""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.paths = tuple(self.root / name for name in ("data", "backups", "logs"))
        self.before: dict[Path, dict[str, str]] = {}

    def capture(self) -> dict[str, dict[str, object]]:
        self.before = {path: file_manifest(path) for path in self.paths}
        return self.summary(self.before)

    @staticmethod
    def summary(manifests: dict[Path, dict[str, str]]) -> dict[str, dict[str, object]]:
        return {
            path.name: {
                "files": len(manifest),
                "sha256": manifest_digest(manifest),
            }
            for path, manifest in manifests.items()
        }

    def verify(self) -> dict[str, dict[str, object]]:
        if not self.before:
            raise GateFailure("真实数据清单尚未捕获")
        after = {path: file_manifest(path) for path in self.paths}
        changes: dict[str, dict[str, list[str]]] = {}
        for path in self.paths:
            added, removed, changed = manifest_diff(self.before[path], after[path])
            if added or removed or changed:
                changes[path.name] = {
                    "added": added,
                    "removed": removed,
                    "changed": changed,
                }
        if changes:
            raise GateFailure(f"质量门改动了真实数据：{json.dumps(changes, ensure_ascii=False)}")
        return self.summary(after)


def javascript_files(root: Path) -> list[Path]:
    web_root = root / "web"
    files = [
            path
            for path in web_root.rglob("*")
            if path.is_file() and path.suffix.casefold() in {".js", ".mjs"}
    ]
    files.extend(
        path
        for path in (root / "tests").glob("*.mjs")
        if path.is_file()
    )
    return sorted(
        files,
        key=lambda item: item.as_posix(),
    )


def node_test_files(root: Path) -> list[Path]:
    return sorted(
        (path for path in (root / "tests").glob("*.test.js") if path.is_file()),
        key=lambda item: item.as_posix(),
    )


def relative_arguments(root: Path, paths: Sequence[Path]) -> tuple[str, ...]:
    return tuple(path.relative_to(root).as_posix() for path in paths)


def build_steps(mode: str, root: Path, python: str, node: str, npm: str | None) -> list[GateStep]:
    if mode not in {"preflight", "release"}:
        raise ValueError(f"未知质量门模式：{mode}")
    js_files = relative_arguments(root, javascript_files(root))
    node_tests = relative_arguments(root, node_test_files(root))
    if not js_files:
        raise GateFailure("web 目录中没有 JavaScript 文件")
    if not node_tests:
        raise GateFailure("tests 目录中没有 Node 测试")

    steps = [
        GateStep(
            "dependency-locks",
            (
                python,
                "tools/dependency_locks.py",
                *(('--semantic-only',) if mode == "preflight" else ('--environment', 'dev')),
            ),
        ),
        GateStep("pip-check", (python, "-m", "pip", "check")),
    ]
    if mode == "release":
        steps.append(GateStep("ruff", (python, "-m", "ruff", "check", ".")))
    steps.append(GateStep(
        "python-compile",
        (
            python, "-m", "compileall", "-q",
            "core", "routes", "tests", "tools", "server.py", "verify_regression.py",
        ),
    ))
    steps.extend(
        GateStep(f"javascript-syntax:{filename}", (node, "--check", filename))
        for filename in js_files
    )
    if mode == "release":
        steps.extend([
            GateStep("coverage-erase", (python, "-m", "coverage", "erase")),
            GateStep("python-tests-coverage", (python, "-m", "coverage", "run", "--branch", "-m", "pytest", "-q")),
            GateStep("coverage-report", (python, "-m", "coverage", "report")),
        ])
    else:
        steps.append(GateStep("python-tests", (python, "-m", "pytest", "-q")))
    steps.append(GateStep("node-tests", (node, "--test", *node_tests)))
    if mode == "release":
        if npm is None:
            raise GateFailure("发布质量门需要 npm")
        steps.extend([
            GateStep("node-dependencies", (npm, "ls", "--depth=0", "--json")),
            GateStep("browser-e2e-axe", (node, "tests/browser_e2e.mjs")),
        ])
    steps.append(GateStep("diff-check", ("git", "diff", "--check")))
    return steps


class GateRunner:
    def __init__(
        self,
        root: Path,
        *,
        executor: Executor = subprocess.run,
        environment: dict[str, str] | None = None,
    ):
        self.root = root.resolve()
        self.executor = executor
        self.environment = dict(os.environ if environment is None else environment)

    def run(self, steps: Sequence[GateStep], *, keep_going: bool = False) -> list[StepResult]:
        results: list[StepResult] = []
        for index, step in enumerate(steps, start=1):
            print(f"[gate {index}/{len(steps)}] {step.name}", flush=True)
            started = time.monotonic()
            completed = self.executor(
                list(step.command),
                cwd=self.root,
                env=self.environment,
                text=True,
            )
            result = StepResult(
                name=step.name,
                command=step.command,
                returncode=completed.returncode,
                duration_seconds=round(time.monotonic() - started, 3),
            )
            results.append(result)
            if completed.returncode != 0 and not keep_going:
                break
        return results


def _executable(value: str, label: str) -> str:
    candidate = Path(value)
    if candidate.is_file():
        return str(candidate.resolve())
    found = shutil.which(value)
    if found:
        return found
    raise GateFailure(f"未找到 {label}：{value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行本地酒馆质量门")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true", help="运行无新增依赖的离线预检")
    mode.add_argument("--release", action="store_true", help="运行完整发布质量门")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--node", default="node")
    parser.add_argument("--npm", default="npm")
    parser.add_argument("--keep-going", action="store_true")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    selected_mode = "release" if args.release else "preflight"
    guard = RealDataGuard(root)
    try:
        python = _executable(args.python, "Python")
        node = _executable(args.node, "Node.js")
        npm = _executable(args.npm, "npm") if selected_mode == "release" else shutil.which(args.npm)
        steps = build_steps(selected_mode, root, python, node, npm)
        before = guard.capture()
        environment = dict(os.environ)
        environment.update({
            "PYTHONUTF8": "1",
            "TAVERN_TEST_PYTHON": python,
        })
        results = GateRunner(root, environment=environment).run(
            steps,
            keep_going=args.keep_going,
        )
        after = guard.verify()
    except GateFailure as error:
        print(json.dumps({
            "status": "failed",
            "mode": selected_mode,
            "release_ready": False,
            "error": str(error),
        }, ensure_ascii=False, indent=2))
        return 1

    passed = len(results) == len(steps) and all(result.returncode == 0 for result in results)
    print(json.dumps({
        "status": "passed" if passed else "failed",
        "mode": selected_mode,
        "release_ready": passed and selected_mode == "release",
        "release_only_checks_skipped": selected_mode == "preflight",
        "real_data_before": before,
        "real_data_after": after,
        "steps": [
            {
                "name": result.name,
                "returncode": result.returncode,
                "duration_seconds": result.duration_seconds,
            }
            for result in results
        ],
    }, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
