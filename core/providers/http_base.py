"""云端 Provider 共用的安全 httpx 边界。"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from core.model_provider import (
    DNSResolver,
    ProviderError,
    resolve_public_addresses,
    validate_cloud_base_url,
)


DEFAULT_PROVIDER_TIMEOUT = 300.0
PROVIDER_RESPONSE_MAX_BYTES = 16 * 1024 * 1024
PROVIDER_SSE_LINE_MAX_BYTES = 2 * 1024 * 1024


def _response_too_large() -> ProviderError:
    return ProviderError(
        "provider_response_too_large",
        "Provider 响应超过本地安全上限",
    )


class _PinnedAsyncHTTPTransport(httpx.AsyncBaseTransport):
    """把实际 TCP 目标固定到已校验 IP，同时保留原 Host 与 TLS SNI。

    HTTPX 仍看到原始请求 URL；只在默认真实网络 transport 的最内层把 URL
    host 替换成已验证地址，并通过 ``sni_hostname`` 扩展让 httpcore 继续按
    原域名验证证书。这样既不二次解析 DNS，也不降低 TLS 身份校验。
    """

    def __init__(self, hostname: str, addresses: tuple[str, ...]) -> None:
        if not addresses:
            raise ValueError("Provider 固定连接地址不能为空")
        self._hostname = hostname.rstrip(".").casefold()
        self._addresses = addresses
        self._transport = httpx.AsyncHTTPTransport(trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request_host = request.url.raw_host.decode("ascii").rstrip(".").casefold()
        if request_host != self._hostname:
            raise httpx.ConnectError(
                "Provider 请求主机超出固定连接边界",
                request=request,
            )
        headers = request.headers.copy()
        headers["Host"] = request.url.netloc.decode("ascii")
        extensions = dict(request.extensions)
        extensions["sni_hostname"] = self._hostname
        for index, address in enumerate(self._addresses):
            pinned = httpx.Request(
                method=request.method,
                url=request.url.copy_with(host=address),
                headers=headers,
                stream=request.stream,
                extensions=extensions,
            )
            try:
                # AsyncHTTPTransport 负责把 httpcore 网络异常稳定映射为
                # httpx.RequestError；只在尚未发送正文的 connect 失败时尝试
                # 下一个已验证地址，禁止对写入阶段故障重放 POST。
                return await self._transport.handle_async_request(pinned)
            except httpx.ConnectError:
                if index + 1 >= len(self._addresses):
                    raise
        raise AssertionError("Provider 固定连接地址循环未返回")

    async def aclose(self) -> None:
        await self._transport.aclose()


class HTTPProviderBase:
    def __init__(
        self,
        base_url: str,
        *,
        resolver: DNSResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_PROVIDER_TIMEOUT,
        context_limit: int = 32_768,
    ) -> None:
        self._resolver = resolver
        self.base_url = validate_cloud_base_url(base_url, resolver=resolver)
        self._transport = transport
        self._timeout = timeout
        self._context_limit = context_limit
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    def _headers(self) -> Mapping[str, str]:
        return {"Accept": "application/json"}

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None and not self._client.is_closed:
            return self._client
        async with self._client_lock:
            if self._client is not None and not self._client.is_closed:
                return self._client
            # 显式注入的 MockTransport 保留测试路径；真实网络必须把连接固定到
            # 这一次解析并验证通过的公网地址，禁止 HTTP 栈再次查询 DNS。
            transport = self._transport
            if transport is None:
                normalized = validate_cloud_base_url(
                    self.base_url,
                    resolve_dns=False,
                )
                hostname = urlsplit(normalized).hostname or ""
                addresses = await asyncio.to_thread(
                    resolve_public_addresses,
                    hostname,
                    resolver=self._resolver,
                )
                transport = _PinnedAsyncHTTPTransport(hostname, addresses)
            self._client = httpx.AsyncClient(
                base_url=f"{self.base_url}/",
                headers=dict(self._headers()),
                timeout=httpx.Timeout(self._timeout, connect=10.0),
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            )
            return self._client

    async def close(self) -> None:
        async with self._client_lock:
            if self._client is not None and not self._client.is_closed:
                await self._client.aclose()

    @staticmethod
    def _check_content_length(
        response: httpx.Response,
        *,
        limit: int = PROVIDER_RESPONSE_MAX_BYTES,
    ) -> None:
        """用声明长度快速拒绝；实际解压后字节仍由读取循环复核。"""

        for value in response.headers.get_list("content-length"):
            for item in value.split(","):
                text = item.strip()
                if text.isdecimal() and int(text) > limit:
                    raise _response_too_large()

    async def _read_response_bytes(
        self,
        response: httpx.Response,
        *,
        limit: int = PROVIDER_RESPONSE_MAX_BYTES,
    ) -> bytes:
        """有界读取解压后的响应正文。"""

        self._check_content_length(response, limit=limit)
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > limit:
                raise _response_too_large()
            chunks.append(chunk)
        return b"".join(chunks)

    async def iter_bounded_sse_lines(
        self,
        response: httpx.Response,
        *,
        total_limit: int = PROVIDER_RESPONSE_MAX_BYTES,
        line_limit: int = PROVIDER_SSE_LINE_MAX_BYTES,
    ) -> AsyncIterator[str]:
        """逐行读取解压后的 SSE，限制总量和无换行单条记录。"""

        self._check_content_length(response, limit=total_limit)
        pending = bytearray()
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > total_limit:
                raise _response_too_large()
            pending.extend(chunk)
            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    if len(pending) > line_limit:
                        raise _response_too_large()
                    break
                if newline > line_limit:
                    raise _response_too_large()
                line = bytes(pending[:newline])
                del pending[: newline + 1]
                yield line.decode("utf-8", errors="replace")
        if pending:
            if len(pending) > line_limit:
                raise _response_too_large()
            yield bytes(pending).decode("utf-8", errors="replace")

    async def get_context_limit(self, model: str) -> dict[str, object]:
        """云端协议没有统一的上下文元数据接口，使用本机保守配置值。"""
        del model
        return {
            "context_limit": self._context_limit,
            "source": "provider_config",
        }

    @staticmethod
    def _http_error(status_code: int) -> ProviderError:
        if status_code in {401, 403}:
            return ProviderError(
                "provider_auth_failed",
                "Provider 凭据验证失败",
                http_status=status_code,
            )
        if status_code == 429:
            return ProviderError(
                "provider_rate_limited",
                "Provider 请求过于频繁",
                http_status=status_code,
            )
        return ProviderError(
            "provider_http_error",
            "Provider 返回了错误状态",
            http_status=status_code,
        )

    @staticmethod
    def _network_error(exc: Exception) -> ProviderError:
        if isinstance(exc, httpx.TimeoutException):
            return ProviderError("provider_timeout", "Provider 请求超时")
        return ProviderError("provider_unreachable", "Provider 无法连接")

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = await self._ensure_client()
        try:
            async with client.stream(method, path, json=payload) as response:
                if not 200 <= response.status_code < 300:
                    raise self._http_error(response.status_code)
                raw_body = await self._read_response_bytes(response)
        except httpx.RequestError as exc:
            raise self._network_error(exc) from exc
        try:
            body = httpx.Response(200, content=raw_body).json()
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ProviderError(
                "provider_invalid_response",
                "Provider 返回内容无效",
            ) from exc
        if not isinstance(body, dict):
            raise ProviderError(
                "provider_invalid_response",
                "Provider 返回内容无效",
            )
        return body
