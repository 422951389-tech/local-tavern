from __future__ import annotations

from pathlib import Path

import pytest

from tools.dependency_locks import (
    LockValidationError,
    parse_hash_lock,
    parse_semantic_lock,
    verify_environment,
    verify_lock_overlap,
    verify_lock_pair,
)


ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_repository_semantic_locks_are_exact_sorted_and_compatible():
    runtime = parse_semantic_lock(ROOT / "requirements.lock.txt")
    dev = parse_semantic_lock(ROOT / "requirements-dev.lock.txt")
    desktop = parse_semantic_lock(ROOT / "requirements-desktop.lock.txt")
    verify_lock_overlap(runtime, dev)
    verify_lock_overlap(runtime, desktop)
    verify_lock_overlap(dev, desktop)
    assert len(runtime) == 22
    assert len(dev) == 8
    assert len(desktop) == 33
    assert {"coverage", "pytest", "pytest-asyncio", "ruff"} <= set(dev)
    assert set(runtime) <= set(desktop)
    assert {
        "pyinstaller",
        "pyinstaller-hooks-contrib",
        "pyside6",
        "pyside6-addons",
        "pyside6-essentials",
        "shiboken6",
    } <= set(desktop)
    assert all(desktop[name] == version for name, version in runtime.items())


def test_repository_desktop_semantic_and_hash_locks_are_identical():
    semantic = ROOT / "requirements-desktop.lock.txt"
    hashed = ROOT / "requirements-desktop.hashes.txt"
    locked = verify_lock_pair(semantic, hashed)
    assert locked == parse_semantic_lock(semantic)

    input_lines = [
        line.strip()
        for line in (ROOT / "requirements-desktop.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert input_lines[0] == "-r requirements.txt"
    assert input_lines[1:] == ["pyinstaller==6.21.0", "PySide6==6.11.1"]


def test_hash_lock_requires_sorted_unique_sha256_and_matches_semantic(tmp_path: Path):
    semantic = _write(tmp_path / "semantic.txt", "alpha==1.0\nbeta-core==2.0\n")
    hashed = _write(
        tmp_path / "hashed.txt",
        "alpha==1.0 \\\n"
        "    --hash=sha256:" + "1" * 64 + "\n"
        "beta-core==2.0 \\\n"
        "    --hash=sha256:" + "2" * 64 + " \\\n"
        "    --hash=sha256:" + "3" * 64 + "\n",
    )
    assert verify_lock_pair(semantic, hashed) == {
        "alpha": "1.0",
        "beta-core": "2.0",
    }

    duplicate = _write(
        tmp_path / "duplicate.txt",
        "alpha==1.0 --hash=sha256:" + "1" * 64
        + " --hash=sha256:" + "1" * 64 + "\n",
    )
    with pytest.raises(LockValidationError, match="唯一且排序"):
        parse_hash_lock(duplicate)


@pytest.mark.parametrize(
    "content, message",
    [
        ("alpha>=1.0\n", "精确版本"),
        ("alpha==1.0 --hash=sha512:" + "1" * 64 + "\n", "SHA-256"),
        ("alpha==1.0\n", "缺少 SHA-256"),
        ("beta==1.0 --hash=sha256:" + "2" * 64 + "\nalpha==1.0 --hash=sha256:" + "1" * 64 + "\n", "排序"),
    ],
)
def test_invalid_hash_or_order_is_rejected(tmp_path: Path, content: str, message: str):
    path = _write(tmp_path / "lock.txt", content)
    with pytest.raises(LockValidationError, match=message):
        parse_hash_lock(path)


def test_lock_pair_reports_missing_extra_and_version_mismatch(tmp_path: Path):
    semantic = _write(tmp_path / "semantic.txt", "alpha==1.0\nbeta==2.0\n")
    hashed = _write(
        tmp_path / "hashed.txt",
        "alpha==9.0 --hash=sha256:" + "1" * 64 + "\n"
        "gamma==3.0 --hash=sha256:" + "3" * 64 + "\n",
    )
    with pytest.raises(LockValidationError, match="缺失=.*beta.*多余=.*gamma.*版本不符=.*alpha"):
        verify_lock_pair(semantic, hashed)


def test_environment_requires_exact_versions_and_rejects_unlocked_extras():
    expected = {"alpha": "1.0", "beta-core": "2.0"}
    assert verify_environment(
        expected,
        installed={"alpha": "1.0", "beta_core": "2.0", "pip": "25.0"},
    )["packages_checked"] == 2

    with pytest.raises(LockValidationError, match="版本不符=.*alpha.*多余=.*secret"):
        verify_environment(
            expected,
            installed={"alpha": "9.0", "beta-core": "2.0", "secret": "1"},
        )
