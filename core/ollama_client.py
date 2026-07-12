"""Ollama 客户端 — 异步流式调用 Ollama /api/chat

处理：
- 流式响应（NDJSON）
- thinking 字段剥离
- 错误重试
- 模型切换
"""
import json
import logging
import re
from typing import AsyncIterator, Optional

import httpx

logger = logging.getLogger(__name__)

OLLAMA_HOST = "http://localhost:11434"
DEFAULT_TIMEOUT = 300.0  # 35B 首字可能慢


class OllamaClient:
    def __init__(self, host: str = OLLAMA_HOST):
        self.host = host
        self._client: Optional[httpx.AsyncClient] = None
        self._warned_json_parse = False

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.host,
                timeout=httpx.Timeout(DEFAULT_TIMEOUT, connect=10.0),
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def list_models(self) -> list[str]:
        """列出 Ollama 中所有可用模型"""
        client = await self._ensure_client()
        try:
            r = await client.get("/api/tags")
            r.raise_for_status()
            data = r.json()
            return [m["name"] for m in data.get("models", [])]
        except Exception as e:
            logger.error("列出模型失败: %s", e)
            return []

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        think: bool = True,
        num_predict: int = 4096,
        temperature: float = 0.8,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
    ) -> AsyncIterator[dict]:
        """流式调用 /api/chat

        每次 yield 一个 dict:
          - {"type": "thinking", "content": "..."}
          - {"type": "content", "content": "..."}
          - {"type": "done", "content": ""}
          - {"type": "error", "content": "..."}
        """
        client = await self._ensure_client()
        options = {
            "num_predict": num_predict,
            "temperature": temperature,
        }
        # top_p/top_k 仅在显式传入时透传，避免覆盖 Ollama 默认行为
        if top_p is not None:
            options["top_p"] = top_p
        if top_k is not None:
            options["top_k"] = top_k
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": think,
            "options": options,
        }

        try:
            async with client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code != 200:
                    err = await resp.aread()
                    yield {"type": "error", "content": f"HTTP {resp.status_code}: {err.decode('utf-8', errors='ignore')}"}
                    return

                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        # E1：限流日志，防止 LLM 抖动时刷屏
                        if not self._warned_json_parse:
                            logger.warning("无法解析 chunk（后续同型警告将忽略）: %s", line[:200])
                            self._warned_json_parse = True
                        continue

                    # Ollama 流式 chunk 结构：
                    # {"message": {"role": "assistant", "content": "...", "thinking": "..."}, "done": false}
                    msg = chunk.get("message", {})

                    thinking = msg.get("thinking", "")
                    content = msg.get("content", "")

                    # 某些模型在 think=false 时仍把思考过程写在 content 里
                    # 过滤 <thinking>...</thinking> 块
                    if not think:
                        content = re.sub(r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL)

                    if thinking and think:
                        yield {"type": "thinking", "content": thinking}
                    if content:
                        yield {"type": "content", "content": content}

                    if chunk.get("done"):
                        yield {"type": "done", "content": ""}
                        return
        except httpx.RequestError as e:
            yield {"type": "error", "content": f"网络错误: {e}"}
        except Exception as e:
            logger.exception("流式调用异常")
            yield {"type": "error", "content": f"未知错误: {e}"}

    async def summarize_once(
        self,
        model: str,
        dropped_messages: list[dict],
        num_predict: int = 1024,
        temperature: float = 0.5,
    ) -> str:
        """非流式一次性调用，让模型把被截断的对话压成梗概。

        使用 prompts/summary.md 作为模板（单一事实源），
        文件不存在时回退硬编码兜底。返回模型产出的原始文本字符串。
        失败抛异常，由调用方兜底。
        """
        from core.config import PROMPTS_DIR

        client = await self._ensure_client()
        convo = []
        for m in dropped_messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            convo.append(f"[{role}]: {content}")
        convo_str = "\n".join(convo)

        template_path = PROMPTS_DIR / "summary.md"
        if template_path.exists():
            system_prompt = template_path.read_text(encoding="utf-8")
        else:
            system_prompt = (
                "你是剧情备忘记录员。把下面这段已发生的对话压缩成梗概，"
                "只填空，不要自由发挥、不要新增剧情。"
                "严格遵守以下模板逐行填写；可选字段填不出就写\"无\"，不要编造：\n"
                "前情提要(必填,1-3句纯文本): ...\n"
                "时间线(可选,一句话): ...\n"
                "关键事件(可选,每条一句,最多5条,逐行写):\n"
                "- ...\n"
                "角色关系/立场(可选,每条一句,逐行写):\n"
                "- ...\n"
            )
        system_prompt += "\n\n被总结的对话:\n"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": convo_str},
            ],
            "stream": False,
            "think": False,
            "options": {"num_predict": num_predict, "temperature": temperature},
        }

        resp = await client.post("/api/chat", json=payload, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        msg = data.get("message", {})
        content = msg.get("content", "") or ""
        content = re.sub(r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL)
        return content.strip()


# 全局单例
_client_instance: Optional[OllamaClient] = None


def get_client() -> OllamaClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = OllamaClient()
    return _client_instance