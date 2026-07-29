"""云端 Provider 共用的安全 httpx 边界。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from core.model_provider import DNSResolver, ProviderError, validate_cloud_base_url


DEFAULT_PROVIDER_TIMEOUT = 300.0


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

    def _headers(self) -> Mapping[str, str]:
        return {"Accept": "application/json"}

    async def _ensure_client(self) -> httpx.AsyncClient:
        validate_cloud_base_url(self.base_url, resolver=self._resolver)
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=f"{self.base_url}/",
                headers=dict(self._headers()),
                timeout=httpx.Timeout(self._timeout, connect=10.0),
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

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
            response = await client.request(method, path, json=payload)
        except httpx.RequestError as exc:
            raise self._network_error(exc) from exc
        if not 200 <= response.status_code < 300:
            raise self._http_error(response.status_code)
        try:
            body = response.json()
        except (TypeError, ValueError) as exc:
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
