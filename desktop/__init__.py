"""本地酒馆无端口桌面运行时。

该包只负责把现有 ASGI 应用嵌入 QtWebEngine；它不会启动 Uvicorn、
不会监听 TCP，也不会改变业务层路由契约。
"""

from .asgi_bridge import AsgiBridge
from .runtime import AsyncioRuntime

__all__ = ["AsgiBridge", "AsyncioRuntime"]
