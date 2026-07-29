"""QWebChannel 与内存 ASGI 桥之间的稳定消息协议。"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlsplit


MAX_REQUEST_ID_LENGTH = 128
MAX_PATH_LENGTH = 8 * 1024
MAX_HEADER_COUNT = 128
MAX_HEADER_BYTES = 64 * 1024
MAX_BODY_BYTES = 16 * 1024 * 1024

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


class BridgeProtocolError(ValueError):
    """可安全发送给页面的桥协议错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        request_id: str = "",
        status: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.request_id = request_id
        self.status = status


@dataclass(frozen=True, slots=True)
class BridgeRequest:
    request_id: str
    method: str
    path: str
    headers: tuple[tuple[str, str], ...]
    body: bytes

    @classmethod
    def from_payload(cls, payload: object) -> "BridgeRequest":
        if not isinstance(payload, dict):
            raise BridgeProtocolError("invalid_request", "桌面请求必须是对象")

        raw_id = payload.get("id", "")
        request_id = raw_id if isinstance(raw_id, str) else ""
        if (
            not request_id
            or len(request_id) > MAX_REQUEST_ID_LENGTH
            or _REQUEST_ID_RE.fullmatch(request_id) is None
        ):
            raise BridgeProtocolError(
                "invalid_request_id",
                "桌面请求 ID 无效",
                request_id=request_id if len(request_id) <= MAX_REQUEST_ID_LENGTH else "",
            )

        raw_method = payload.get("method", "GET")
        if not isinstance(raw_method, str):
            raise BridgeProtocolError(
                "invalid_method", "桌面请求方法无效", request_id=request_id
            )
        method = raw_method.upper()
        if method not in _ALLOWED_METHODS:
            raise BridgeProtocolError(
                "invalid_method", "桌面请求方法不受支持", request_id=request_id
            )

        path = payload.get("path")
        if not isinstance(path, str) or not path or len(path) > MAX_PATH_LENGTH:
            raise BridgeProtocolError(
                "invalid_path", "桌面请求路径无效", request_id=request_id
            )
        split = urlsplit(path)
        if (
            split.scheme
            or split.netloc
            or not split.path.startswith("/")
            or split.path.startswith("//")
            or "\\" in split.path
            or "\0" in path
            or split.fragment
        ):
            raise BridgeProtocolError(
                "invalid_path", "桌面请求只允许应用内相对路径", request_id=request_id
            )
        if not (
            split.path == "/api"
            or split.path.startswith("/api/")
            or split.path == "/health"
            or split.path.startswith("/health/")
        ):
            raise BridgeProtocolError(
                "path_not_allowed", "桌面桥只接受 API 与健康检查路径", request_id=request_id
            )

        headers = _validate_headers(payload.get("headers", ()), request_id=request_id)
        body = _decode_body(payload.get("body_base64", ""), request_id=request_id)
        return cls(request_id, method, path, headers, body)


def _validate_headers(
    raw_headers: object,
    *,
    request_id: str,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw_headers, (list, tuple)):
        raise BridgeProtocolError(
            "invalid_headers", "桌面请求头必须是二维列表", request_id=request_id
        )
    if len(raw_headers) > MAX_HEADER_COUNT:
        raise BridgeProtocolError(
            "headers_too_large", "桌面请求头数量超过限制", request_id=request_id
        )
    total = 0
    normalized: list[tuple[str, str]] = []
    for item in raw_headers:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise BridgeProtocolError(
                "invalid_headers", "桌面请求头条目无效", request_id=request_id
            )
        name, value = item
        if not isinstance(name, str) or not isinstance(value, str):
            raise BridgeProtocolError(
                "invalid_headers", "桌面请求头名称和值必须是字符串", request_id=request_id
            )
        lower_name = name.lower()
        if _HEADER_NAME_RE.fullmatch(name) is None or "\r" in value or "\n" in value or "\0" in value:
            raise BridgeProtocolError(
                "invalid_headers", "桌面请求头包含非法字符", request_id=request_id
            )
        try:
            name_bytes = lower_name.encode("ascii")
            value_bytes = value.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise BridgeProtocolError(
                "invalid_headers", "桌面请求头编码无效", request_id=request_id
            ) from exc
        total += len(name_bytes) + len(value_bytes)
        if total > MAX_HEADER_BYTES:
            raise BridgeProtocolError(
                "headers_too_large", "桌面请求头超过大小限制", request_id=request_id
            )
        normalized.append((lower_name, value))
    return tuple(normalized)


def _decode_body(raw_body: object, *, request_id: str) -> bytes:
    if not isinstance(raw_body, str):
        raise BridgeProtocolError(
            "invalid_body", "桌面请求体编码无效", request_id=request_id
        )
    if len(raw_body) > ((MAX_BODY_BYTES + 2) // 3) * 4 + 4:
        raise BridgeProtocolError(
            "body_too_large", "桌面请求体超过大小限制", request_id=request_id
        )
    try:
        body = base64.b64decode(raw_body, validate=True) if raw_body else b""
    except (binascii.Error, ValueError) as exc:
        raise BridgeProtocolError(
            "invalid_body", "桌面请求体不是有效 Base64", request_id=request_id
        ) from exc
    if len(body) > MAX_BODY_BYTES:
        raise BridgeProtocolError(
            "body_too_large", "桌面请求体超过大小限制", request_id=request_id
        )
    return body


def response_start_event(
    request_id: str,
    status: int,
    headers: Iterable[tuple[str, str]],
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "type": "response_start",
        "status": int(status),
        "headers": [[name, value] for name, value in headers],
    }


def body_event(request_id: str, body: bytes) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "type": "body",
        "body_base64": base64.b64encode(body).decode("ascii"),
    }


def complete_event(request_id: str) -> dict[str, Any]:
    return {"request_id": request_id, "type": "complete"}


def error_event(
    request_id: str,
    code: str,
    message: str,
    *,
    status: int = 0,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "type": "error",
        "code": code,
        "message": message,
        "status": int(status),
    }


def encode_event(event: dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"))
