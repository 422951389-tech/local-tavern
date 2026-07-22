"""校验语义依赖锁、制品哈希锁与隔离环境中的精确包集合。"""
from __future__ import annotations

import argparse
import re
from importlib import metadata
from pathlib import Path
from typing import Iterable, Mapping


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_LOCK = ROOT / "requirements.lock.txt"
RUNTIME_HASH_LOCK = ROOT / "requirements.hashes.txt"
DEV_LOCK = ROOT / "requirements-dev.lock.txt"
DEV_HASH_LOCK = ROOT / "requirements-dev.hashes.txt"

_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._+!-]*[A-Za-z0-9])?$")
_HASH_RE = re.compile(r"^--hash=sha256:([0-9a-f]{64})$")


class LockValidationError(ValueError):
    """依赖锁无法证明精确版本或制品身份。"""


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def _read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise LockValidationError(f"依赖锁不可读取：{Path(path).name}") from exc


def _exact_pin(value: str, *, line_number: int) -> tuple[str, str, str]:
    if value.count("==") != 1:
        raise LockValidationError(f"第 {line_number} 行必须使用单个精确版本 ==")
    name, version = value.split("==", 1)
    if (
        value != f"{name}=={version}"
        or not _NAME_RE.fullmatch(name)
        or not _VERSION_RE.fullmatch(version)
    ):
        raise LockValidationError(f"第 {line_number} 行包含无效依赖条目")
    return canonical_name(name), name, version


def _require_new_pin(
    packages: Mapping[str, object],
    ordered_names: list[str],
    canonical: str,
    *,
    line_number: int,
) -> None:
    if canonical in packages:
        raise LockValidationError(f"第 {line_number} 行包含重复包名")
    ordered_names.append(canonical)


def _require_sorted(ordered_names: list[str]) -> None:
    if ordered_names != sorted(ordered_names):
        raise LockValidationError("依赖锁必须按规范化包名排序")


