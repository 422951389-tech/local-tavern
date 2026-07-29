"""统一模型 Provider 协议、安全错误与流式解析工具。"""
from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import unquote, urlsplit, urlunsplit


ProviderEvent = dict[str, Any]
DNSResolver = Callable[[str], Iterable[str]]


@dataclass(frozen=True)
class ProviderCapabilities:
    model_listing: bool = True
    streaming_chat: bool = True
    summarize: bool = True
    request_thinking: bool = False
    stream_thinking: bool = True

    def as_public(self) -> dict[str, bool]:
        return asdict(self)


class ProviderError(RuntimeError):
    """只携带可公开的稳定错误码与消息。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)

    def as_detail(self) -> dict[str, object]:
        details: dict[str, object] = {}
        if self.http_status is not None:
            details["upstream_status"] = self.http_status
        return {
            "code": self.code,
            "message": self.message,
            "details": details,
        }


class ProviderValidationError(ProviderError):
    """Provider 配置未通过安全边界。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


@runtime_checkable
class ModelProvider(Protocol):
    """聊天、模型枚举与摘要使用的统一异步协议。"""

    capabilities: ProviderCapabilities

    async def list_models(self) -> list[str]: ...

    async def get_context_limit(self, model: str) -> dict[str, object]: ...

    def chat_stream(
        self,
        model: str,
        messages: list[dict],
        think: bool = True,
        num_predict: int = 4096,
        temperature: float = 0.8,
        top_p: float | None = None,
        top_k: int | None = None,
        num_ctx: int | None = None,
    ) -> AsyncIterator[ProviderEvent]: ...

    async def summarize_once(
        self,
        model: str,
        dropped_messages: list[dict],
        num_predict: int = 1024,
        temperature: float = 0.5,
    ) -> str: ...

    async def close(self) -> None: ...


def _system_resolver(host: str) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ProviderValidationError(
            "provider_host_unresolved",
            "Provider 主机无法解析",
        ) from exc
    return tuple(sorted({record[4][0].split("%", 1)[0] for record in records}))


def _is_public_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_global
    except ValueError:
        return False


def validate_cloud_base_url(
    value: object,
    *,
    resolver: DNSResolver | None = None,
    resolve_dns: bool = True,
) -> str:
    """返回规范化公网 HTTPS 根地址，并阻断 SSRF 常见入口。"""

    if not isinstance(value, str) or not value or value != value.strip():
        raise ProviderValidationError("provider_url_invalid", "Provider URL 无效")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ProviderValidationError("provider_url_invalid", "Provider URL 无效")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ProviderValidationError("provider_url_invalid", "Provider URL 无效") from exc
    if parsed.scheme.casefold() != "https" or not parsed.netloc:
        raise ProviderValidationError(
            "provider_url_https_required",
            "云端 Provider 必须使用 HTTPS",
        )
    if parsed.username is not None or parsed.password is not None:
        raise ProviderValidationError(
            "provider_url_credentials_forbidden",
            "Provider URL 不得包含凭据",
        )
    if parsed.query or parsed.fragment:
        raise ProviderValidationError(
            "provider_url_components_forbidden",
            "Provider URL 不得包含查询或片段",
        )
    if port == 0:
        raise ProviderValidationError("provider_url_invalid", "Provider URL 无效")
    host = (parsed.hostname or "").rstrip(".").casefold()
    if not host:
        raise ProviderValidationError("provider_url_invalid", "Provider URL 无效")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ProviderValidationError("provider_url_invalid", "Provider URL 无效") from exc
    if (
        ascii_host == "localhost"
        or ascii_host.endswith((".localhost", ".local", ".internal", ".home.arpa"))
    ):
        raise ProviderValidationError(
            "provider_url_private_forbidden",
            "Provider URL 必须指向公网主机",
        )

    decoded_path = unquote(parsed.path)
    if "\\" in decoded_path or any(
        segment in {".", ".."} for segment in decoded_path.split("/")
    ):
        raise ProviderValidationError("provider_url_invalid", "Provider URL 路径无效")

    try:
        literal = ipaddress.ip_address(ascii_host.split("%", 1)[0])
    except ValueError:
        if resolve_dns:
            addresses = tuple((resolver or _system_resolver)(ascii_host))
            if not addresses or any(not _is_public_address(address) for address in addresses):
                raise ProviderValidationError(
                    "provider_url_private_forbidden",
                    "Provider URL 必须指向公网主机",
                )
    else:
        if not literal.is_global:
            raise ProviderValidationError(
                "provider_url_private_forbidden",
                "Provider URL 必须指向公网主机",
            )

    rendered_host = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    netloc = rendered_host if port is None else f"{rendered_host}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit(("https", netloc, path, "", ""))


async def iter_sse_records(
    lines: AsyncIterator[str],
) -> AsyncIterator[tuple[str, str]]:
    """按 SSE 记录边界解析 event/data，并保留注释 keepalive。"""

    event_name = "message"
    data_parts: list[str] = []
    saw_comment = False

    async def flush() -> tuple[str, str] | None:
        nonlocal event_name, data_parts, saw_comment
        result: tuple[str, str] | None = None
        if data_parts:
            result = (event_name, "\n".join(data_parts))
        elif saw_comment:
            result = ("keepalive", "")
        event_name = "message"
        data_parts = []
        saw_comment = False
        return result

    async for raw_line in lines:
        line = raw_line.rstrip("\r")
        if not line:
            record = await flush()
            if record is not None:
                yield record
            continue
        if line.startswith(":"):
            saw_comment = True
            continue
        field, separator, raw_value = line.partition(":")
        value = raw_value[1:] if separator and raw_value.startswith(" ") else raw_value
        if field == "event":
            event_name = value or "message"
        elif field == "data":
            data_parts.append(value)

    record = await flush()
    if record is not None:
        yield record


def stream_error_event(error: ProviderError) -> ProviderEvent:
    event: ProviderEvent = {
        "type": "error",
        "code": error.code,
        "content": error.message,
    }
    if error.http_status is not None:
        event["http_status"] = error.http_status
    return event


def strip_thinking_blocks(value: str) -> str:
    return re.sub(r"<thinking>.*?</thinking>", "", value, flags=re.DOTALL).strip()


def summary_messages(
    dropped_messages: list[dict],
    *,
    prompts_dir: Path | None = None,
) -> list[dict[str, str]]:
    """复用项目摘要模板构造跨 Provider 的非流式消息。"""

    if prompts_dir is None:
        from core.config import PROMPTS_DIR

        prompts_dir = PROMPTS_DIR
    template_path = Path(prompts_dir) / "summary.md"
    if template_path.exists():
        system_prompt = template_path.read_text(encoding="utf-8")
    else:
        system_prompt = (
            "你是剧情备忘记录员。把下面这段已发生的对话压缩成梗概，"
            "只填空，不要自由发挥、不要新增剧情。"
            "前情提要必须简洁；无法确认的可选字段写‘无’。"
        )
    conversation = "\n".join(
        f"[{message.get('role', 'user')}]: {message.get('content', '')}"
        for message in dropped_messages
        if isinstance(message, Mapping)
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": conversation},
    ]
