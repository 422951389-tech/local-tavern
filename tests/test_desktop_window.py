from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_qt_offscreen_recovery_layer_and_single_instance_release():
    desktop_python = ROOT / ".venv-desktop" / "Scripts" / "python.exe"
    assert desktop_python.is_file(), "Qt offscreen 验收需要 .venv-desktop"
    environment = dict(os.environ)
    environment.update({
        "PYTHONPATH": str(ROOT),
        "QT_QPA_PLATFORM": "offscreen",
        "QTWEBENGINE_DISABLE_SANDBOX": "1",
        "QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu --no-sandbox",
    })
    completed = subprocess.run(
        [str(desktop_python), str(ROOT / "tests" / "desktop_window_offscreen_probe.py")],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, completed.stderr
    result = json.loads(lines[-1])
    assert result == {
        "status": "passed",
        "manual_reload_requests": 1,
        "automatic_reload_incidents": 2,
        "automatic_reload_per_incident": 1,
        "runtime_restarts": 0,
        "single_instance_reacquired": True,
    }
