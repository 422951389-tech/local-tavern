"""本地酒馆桌面应用入口。"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .environment import (
    ensure_prompt_assets,
    ensure_storage_directories,
    migrate_legacy_data,
    paths_touch_root,
    prepare_desktop_environment,
)
from .legacy_guard import detect_verified_legacy_service
from .resources import register_tavern_scheme, resolve_resource_root, resolve_web_root


logger = logging.getLogger(__name__)
SMOKE_SCHEMA_VERSION = 1
SMOKE_TIMEOUT_MS = 60_000
SMOKE_TITLE_PREFIX = "__LOCAL_TAVERN_SMOKE_V1__"
LEGACY_ROOT = Path("C:/local-tavern")
DESKTOP_RENDERERS = frozenset({"software", "hardware"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地酒馆无端口桌面应用")
    parser.add_argument(
        "--smoke-test",
        type=Path,
        metavar="RESULT_JSON",
        help="启动桌面页并通过 QWebChannel 调用 /health/live，写入验收 JSON 后退出",
    )
    parser.add_argument(
        "--smoke-hold-ms",
        type=int,
        default=0,
        help="smoke 结果落盘后继续保持完整运行状态的毫秒数",
    )
    parser.add_argument(
        "--journey-test",
        type=Path,
        metavar="RESULT_JSON",
        help="在受保护的隔离环境中执行真实桌面用户旅程并写入验收 JSON",
    )
    parser.add_argument(
        "--journey-screenshot",
        type=Path,
        metavar="SCREENSHOT_PNG",
        help="桌面旅程主界面截图的隔离临时路径",
    )
    parser.add_argument(
        "--journey-stage",
        choices=("seed", "verify"),
        help="桌面旅程阶段：首次写入或重启验证",
    )
    parser.add_argument(
        "--journey-hold-ms",
        type=int,
        default=3000,
        help="旅程结果落盘后继续保持完整运行状态的毫秒数",
    )
    return parser


def _write_smoke_result(path: Path, result: dict[str, Any]) -> None:
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise ValueError("--smoke-test 必须使用绝对路径")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)


def _base_smoke_result() -> dict[str, Any]:
    return {
        "schema_version": SMOKE_SCHEMA_VERSION,
        "pid": os.getpid(),
        "ready": False,
        "page_loaded": False,
        "api_live": False,
        "api_status": 0,
        "runtime": "in_process_asgi",
        "transport": "qwebchannel",
        "scheme": "tavern://app",
        "tcp_listener_started": False,
        "off_the_record": False,
        "desktop_renderer": os.environ.get(
            "TAVERN_DESKTOP_RENDERER_ACTIVE",
            "software",
        ),
        "error": "",
    }


def _resolve_desktop_renderer(
    settings_path: Path,
    environment: dict[str, str] | None = None,
) -> str:
    """在导入 QtWebEngine 前确定渲染器；异常配置安全回退到软件模式。"""

    env = os.environ if environment is None else environment
    override = str(env.get("TAVERN_DESKTOP_RENDERER", "")).strip().casefold()
    if override in DESKTOP_RENDERERS:
        return override
    try:
        payload = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return "software"
    configured = (
        str(payload.get("desktop_renderer", "")).strip().casefold()
        if isinstance(payload, dict)
        else ""
    )
    return configured if configured in DESKTOP_RENDERERS else "software"


def _configure_desktop_renderer(
    settings_path: Path,
    environment: dict[str, str] | None = None,
) -> str:
    """写入 Qt WebEngine 启动标志，必须在任何 QtWebEngine 导入前调用。"""

    env = os.environ if environment is None else environment
    mode = _resolve_desktop_renderer(settings_path, env)
    flags = [
        flag
        for flag in str(env.get("QTWEBENGINE_CHROMIUM_FLAGS", "")).split()
        if flag != "--disable-gpu"
    ]
    if mode == "software":
        flags.append("--disable-gpu")
    env["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags)
    env["TAVERN_DESKTOP_RENDERER_ACTIVE"] = mode
    return mode


def _append_bootstrap_log(stage: str, exc: BaseException) -> Path | None:
    """在正式日志尚未配置时，把启动失败写到稳定用户目录。"""
    candidates: list[Path] = []
    configured_log_dir = os.environ.get("TAVERN_LOG_DIR", "").strip()
    if configured_log_dir:
        candidates.append(Path(configured_log_dir).expanduser())
    configured_user_root = os.environ.get("TAVERN_DESKTOP_USER_ROOT", "").strip()
    if configured_user_root:
        candidates.append(Path(configured_user_root).expanduser() / "logs")
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidates.append(Path(local_app_data).expanduser() / "LocalTavern" / "logs")
    candidates.append(Path.home() / "AppData" / "Local" / "LocalTavern" / "logs")

    summary = " ".join(str(exc).split())[:1000]
    line = (
        f"{datetime.now(timezone.utc).isoformat()} "
        f"stage={stage} exception={type(exc).__name__} detail={summary}\n"
    )
    visited: set[Path] = set()
    for directory in candidates:
        try:
            resolved = directory.resolve(strict=False)
            if resolved in visited:
                continue
            visited.add(resolved)
            resolved.mkdir(parents=True, exist_ok=True)
            target = resolved / "tavern-desktop-bootstrap.log"
            with target.open("a", encoding="utf-8") as handle:
                handle.write(line)
            return target
        except (OSError, RuntimeError, ValueError):
            continue
    return None


def _write_smoke_error(path: Path | None, error: str) -> None:
    if path is None:
        return
    result = _base_smoke_result()
    result["error"] = error
    try:
        _write_smoke_result(path, result)
    except Exception as exc:  # noqa: BLE001 - 失败路径只能继续写 bootstrap 日志
        _append_bootstrap_log("smoke_result_write", exc)


def _fail_before_qt(
    *,
    smoke_path: Path | None,
    stage: str,
    smoke_error: str,
    exc: BaseException,
) -> int:
    _append_bootstrap_log(stage, exc)
    _write_smoke_error(smoke_path, smoke_error)
    print(f"[错误] 桌面启动失败（{stage}）：{type(exc).__name__}", file=sys.stderr)
    return 4


def _cleanup_desktop_resources(runtime: Any, instance: Any) -> None:
    """关闭运行时且始终释放单实例锁，避免失败退出阻塞下一次启动。"""
    failures: list[BaseException] = []
    try:
        if runtime is not None:
            runtime.shutdown(timeout=30.0)
    except BaseException as exc:  # noqa: BLE001 - 仍须继续释放进程级锁
        failures.append(exc)
    finally:
        try:
            if instance is not None:
                instance.release()
        except BaseException as exc:  # noqa: BLE001 - 汇总后交给调用方显示
            failures.append(exc)
    if failures:
        for secondary in failures[1:]:
            logger.error(
                "desktop_cleanup_secondary_failure exception=%s",
                type(secondary).__name__,
                exc_info=(type(secondary), secondary, secondary.__traceback__),
            )
        raise failures[0]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.smoke_hold_ms < 0:
        parser.error("--smoke-hold-ms 不能为负数")
    if args.smoke_test is not None and not args.smoke_test.expanduser().is_absolute():
        parser.error("--smoke-test 必须使用绝对路径")
    journey_values = (
        args.journey_test,
        args.journey_screenshot,
        args.journey_stage,
    )
    journey_requested = any(value is not None for value in journey_values)
    if args.smoke_test is not None and journey_requested:
        parser.error("--smoke-test 与 --journey-test 不能同时使用")
    if journey_requested and not all(value is not None for value in journey_values):
        parser.error("桌面旅程测试必须同时提供结果、截图与阶段参数")
    if args.journey_hold_ms < 1000:
        parser.error("--journey-hold-ms 不能小于 1000")
    if journey_requested:
        from .journey import DesktopJourneyError, validate_journey_paths

        try:
            validate_journey_paths(args.journey_test, args.journey_screenshot)
        except DesktopJourneyError as exc:
            parser.error(str(exc))

    try:
        resource_root = resolve_resource_root()
        paths = prepare_desktop_environment(
            resource_root,
            defer_storage_initialization=True,
        )
        settings_path = Path(
            os.environ.get(
                "TAVERN_SETTINGS_PATH",
                str(paths.user_root / "data" / "settings.json"),
            )
        )
        _configure_desktop_renderer(settings_path)
    except Exception as exc:  # noqa: BLE001 - Qt 前必须稳定落盘并退出
        if journey_requested:
            from .journey import write_journey_failure

            write_journey_failure(
                args.journey_test,
                args.journey_stage,
                "desktop_bootstrap_failed",
            )
        return _fail_before_qt(
            smoke_path=args.smoke_test,
            stage="bootstrap_environment",
            smoke_error="desktop_bootstrap_failed",
            exc=exc,
        )

    try:
        register_tavern_scheme()
        from PySide6.QtCore import QCoreApplication, Qt, QTimer
        from PySide6.QtWidgets import QApplication, QMessageBox
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        application = QApplication(sys.argv[:1])
    except Exception as exc:  # noqa: BLE001 - Qt 初始化错误同样需要稳定 smoke
        if journey_requested:
            from .journey import write_journey_failure

            write_journey_failure(args.journey_test, args.journey_stage, "qt_unavailable")
        return _fail_before_qt(
            smoke_path=args.smoke_test,
            stage="qt_initialization",
            smoke_error="qt_unavailable",
            exc=exc,
        )

    application.setApplicationName("本地酒馆")
    application.setApplicationDisplayName("本地酒馆 · 叙事工作台")
    application.setOrganizationName("LocalTavern")

    window: Any = None
    runtime: Any = None
    instance: Any = None
    cleanup_lock = threading.Lock()
    cleaned = False
    startup_stage = "legacy_detection"

    def activate_existing_window() -> None:
        if window is not None:
            window.activate()

    def cleanup() -> None:
        nonlocal cleaned
        with cleanup_lock:
            if cleaned:
                return
            _cleanup_desktop_resources(runtime, instance)
            cleaned = True

    try:
        isolated_automation = (
            (args.smoke_test is not None or journey_requested)
            and not paths_touch_root(
                paths,
                LEGACY_ROOT,
            )
        )
        if not isolated_automation:
            running = detect_verified_legacy_service(LEGACY_ROOT)
            if running is not None:
                message = (
                    f"检测到旧版本地酒馆服务仍在运行（PID {running.pid}）。\n\n"
                    "为避免聊天、摘要或备份写入期间复制出不一致存档，"
                    "桌面版没有启动。\n"
                    "请先执行 C:\\local-tavern\\stop_tavern.bat，再重新打开桌面版。"
                )
                if args.smoke_test is not None:
                    _write_smoke_error(args.smoke_test, "legacy_service_running")
                else:
                    QMessageBox.warning(None, "请先关闭旧版本地服务", message)
                return 3
            startup_stage = "legacy_migration"
            migrate_legacy_data(paths, LEGACY_ROOT)

        startup_stage = "storage_initialization"
        ensure_storage_directories(paths)
        ensure_prompt_assets(paths)

        startup_stage = "desktop_component_import"
        from .qt_bridge import TavernBridge
        from .runtime import AsyncioRuntime
        from .single_instance import SingleInstance
        from .window import TavernWindow

        startup_stage = "single_instance"
        instance = SingleInstance(paths.user_root, activate_existing_window)
        if not instance.acquire():
            if args.smoke_test is not None:
                _write_smoke_error(args.smoke_test, "instance_already_running")
                return 3
            if journey_requested:
                from .journey import write_journey_failure

                write_journey_failure(
                    args.journey_test,
                    args.journey_stage,
                    "instance_already_running",
                )
                return 3
            return 0

        startup_stage = "application_import"
        from core.logging_config import configure_logging

        configure_logging(hidden=True)
        from core import config as core_config
        from server import app as asgi_app
        if journey_requested:
            from .journey import install_journey_provider_registry

            install_journey_provider_registry()

        startup_stage = "asgi_runtime"
        trusted_host = f"127.0.0.1:{core_config.PORT}"
        runtime = AsyncioRuntime(
            asgi_app,
            trusted_host=trusted_host,
            trusted_origin=f"http://{trusted_host}",
        )
        bridge = TavernBridge(runtime)
        runtime.start(timeout=30.0)
        startup_stage = "window_creation"
        window = TavernWindow(resolve_web_root(resource_root), bridge, cleanup)
        application.aboutToQuit.connect(cleanup)
        if args.smoke_test is not None:
            window.move(-32_000, -32_000)
        elif journey_requested:
            window.resize(1440, 900)
        window.show()
        QTimer.singleShot(0, window.start_loading)

        smoke = None
        if args.smoke_test is not None:
            smoke = _SmokeController(
                application,
                window,
                bridge,
                args.smoke_test,
                args.smoke_hold_ms,
            )
            smoke.start()

        journey = None
        if journey_requested:
            from .journey import JourneyController

            journey = JourneyController(
                application,
                window,
                args.journey_test,
                args.journey_screenshot,
                args.journey_stage,
                args.journey_hold_ms,
            )
            journey.start()

        exit_code = application.exec()
        cleanup()
        if smoke is not None:
            return 0 if smoke.success else 5
        if journey is not None:
            return 0 if journey.success else 6
        return int(exit_code)
    except Exception as exc:  # noqa: BLE001 - 顶层只显示稳定错误，完整信息进日志
        bootstrap_log = _append_bootstrap_log(startup_stage, exc)
        logger.error(
            "desktop_startup_failed stage=%s exception=%s",
            startup_stage,
            type(exc).__name__,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        try:
            cleanup()
        except Exception:
            logger.exception("desktop_startup_cleanup_failed")
        if args.smoke_test is not None:
            error_code = (
                "desktop_migration_failed"
                if startup_stage == "legacy_migration"
                else "desktop_startup_failed"
            )
            _write_smoke_error(args.smoke_test, error_code)
        elif journey_requested:
            from .journey import write_journey_failure

            write_journey_failure(
                args.journey_test,
                args.journey_stage,
                "desktop_startup_failed",
            )
        else:
            detail = (
                f"启动详情已写入：\n{bootstrap_log}"
                if bootstrap_log is not None
                else "启动错误未能写入日志，请检查用户目录写入权限。"
            )
            message = (
                "首次数据迁移失败，原目录和目标目录均未被删除。\n\n"
                if startup_stage == "legacy_migration"
                else "桌面应用未能安全启动。\n\n"
            )
            try:
                QMessageBox.critical(None, "本地酒馆启动失败", message + detail)
            except Exception as dialog_exc:  # noqa: BLE001 - 最后降级到 stderr
                _append_bootstrap_log("startup_error_dialog", dialog_exc)
                print("[错误] 桌面应用未能安全启动。", file=sys.stderr)
        return 4


class _SmokeController:
    """GUI smoke：结果先落盘，再在完整运行态保持指定时间。"""

    def __init__(
        self,
        application,
        window,
        bridge,
        result_path: Path,
        hold_ms: int,
    ) -> None:
        from PySide6.QtCore import QObject

        # QObject 由组合持有，避免类定义阶段强制导入 Qt。
        self._owner = QObject(window)
        self._application = application
        self._window = window
        self._bridge = bridge
        self._result_path = result_path
        self._hold_ms = hold_ms
        self._finished = False
        self.success = False
        self._page_probe_started = False
        self._bridge_event_count = 0
        self._last_bridge_event = "none"
        self._bridge.bridgeEvent.connect(self._bridge_event)

    def start(self) -> None:
        from PySide6.QtCore import QTimer

        self._window.page.consoleMessage.connect(self._console_message)
        self._window.install_document_script(
            "local-tavern-smoke-v1",
            _smoke_probe_script(),
        )
        QTimer.singleShot(SMOKE_TIMEOUT_MS, self._timeout)

    def _console_message(self, message: str) -> None:
        if self._finished or not message.startswith(SMOKE_TITLE_PREFIX):
            return
        try:
            encoded = message[len(SMOKE_TITLE_PREFIX) :]
            parsed = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            parsed = {}
        self._page_probe_started = True
        if not isinstance(parsed, dict) or not parsed.get("ok"):
            code = parsed.get("error") if isinstance(parsed, dict) else None
            safe_code = code if isinstance(code, str) and code else "invalid_smoke_result"
            self._finish(
                error=f"api_live_failed:{safe_code[:80]}",
                api_status=int(parsed.get("status", 0) or 0),
            )
            return
        if not self._window.is_off_the_record:
            self._finish(
                error="profile_not_off_the_record",
                api_status=int(parsed.get("status", 0) or 0),
            )
            return
        self._finish(api_status=int(parsed.get("status", 0) or 0))

    def _timeout(self) -> None:
        if not self._finished:
            diagnostic = self._window.last_diagnostic
            stage = "probe_started" if self._page_probe_started else "probe_not_reported"
            parts = ["smoke_timeout", stage]
            if diagnostic:
                parts.append(diagnostic)
            parts.append(f"bridge_{self._last_bridge_event}_{self._bridge_event_count}")
            error = ":".join(parts)
            self._finish(error=error)

    def _bridge_event(self, raw_event: str) -> None:
        try:
            event = json.loads(raw_event)
        except (TypeError, json.JSONDecodeError):
            self._last_bridge_event = "invalid"
            self._bridge_event_count += 1
            return
        event_type = event.get("type") if isinstance(event, dict) else None
        self._last_bridge_event = (
            event_type if isinstance(event_type, str) and event_type else "invalid"
        )[:40]
        self._bridge_event_count += 1

    def _finish(self, *, error: str = "", api_status: int = 0) -> None:
        from PySide6.QtCore import QTimer

        if self._finished:
            return
        self._finished = True
        self.success = not error
        result = _base_smoke_result()
        result.update(
            {
                "ready": self.success,
                "page_loaded": self._page_probe_started,
                "api_live": self.success,
                "api_status": api_status,
                "off_the_record": bool(self._window.is_off_the_record),
                "error": error,
            }
        )
        _write_smoke_result(self._result_path, result)
        QTimer.singleShot(self._hold_ms, self._application.quit)


def _smoke_probe_script() -> str:
    """在 document creation 注入；不等待 Qt 的 loadFinished。"""
    return f"""
        (() => {{
            if (globalThis.__localTavernSmokeInstalled) return;
            globalThis.__localTavernSmokeInstalled = true;
            const prefix = {json.dumps(SMOKE_TITLE_PREFIX)};
            const report = payload => {{
                const encoded = btoa(JSON.stringify(payload));
                console.info(prefix + encoded);
            }};
            let attempts = 0;
            let started = false;
            const timer = setInterval(() => {{
                attempts += 1;
                if (started) return;
                const transport = globalThis.TavernDesktopTransport;
                if (!transport || typeof transport.ready !== 'function'
                    || typeof transport.fetch !== 'function') {{
                    if (attempts >= 1000) {{
                        clearInterval(timer);
                        report({{ ok: false, status: 0, error: 'desktop_transport_missing' }});
                    }}
                    return;
                }}
                started = true;
                clearInterval(timer);
                Promise.resolve()
                    .then(() => transport.ready())
                    .then(() => transport.fetch('/health/live', {{ cache: 'no-store' }}))
                    .then(async response => ({{ response, payload: await response.json() }}))
                    .then(({{ response, payload }}) => report({{
                        ok: response.ok && payload && payload.status === 'alive'
                            && payload.service === 'local-tavern',
                        status: response.status,
                        error: ''
                    }}))
                    .catch(error => report({{
                        ok: false,
                        status: Number.isInteger(error && error.status) ? error.status : 0,
                        error: error && typeof error.code === 'string'
                            ? error.code : 'api_live_failed'
                    }}));
            }}, 25);
        }})();
    """


if __name__ == "__main__":
    raise SystemExit(main())
