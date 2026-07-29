from __future__ import annotations

import re
from pathlib import Path

import pytest

from core import config
from core.runtime_validation import (
    RUNTIME_LOCK_PATH,
    RuntimeValidationError,
    assert_runtime,
    validate_runtime,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PATH_ENV_NAMES = (
    "TAVERN_BASE_DIR",
    "TAVERN_DATA_DIR",
    "TAVERN_SETTINGS_PATH",
    "TAVERN_PROJECTS_DIR",
    "TAVERN_WEB_DIR",
    "TAVERN_PROMPTS_DIR",
    "TAVERN_RECOVERY_DIR",
    "TAVERN_MIGRATIONS_DIR",
    "TAVERN_BACKUP_DIR",
    "TAVERN_LOG_DIR",
    "TAVERN_PID_PATH",
    "TAVERN_STOP_REQUEST_PATH",
    "TAVERN_LOG_FILE",
)


def _active_requirements(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            continue
        name, separator, version = line.partition("==")
        assert separator == "==", line
        assert re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._-]+\])?",
            name,
        ), line
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]*", version), line
        result[re.sub(r"[-_.]+", "-", name).casefold()] = version
    return result


def test_real_runtime_lock_matches_the_verified_venv():
    result = validate_runtime()
    assert result == {
        "ok": True,
        "python": "3.12",
        "packages_checked": 22,
    }


def test_runtime_lock_is_the_complete_verified_windows_closure():
    assert RUNTIME_LOCK_PATH == REPO_ROOT / "requirements.lock.txt"
    assert _active_requirements(RUNTIME_LOCK_PATH) == {
        "annotated-types": "0.7.0",
        "anyio": "4.14.1",
        "certifi": "2026.6.17",
        "click": "8.4.2",
        "colorama": "0.4.6",
        "fastapi": "0.115.0",
        "h11": "0.16.0",
        "httpcore": "1.0.9",
        "httptools": "0.8.0",
        "httpx": "0.27.0",
        "idna": "3.18",
        "pydantic": "2.9.0",
        "pydantic-core": "2.23.2",
        "python-dotenv": "1.2.2",
        "pyyaml": "6.0.1",
        "sniffio": "1.3.1",
        "starlette": "0.38.6",
        "typing-extensions": "4.16.0",
        "tzdata": "2026.3",
        "uvicorn": "0.32.0",
        "watchfiles": "1.2.0",
        "websockets": "16.1",
    }


def test_direct_runtime_versions_remain_at_the_verified_baseline():
    assert _active_requirements(REPO_ROOT / "requirements.txt") == {
        "fastapi": "0.115.0",
        "uvicorn[standard]": "0.32.0",
        "httpx": "0.27.0",
        "pyyaml": "6.0.1",
        "pydantic": "2.9.0",
    }


def test_development_lock_uses_only_exact_verified_entries():
    direct = _active_requirements(REPO_ROOT / "requirements-dev.txt")
    locked = _active_requirements(REPO_ROOT / "requirements-dev.lock.txt")
    assert direct == {
        "coverage": "7.15.2",
        "pytest": "8.3.5",
        "pytest-asyncio": "0.25.3",
        "ruff": "0.15.22",
    }
    assert direct.items() <= locked.items()
    assert len(locked) == 8
    assert {"iniconfig", "packaging", "pluggy", "colorama"} <= locked.keys()


