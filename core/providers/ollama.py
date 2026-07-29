"""现有 OllamaClient 的统一 Provider 适配器。"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from core.model_provider import ProviderCapabilities
from core.ollama_client import OllamaClient, get_client


class OllamaProvider:
    capabilities = ProviderCapabilities(request_thinking=True)

    def __init__(self, client: OllamaClient | None = None) -> None:
        self._injected_client = client

    def _client(self) -> OllamaClient:
        # 默认每次动态读取，保留 tests/conftest.py 对全局 fake 的注入能力。
        return self._injected_client or get_client()

    async def list_models(self) -> list[str]:
        return await self._client().list_models()

    async def get_context_limit(self, model: str) -> dict[str, object]:
        return await self._client().get_context_limit(model)

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
        async for event in self._client().chat_stream(
            model=model,
            messages=messages,
            think=think,
            num_predict=num_predict,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            num_ctx=num_ctx,
        ):
            yield event

    async def summarize_once(
        self,
        model: str,
        dropped_messages: list[dict],
        num_predict: int = 1024,
        temperature: float = 0.5,
    ) -> str:
        return await self._client().summarize_once(
            model,
            dropped_messages,
            num_predict=num_predict,
            temperature=temperature,
        )

    async def close(self) -> None:
        if self._injected_client is not None:
            await self._injected_client.close()
