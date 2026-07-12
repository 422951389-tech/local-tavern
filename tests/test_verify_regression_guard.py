import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "verify_regression.py"


def _run_guarded(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=5,
        check=False,
    )


def test_default_invocation_refuses_before_live_access():
    result = _run_guarded()
    assert result.returncode == 2
    assert "--allow-live-data" in result.stdout + result.stderr


def test_live_switch_still_requires_explicit_targets():
    result = _run_guarded("--allow-live-data")
    assert result.returncode == 2
    output = result.stdout + result.stderr
    assert "--project" in output
    assert "--save" in output
    assert "--data-dir" in output
    assert "--model" in output
