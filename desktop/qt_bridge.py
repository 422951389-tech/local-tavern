"""QWebChannel 暴露对象；业务桥仍位于不依赖 Qt 的 asgi_bridge.py。"""

from __future__ import annotations

import json
import logging
from typing import Any

from .protocol import encode_event, error_event
from .runtime import AsyncioRuntime


logger = logging.getLogger(__name__)

try:
    from PySide6.QtCore import QObject, Qt, Signal, Slot

    PYSIDE6_AVAILABLE = True
except ImportError:
    PYSIDE6_AVAILABLE = False


if PYSIDE6_AVAILABLE:

    class TavernBridge(QObject):
        """页面中注册名固定为 ``tavernBridge``。"""

        # QObject 自身已有 event(QEvent*) 虚方法，不能用同名 Signal 覆盖；
        # 页面把 bridgeEvent 视为协议中的逻辑 event 流。
        bridgeEvent = Signal(str)
        _worker_event = Signal(str)

        def __init__(self, runtime: AsyncioRuntime, parent: QObject | None = None) -> None:
            super().__init__(parent)
            self._runtime = runtime
            self._worker_event.connect(
                self._publish_event,
                Qt.ConnectionType.QueuedConnection,
            )
            runtime.set_event_sink(self.publish_from_worker)

        @Slot(str)
        def dispatch(self, request_json: str) -> None:
            try:
                payload = json.loads(request_json)
            except (TypeError, json.JSONDecodeError):
                self.publish_from_worker(
                    error_event("", "invalid_json", "桌面请求不是有效 JSON")
                )
                return
            self._runtime.dispatch(payload)

        @Slot(str)
        def cancel(self, request_id: str) -> None:
            if not isinstance(request_id, str):
                return
            self._runtime.cancel(request_id)

        def publish_from_worker(self, event: dict[str, Any]) -> None:
            self._worker_event.emit(encode_event(event))

        @Slot(str)
        def _publish_event(self, payload: str) -> None:
            self.bridgeEvent.emit(payload)

else:

    class TavernBridge:  # pragma: no cover - 仅提供清晰的缺依赖错误
        def __init__(self, *_args, **_kwargs) -> None:
            raise RuntimeError("桌面运行需要 PySide6")
