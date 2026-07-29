"""内置模型 Provider 适配器。"""

from core.providers.anthropic import AnthropicProvider
from core.providers.ollama import OllamaProvider
from core.providers.openai_compatible import OpenAICompatibleProvider

__all__ = [
    "AnthropicProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
]
