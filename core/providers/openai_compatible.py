"""OpenAI Chat Completions 兼容 Provider。"""
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


class OpenAICompatibleProvider(HTTPProviderBase):
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
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def list_models(self) -> list[str]:
        try:
            body = await self._request_json("GET", "models")
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
        del top_k, num_ctx
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "max_tokens": num_predict,
            "temperature": temperature,
        }
        if top_p is not None:
            payload["top_p"] = top_p
        client = await self._ensure_client()
        saw_finish = False
        try:
            async with client.stream("POST", "chat/completions", json=payload) as response:
                if not 200 <= response.status_code < 300:
                    yield stream_error_event(self._http_error(response.status_code))
                    return
                async for event_name, data in iter_sse_records(response.aiter_lines()):
                    if event_name in {"keepalive", "ping"}:
                        yield {"type": "keepalive", "content": ""}
                        continue
                    if data.strip() == "[DONE]":
                        yield {"type": "done", "content": ""}
                        return
                    try:
                        chunk = json.loads(data)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(chunk, Mapping):
                        continue
                    choices = chunk.get("choices")
                    if not isinstance(choices, list):
                        continue
                    for choice in choices:
                        if not isinstance(choice, Mapping):
                            continue
                        delta = choice.get("delta")
                        if isinstance(delta, Mapping):
                            reasoning = delta.get("reasoning_content")
                            content = delta.get("content")
                            if think and isinstance(reasoning, str) and reasoning:
                                yield {"type": "thinking", "content": reasoning}
                            if isinstance(content, str) and content:
                                yield {"type": "content", "content": content}
                        if choice.get("finish_reason") is not None:
                            saw_finish = True
        except httpx.RequestError as exc:
            yield stream_error_event(self._network_error(exc))
            return
        if saw_finish:
            yield {"type": "done", "content": ""}

    async def summarize_once(
        self,
        model: str,
        dropped_messages: list[dict],
        num_predict: int = 1024,
        temperature: float = 0.5,
    ) -> str:
        body = await self._request_json(
            "POST",
            "chat/completions",
            payload={
                "model": model,
                "messages": summary_messages(dropped_messages),
                "stream": False,
                "max_tokens": num_predict,
                "temperature": temperature,
            },
        )
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("provider_invalid_response", "Provider 返回内容无效")
        first = choices[0]
        message = first.get("message") if isinstance(first, Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str):
            raise ProviderError("provider_invalid_response", "Provider 返回内容无效")
        return strip_thinking_blocks(content)
