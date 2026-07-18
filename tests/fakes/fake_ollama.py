"""不访问网络的 Ollama 测试替身。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import AsyncIterator


NORMAL_REPLY = """📍 隔离测试酒馆 | ⏱️ 午后 / 晴

🎯 [主线] 任务名称：验证测试隔离
📌 当前场景：临时数据目录
➡️ 下一目标：完成自动化验证

👤 测试用户

🎭 测试角色 | 💝 ████░░░░░░ 40%

💭 内心想法：当前响应来自 fake Ollama。

👗 穿着：测试服

🧍 当前姿势：站立

💬 对白："隔离测试响应。" (预期影响：验证正常流)

💡 行动建议
- 继续验证
"""


class FakeOllamaClient:
    """提供可脚本化事件的内存客户端，并记录全部调用。"""

    def __init__(self) -> None:
        self.models = ["fake-model:latest"]
        self.events: list[dict] = []
        self.delay = 0.0
        self.chat_calls: list[dict] = []
        self.summary_calls: list[dict] = []
        self.context_limit_calls: list[str] = []
        self.context_limits: dict[str, dict] = {
            "fake-model:latest": {
                "context_limit": 32768,
                "source": "fake_model_metadata",
            }
        }
        self.closed = False
        self.block_before_first = False
        self.pause_after: int | None = None
        self.entered = asyncio.Event()
        self.paused = asyncio.Event()
        self.release = asyncio.Event()
        self.configure("normal")

    def configure(
        self,
        scenario: str,
        *,
        delay: float = 0.0,
        block_before_first: bool = False,
        pause_after: int | None = None,
    ) -> "FakeOllamaClient":
        scenarios = {
            "normal": [
                {"type": "thinking", "content": "测试思考"},
                {"type": "content", "content": NORMAL_REPLY},
                {"type": "done", "content": ""},
            ],
            "error": [
                {
                    "type": "error",
                    "code": "upstream_http_error",
                    "http_status": 503,
                    "content": "fake Ollama 上游错误",
                },
            ],
            "eof": [
                {"type": "content", "content": "未完成的 fake 响应"},
            ],
        }
        if scenario not in scenarios:
            raise ValueError(f"未知 fake Ollama 场景: {scenario}")
        self.events = deepcopy(scenarios[scenario])
        self.delay = delay
        self.block_before_first = block_before_first
        self.pause_after = pause_after
        self.entered = asyncio.Event()
        self.paused = asyncio.Event()
        self.release = asyncio.Event()
        return self

    async def list_models(self) -> list[str]:
        return list(self.models)

    async def probe_health(self) -> dict[str, object]:
        return {"ok": bool(self.models), **({} if self.models else {"code": "no_models"})}

    async def get_context_limit(self, model: str) -> dict:
        self.context_limit_calls.append(model)
        return deepcopy(self.context_limits.get(model, {
            "context_limit": 32768,
            "source": "fake_fallback",
        }))

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
    ) -> AsyncIterator[dict]:
        self.chat_calls.append({
            "model": model,
            "messages": deepcopy(messages),
            "think": think,
            "num_predict": num_predict,
            "num_ctx": num_ctx,
            "temperature": temperature,
            "top_p": top_p,
            "top_k": top_k,
        })
        self.entered.set()
        if self.block_before_first:
            await self.release.wait()
        for index, event in enumerate(self.events, start=1):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield deepcopy(event)
            if self.pause_after == index:
                self.paused.set()
                await self.release.wait()

    async def summarize_once(
        self,
        model: str,
        dropped_messages: list[dict],
        num_predict: int = 1024,
        temperature: float = 0.5,
    ) -> str:
        self.summary_calls.append({
            "model": model,
            "dropped_messages": deepcopy(dropped_messages),
            "num_predict": num_predict,
            "temperature": temperature,
        })
        return "前情提要: fake Ollama 测试总结"

    async def close(self) -> None:
        self.closed = True
