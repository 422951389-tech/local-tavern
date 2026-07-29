"""QLockFile + QLocalServer 单实例与前台激活。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path


try:
    from PySide6.QtCore import QLockFile, QObject
    from PySide6.QtNetwork import QLocalServer, QLocalSocket

    PYSIDE6_AVAILABLE = True
except ImportError:
    PYSIDE6_AVAILABLE = False


def instance_name(user_root: Path) -> str:
    canonical = str(Path(user_root).resolve(strict=False)).casefold().encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()[:20]
    return f"LocalTavern.Desktop.v1.{digest}"


if PYSIDE6_AVAILABLE:

    class SingleInstance(QObject):
        def __init__(
            self,
            user_root: Path,
            activate: Callable[[], None],
            parent: QObject | None = None,
        ) -> None:
            super().__init__(parent)
            self._user_root = Path(user_root).resolve(strict=False)
            self._user_root.mkdir(parents=True, exist_ok=True)
            self._name = instance_name(self._user_root)
            self._activate = activate
            self._lock = QLockFile(str(self._user_root / "runtime" / "desktop.lock"))
            self._lock.setStaleLockTime(0)
            self._server = QLocalServer(self)
            self._server.newConnection.connect(self._accept_connections)
            self._owned = False

        @property
        def owned(self) -> bool:
            return self._owned

        def acquire(self) -> bool:
            if self._owned:
                return True
            (self._user_root / "runtime").mkdir(parents=True, exist_ok=True)
            if not self._lock.tryLock(0):
                self._notify_existing()
                return False
            QLocalServer.removeServer(self._name)
            if not self._server.listen(self._name):
                self._lock.unlock()
                raise RuntimeError("无法建立桌面单实例通道")
            self._owned = True
            return True

        def release(self) -> None:
            if not self._owned:
                return
            self._server.close()
            QLocalServer.removeServer(self._name)
            self._lock.unlock()
            self._owned = False

        def _notify_existing(self) -> None:
            socket = QLocalSocket()
            socket.connectToServer(self._name)
            if not socket.waitForConnected(500):
                socket.abort()
                return
            socket.write(b"activate\n")
            socket.flush()
            socket.waitForBytesWritten(500)
            socket.disconnectFromServer()

        def _accept_connections(self) -> None:
            while self._server.hasPendingConnections():
                socket = self._server.nextPendingConnection()
                if socket is None:
                    continue
                socket.readyRead.connect(
                    lambda connection=socket: self._read_activation(connection)
                )
                if socket.bytesAvailable():
                    self._read_activation(socket)

        def _read_activation(self, socket: QLocalSocket) -> None:
            payload = bytes(socket.readAll())
            if b"activate" in payload:
                self._activate()
            socket.disconnectFromServer()
            socket.deleteLater()

else:

    class SingleInstance:  # pragma: no cover - 仅提供清晰的缺依赖错误
        def __init__(self, *_args, **_kwargs) -> None:
            raise RuntimeError("桌面运行需要 PySide6 QtNetwork")
