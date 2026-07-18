"""启动前校验 Python 版本与精确运行时依赖锁。"""
from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path
from typing import Callable, Iterable


EXPECTED_PYTHON = (3, 12)
RUNTIME_LOCK_PATH = Path(__file__).resolve().parents[1] / "requirements.lock.txt"

_NAME_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$"
)
_VERSION_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._+!-]*[A-Za-z0-9])?$"
)


def _canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


class RuntimeValidationError(RuntimeError):
    """只携带稳定、脱敏的运行时校验结果。"""

    def __init__(self, result: dict):
        self.result = dict(result)
        super().__init__(str(self.result.get("message", "运行环境校验失败")))


def _read_runtime_lock(lock_path: Path) -> tuple[dict[str, str] | None, dict | None]:
    try:
        content = Path(lock_path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, {
            "ok": False,
            "code": "lock_invalid",
            "message": "运行时依赖锁不可读取",
        }

    packages: dict[str, str] = {}
    for line_number, raw_line in enumerate(content.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.count("==") != 1:
            return None, {
                "ok": False,
                "code": "lock_invalid",
                "message": "运行时依赖锁必须全部使用精确版本",
                "line": line_number,
            }
        name, version = line.split("==", 1)
        if (
            line != f"{name}=={version}"
            or not _NAME_RE.fullmatch(name)
            or not _VERSION_RE.fullmatch(version)
        ):
            return None, {
                "ok": False,
                "code": "lock_invalid",
                "message": "运行时依赖锁包含无效条目",
                "line": line_number,
            }
        canonical = _canonical_name(name)
        if canonical in packages:
            return None, {
                "ok": False,
                "code": "lock_invalid",
                "message": "运行时依赖锁包含重复包名",
                "line": line_number,
            }
        packages[canonical] = version

    if not packages:
        return None, {
            "ok": False,
            "code": "lock_invalid",
            "message": "运行时依赖锁不能为空",
        }
    return packages, None


def _major_minor(version_info: Iterable[int]) -> tuple[int, int] | None:
    try:
        values = tuple(version_info)
    except TypeError:
        return None
    if len(values) < 2 or type(values[0]) is not int or type(values[1]) is not int:
        return None
    return values[0], values[1]


def validate_runtime(
    lock_path: Path = RUNTIME_LOCK_PATH,
    *,
    version_info: Iterable[int] | None = None,
    version_getter: Callable[[str], str] | None = None,
) -> dict:
    """返回稳定校验结构；任何失败都不暴露路径或底层异常原文。"""
    actual_python = _major_minor(sys.version_info if version_info is None else version_info)
    if actual_python != EXPECTED_PYTHON:
        actual_text = (
            f"{actual_python[0]}.{actual_python[1]}"
            if actual_python is not None
            else "unknown"
        )
        return {
            "ok": False,
            "code": "python_version",
            "message": "运行环境必须使用 Python 3.12",
            "expected": "3.12",
            "actual": actual_text,
        }

    packages, error = _read_runtime_lock(Path(lock_path))
    if error is not None:
        return error
    assert packages is not None

    lookup = metadata.version if version_getter is None else version_getter
    missing: list[str] = []
    mismatched: list[dict[str, str]] = []
    for name, expected in sorted(packages.items()):
        try:
            actual = lookup(name)
        except Exception:
            missing.append(name)
            continue
        if not isinstance(actual, str) or actual != expected:
            safe_actual = (
                actual
                if isinstance(actual, str) and _VERSION_RE.fullmatch(actual)
                else "unknown"
            )
            mismatched.append({
                "name": name,
                "expected": expected,
                "actual": safe_actual,
            })

    if missing:
        return {
            "ok": False,
            "code": "package_missing",
            "message": "运行时依赖缺失",
            "packages": missing,
        }
    if mismatched:
        return {
            "ok": False,
            "code": "package_version",
            "message": "运行时依赖版本与锁文件不一致",
            "packages": mismatched,
        }
    return {
        "ok": True,
        "python": "3.12",
        "packages_checked": len(packages),
    }


def assert_runtime(
    lock_path: Path = RUNTIME_LOCK_PATH,
    *,
    version_info: Iterable[int] | None = None,
    version_getter: Callable[[str], str] | None = None,
) -> dict:
    """校验成功时返回结果，失败时抛出不含敏感细节的稳定异常。"""
    result = validate_runtime(
        lock_path,
        version_info=version_info,
        version_getter=version_getter,
    )
    if not result["ok"]:
        raise RuntimeValidationError(result)
    return result
