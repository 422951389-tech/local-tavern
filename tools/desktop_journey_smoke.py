"""对冻结后的桌面程序执行隔离、无网络的真实用户旅程验收。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop.journey import (  # noqa: E402
    JOURNEY_ENVIRONMENT_FLAG,
    JOURNEY_ROOT_PREFIX,
    JOURNEY_SCHEMA_VERSION,
)
from tools.desktop_smoke import (  # noqa: E402
    _descendant_pids,
    _listening_pids,
    _process_parent_map,
)


class DesktopJourneySmokeError(RuntimeError):
    pass


_PATH_ENVIRONMENTS = (
    "TAVERN_BASE_DIR",
    "TAVERN_DESKTOP_USER_ROOT",
    "TAVERN_DATA_DIR",
    "TAVERN_SETTINGS_PATH",
    "TAVERN_PROJECTS_DIR",
    "TAVERN_PROMPTS_DIR",
    "TAVERN_RECOVERY_DIR",
    "TAVERN_MIGRATIONS_DIR",
    "TAVERN_BACKUP_DIR",
    "TAVERN_LOG_DIR",
    "TAVERN_LOG_FILE",
    "TAVERN_PID_PATH",
    "TAVERN_STOP_REQUEST_PATH",
    "TAVERN_PROVIDER_DATA_DIR",
    "TAVERN_PROVIDER_CONFIG_PATH",
    "TAVERN_PROVIDER_SECRETS_PATH",
)

_RESULT_KEYS = {
    "schema_version",
    "pid",
    "stage",
    "ready",
    "page_loaded",
    "api_live",
    "project_created",
    "saves_created",
    "chat_completed",
    "refresh_verified",
    "save_switch_verified",
    "layout_verified",
    "scroll_verified",
    "scroll_frame_samples",
    "persistence_verified",
    "message_count",
    "screenshot_saved",
    "runtime",
    "transport",
    "scheme",
    "tcp_listener_started",
    "off_the_record",
    "desktop_renderer",
    "error",
}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _validate_isolated_root(root: Path) -> Path:
    isolated = Path(root).resolve(strict=True)
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    if (
        not isolated.is_dir()
        or not isolated.name.startswith(JOURNEY_ROOT_PREFIX)
        or not _is_within(isolated, temp_root)
        or isolated == temp_root
    ):
        raise DesktopJourneySmokeError("桌面旅程根目录不符合临时隔离契约")
    return isolated


def _build_isolated_environment(root: Path) -> dict[str, str]:
    isolated = _validate_isolated_root(root)
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("TAVERN_")
    }
    paths = {
        "TAVERN_BASE_DIR": isolated,
        "TAVERN_DESKTOP_USER_ROOT": isolated,
        "TAVERN_DATA_DIR": isolated / "data",
        "TAVERN_SETTINGS_PATH": isolated / "data" / "settings.json",
        "TAVERN_PROJECTS_DIR": isolated / "data" / "projects",
        "TAVERN_PROMPTS_DIR": isolated / "prompts",
        "TAVERN_RECOVERY_DIR": isolated / "data" / ".recovery",
        "TAVERN_MIGRATIONS_DIR": isolated / "data" / ".migrations",
        "TAVERN_BACKUP_DIR": isolated / "backups",
        "TAVERN_LOG_DIR": isolated / "logs",
        "TAVERN_LOG_FILE": isolated / "logs" / "tavern.log",
        "TAVERN_PID_PATH": isolated / "runtime" / "tavern.pid",
        "TAVERN_STOP_REQUEST_PATH": isolated / "runtime" / "tavern.stop.pid",
        "TAVERN_PROVIDER_DATA_DIR": isolated / "providers",
        "TAVERN_PROVIDER_CONFIG_PATH": isolated / "providers" / "providers.json",
        "TAVERN_PROVIDER_SECRETS_PATH": isolated
        / "providers"
        / "provider-secrets.json",
    }
    for name, path in paths.items():
        resolved = path.resolve(strict=False)
        if not _is_within(resolved, isolated):
            raise DesktopJourneySmokeError(f"隔离路径越界：{name}")
        environment[name] = str(resolved)
    environment.update(
        {
            "PYTHONUTF8": "1",
            JOURNEY_ENVIRONMENT_FLAG: "1",
            "TAVERN_OLLAMA_HOST": "http://127.0.0.1:1",
            "TAVERN_ALLOW_REMOTE": "false",
            "TAVERN_BACKUP_SCHEDULE_ENABLED": "false",
            "TAVERN_CHAT_GENERATION_MAX_SECONDS": "30",
        }
    )
    return environment


def _build_command(
    executable: Path,
    *,
    result_path: Path,
    screenshot_path: Path,
    stage: str,
    hold_ms: int,
) -> list[str]:
    if stage not in {"seed", "verify"}:
        raise DesktopJourneySmokeError("桌面旅程阶段无效")
    return [
        str(executable),
        "--journey-test",
        str(result_path),
        "--journey-screenshot",
        str(screenshot_path),
        "--journey-stage",
        stage,
        "--journey-hold-ms",
        str(hold_ms),
    ]


def _validate_result(result: dict, *, pid: int, stage: str) -> None:
    if set(result) != _RESULT_KEYS:
        raise DesktopJourneySmokeError("桌面旅程结果包含未知或缺失字段")
    if result.get("schema_version") != JOURNEY_SCHEMA_VERSION:
        raise DesktopJourneySmokeError("桌面旅程结果版本不匹配")
    if result.get("pid") != pid or result.get("stage") != stage:
        raise DesktopJourneySmokeError("桌面旅程结果与当前进程阶段不匹配")
    reported_error = result.get("error")
    if isinstance(reported_error, str) and reported_error:
        raise DesktopJourneySmokeError(
            f"桌面旅程报告错误：{reported_error[:120]}"
        )
    for field in (
        "ready",
        "page_loaded",
        "api_live",
        "project_created",
        "saves_created",
        "chat_completed",
        "refresh_verified",
        "save_switch_verified",
        "layout_verified",
        "scroll_verified",
        "screenshot_saved",
        "off_the_record",
    ):
        if result.get(field) is not True:
            raise DesktopJourneySmokeError(f"桌面旅程未通过：{field}")
    expected_persistence = stage == "verify"
    if result.get("persistence_verified") is not expected_persistence:
        raise DesktopJourneySmokeError("桌面旅程重启持久化标记不符合阶段契约")
    if result.get("message_count", 0) < 2:
        raise DesktopJourneySmokeError("桌面旅程未保留完整的一轮对话")
    if result.get("scroll_frame_samples", 0) < 30:
        raise DesktopJourneySmokeError("桌面旅程滚动逐帧样本不足")
    expected_strings = {
        "runtime": "in_process_asgi",
        "transport": "qwebchannel",
        "scheme": "tavern://app",
        "error": "",
    }
    for field, expected in expected_strings.items():
        if result.get(field) != expected:
            raise DesktopJourneySmokeError(f"桌面旅程字段不符合契约：{field}")
    if result.get("desktop_renderer") != "software":
        raise DesktopJourneySmokeError("桌面旅程未启用稳定软件渲染器")
    if result.get("tcp_listener_started") is not False:
        raise DesktopJourneySmokeError("桌面旅程报告启动了 TCP 监听器")


def _read_result_with_listener_checks(
    result_path: Path,
    process: subprocess.Popen[bytes],
    deadline: float,
) -> tuple[dict, list[list[int]], set[int]]:
    samples: list[list[int]] = []
    observed_processes = {process.pid}
    next_listener_check = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_listener_check:
            owned = {process.pid, *_descendant_pids(process.pid)}
            observed_processes.update(owned)
            listeners = sorted(_listening_pids() & owned)
            samples.append(listeners)
            if listeners:
                raise DesktopJourneySmokeError(
                    f"桌面旅程进程树开启了 TCP 监听端口：PID {listeners}"
                )
            next_listener_check = now + 0.5
        if result_path.is_file():
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                time.sleep(0.05)
                continue
            if not isinstance(payload, dict):
                raise DesktopJourneySmokeError("桌面旅程结果不是对象")
            return payload, samples, observed_processes
        returncode = process.poll()
        if returncode is not None:
            raise DesktopJourneySmokeError(
                f"桌面程序在写入旅程结果前退出：{returncode}"
            )
        time.sleep(0.05)
    raise DesktopJourneySmokeError("等待桌面旅程结果超时")


def _launch_stage(
    executable: Path,
    *,
    root: Path,
    stage: str,
    timeout_seconds: float,
    hold_ms: int,
) -> tuple[dict[str, object], Path]:
    isolated = _validate_isolated_root(root)
    result_path = (isolated / f"journey-{stage}-result.json").resolve(strict=False)
    screenshot_path = (isolated / f"journey-{stage}.png").resolve(strict=False)
    if not _is_within(result_path, isolated) or not _is_within(
        screenshot_path, isolated
    ):
        raise DesktopJourneySmokeError("桌面旅程产物路径越界")
    environment = _build_isolated_environment(isolated)
    command = _build_command(
        executable,
        result_path=result_path,
        screenshot_path=screenshot_path,
        stage=stage,
        hold_ms=hold_ms,
    )
    process = subprocess.Popen(
        command,
        cwd=executable.parent,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout_seconds
    observed_processes = {process.pid}
    try:
        result, samples, observed_processes = _read_result_with_listener_checks(
            result_path,
            process,
            deadline,
        )
        _validate_result(result, pid=process.pid, stage=stage)
        if (
            not screenshot_path.is_file()
            or screenshot_path.stat().st_size < 1024
            or screenshot_path.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n"
        ):
            raise DesktopJourneySmokeError("桌面旅程截图不是有效 PNG")
        for _index in range(3):
            owned = {process.pid, *_descendant_pids(process.pid)}
            observed_processes.update(owned)
            listeners = sorted(_listening_pids() & owned)
            samples.append(listeners)
            if listeners:
                raise DesktopJourneySmokeError(
                    f"桌面旅程保持阶段开启了 TCP 监听端口：PID {listeners}"
                )
            time.sleep(0.25)
        remaining = max(0.1, deadline - time.monotonic())
        returncode = process.wait(timeout=remaining)
        if returncode != 0:
            raise DesktopJourneySmokeError(f"桌面旅程退出码异常：{returncode}")
        shutdown_deadline = time.monotonic() + 5.0
        residual = sorted(observed_processes & set(_process_parent_map()))
        while residual and time.monotonic() < shutdown_deadline:
            time.sleep(0.1)
            residual = sorted(observed_processes & set(_process_parent_map()))
        if residual:
            raise DesktopJourneySmokeError(f"桌面旅程关闭后仍有残留进程：{residual}")
        return {
            "stage": stage,
            "status": "passed",
            "message_count": result["message_count"],
            "refresh_verified": result["refresh_verified"],
            "save_switch_verified": result["save_switch_verified"],
            "layout_verified": result["layout_verified"],
            "scroll_verified": result["scroll_verified"],
            "scroll_frame_samples": result["scroll_frame_samples"],
            "persistence_verified": result["persistence_verified"],
            "desktop_renderer": result["desktop_renderer"],
            "tcp_listener_samples": samples,
            "process_exited": True,
        }, screenshot_path
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    target = Path(path).resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)


def run_journey(
    executable: Path,
    *,
    artifacts_dir: Path,
    timeout_seconds: float,
    hold_ms: int,
) -> dict[str, object]:
    if os.name != "nt":
        raise DesktopJourneySmokeError("桌面旅程验收仅支持 Windows")
    target = Path(executable).resolve(strict=True)
    if target.suffix.casefold() != ".exe":
        raise DesktopJourneySmokeError("桌面旅程验收目标必须是 .exe")
    output = Path(artifacts_dir).expanduser().resolve(strict=False)
    output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=JOURNEY_ROOT_PREFIX) as raw_root:
        root = _validate_isolated_root(Path(raw_root))
        stages: list[dict[str, object]] = []
        screenshots: list[str] = []
        for stage in ("seed", "verify"):
            stage_result, temporary_screenshot = _launch_stage(
                target,
                root=root,
                stage=stage,
                timeout_seconds=timeout_seconds,
                hold_ms=hold_ms,
            )
            destination = output / f"desktop-journey-{stage}.png"
            shutil.copy2(temporary_screenshot, destination)
            stages.append(stage_result)
            screenshots.append(str(destination))

        summary: dict[str, object] = {
            "status": "passed",
            "runtime": "in_process_asgi",
            "transport": "qwebchannel",
            "tcp_listener_started": False,
            "isolated_user_data": True,
            "restart_persistence_verified": True,
            "scroll_verified": all(
                bool(stage["scroll_verified"]) for stage in stages
            ),
            "desktop_renderer": "software",
            "stages": stages,
            "screenshots": screenshots,
        }
        _atomic_write_json(output / "desktop-journey-result.json", summary)
        return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验收本地酒馆真实桌面用户旅程")
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=Path("artifacts") / "desktop-journey",
    )
    parser.add_argument("--timeout", type=float, default=150.0)
    parser.add_argument("--hold-ms", type=int, default=3000)
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.hold_ms < 1000:
        print(
            json.dumps(
                {"status": "failed", "error": "验收时间参数无效"}, ensure_ascii=False
            )
        )
        return 2
    try:
        result = run_journey(
            args.executable,
            artifacts_dir=args.artifacts_dir,
            timeout_seconds=args.timeout,
            hold_ms=args.hold_ms,
        )
    except (DesktopJourneySmokeError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
