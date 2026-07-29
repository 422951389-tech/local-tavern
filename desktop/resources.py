"""打包资源定位与 ``tavern://app`` 只读 scheme。"""

from __future__ import annotations

import mimetypes
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


TAVERN_SCHEME = b"tavern"
TAVERN_HOST = "app"

_CSP = (
    "default-src 'self' qrc:; "
    "script-src 'self' qrc:; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "media-src 'self' blob:; "
    "connect-src 'none'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "worker-src 'none'"
)
_CSP_META = (
    '<meta http-equiv="Content-Security-Policy" content="'
    + _CSP
    + '">\n    <meta http-equiv="X-Content-Type-Options" content="nosniff">'
    + '\n    <meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">'
)


class ResourceError(ValueError):
    pass


def resolve_resource_root() -> Path:
    """返回源码根或 PyInstaller ``_MEIPASS`` 资源根。"""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root).resolve(strict=True)
    return Path(__file__).resolve().parents[1]


def resolve_web_root(resource_root: Path | None = None) -> Path:
    root = Path(resource_root or resolve_resource_root()).resolve(strict=True)
    web = (root / "web").resolve(strict=True)
    if not web.is_dir() or not (web / "index.html").is_file():
        raise ResourceError("桌面包缺少 web/index.html")
    return web


def resolve_web_asset(url_path: str, web_root: Path | None = None) -> Path:
    """把 scheme URL path 安全映射到 web 根，阻断 traversal 与 symlink 越界。"""
    if not isinstance(url_path, str) or "\0" in url_path or "\\" in url_path:
        raise ResourceError("资源路径无效")
    split = urlsplit(url_path)
    if split.scheme or split.netloc or split.query or split.fragment:
        raise ResourceError("资源路径必须是纯应用内路径")
    try:
        decoded = unquote(split.path, errors="strict")
    except UnicodeError as exc:
        raise ResourceError("资源路径编码无效") from exc
    if decoded.startswith("//") or "\\" in decoded or "\0" in decoded:
        raise ResourceError("资源路径无效")
    posix = PurePosixPath(decoded)
    parts = tuple(part for part in posix.parts if part not in {"", "/"})
    if any(part in {".", ".."} for part in parts):
        raise ResourceError("资源路径禁止目录穿越")

    if not parts or parts == ("index.html",):
        relative = Path("index.html")
    elif parts[0] == "static" and len(parts) > 1:
        relative = Path(*parts[1:])
    else:
        raise ResourceError("资源路径不在允许范围内")

    root = Path(web_root or resolve_web_root()).resolve(strict=True)
    try:
        target = (root / relative).resolve(strict=True)
        target.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ResourceError("资源不存在或越界") from exc
    if not target.is_file():
        raise ResourceError("资源不是文件")
    return target


def content_type_for(path: Path) -> bytes:
    suffix = path.suffix.lower()
    if suffix in {".js", ".mjs"}:
        return b"text/javascript; charset=utf-8"
    if suffix == ".css":
        return b"text/css; charset=utf-8"
    if suffix == ".html":
        return b"text/html; charset=utf-8"
    if suffix == ".json":
        return b"application/json; charset=utf-8"
    guessed, _encoding = mimetypes.guess_type(path.name)
    return (guessed or "application/octet-stream").encode("ascii")


def read_asset(path: Path) -> bytes:
    data = path.read_bytes()
    if path.name == "index.html":
        text = data.decode("utf-8")
        if "Content-Security-Policy" not in text:
            marker = '<meta charset="UTF-8">'
            if marker not in text:
                raise ResourceError("index.html 缺少 charset 元数据")
            text = text.replace(marker, f"{marker}\n    {_CSP_META}", 1)
        return text.encode("utf-8")
    return data


try:
    from PySide6.QtCore import QByteArray, QBuffer, QIODevice
    from PySide6.QtWebEngineCore import (
        QWebEngineUrlRequestJob,
        QWebEngineUrlScheme,
        QWebEngineUrlSchemeHandler,
    )

    PYSIDE6_AVAILABLE = True
except ImportError:  # 单元测试与普通服务环境无需安装 Qt
    PYSIDE6_AVAILABLE = False


_scheme_registered = False


def register_tavern_scheme() -> None:
    """必须在构造 QApplication 前调用。"""
    global _scheme_registered
    if _scheme_registered:
        return
    if not PYSIDE6_AVAILABLE:
        raise RuntimeError("桌面运行需要 PySide6 QtWebEngine")
    scheme = QWebEngineUrlScheme(TAVERN_SCHEME)
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.HostAndPort)
    scheme.setDefaultPort(-1)
    scheme.setFlags(
        QWebEngineUrlScheme.Flag.SecureScheme
        | QWebEngineUrlScheme.Flag.LocalScheme
        | QWebEngineUrlScheme.Flag.LocalAccessAllowed
        | QWebEngineUrlScheme.Flag.CorsEnabled
        | QWebEngineUrlScheme.Flag.FetchApiAllowed
    )
    QWebEngineUrlScheme.registerScheme(scheme)
    _scheme_registered = True


if PYSIDE6_AVAILABLE:

    class TavernSchemeHandler(QWebEngineUrlSchemeHandler):
        """仅为固定 host 提供 GET/HEAD 只读打包资源。"""

        def __init__(self, web_root: Path, parent=None) -> None:
            super().__init__(parent)
            self._web_root = Path(web_root).resolve(strict=True)
            # PySide 的 Python wrapper 必须与 Chromium 异步读取期同寿命；仅设置
            # C++ QObject parent 不足以阻止 wrapper 被引用计数提前回收。
            self._active_devices: dict[int, QBuffer] = {}

        def requestStarted(self, job: "QWebEngineUrlRequestJob") -> None:  # noqa: N802 - Qt API
            url = job.requestUrl()
            method = bytes(job.requestMethod()).upper()
            if url.scheme() != TAVERN_SCHEME.decode("ascii") or url.host() != TAVERN_HOST:
                job.fail(QWebEngineUrlRequestJob.Error.UrlInvalid)
                return
            if method not in {b"GET", b"HEAD"}:
                job.fail(QWebEngineUrlRequestJob.Error.RequestDenied)
                return
            try:
                asset = resolve_web_asset(url.path(), self._web_root)
                payload = b"" if method == b"HEAD" else read_asset(asset)
            except ResourceError:
                job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
                return
            except OSError:
                job.fail(QWebEngineUrlRequestJob.Error.RequestFailed)
                return

            buffer = QBuffer(job)
            buffer.setData(QByteArray(payload))
            buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            device_key = id(job)
            self._active_devices[device_key] = buffer

            def release_device(*_args) -> None:
                self._active_devices.pop(device_key, None)

            job.destroyed.connect(release_device)
            buffer.aboutToClose.connect(release_device)
            job.reply(content_type_for(asset), buffer)

else:

    class TavernSchemeHandler:  # pragma: no cover - 仅提供清晰的缺依赖错误
        def __init__(self, *_args, **_kwargs) -> None:
            raise RuntimeError("桌面运行需要 PySide6 QtWebEngine")
