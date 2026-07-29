"""对冻结后的桌面程序执行真实窗口、桥接与无监听端口验收。"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import tempfile
import time
from ctypes import wintypes
from pathlib import Path


EXPECTED_RESULT = {
    "schema_version": 1,
    "ready": True,
    "page_loaded": True,
    "api_live": True,
    "api_status": 200,
    "runtime": "in_process_asgi",
    "transport": "qwebchannel",
    "scheme": "tavern://app",
    "tcp_listener_started": False,
    "off_the_record": True,
    "error": "",
}


class DesktopSmokeError(RuntimeError):
    pass


def _process_parent_map() -> dict[int, int]:
    if os.name != "nt":
        return {}

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot in {None, invalid_handle}:
        raise DesktopSmokeError("无法读取 Windows 进程树")
    result: dict[int, int] = {}
    try:
        entry = ProcessEntry32()
        entry.dwSize = ctypes.sizeof(ProcessEntry32)
        success = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while success:
            result[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            success = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if not result:
        raise DesktopSmokeError("Windows 进程树为空")
    return result


def _descendant_pids(root_pid: int) -> set[int]:
    parents = _process_parent_map()
    descendants: set[int] = set()
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        children = {
            pid
            for pid, parent_pid in parents.items()
            if parent_pid == parent and pid not in descendants
        }
        descendants.update(children)
        frontier.extend(children)
    return descendants


def _listening_pids() -> set[int]:
    completed = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise DesktopSmokeError("无法读取 Windows TCP 监听表")
    listeners: set[int] = set()
    for line in completed.stdout.splitlines():
        columns = line.split()
        if len(columns) < 5 or columns[0].upper() != "TCP":
            continue
        if columns[3].upper() != "LISTENING":
            continue
        try:
            listeners.add(int(columns[4]))
        except ValueError:
            continue
    return listeners


def _read_result(path: Path, process: subprocess.Popen[bytes], deadline: float) -> dict:
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                time.sleep(0.1)
                continue
            if isinstance(value, dict):
                return value
            raise DesktopSmokeError("桌面 smoke 结果不是对象")
        returncode = process.poll()
        if returncode is not None:
            raise DesktopSmokeError(f"桌面程序在写入 smoke 结果前退出：{returncode}")
        time.sleep(0.1)
    raise DesktopSmokeError("等待桌面 smoke 结果超时")


def _validate_result(result: dict, pid: int) -> None:
    if result.get("pid") != pid:
        raise DesktopSmokeError("桌面 smoke PID 与启动进程不一致")
    for key, expected in EXPECTED_RESULT.items():
        if result.get(key) != expected:
            raise DesktopSmokeError(
                f"桌面 smoke 字段不符合契约：{key}={result.get(key)!r}"
            )
    if set(result) != {"pid", *EXPECTED_RESULT}:
        raise DesktopSmokeError("桌面 smoke 结果包含未知或缺失字段")


def run_smoke(executable: Path, *, timeout_seconds: float, hold_ms: int) -> dict[str, object]:
    if os.name != "nt":
        raise DesktopSmokeError("桌面发行验收仅支持 Windows")
    executable = executable.resolve(strict=True)
    if executable.suffix.casefold() != ".exe":
        raise DesktopSmokeError("桌面验收目标必须是 .exe")

    with tempfile.TemporaryDirectory(prefix="local-tavern-desktop-smoke-") as raw_root:
        root = Path(raw_root).resolve()
        result_path = root / "smoke-result.json"
        # 禁止调用者残留的 TAVERN_* 覆盖测试根目录；否则 settings/projects 等
        # 未显式重设的路径仍会接触真实用户数据。
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("TAVERN_")
        }
        environment.update({
            "PYTHONUTF8": "1",
            "TAVERN_BASE_DIR": str(root),
            "TAVERN_DESKTOP_USER_ROOT": str(root),
            "TAVERN_DATA_DIR": str(root / "data"),
            "TAVERN_SETTINGS_PATH": str(root / "data" / "settings.json"),
            "TAVERN_PROJECTS_DIR": str(root / "data" / "projects"),
            "TAVERN_PROMPTS_DIR": str(root / "prompts"),
            "TAVERN_RECOVERY_DIR": str(root / "data" / ".recovery"),
            "TAVERN_MIGRATIONS_DIR": str(root / "data" / ".migrations"),
            "TAVERN_BACKUP_DIR": str(root / "backups"),
            "TAVERN_LOG_DIR": str(root / "logs"),
            "TAVERN_LOG_FILE": str(root / "logs" / "tavern.log"),
            "TAVERN_PID_PATH": str(root / "runtime" / "tavern.pid"),
            "TAVERN_STOP_REQUEST_PATH": str(root / "runtime" / "tavern.stop.pid"),
            "TAVERN_PROVIDER_DATA_DIR": str(root / "providers"),
            "TAVERN_PROVIDER_CONFIG_PATH": str(root / "providers" / "providers.json"),
            "TAVERN_PROVIDER_SECRETS_PATH": str(root / "providers" / "provider-secrets.json"),
            "TAVERN_OLLAMA_HOST": "http://127.0.0.1:1",
            "TAVERN_BACKUP_SCHEDULE_ENABLED": "false",
        })
        process = subprocess.Popen(
            [
                str(executable),
                "--smoke-test",
                str(result_path),
                "--smoke-hold-ms",
                str(hold_ms),
            ],
            cwd=executable.parent,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout_seconds
        try:
            result = _read_result(result_path, process, deadline)
            _validate_result(result, process.pid)
            samples: list[list[int]] = []
            observed_processes = {process.pid}
            for _index in range(3):
                owned_processes = {process.pid, *_descendant_pids(process.pid)}
                observed_processes.update(owned_processes)
                listeners = sorted(_listening_pids() & owned_processes)
                samples.append(listeners)
                if listeners:
                    raise DesktopSmokeError(
                        f"桌面进程树开启了 TCP 监听端口：PID {listeners}"
                    )
                time.sleep(0.25)
            remaining = max(0.1, deadline - time.monotonic())
            returncode = process.wait(timeout=remaining)
            if returncode != 0:
                raise DesktopSmokeError(f"桌面程序 smoke 退出码异常：{returncode}")
            shutdown_deadline = time.monotonic() + 5.0
            residual = sorted(observed_processes & set(_process_parent_map()))
            while residual and time.monotonic() < shutdown_deadline:
                time.sleep(0.1)
                residual = sorted(observed_processes & set(_process_parent_map()))
            if residual:
                raise DesktopSmokeError(f"桌面关闭后仍有残留进程：{residual}")
            return {
                "status": "passed",
                "pid": result["pid"],
                "runtime": result["runtime"],
                "transport": result["transport"],
                "scheme": result["scheme"],
                "tcp_listener_samples": samples,
                "process_exited": True,
            }
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验收本地酒馆独立桌面发行版")
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--hold-ms", type=int, default=4000)
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.hold_ms < 1000:
        print(json.dumps({"status": "failed", "error": "验收时间参数无效"}, ensure_ascii=False))
        return 2
    try:
        result = run_smoke(
            args.executable,
            timeout_seconds=args.timeout,
            hold_ms=args.hold_ms,
        )
    except (DesktopSmokeError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
