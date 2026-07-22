from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable).resolve()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_live(base_url: str, process: subprocess.Popen, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=0.5) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"隔离启动器提前退出，code={process.returncode}")
            try:
                response = client.get(f"{base_url}/health/live")
            except httpx.RequestError:
                time.sleep(0.1)
                continue
            if response.status_code == 200 and response.json() == {
                "status": "alive",
                "service": "local-tavern",
                "contract_version": 1,
            }:
                return response.json()
            time.sleep(0.1)
    raise AssertionError("隔离启动器未在时限内返回精确 live 标记")


def test_isolated_hidden_launcher_health_pid_and_graceful_stop(tmp_path: Path):
    """真实启动子进程，但所有可写路径与 Ollama 都严格隔离。"""
    port = _free_loopback_port()
    data_dir = tmp_path / "data"
    backups_dir = tmp_path / "backups"
    logs_dir = tmp_path / "logs"
    runtime_dir = tmp_path / "runtime"
    for directory in (data_dir, backups_dir, logs_dir, runtime_dir):
        directory.mkdir(parents=True)

    pid_path = runtime_dir / "tavern.pid"
    stop_path = runtime_dir / "tavern.stop.pid"
    log_path = logs_dir / "tavern.log"
    env = os.environ.copy()
    env.update({
        "PYTHONUTF8": "1",
        "TAVERN_BASE_DIR": str(REPO_ROOT),
        "TAVERN_DATA_DIR": str(data_dir),
        "TAVERN_SETTINGS_PATH": str(data_dir / "settings.json"),
        "TAVERN_PROJECTS_DIR": str(data_dir / "projects"),
        "TAVERN_RECOVERY_DIR": str(data_dir / ".recovery"),
        "TAVERN_MIGRATIONS_DIR": str(data_dir / ".migrations"),
        "TAVERN_BACKUP_DIR": str(backups_dir),
        "TAVERN_LOG_DIR": str(logs_dir),
        "TAVERN_LOG_FILE": str(log_path),
        "TAVERN_PID_PATH": str(pid_path),
        "TAVERN_STOP_REQUEST_PATH": str(stop_path),
        "TAVERN_WEB_DIR": str(REPO_ROOT / "web"),
        "TAVERN_HOST": "127.0.0.1",
        "TAVERN_PORT": str(port),
        "TAVERN_ALLOW_REMOTE": "false",
        "TAVERN_OLLAMA_HOST": "http://127.0.0.1:1",
        "TAVERN_OLLAMA_HEALTH_TIMEOUT_MS": "250",
        "TAVERN_BACKUP_SCHEDULE_ENABLED": "false",
    })
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen(
        [
            str(PYTHON),
            "-X",
            "utf8",
            "-m",
            "core.launcher",
            "serve",
            "--hidden",
            "--workers",
            "1",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    try:
        base_url = f"http://127.0.0.1:{port}"
        assert _wait_for_live(base_url, process)["status"] == "alive"
        with httpx.Client(timeout=2) as client:
            ready = client.get(f"{base_url}/health/ready")
            root = client.get(f"{base_url}/")
        assert ready.status_code == 503
        assert ready.json()["status"] == "not_ready"
        assert ready.json()["checks"]["ollama"]["status"] == "error"
        assert root.status_code == 200

        metadata = json.loads(pid_path.read_text(encoding="utf-8"))
        # Windows venv redirector can be a waiting parent process; PID 文件必须记录
        # 实际监听者，下面的 process_guard 会再以命令、项目根和端口三重验证。
        assert type(metadata["pid"]) is int and metadata["pid"] > 0
        assert metadata["port"] == port

        stopped = subprocess.run(
            [
                str(PYTHON),
                "-X",
                "utf8",
                "-m",
                "core.process_guard",
                "stop",
                "--timeout",
                "8",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            creationflags=creationflags,
        )
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        process_exit = process.wait(timeout=15)
        expected_exits = {0, 0xC000013A} if sys.platform == "win32" else {0}
        assert process_exit in expected_exits
        assert not pid_path.exists()
        assert not stop_path.exists()
        assert "lifespan shutdown" in log_path.read_text(encoding="utf-8")
    finally:
        if pid_path.exists():
            subprocess.run(
                [
                    str(PYTHON),
                    "-X",
                    "utf8",
                    "-m",
                    "core.process_guard",
                    "stop",
                    "--timeout",
                    "2",
                ],
                cwd=REPO_ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                creationflags=creationflags,
            )
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