def parse_semantic_lock(path: Path) -> dict[str, str]:
    packages: dict[str, str] = {}
    ordered_names: list[str] = []
    for line_number, raw_line in enumerate(_read_text(path).splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-", "--")) or "\\" in line:
            raise LockValidationError(f"第 {line_number} 行不是纯精确版本条目")
        canonical, _name, version = _exact_pin(line, line_number=line_number)
        _require_new_pin(
            packages,
            ordered_names,
            canonical,
            line_number=line_number,
        )
        packages[canonical] = version
    if not packages:
        raise LockValidationError("语义依赖锁不能为空")
    _require_sorted(ordered_names)
    return packages


def _logical_hash_lines(content: str) -> Iterable[tuple[int, str]]:
    start_line = 0
    parts: list[str] = []
    for line_number, raw_line in enumerate(content.splitlines(), 1):
        stripped = raw_line.strip()
        if not parts and (not stripped or stripped.startswith("#")):
            continue
        if not parts:
            start_line = line_number
        if not stripped or stripped.startswith("#"):
            raise LockValidationError(f"第 {line_number} 行中断了制品哈希条目")
        continued = stripped.endswith("\\")
        part = stripped[:-1].rstrip() if continued else stripped
        if not part:
            raise LockValidationError(f"第 {line_number} 行包含空续行")
        parts.append(part)
        if not continued:
            yield start_line, " ".join(parts)
            parts = []
    if parts:
        raise LockValidationError(f"第 {start_line} 行的制品哈希条目未结束")


def parse_hash_lock(path: Path) -> dict[str, tuple[str, tuple[str, ...]]]:
    packages: dict[str, tuple[str, tuple[str, ...]]] = {}
    ordered_names: list[str] = []
    for line_number, logical_line in _logical_hash_lines(_read_text(path)):
        tokens = logical_line.split()
        canonical, _name, version = _exact_pin(tokens[0], line_number=line_number)
        hashes: list[str] = []
        for token in tokens[1:]:
            match = _HASH_RE.fullmatch(token)
            if match is None:
                raise LockValidationError(f"第 {line_number} 行包含非 SHA-256 制品参数")
            hashes.append(match.group(1))
        if not hashes:
            raise LockValidationError(f"第 {line_number} 行缺少 SHA-256 制品哈希")
        if hashes != sorted(set(hashes)):
            raise LockValidationError(f"第 {line_number} 行的制品哈希必须唯一且排序")
        _require_new_pin(
            packages,
            ordered_names,
            canonical,
            line_number=line_number,
        )
        packages[canonical] = (version, tuple(hashes))
    if not packages:
        raise LockValidationError("制品哈希锁不能为空")
    _require_sorted(ordered_names)
    return packages


def verify_lock_pair(semantic_path: Path, hash_path: Path) -> dict[str, str]:
    semantic = parse_semantic_lock(semantic_path)
    hashed = parse_hash_lock(hash_path)
    hashed_versions = {name: value[0] for name, value in hashed.items()}
    if semantic != hashed_versions:
        missing = sorted(set(semantic) - set(hashed_versions))
        extra = sorted(set(hashed_versions) - set(semantic))
        mismatched = sorted(
            name
            for name in set(semantic) & set(hashed_versions)
            if semantic[name] != hashed_versions[name]
        )
        raise LockValidationError(
            "语义锁与制品哈希锁不一致"
            f"（缺失={missing}，多余={extra}，版本不符={mismatched}）"
        )
    return semantic


def verify_lock_overlap(runtime: Mapping[str, str], dev: Mapping[str, str]) -> None:
    conflicts = sorted(
        name
        for name in set(runtime) & set(dev)
        if runtime[name] != dev[name]
    )
    if conflicts:
        raise LockValidationError(f"运行锁与开发锁版本冲突：{conflicts}")


def installed_versions() -> dict[str, str]:
    installed: dict[str, str] = {}
    for distribution in metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        if not isinstance(raw_name, str) or not raw_name:
            continue
        name = canonical_name(raw_name)
        version = distribution.version
        previous = installed.get(name)
        if previous is not None and previous != version:
            raise LockValidationError(f"环境包含同名不同版本包：{name}")
        installed[name] = version
    return installed


def verify_environment(
    expected: Mapping[str, str],
    *,
    installed: Mapping[str, str] | None = None,
    allowed_extras: Iterable[str] = ("pip",),
) -> dict[str, object]:
    actual = {
        canonical_name(name): version
        for name, version in (installed_versions() if installed is None else installed).items()
    }
    allowed = {canonical_name(name) for name in allowed_extras}
    missing = sorted(set(expected) - set(actual))
    mismatched = sorted(
        name
        for name in set(expected) & set(actual)
        if expected[name] != actual[name]
    )
    extra = sorted(set(actual) - set(expected) - allowed)
    if missing or mismatched or extra:
        raise LockValidationError(
            "环境包集合与锁不一致"
            f"（缺失={missing}，版本不符={mismatched}，多余={extra}）"
        )
    return {
        "ok": True,
        "packages_checked": len(expected),
        "allowed_bootstrap_packages": sorted(set(actual) & allowed),
    }


def _paths(root: Path) -> tuple[Path, Path, Path, Path]:
    return (
        root / RUNTIME_LOCK.name,
        root / RUNTIME_HASH_LOCK.name,
        root / DEV_LOCK.name,
        root / DEV_HASH_LOCK.name,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验本地酒馆依赖锁与精确环境")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--semantic-only", action="store_true")
    parser.add_argument("--environment", choices=("runtime", "dev"))
    args = parser.parse_args(argv)

    runtime_lock, runtime_hash, dev_lock, dev_hash = _paths(args.root.resolve())
    try:
        if args.semantic_only:
            runtime = parse_semantic_lock(runtime_lock)
            dev = parse_semantic_lock(dev_lock)
        else:
            runtime = verify_lock_pair(runtime_lock, runtime_hash)
            dev = verify_lock_pair(dev_lock, dev_hash)
        verify_lock_overlap(runtime, dev)
        environment = None
        if args.environment:
            expected = runtime if args.environment == "runtime" else {**runtime, **dev}
            environment = verify_environment(expected)
    except LockValidationError as exc:
        print(f"[依赖锁失败] {exc}")
        return 1

    print({
        "ok": True,
        "runtime_packages": len(runtime),
        "dev_packages": len(dev),
        "hashes_checked": not args.semantic_only,
        "environment": environment,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