def test_setup_is_explicit_python_312_venv_install_from_runtime_hash_lock():
    source = (REPO_ROOT / "setup.bat").read_text(encoding="utf-8")
    assert "%~dp0" in source
    assert "py -3.12" in source
    assert "-m venv" in source
    assert '"%~dp0.venv\\Scripts\\python.exe"' in source
    assert '--requirement "%~dp0requirements.hashes.txt"' in source
    assert "--require-hashes" in source
    assert "--only-binary=:all:" in source
    assert "from core.runtime_validation import assert_runtime" in source
    assert "uvicorn server:app" not in source
    assert (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.12"


def test_validator_success_uses_canonical_names(tmp_path):
    lock = tmp_path / "runtime.lock"
    lock.write_text("Demo_Pkg==1.2.3\nsecond.pkg==4.5.6\n", encoding="utf-8")
    installed = {"demo-pkg": "1.2.3", "second-pkg": "4.5.6"}
    result = validate_runtime(
        lock,
        version_info=(3, 12, 99),
        version_getter=installed.__getitem__,
    )
    assert result == {"ok": True, "python": "3.12", "packages_checked": 2}


def test_wrong_python_short_circuits_before_lock_or_package_access(tmp_path):
    touched = False

    def lookup(_name: str) -> str:
        nonlocal touched
        touched = True
        raise AssertionError

    result = validate_runtime(
        tmp_path / "does-not-exist",
        version_info=(3, 13, 0),
        version_getter=lookup,
    )
    assert result["code"] == "python_version"
    assert result["expected"] == "3.12"
    assert result["actual"] == "3.13"
    assert touched is False


@pytest.mark.parametrize(
    "content",
    (
        "",
        "package>=1.0\n",
        "package\n",
        "package[extra]==1.0\n",
        "package ==1.0\n",
        "package==1.0 # inline\n",
        "package==1.0; python_version > '3'\n",
        "package==1.0\nPackage==1.0\n",
        "package==1.0==2.0\n",
    ),
)
def test_invalid_or_non_exact_lock_is_rejected_without_echoing_content(tmp_path, content):
    lock = tmp_path / "private-directory" / "runtime.lock"
    lock.parent.mkdir()
    lock.write_text(content, encoding="utf-8")
    result = validate_runtime(lock, version_info=(3, 12))
    assert result["ok"] is False
    assert result["code"] == "lock_invalid"
    assert str(lock.parent) not in str(result)
    if content.strip():
        assert content.strip() not in str(result)


def test_unreadable_lock_result_does_not_disclose_path(tmp_path):
    secret_path = tmp_path / "secret-token" / "runtime.lock"
    result = validate_runtime(secret_path, version_info=(3, 12))
    assert result == {
        "ok": False,
        "code": "lock_invalid",
        "message": "运行时依赖锁不可读取",
    }
    assert str(secret_path) not in str(result)


def test_missing_package_and_lookup_exception_are_sanitized(tmp_path):
    lock = tmp_path / "runtime.lock"
    lock.write_text("safe-package==1.0\n", encoding="utf-8")

    def lookup(_name: str) -> str:
        raise RuntimeError(r"C:\private\api-key.txt")

    result = validate_runtime(
        lock,
        version_info=(3, 12),
        version_getter=lookup,
    )
    assert result == {
        "ok": False,
        "code": "package_missing",
        "message": "运行时依赖缺失",
        "packages": ["safe-package"],
    }
    assert "private" not in str(result)
    assert "api-key" not in str(result)


def test_version_mismatch_reports_only_safe_versions(tmp_path):
    lock = tmp_path / "runtime.lock"
    lock.write_text("alpha==1.0\nbeta==2.0\n", encoding="utf-8")
    installed = {"alpha": "1.1", "beta": r"C:\private\token"}
    result = validate_runtime(
        lock,
        version_info=(3, 12),
        version_getter=installed.__getitem__,
    )
    assert result == {
        "ok": False,
        "code": "package_version",
        "message": "运行时依赖版本与锁文件不一致",
        "packages": [
            {"name": "alpha", "expected": "1.0", "actual": "1.1"},
            {"name": "beta", "expected": "2.0", "actual": "unknown"},
        ],
    }
    assert "private" not in str(result)


def test_assert_runtime_raises_stable_sanitized_error(tmp_path):
    with pytest.raises(RuntimeValidationError) as captured:
        assert_runtime(tmp_path / "secret" / "missing.lock", version_info=(3, 12))
    assert captured.value.result["code"] == "lock_invalid"
    assert "secret" not in str(captured.value)


@pytest.mark.parametrize("env_name", PATH_ENV_NAMES)
@pytest.mark.parametrize("value", ("", "   ", "relative/path", r"..\outside"))
def test_all_path_overrides_reject_empty_and_relative_values(
    monkeypatch,
    tmp_path,
    env_name,
    value,
):
    monkeypatch.setenv(env_name, value)
    with pytest.raises(ValueError, match="不能为空|必须是绝对路径"):
        config._configured_path(env_name, tmp_path / "fallback")


def test_absolute_path_override_is_normalized(monkeypatch, tmp_path):
    configured = tmp_path / "nested" / ".." / "target"
    monkeypatch.setenv("TAVERN_TEST_PATH", str(configured))
    assert config._configured_path(
        "TAVERN_TEST_PATH",
        tmp_path / "fallback",
    ) == (tmp_path / "target").resolve(strict=False)


@pytest.mark.parametrize(
    "value",
    (
        "localhost:11434",
        "ftp://localhost:11434",
        "http://",
        "http://user:password@localhost:11434",
        "http://localhost:11434/api/tags",
        "http://localhost:11434?token=secret",
        "http://localhost:11434#fragment",
        "http://localhost:99999",
        "http://local host:11434",
    ),
)
def test_ollama_url_rejects_invalid_roots(monkeypatch, value):
    monkeypatch.setenv("TAVERN_TEST_OLLAMA", value)
    with pytest.raises(ValueError, match=r"有效的 HTTP\(S\) 根地址"):
        config._configured_http_url("TAVERN_TEST_OLLAMA", "http://localhost:11434")


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("http://localhost:11434/", "http://localhost:11434"),
        ("https://127.0.0.1", "https://127.0.0.1"),
        ("http://[::1]:11434", "http://[::1]:11434"),
    ),
)
def test_ollama_url_accepts_valid_http_roots(monkeypatch, value, expected):
    monkeypatch.setenv("TAVERN_TEST_OLLAMA", value)
    assert config._configured_http_url(
        "TAVERN_TEST_OLLAMA",
        "http://localhost:11434",
    ) == expected


