"""由桌面依赖 Python 直接执行的 Qt offscreen 恢复层验收。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from desktop.main import _cleanup_desktop_resources
from desktop.single_instance import SingleInstance
from desktop.window import TavernWindow


class _Bridge(QObject):
    pass


class _RendererStatus:
    value = 1


def _pump(application: QApplication, count: int = 8) -> None:
    for _index in range(count):
        application.processEvents()


def main() -> int:
    application = QApplication.instance() or QApplication(["desktop-window-offscreen"])
    # Windows 的 offscreen platform 不枚举系统字体；显式加载与生产界面一致的
    # 中文 UI 字体，保证截图能检查真实文案而不是占位方框。
    font_ids = [
        QFontDatabase.addApplicationFont(str(path))
        for path in (
            Path("C:/Windows/Fonts/msyh.ttc"),
            Path("C:/Windows/Fonts/msyhbd.ttc"),
        )
        if path.is_file()
    ]
    assert font_ids and all(font_id >= 0 for font_id in font_ids)
    shutdown_calls: list[str] = []
    reload_calls: list[str] = []

    with tempfile.TemporaryDirectory(prefix="local-tavern-window-offscreen-") as raw_root:
        root = Path(raw_root)
        web_root = root / "web"
        web_root.mkdir()
        window = TavernWindow(web_root, _Bridge(), lambda: shutdown_calls.append("shutdown"))
        window._request_page_reload = lambda: reload_calls.append("reload")
        window.show()
        _pump(application)

        window._view.loadFinished.emit(False)
        _pump(application)
        assert window.recovery_visible
        assert window.last_diagnostic == "page_load_failed"
        assert window._recovery_reload_button.minimumHeight() >= 44
        assert window._recovery_reload_button.accessibleName()
        assert window._recovery_diagnostics_button.accessibleName()
        assert window._recovery_exit_button.accessibleName()

        window._recovery_diagnostics_button.click()
        _pump(application)
        assert not window._recovery_details.isHidden()
        assert "page_load_failed" in window._recovery_details.toPlainText()
        screenshot_target = os.environ.get("TAVERN_RECOVERY_SCREENSHOT", "").strip()
        if screenshot_target:
            screenshot = Path(screenshot_target).resolve(strict=False)
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            assert window.grab().save(str(screenshot), "PNG")

        window._recovery_reload_button.click()
        _pump(application)
        assert reload_calls == ["reload"]
        assert shutdown_calls == []

        window._view.loadFinished.emit(True)
        _pump(application)
        assert not window.recovery_visible
        assert window.renderer_reload_attempts == 0

        window._renderer_terminated(_RendererStatus(), -1)
        _pump(application)
        assert window.recovery_visible
        assert window.renderer_reload_attempts == 1
        assert window.automatic_reload_total == 1
        assert reload_calls == ["reload", "reload"]
        assert shutdown_calls == []

        window._renderer_terminated(_RendererStatus(), -2)
        _pump(application)
        assert window.renderer_reload_attempts == 1
        assert window.automatic_reload_total == 1
        assert reload_calls == ["reload", "reload"]
        assert "已停止自动重载" in window._recovery_message.text()

        window._view.loadFinished.emit(True)
        _pump(application)
        assert not window.recovery_visible
        assert window.renderer_reload_attempts == 0

        window._renderer_terminated(_RendererStatus(), -3)
        _pump(application)
        assert window.automatic_reload_total == 2
        assert reload_calls == ["reload", "reload", "reload"]

        window._recovery_exit_button.click()
        _pump(application)
        assert shutdown_calls == ["shutdown"]

        class _FailingRuntime:
            def shutdown(self, *, timeout: float) -> None:
                assert timeout == 30.0
                raise TimeoutError("forced runtime failure")

        instance_root = root / "single-instance"
        first = SingleInstance(instance_root, lambda: None)
        assert first.acquire()
        try:
            _cleanup_desktop_resources(_FailingRuntime(), first)
        except TimeoutError:
            pass
        else:
            raise AssertionError("清理失败必须向调用方报告")
        assert not first.owned

        second = SingleInstance(instance_root, lambda: None)
        assert second.acquire(), "失败清理后不应残留单实例锁"
        second.release()

    print(json.dumps({
        "status": "passed",
        "manual_reload_requests": 1,
        "automatic_reload_incidents": 2,
        "automatic_reload_per_incident": 1,
        "runtime_restarts": 0,
        "single_instance_reacquired": True,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
