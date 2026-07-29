"""本地 HTTP Host 边界；避免同源判断信任任意 Host 头。"""
from __future__ import annotations

from urllib.parse import urlsplit

from core.config import HOST, PORT


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def host_header_allowed(
    value: object,
    *,
    configured_host: str = HOST,
    configured_port: int = PORT,
) -> bool:
    """校验 Host 语法、端口和本机回环白名单。"""
    if not isinstance(value, str) or not value or len(value) > 255:
        return False
    try:
        parsed = urlsplit(f"//{value}")
        hostname = parsed.hostname.casefold() if parsed.hostname else ""
        port = parsed.port
    except ValueError:
        return False
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or (port is not None and port != configured_port)
    ):
        return False
    allowed = set(_LOOPBACK_HOSTS)
    allowed.add(configured_host.casefold())
    return hostname in allowed
