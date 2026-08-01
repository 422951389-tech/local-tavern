"""Anthropic Messages API 原生 Provider。"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from core.model_provider import (
    DNSResolver,
    ProviderError,
    ProviderCapabilities,
    iter_sse_records,
    stream_error_event,
    strip_thinking_blocks,
    summary_messages,
)
from core.providers.http_base import HTTPProviderBase


class AnthropicProvider(HTTPProviderBase):
    capabilities = ProviderCapabilities(request_thinking=False)

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        configured_models: tuple[str, ...] = (),
        context_limit: int = 32_768,
        resolver: DNSResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ProviderError("provider_credential_missing", "Provider 凭据未配置")
        self._api_key = api_key
        self._configured_models = tuple(configured_models)
        super().__init__(
            base_url,
            resolver=resolver,
            transport=transport,
            context_limit=context_limit,
        )

    def _headers(self) -> Mapping[str, str]:
        return {
            "Accept": "application/json",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
        }

    async def list_models(self) -> list[str]:
        try:
            body = await self._request_json("GET", "v1/models")
        except ProviderError as exc:
            if exc.http_status in {404, 405, 501} and self._configured_models:
                return sorted(set(self._configured_models))
            raise
        rows = body.get("data")
        if not isinstance(rows, list):
            raise ProviderError("provider_invalid_response", "Provider 返回内容无效")
        models = {
            row.get("id")
            for row in rows
            if isinstance(row, Mapping) and isinstance(row.get("id"), str) and row["id"]
        }
        if not models and self._configured_models:
            return sorted(set(self._configured_models))
        return sorted(models)

    @staticmethod
    def _messages_payload(messages: list[dict]) -> tuple[str | None, list[dict]]:
        system_parts: list[str] = []
        converted: list[dict] = []
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            role = message.get("role")
            content = message.get("content")
            if not isinstance(content, str):
                continue
            if role == "system":
                system_parts.append(content)
            elif role in {"user", "assistant"}:
                converted.append({"role": role, "content": content})
        return ("\n\n".join(system_parts) or None, converted)

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        think: bool = True,
        num_predict: int = 4096,
        temperature: float = 0.8,
        top_p: float | None = None,
        top_k: int | None = None,
        num_ctx: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        del num_ctx
        system, converted = self._messages_payload(messages)
        payload: dict[str, Any] = {
            "model": model,
            "messages": converted,
            "stream": True,
            "max_tokens": num_predict,
            "temperature": temperature,
        }
        if system is not None:
            payload["system"] = system
        if top_p is not None:
            payload["top_p"] = top_p
        if top_k is not None:
            payload["top_k"] = top_k
        client = await self._ensure_client()
        try:
            async with client.stream("POST", "v1/messages", json=payload) as response:
                if not 200 <= response.status_code < 300:
                    yield stream_error_event(self._http_error(response.status_code))
                    return
                async for event_name, data in iter_sse_records(
                    self.iter_bounded_sse_lines(response)
                ):
                    if event_name in {"keepalive", "ping"}:
                        yield {"type": "keepalive", "content": ""}
                        continue
                    try:
                        chunk = json.loads(data)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(chunk, Mapping):
                        continue
                    event_type = chunk.get("type") or event_name
                    if event_type == "message_stop":
                        yield {"type": "done", "content": ""}
                        return
                    if event_type == "error":
                        yield stream_error_event(
                            ProviderError("provider_stream_error", "Provider 流式响应失败")
                        )
                        return
                    if event_type != "content_block_delta":
                        continue
                    delta = chunk.get("delta")
                    if not isinstance(delta, Mapping):
                        continue
                    if delta.get("type") == "thinking_delta":
                        thinking = delta.get("thinking")
                        if think and isinstance(thinking, str) and thinking:
                            yield {"type": "thinking", "content": thinking}
                    elif delta.get("type") == "text_delta":
                        text = delta.get("text")
                        if isinstance(text, str) and text:
                            yield {"type": "content", "content": text}
        except ProviderError as exc:
            yield stream_error_event(exc)
        except httpx.RequestError as exc:
            yield stream_error_event(self._network_error(exc))

    async def summarize_once(
        self,
        model: str,
        dropped_messages: list[dict],
        num_predict: int = 1024,
        temperature: float = 0.5,
    ) -> str:
        system, messages = self._messages_payload(summary_messages(dropped_messages))
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "max_tokens": num_predict,
            "temperature": temperature,
        }
        if system is not None:
            payload["system"] = system
        body = await self._request_json("POST", "v1/messages", payload=payload)
        blocks = body.get("content")
        if not isinstance(blocks, list):
            raise ProviderError("provider_invalid_response", "Provider 返回内容无效")
        text = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, Mapping)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
        if not text:
            raise ProviderError("provider_invalid_response", "Provider 返回内容无效")
        return strip_thinking_blocks(text)
