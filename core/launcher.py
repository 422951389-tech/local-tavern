"""本地酒馆统一启动器：环境校验、独占端口、health 门禁与浏览器。"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import tempfile
import threading
import time
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable


logger = logging.getLogger(__name__)
LIVE_MARKER = {
    "status": "alive",
    "service": "local-tavern",
    "contract_version": 1,
}


def browser_host(host: str) -> str:
    """监听任意地址时，浏览器仍只访问本机回环。"""
    return "127.0.0.1" if host in {"0.0.0.0", "::"} else host


def service_url(host: str, port: int) -> str:
    browser = browser_host(host)
    formatted = f"[{browser}]" if ":" in browser else browser
    return f"http://{formatted}:{port}"


def bind_socket(host: str, port: int) -> socket.socket:
    """先占有最终监听 socket，消除检查后被其他进程抢占的竞态。"""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    bound = socket.socket(family=family, type=socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            bound.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            bound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bound.bind((host, port))
        bound.set_inheritable(True)
        return bound
    except BaseException:
        bound.close()
        raise


def health_marker_matches(
    base_url: str,
    *,
    timeout_seconds: float = 1.0,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> bool:
    """只接受固定 live 合约；任意根路径 200 不算本项目。"""
    request = urllib.request.Request(
        f"{base_url}/health/live",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            if getattr(response, "status", None) != 200:
                return False
            body = response.read(4097)
    except Exception:
        return False
    if len(body) > 4096:
        return False
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return False
    return payload == LIVE_MARKER


def wait_for_live(
    base_url: str,
    *,
    timeout_seconds: float = 40.0,
    poll_seconds: float = 0.25,
    probe: Callable[[str], bool] = health_marker_matches,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        if probe(base_url):
            return True
        time.sleep(max(0.01, poll_seconds))
    return False


def open_browser_when_live(
    base_url: str,
    *,
    timeout_seconds: float = 40.0,
    probe: Callable[[str], bool] = health_marker_matches,
    browser_open: Callable[[str], object] = webbrowser.open,
) -> bool:
    if not wait_for_live(base_url, timeout_seconds=timeout_seconds, probe=probe):
        logger.error("startup_failed code=health_timeout")
        return False
    try:
        browser_open(base_url)
    except Exception as exc:
        logger.error(
            "browser_open_failed exception=%s",
            type(exc).__name__,
        )
        return False
    return True


def _existing_instance_is_owned(config) -> bool:
    from core.process_guard import (
        _listening_pids,
        _process_command_line,
        _read_json,
        metadata_matches_process,
    )

    metadata = _read_json(config.PID_PATH)
    if not metadata or not isinstance(metadata.get("pid"), int):
        return False
    pid = metadata["pid"]
    return metadata_matches_process(
        metadata,
        command_line=_process_command_line(pid),
        listening_pids=_listening_pids(config.PORT),
        project_root=config.BASE_DIR,
    )


def _bootstrap_hidden_log(project_root: Path) -> logging.Handler:
    """在 core.config 导入前建立兜底日志，使配置错误也能定位。"""
    fallback = project_root / "logs" / "tavern.log"
    raw = os.environ.get("TAVERN_LOG_FILE", "").strip()
    candidate = Path(raw).expanduser() if raw else fallback
    if not candidate.is_absolute():
        candidate = fallback
    emergency = Path(tempfile.gettempdir()) / "local-tavern" / "tavern-bootstrap.log"
    candidates = list(dict.fromkeys((candidate, fallback, emergency)))
    handler: logging.Handler | None = None
    for log_path in candidates:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                log_path,
                maxBytes=1024 * 1024,
                backupCount=2,
                encoding="utf-8",
                delay=True,
            )
            break
        except OSError:
            continue
    if handler is None:
        handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    return handler


def _remove_handler(handler: logging.Handler | None) -> None:
    if handler is None:
        return
    logging.getLogger().removeHandler(handler)
    handler.close()


def serve(*, hidden: bool, open_browser: bool) -> int:
    project_root = Path(__file__).resolve().parents[1]
    os.chdir(project_root)
    bootstrap = _bootstrap_hidden_log(project_root) if hidden else None
    try:
        try:
            from core import config
            from core.logging_config import configure_logging
            from core.runtime_validation import assert_runtime

            configure_logging(hidden=hidden)
            _remove_handler(bootstrap)
            bootstrap = None
            assert_runtime()
        except Exception as exc:
            logging.getLogger(__name__).error(
                "startup_failed code=environment exception=%s",
                type(exc).__name__,
            )
            if not hidden:
                print("[错误] 运行环境校验失败；请执行 setup.bat。")
            return 2

        base_url = service_url(config.HOST, config.PORT)
        try:
            bound = bind_socket(config.HOST, config.PORT)
        except OSError as exc:
            owned = _existing_instance_is_owned(config)
            healthy = owned and health_marker_matches(base_url)
            if healthy:
                logger.info("existing_instance status=healthy")
                if open_browser:
                    webbrowser.open(base_url)
                return 0
            logger.error(
                "startup_failed code=port_in_use exception=%s",
                type(exc).__name__,
            )
            if not hidden:
                print(f"[错误] 端口 {config.PORT} 已被其他程序占用；未打开浏览器。")
            return 3

        try:
            import uvicorn

            uvicorn_config = uvicorn.Config(
                "server:app",
                host=config.HOST,
                port=config.PORT,
                workers=1,
                log_level="info",
                log_config=None,
                access_log=False,
            )
            uvicorn_server = uvicorn.Server(uvicorn_config)
            if open_browser:
                threading.Thread(
                    target=open_browser_when_live,
                    args=(base_url,),
                    daemon=True,
                    name="local-tavern-browser-gate",
                ).start()
            uvicorn_server.run(sockets=[bound])
            if not uvicorn_server.started:
                logger.error("startup_failed code=server_startup")
                return 4
            return 0
        except Exception as exc:
            logger.error(
                "startup_failed code=server exception=%s",
                type(exc).__name__,
            )
            if not hidden:
                print("[错误] 服务启动失败；请查看上方日志。")
            return 4
        finally:
            bound.close()
    finally:
        _remove_handler(bootstrap)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地酒馆统一启动器")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve", help="校验环境并启动单 worker 服务")
    serve_parser.add_argument("--hidden", action="store_true")
    serve_parser.add_argument("--open-browser", action="store_true")
    serve_parser.add_argument("--workers", type=int, choices=(1,), default=1)
    args = parser.parse_args(argv)
    if args.command == "serve":
        return serve(hidden=args.hidden, open_browser=args.open_browser)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
