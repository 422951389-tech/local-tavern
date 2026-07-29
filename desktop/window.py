"""受限 QtWebEngine 窗口。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from .qt_bridge import TavernBridge
from .resources import TAVERN_HOST, TAVERN_SCHEME, TavernSchemeHandler


logger = logging.getLogger(__name__)

try:
    from PySide6.QtCore import Qt, QTimer, QUrl, Signal
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineCore import (
        QWebEngineLoadingInfo,
        QWebEnginePage,
        QWebEngineProfile,
        QWebEngineScript,
    )
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import (
        QFileDialog,
        QFrame,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QStackedWidget,
        QVBoxLayout,
        QWidget,
    )

    PYSIDE6_AVAILABLE = True
except ImportError:
    PYSIDE6_AVAILABLE = False


if PYSIDE6_AVAILABLE:

    class _LockedPage(QWebEnginePage):
        blockedNavigation = Signal(str)
        diagnostic = Signal(str)
        consoleMessage = Signal(str)

        def acceptNavigationRequest(self, url, navigation_type, is_main_frame):  # noqa: N802
            del navigation_type
            if not is_main_frame:
                return True
            if url.scheme() == TAVERN_SCHEME.decode("ascii") and url.host() == TAVERN_HOST:
                return True
            if url.scheme() == "about" and url.toString() == "about:blank":
                return True
            self.blockedNavigation.emit(url.toString())
            return False

        def createWindow(self, _window_type):  # noqa: N802
            return None

        def javaScriptConsoleMessage(self, level, message, line_number, source_id):  # noqa: N802
            del line_number, source_id
            self.consoleMessage.emit(str(message))
            if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
                lowered = str(message).casefold()
                if "frame-ancestors" in lowered and "ignored" in lowered:
                    return
                if "qwebchannel" in lowered:
                    code = "console_qwebchannel_error"
                elif "content security policy" in lowered or "csp" in lowered:
                    code = "console_csp_error"
                else:
                    code = "console_javascript_error"
                self.diagnostic.emit(code)


    class TavernWindow(QMainWindow):
        pageLoaded = Signal(bool)

        def __init__(
            self,
            web_root: Path,
            bridge: TavernBridge,
            shutdown: Callable[[], None],
            parent=None,
        ) -> None:
            super().__init__(parent)
            self._shutdown = shutdown
            self._closed = False
            self._last_load_result: bool | None = None
            self._last_diagnostic = ""
            self._loading_started = False
            self._renderer_reload_attempts = 0
            self._automatic_reload_total = 0
            self._reload_request_count = 0
            self._injected_scripts: list[QWebEngineScript] = []
            self.setWindowTitle("本地酒馆 · 叙事工作台")
            self.resize(1440, 920)
            self.setMinimumSize(960, 640)

            # 无 storageName 的 profile 是 off-the-record；资源与 API 均不经网络缓存。
            self._profile = QWebEngineProfile(self)
            self._profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
            self._profile.setPersistentCookiesPolicy(
                QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
            )
            self._scheme_handler = TavernSchemeHandler(web_root, self._profile)
            self._profile.installUrlSchemeHandler(TAVERN_SCHEME, self._scheme_handler)

            self._page = _LockedPage(self._profile, self)
            self._channel = QWebChannel(self._page)
            self._channel.registerObject("tavernBridge", bridge)
            self._page.setWebChannel(self._channel)
            self._page.blockedNavigation.connect(self._navigation_blocked)
            self._page.diagnostic.connect(self._record_diagnostic)
            self._page.loadingChanged.connect(self._loading_changed)
            self._page.renderProcessTerminated.connect(self._renderer_terminated)

            self._view = QWebEngineView(self)
            self._view.setPage(self._page)
            self._view.loadFinished.connect(self._record_load_result)
            self._recovery = self._build_recovery_layer()
            self._content_stack = QStackedWidget(self)
            self._content_stack.addWidget(self._view)
            self._content_stack.addWidget(self._recovery)
            self._content_stack.setCurrentWidget(self._view)
            self.setCentralWidget(self._content_stack)
            self._profile.downloadRequested.connect(self._download_requested)

        @property
        def page(self) -> QWebEnginePage:
            return self._page

        @property
        def is_off_the_record(self) -> bool:
            return self._profile.isOffTheRecord()

        @property
        def last_load_result(self) -> bool | None:
            return self._last_load_result

        @property
        def last_diagnostic(self) -> str:
            return self._last_diagnostic

        @property
        def recovery_visible(self) -> bool:
            return self._content_stack.currentWidget() is self._recovery

        @property
        def renderer_reload_attempts(self) -> int:
            return self._renderer_reload_attempts

        @property
        def automatic_reload_total(self) -> int:
            return self._automatic_reload_total

        @property
        def reload_request_count(self) -> int:
            return self._reload_request_count

        def activate(self) -> None:
            if self.isMinimized():
                self.showNormal()
            self.show()
            self.raise_()
            self.activateWindow()

        def start_loading(self) -> None:
            if self._loading_started:
                return
            self._loading_started = True
            self._view.setUrl(QUrl("tavern://app/index.html"))

        def _build_recovery_layer(self) -> QWidget:
            container = QWidget(self)
            container.setObjectName("desktopRecoveryLayer")
            outer = QVBoxLayout(container)
            outer.setContentsMargins(48, 48, 48, 48)
            outer.addStretch(1)

            card = QFrame(container)
            self._recovery_card = card
            card.setObjectName("desktopRecoveryCard")
            card.setMinimumWidth(620)
            card.setMinimumHeight(410)
            card.setMaximumWidth(760)
            layout = QVBoxLayout(card)
            layout.setContentsMargins(40, 36, 40, 36)
            layout.setSpacing(16)

            eyebrow = QLabel("本地界面恢复", card)
            eyebrow.setObjectName("desktopRecoveryEyebrow")
            title = QLabel("界面暂时无法显示", card)
            title.setObjectName("desktopRecoveryTitle")
            self._recovery_message = QLabel(
                "应用后台仍在本机运行。请选择重新加载界面，或查看诊断信息。",
                card,
            )
            self._recovery_message.setObjectName("desktopRecoveryMessage")
            self._recovery_message.setWordWrap(True)
            layout.addWidget(eyebrow)
            layout.addWidget(title)
            layout.addWidget(self._recovery_message)

            self._recovery_details = QPlainTextEdit(card)
            self._recovery_details.setObjectName("desktopRecoveryDetails")
            self._recovery_details.setReadOnly(True)
            self._recovery_details.setTabChangesFocus(True)
            self._recovery_details.setFixedHeight(96)
            self._recovery_details.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self._recovery_details.hide()
            layout.addWidget(self._recovery_details)
            layout.addStretch(1)

            actions = QHBoxLayout()
            actions.setSpacing(12)
            self._recovery_reload_button = QPushButton("重新加载界面", card)
            self._recovery_reload_button.setObjectName("desktopRecoveryPrimary")
            self._recovery_reload_button.setAccessibleName("重新加载本地酒馆界面")
            self._recovery_reload_button.setMinimumHeight(44)
            self._recovery_reload_button.setDefault(True)
            self._recovery_diagnostics_button = QPushButton("打开诊断信息", card)
            self._recovery_diagnostics_button.setObjectName("desktopRecoverySecondary")
            self._recovery_diagnostics_button.setAccessibleName("打开界面诊断信息")
            self._recovery_diagnostics_button.setMinimumHeight(44)
            self._recovery_exit_button = QPushButton("退出", card)
            self._recovery_exit_button.setObjectName("desktopRecoveryExit")
            self._recovery_exit_button.setAccessibleName("安全退出本地酒馆")
            self._recovery_exit_button.setMinimumHeight(44)
            actions.addWidget(self._recovery_reload_button)
            actions.addWidget(self._recovery_diagnostics_button)
            actions.addStretch(1)
            actions.addWidget(self._recovery_exit_button)
            layout.addLayout(actions)

            self._recovery_reload_button.clicked.connect(self._reload_interface)
            self._recovery_diagnostics_button.clicked.connect(
                self._toggle_recovery_diagnostics
            )
            self._recovery_exit_button.clicked.connect(self.close)
            outer.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
            outer.addStretch(1)

            container.setStyleSheet("""
                QWidget#desktopRecoveryLayer {
                    background: #0c0a09;
                    color: #f5f5f4;
                    font-family: "Microsoft YaHei UI";
                }
                QFrame#desktopRecoveryCard {
                    background: #1c1917;
                    border: 1px solid #57534e;
                    border-radius: 14px;
                }
                QLabel#desktopRecoveryEyebrow {
                    color: #e7b85b;
                    font-size: 14px;
                    font-weight: 700;
                }
                QLabel#desktopRecoveryTitle {
                    color: #fafaf9;
                    font-size: 28px;
                    font-weight: 700;
                }
                QLabel#desktopRecoveryMessage {
                    color: #d6d3d1;
                    font-size: 16px;
                }
                QPlainTextEdit#desktopRecoveryDetails {
                    color: #e7e5e4;
                    background: #0c0a09;
                    border: 1px solid #44403c;
                    border-radius: 8px;
                    padding: 12px;
                    font-family: "Microsoft YaHei UI";
                    font-size: 14px;
                }
                QPushButton {
                    border: 1px solid #78716c;
                    border-radius: 8px;
                    padding: 9px 16px;
                    color: #fafaf9;
                    background: #292524;
                    font-size: 15px;
                    font-weight: 600;
                }
                QPushButton:hover { background: #44403c; }
                QPushButton:focus { border: 2px solid #f5c96a; }
                QPushButton#desktopRecoveryPrimary {
                    color: #1c1917;
                    background: #e7b85b;
                    border-color: #f5c96a;
                }
                QPushButton#desktopRecoveryPrimary:hover { background: #f5c96a; }
                QPushButton#desktopRecoveryExit {
                    color: #fecaca;
                    border-color: #7f1d1d;
                    background: #2b1515;
                }
            """)
            return container

        def _show_recovery(self, message: str) -> None:
            self._recovery_message.setText(str(message))
            code = self._last_diagnostic or "page_state_unknown"
            self._recovery_details.setPlainText(
                "诊断代码：" + code
                + "\n运行方式：in_process_asgi / qwebchannel"
                + "\n数据状态：此恢复操作不会删除或重置本地存档。"
            )
            self._recovery_details.hide()
            self._recovery_diagnostics_button.setText("打开诊断信息")
            self._content_stack.setCurrentWidget(self._recovery)
            self._recovery_reload_button.setFocus(Qt.FocusReason.OtherFocusReason)

        def _hide_recovery(self) -> None:
            self._recovery_details.hide()
            self._recovery_diagnostics_button.setText("打开诊断信息")
            self._content_stack.setCurrentWidget(self._view)

        def _toggle_recovery_diagnostics(self) -> None:
            visible = not self._recovery_details.isVisible()
            self._recovery_details.setVisible(visible)
            self._recovery_diagnostics_button.setText(
                "收起诊断信息" if visible else "打开诊断信息"
            )
            if visible:
                self._recovery_details.setFocus(Qt.FocusReason.OtherFocusReason)
            self._recovery_card.adjustSize()
            self._recovery.layout().activate()

        def _reload_interface(self) -> None:
            if self._closed:
                return
            self._reload_request_count += 1
            self.statusBar().showMessage("正在重新加载本地界面…", 5000)
            self._request_page_reload()

        def _request_page_reload(self) -> None:
            if self._view.url().isEmpty():
                self._loading_started = True
                self._view.setUrl(QUrl("tavern://app/index.html"))
            else:
                self._view.reload()

        def install_document_script(self, name: str, source: str) -> None:
            if self._loading_started:
                raise RuntimeError("必须在页面开始加载前安装桌面脚本")
            script = QWebEngineScript()
            script.setName(str(name))
            script.setSourceCode(str(source))
            script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
            script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
            script.setRunsOnSubFrames(False)
            self._page.scripts().insert(script)
            self._injected_scripts.append(script)

        def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
            if self._closed:
                event.accept()
                return
            try:
                self._shutdown()
            except Exception as exc:  # noqa: BLE001 - 不允许带活动任务退出
                logger.error(
                    "desktop_window_shutdown_failed exception=%s",
                    type(exc).__name__,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                QMessageBox.critical(
                    self,
                    "本地酒馆无法安全退出",
                    "后台任务未能完整关闭。窗口将保持打开，请查看日志后重试。",
                )
                event.ignore()
                return
            self._closed = True
            event.accept()

        def _navigation_blocked(self, _url: str) -> None:
            self.statusBar().showMessage("已阻止离开本地酒馆的页面导航", 5000)

        def _record_load_result(self, ok: bool) -> None:
            self._last_load_result = bool(ok)
            if ok:
                self._renderer_reload_attempts = 0
                self._hide_recovery()
            else:
                if not self._last_diagnostic:
                    self._record_diagnostic("page_load_failed")
                self._show_recovery(
                    "本地界面文件未能加载。后台运行时不会重复启动；"
                    "请重新加载界面，失败时可展开诊断信息。"
                )
            self.pageLoaded.emit(bool(ok))

        def _record_diagnostic(self, code: str) -> None:
            self._last_diagnostic = str(code)[:120]

        def _loading_changed(self, info: QWebEngineLoadingInfo) -> None:
            if info.status() == QWebEngineLoadingInfo.LoadStatus.LoadFailedStatus:
                domain = int(info.errorDomain().value)
                code = int(info.errorCode())
                self._record_diagnostic(f"load_failed_{domain}_{code}")

        def _renderer_terminated(self, status, exit_code: int) -> None:
            if self._closed:
                return
            status_value = int(getattr(status, "value", status))
            self._record_diagnostic(
                f"renderer_terminated_{status_value}_{int(exit_code)}"
            )
            if self._renderer_reload_attempts < 1:
                self._renderer_reload_attempts += 1
                self._automatic_reload_total += 1
                self._show_recovery(
                    "界面渲染进程意外退出，正在自动重新加载一次。"
                    "后台运行时与本地存档保持不变。"
                )
                QTimer.singleShot(0, self._reload_interface)
                return
            self._show_recovery(
                "界面渲染进程再次退出，已停止自动重载以避免循环。"
                "请查看诊断信息后手动重载或安全退出。"
            )

        def _download_requested(self, download) -> None:
            suggested = download.downloadFileName() or "本地酒馆导出.json"
            selected, _filter = QFileDialog.getSaveFileName(
                self,
                "保存本地酒馆导出文件",
                suggested,
                "JSON 文件 (*.json);;所有文件 (*)",
            )
            if not selected:
                download.cancel()
                return
            path = Path(selected)
            download.setDownloadDirectory(str(path.parent))
            download.setDownloadFileName(path.name)
            download.accept()

else:

    class TavernWindow:  # pragma: no cover - 仅提供清晰的缺依赖错误
        def __init__(self, *_args, **_kwargs) -> None:
            raise RuntimeError("桌面运行需要 PySide6 QtWebEngine")
