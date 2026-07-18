"""Ollama 客户端 — 异步流式调用 Ollama /api/chat

处理：
- 流式响应（NDJSON）
- thinking 字段剥离
- 错误重试
- 模型切换
"""
import asyncio
import json
import logging
import re
import time
from typing import AsyncIterator, Optional

import httpx

from core.config import (
    MODEL_CONTEXT_CACHE_SECONDS,
    OLLAMA_HEALTH_TIMEOUT_MS,
    OLLAMA_HOST,
    PROMPT_CONTEXT_FALLBACK,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300.0  # 35B 首字可能慢


class OllamaClient:
    def __init__(
        self,
        host: str = OLLAMA_HOST,
        *,
        health_transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.host = host
        self._client: Optional[httpx.AsyncClient] = None
        self._health_transport = health_transport
        self._warned_json_parse = False
        self._context_limit_cache: dict[str, tuple[float, dict]] = {}
        self._context_limit_lock = asyncio.Lock()

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

    async def probe_health(self) -> dict[str, object]:
        """用独立短超时检查 Ollama；结果不含 URL、版本或模型名。"""
        timeout_seconds = OLLAMA_HEALTH_TIMEOUT_MS / 1000
        try:
            async with httpx.AsyncClient(
                base_url=self.host,
                timeout=httpx.Timeout(
                    timeout_seconds,
                    connect=min(1.0, timeout_seconds),
                ),
                transport=self._health_transport,
            ) as client:
                response = await client.get("/api/tags")
            if response.status_code != 200:
                return {"ok": False, "code": "bad_status"}
            try:
                payload = response.json()
            except (ValueError, TypeError):
                return {"ok": False, "code": "invalid_response"}
            models = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(models, list):
                return {"ok": False, "code": "invalid_response"}
            if not models:
                return {"ok": False, "code": "no_models"}
            return {"ok": True}
        except httpx.TimeoutException as exc:
            logger.warning(
                "ollama_health code=timeout exception=%s",
                type(exc).__name__,
            )
            return {"ok": False, "code": "timeout"}
        except httpx.RequestError as exc:
            logger.warning(
                "ollama_health code=unreachable exception=%s",
                type(exc).__name__,
            )
            return {"ok": False, "code": "unreachable"}
        except Exception as exc:
            logger.warning(
                "ollama_health code=invalid_response exception=%s",
                type(exc).__name__,
            )
            return {"ok": False, "code": "invalid_response"}

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

    @staticmethod
    def _valid_context_limit(value: object) -> int | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if isinstance(value, float) and not value.is_integer():
            return None
        parsed = int(value)
        if not 256 <= parsed <= 1_048_576:
            return None
        return parsed

    @classmethod
    def _context_limit_from_show(cls, payload: object) -> tuple[int, str] | None:
        if not isinstance(payload, dict):
            return None
        model_info = payload.get("model_info")
        info_limit: int | None = None
        if isinstance(model_info, dict):
            architecture = model_info.get("general.architecture")
            if isinstance(architecture, str) and architecture:
                info_limit = cls._valid_context_limit(
                    model_info.get(f"{architecture}.context_length")
                )
            if info_limit is None:
                candidates = [
                    parsed
                    for key, value in model_info.items()
                    if isinstance(key, str)
                    and key.endswith(".context_length")
                    and not any(part in key.casefold() for part in ("vision", "clip"))
                    and (parsed := cls._valid_context_limit(value)) is not None
                ]
                if candidates:
                    info_limit = max(candidates)

        parameter_limit: int | None = None
        parameters = payload.get("parameters")
        if isinstance(parameters, str):
            match = re.search(r"(?m)^\s*num_ctx\s+([0-9]+)\s*$", parameters)
            if match:
                parameter_limit = cls._valid_context_limit(int(match.group(1)))

        if parameter_limit is not None:
            if info_limit is not None:
                parameter_limit = min(parameter_limit, info_limit)
            return parameter_limit, "ollama_show_parameters"
        if info_limit is not None:
            return info_limit, "ollama_show_model_info"
        return None

    async def get_context_limit(self, model: str) -> dict:
        """读取并缓存模型上下文上限；任何元数据故障都返回配置回退值。"""

        now = time.monotonic()
        cached = self._context_limit_cache.get(model)
        if cached and cached[0] > now:
            return dict(cached[1])

        async with self._context_limit_lock:
            now = time.monotonic()
            cached = self._context_limit_cache.get(model)
            if cached and cached[0] > now:
                return dict(cached[1])

            result = {
                "context_limit": PROMPT_CONTEXT_FALLBACK,
                "source": "fallback_default",
            }
            client = await self._ensure_client()
            try:
                response = await client.post("/api/show", json={"model": model})
                response.raise_for_status()
                parsed = self._context_limit_from_show(response.json())
                if parsed is not None:
                    result = {"context_limit": parsed[0], "source": parsed[1]}
            except Exception as exc:
                logger.warning(
                    "读取模型上下文上限失败，使用配置回退: %s",
                    type(exc).__name__,
                )

            self._context_limit_cache[model] = (
                now + MODEL_CONTEXT_CACHE_SECONDS,
                dict(result),
            )
            return result

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        think: bool = True,
        num_predict: int = 4096,
        temperature: float = 0.8,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
        num_ctx: Optional[int] = None,
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
        if num_ctx is not None:
            options["num_ctx"] = num_ctx
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
                    yield {
                        "type": "error",
                        "code": "upstream_http_error",
                        "http_status": resp.status_code,
                        "content": f"HTTP {resp.status_code}: {err.decode('utf-8', errors='ignore')}",
                    }
                    return

                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        # E1：限流日志，防止 LLM 抖动时刷屏
                        if not self._warned_json_parse:
                            logger.warning(
                                "invalid_ndjson_chunk length=%s subsequent_warnings=suppressed",
                                len(line),
                            )
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
            yield {
                "type": "error",
                "code": "upstream_network_error",
                "content": f"网络错误: {e}",
            }
        except Exception as e:
            logger.exception("流式调用异常")
            yield {
                "type": "error",
                "code": "upstream_internal_error",
                "content": f"未知错误: {e}",
            }

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
