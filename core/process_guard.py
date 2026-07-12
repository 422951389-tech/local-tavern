"""本项目进程元数据、优雅停止请求与精确 PID 兜底。"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from ctypes import wintypes
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from core.config import BASE_DIR


logger = logging.getLogger(__name__)

PORT = 8765
PID_PATH = BASE_DIR / "tavern.pid"
STOP_REQUEST_PATH = BASE_DIR / "tavern.stop.pid"
STOP_TIMEOUT_SECONDS = 8.0


def _read_json(path: Path) -> dict | None:
    try:
        if path.stat().st_size > 16 * 1024:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _pid_is_running(pid: int) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _process_command_line(pid: int) -> str:
    if os.name == "nt":
        script = (
            f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\"; "
            "if ($p) { [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); $p.CommandLine }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        return result.stdout.strip()
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def _listening_pids(port: int) -> set[int]:
    result = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
        check=False,
    )
    listeners: set[int] = set()
    for line in result.stdout.splitlines():
        columns = line.split()
        if len(columns) < 5 or columns[0].upper() != "TCP":
            continue
        if columns[3].upper() != "LISTENING":
            continue
        local_address = columns[1].rsplit(":", 1)
        if len(local_address) != 2 or local_address[1] != str(port):
            continue
        try:
            listeners.add(int(columns[4]))
        except ValueError:
            continue
    return listeners


def _command_matches_tavern(command_line: str) -> bool:
    command = command_line.lower().replace("\\", "/")
    return ("uvicorn" in command and "server:app" in command) or command.endswith("server.py") or " server.py " in command


def metadata_matches_process(
    metadata: dict,
    *,
    command_line: str,
    listening_pids: set[int],
    project_root: Path = BASE_DIR,
) -> bool:
    pid = metadata.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if metadata.get("port") != PORT:
        return False
    try:
        recorded_root = Path(metadata.get("project_root", "")).resolve(strict=False)
        expected_root = Path(project_root).resolve(strict=False)
    except (OSError, TypeError, ValueError):
        return False
    return (
        recorded_root == expected_root
        and _command_matches_tavern(command_line)
        and pid in listening_pids
    )


def claim_pid_file(path: Path = PID_PATH) -> dict:
    """在 lifespan startup 独占 PID 文件；存活的旧 PID 会阻止第二实例。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = _read_json(path)
        existing_pid = existing.get("pid") if existing else None
        if isinstance(existing_pid, int) and _pid_is_running(existing_pid):
            raise RuntimeError(f"检测到仍在运行的本地酒馆 PID {existing_pid}")
        path.unlink(missing_ok=True)

    metadata = {
        "pid": os.getpid(),
        "token": secrets.token_hex(16),
        "command": subprocess.list2cmdline(sys.argv),
        "working_directory": str(Path.cwd().resolve(strict=False)),
        "project_root": str(BASE_DIR.resolve(strict=False)),
        "port": PORT,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2)
    except FileExistsError as exc:
        raise RuntimeError("PID 文件被另一个启动进程占用") from exc
    return metadata


def release_pid_file(
    metadata: dict,
    path: Path = PID_PATH,
    stop_path: Path = STOP_REQUEST_PATH,
) -> None:
    current = _read_json(Path(path))
    if current and current.get("pid") == metadata.get("pid") and current.get("token") == metadata.get("token"):
        Path(path).unlink(missing_ok=True)
    request = _read_json(Path(stop_path))
    if request and request.get("pid") == metadata.get("pid") and request.get("token") == metadata.get("token"):
        Path(stop_path).unlink(missing_ok=True)


def write_stop_request(metadata: dict, path: Path = STOP_REQUEST_PATH) -> None:
    request = {
        "pid": metadata["pid"],
        "token": metadata["token"],
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    target = Path(path)
    target.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")


async def wait_for_stop_request(
    metadata: dict,
    *,
    path: Path = STOP_REQUEST_PATH,
    poll_seconds: float = 0.25,
    on_stop: Callable[[], None] | None = None,
) -> None:
    """等待匹配 token 的停止请求，再向 uvicorn 主循环发送 SIGINT。"""
    request_path = Path(path)
    while True:
        request = _read_json(request_path) if request_path.exists() else None
        if request and request.get("pid") == metadata.get("pid") and request.get("token") == metadata.get("token"):
            request_path.unlink(missing_ok=True)
            logger.info("收到本项目停止请求，开始优雅关闭 PID %s", metadata.get("pid"))
            (on_stop or (lambda: signal.raise_signal(signal.SIGINT)))()
            return
        await asyncio.sleep(poll_seconds)


def _force_stop_exact_pid(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return True
    except OSError:
        return False


def stop_local_tavern(
    *,
    pid_path: Path = PID_PATH,
    stop_path: Path = STOP_REQUEST_PATH,
    timeout_seconds: float = STOP_TIMEOUT_SECONDS,
) -> int:
    """只停止元数据、命令行和监听端口均匹配的本项目进程。"""
    pid_path = Path(pid_path)
    stop_path = Path(stop_path)
    if not pid_path.exists():
        print("[信息] 未找到本地酒馆 PID 文件，服务未由当前启动器运行。")
        return 0
    metadata = _read_json(pid_path)
    if not metadata:
        print("[拒绝] PID 文件损坏；未结束任何进程。")
        return 2
    pid = metadata.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        print("[拒绝] PID 文件内容无效；未结束任何进程。")
        return 2
    if not _pid_is_running(pid):
        pid_path.unlink(missing_ok=True)
        stop_path.unlink(missing_ok=True)
        print(f"[信息] PID {pid} 已退出，已清理过期运行文件。")
        return 0

    command_line = _process_command_line(pid)
    listeners = _listening_pids(PORT)
    if not metadata_matches_process(
        metadata,
        command_line=command_line,
        listening_pids=listeners,
    ):
        print(f"[拒绝] PID {pid} 与本项目命令/目录/端口不匹配；未结束任何进程。")
        return 3

    write_stop_request(metadata, stop_path)
    print(f"[停止] 已向本地酒馆 PID {pid} 发送优雅关闭请求。")
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            pid_path.unlink(missing_ok=True)
            stop_path.unlink(missing_ok=True)
            print("[完成] 本地酒馆服务已优雅停止。")
            return 0
        time.sleep(0.2)

    command_line = _process_command_line(pid)
    listeners = _listening_pids(PORT)
    if not metadata_matches_process(
        metadata,
        command_line=command_line,
        listening_pids=listeners,
    ):
        print(f"[拒绝] PID {pid} 在强制停止前已不再匹配；未强制结束。")
        return 4
    if not _force_stop_exact_pid(pid):
        print(f"[错误] 无法结束本地酒馆 PID {pid}。")
        return 5
    pid_path.unlink(missing_ok=True)
    stop_path.unlink(missing_ok=True)
    print(f"[完成] 优雅停止超时，已强制结束本项目 PID {pid}。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地酒馆进程守卫")
    subparsers = parser.add_subparsers(dest="command", required=True)
    stop_parser = subparsers.add_parser("stop", help="只停止匹配本项目的 PID")
    stop_parser.add_argument("--timeout", type=float, default=STOP_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    if args.command == "stop":
        return stop_local_tavern(timeout_seconds=args.timeout)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
