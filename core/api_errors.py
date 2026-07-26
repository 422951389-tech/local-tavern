"""HTTP API 错误契约的单一事实源。"""
from __future__ import annotations

from http import HTTPStatus
from typing import Any, Iterable, Mapping

from starlette.responses import JSONResponse, Response


ERROR_SCHEMA_VERSION = 1

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

_DEFAULT_MESSAGES = {
    400: "请求无效",
    401: "请先完成身份验证",
    403: "请求被拒绝",
    404: "请求的资源不存在",
    405: "请求方法不受支持",
    409: "请求与当前状态冲突",
    413: "请求内容过大",
    422: "请求内容无法处理",
    429: "请求过于频繁",
    500: "服务内部错误",
    503: "服务暂不可用",
}


def default_error_message(status_code: int) -> str:
    """返回稳定且不包含底层异常文本的默认消息。"""
    if status_code in _DEFAULT_MESSAGES:
        return _DEFAULT_MESSAGES[status_code]
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "请求失败"


def error_payload(
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """构造严格 v1 envelope；顶层永远只有 ``error``。"""
    safe_code = code if isinstance(code, str) and code else "internal_error"
    safe_message = message if isinstance(message, str) and message else "请求失败"
    safe_details = dict(details) if isinstance(details, Mapping) else {}
    return {
        "error": {
            "schema_version": ERROR_SCHEMA_VERSION,
            "code": safe_code,
            "message": safe_message,
            "details": safe_details,
        }
    }


def apply_security_headers(response: Response) -> Response:
    """对成功与错误响应应用同一组安全头。"""
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
    *,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    response = JSONResponse(
        status_code=status_code,
        content=error_payload(code, message, details),
        headers=dict(headers) if headers else None,
    )
    apply_security_headers(response)
    return response


def _normalized_detail(
    status_code: int,
    detail: object,
) -> tuple[str, str, dict[str, Any]]:
    fallback_code = f"http_{status_code}"
    fallback_message = default_error_message(status_code)
    if isinstance(detail, str):
        return fallback_code, detail or fallback_message, {}
    if not isinstance(detail, Mapping):
        return fallback_code, fallback_message, {}

    code = detail.get("code")
    message = detail.get("message")
    normalized_code = code if isinstance(code, str) and code else fallback_code
    normalized_message = (
        message if isinstance(message, str) and message else fallback_message
    )

    nested = detail.get("details")
    details = dict(nested) if isinstance(nested, Mapping) else {}
    for key, value in detail.items():
        if key not in {"code", "message", "details", "schema_version"}:
            details[key] = value
    return normalized_code, normalized_message, details


def http_error_response(
    status_code: int,
    detail: object,
    *,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """将 FastAPI/Starlette 的旧 ``detail`` 归一到 v1 envelope。"""
    code, message, details = _normalized_detail(status_code, detail)
    return error_response(
        status_code,
        code,
        message,
        details,
        headers=headers,
    )


def _utf8_safe(value: object) -> object:
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, tuple):
        return [_utf8_safe(item) for item in value]
    if isinstance(value, list):
        return [_utf8_safe(item) for item in value]
    return value


def validation_error_detail(errors: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """
    构造脱敏校验错误；严禁将 input、ctx、url 或原始请求体出域。
    """
    issues: list[dict[str, Any]] = []
    for error in errors:
        issue = {
            key: _utf8_safe(error[key])
            for key in ("type", "loc", "msg")
            if key in error
        }
        issues.append(issue)
    return {
        "code": "request_validation_failed",
        "message": "请求参数校验失败",
        "details": {"issues": issues},
    }
