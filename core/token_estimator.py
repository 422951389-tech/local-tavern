"""可替换的 Prompt token 保守估算器。

默认实现不假装等同于任一模型 tokenizer。它按 UTF-8 字节计数并增加消息
边界开销；对中英文、emoji 和未知 byte-fallback tokenizer 都保留安全余量。
模型专用 tokenizer 只需实现同一协议即可替换。
"""
from __future__ import annotations

from typing import Protocol, Sequence


class TokenEstimator(Protocol):
    """PromptAssembler 使用的最小估算协议。"""

    estimator_id: str

    def estimate_text(self, text: str) -> int:
        """返回文本的保守 token 估算量。"""

    def estimate_messages(self, messages: Sequence[dict]) -> int:
        """返回含角色和消息边界开销的保守估算量。"""


class ConservativeTokenEstimator:
    """以一个 UTF-8 字节至多占一个 token 的规则进行保守估算。"""

    estimator_id = "utf8_bytes_v1"
    message_overhead = 8
    reply_overhead = 4

    def estimate_text(self, text: str) -> int:
        if not text:
            return 0
        return len(str(text).encode("utf-8"))

    def estimate_messages(self, messages: Sequence[dict]) -> int:
        total = self.reply_overhead
        for message in messages:
            total += self.message_overhead
            total += self.estimate_text(str(message.get("role", "")))
            total += self.estimate_text(str(message.get("content", "")))
        return total


DEFAULT_TOKEN_ESTIMATOR = ConservativeTokenEstimator()