@pytest.mark.parametrize("host", ("localhost", "127.0.0.1", "127.1.2.3", "::1"))
def test_loopback_host_needs_no_remote_opt_in(monkeypatch, host):
    monkeypatch.setenv("TAVERN_TEST_HOST", host)
    assert config._configured_host(
        "TAVERN_TEST_HOST",
        "127.0.0.1",
    ) == host


@pytest.mark.parametrize("host", ("0.0.0.0", "::", "192.168.1.20", "tavern.lan"))
def test_non_loopback_host_is_always_rejected(monkeypatch, host):
    monkeypatch.setenv("TAVERN_TEST_HOST", host)
    with pytest.raises(ValueError, match="仅支持本机回环地址"):
        config._configured_host(
            "TAVERN_TEST_HOST",
            "127.0.0.1",
        )


@pytest.mark.parametrize(
    "host",
    ("", "bad host", "http://localhost", "bad/name", "-invalid.local", "invalid..local"),
)
def test_invalid_server_host_is_rejected(monkeypatch, host):
    monkeypatch.setenv("TAVERN_TEST_HOST", host)
    with pytest.raises(ValueError, match="不能为空|主机名或 IP 地址"):
        config._configured_host(
            "TAVERN_TEST_HOST",
            "127.0.0.1",
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    (("1", True), ("true", True), ("YES", True), ("0", False), ("off", False)),
)
def test_boolean_config_is_explicit(monkeypatch, raw, expected):
    monkeypatch.setenv("TAVERN_TEST_BOOL", raw)
    assert config._configured_bool("TAVERN_TEST_BOOL", False) is expected


@pytest.mark.parametrize("raw", ("", "enabled", "2", "null"))
def test_invalid_boolean_config_is_rejected(monkeypatch, raw):
    monkeypatch.setenv("TAVERN_TEST_BOOL", raw)
    with pytest.raises(ValueError, match="true/false"):
        config._configured_bool("TAVERN_TEST_BOOL", False)


@pytest.mark.parametrize("raw", ("", "-1", "1.5", "true", " 2x "))
def test_integer_config_rejects_non_decimal_text(monkeypatch, raw):
    monkeypatch.setenv("TAVERN_TEST_INT", raw)
    with pytest.raises(ValueError, match="非负整数"):
        config._configured_int("TAVERN_TEST_INT", 10, minimum=1, maximum=20)


@pytest.mark.parametrize("raw", ("0", "21"))
def test_integer_config_rejects_out_of_range_values(monkeypatch, raw):
    monkeypatch.setenv("TAVERN_TEST_INT", raw)
    with pytest.raises(ValueError, match="必须在"):
        config._configured_int("TAVERN_TEST_INT", 10, minimum=1, maximum=20)
