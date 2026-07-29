from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class _Signal:
    def __init__(self) -> None:
        self._listeners = []

    def connect(self, listener) -> None:
        self._listeners.append(listener)

    def emit(self) -> None:
        for listener in tuple(self._listeners):
            listener()


def _load_with_fake_qt(monkeypatch):
    locks: set[str] = set()
    servers: dict[str, object] = {}

    class QObject:
        def __init__(self, parent=None) -> None:
            self.parent = parent

    class QLockFile:
        def __init__(self, path: str) -> None:
            self.path = path
            self.locked = False

        def setStaleLockTime(self, _milliseconds: int) -> None:  # noqa: N802
            return None

        def tryLock(self, _milliseconds: int) -> bool:  # noqa: N802
            if self.path in locks:
                return False
            locks.add(self.path)
            self.locked = True
            return True

        def unlock(self) -> None:
            if self.locked:
                locks.discard(self.path)
                self.locked = False

    class QLocalServer(QObject):
        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.newConnection = _Signal()  # noqa: N815 - Qt API
            self.pending = []
            self.name = None

        @staticmethod
        def removeServer(name: str) -> None:  # noqa: N802
            servers.pop(name, None)

        def listen(self, name: str) -> bool:
            if name in servers:
                return False
            self.name = name
            servers[name] = self
            return True

        def close(self) -> None:
            if self.name:
                servers.pop(self.name, None)

        def hasPendingConnections(self) -> bool:  # noqa: N802
            return bool(self.pending)

        def nextPendingConnection(self):  # noqa: N802
            return self.pending.pop(0) if self.pending else None

    class QLocalSocket:
        def __init__(self) -> None:
            self.readyRead = _Signal()  # noqa: N815 - Qt API
            self.server = None
            self.payload = b""

        def connectToServer(self, name: str) -> None:  # noqa: N802
            self.server = servers.get(name)

        def waitForConnected(self, _milliseconds: int) -> bool:  # noqa: N802
            return self.server is not None

        def abort(self) -> None:
            self.server = None

        def write(self, payload: bytes) -> int:
            self.payload += payload
            if self.server is not None:
                self.server.pending.append(self)
                self.server.newConnection.emit()
            return len(payload)

        def flush(self) -> bool:
            return True

        def waitForBytesWritten(self, _milliseconds: int) -> bool:  # noqa: N802
            return True

        def disconnectFromServer(self) -> None:  # noqa: N802
            return None

        def bytesAvailable(self) -> int:  # noqa: N802
            return len(self.payload)

        def readAll(self) -> bytes:  # noqa: N802
            result = self.payload
            self.payload = b""
            return result

        def deleteLater(self) -> None:  # noqa: N802
            return None

    package = types.ModuleType("PySide6")
    qt_core = types.ModuleType("PySide6.QtCore")
    qt_core.QLockFile = QLockFile
    qt_core.QObject = QObject
    qt_network = types.ModuleType("PySide6.QtNetwork")
    qt_network.QLocalServer = QLocalServer
    qt_network.QLocalSocket = QLocalSocket
    monkeypatch.setitem(sys.modules, "PySide6", package)
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", qt_core)
    monkeypatch.setitem(sys.modules, "PySide6.QtNetwork", qt_network)

    module_name = "_desktop_single_instance_acceptance"
    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "desktop" / "single_instance.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module, locks, servers


def test_second_desktop_instance_signals_first_then_exits(monkeypatch, tmp_path):
    module, locks, servers = _load_with_fake_qt(monkeypatch)
    activations: list[str] = []

    first = module.SingleInstance(tmp_path, lambda: activations.append("activate"))
    second = module.SingleInstance(tmp_path, lambda: activations.append("wrong"))
    assert first.acquire()
    assert first.owned
    assert len(locks) == 1
    assert len(servers) == 1

    assert not second.acquire()
    assert not second.owned
    assert activations == ["activate"]
    assert len(locks) == 1
    assert len(servers) == 1

    first.release()
    assert not first.owned
    assert locks == set()
    assert servers == {}

    assert second.acquire()
    assert second.owned
    second.release()


def test_instance_identity_is_stable_per_user_root(monkeypatch, tmp_path):
    module, _locks, _servers = _load_with_fake_qt(monkeypatch)
    left = tmp_path / "left"
    right = tmp_path / "right"
    assert module.instance_name(left) == module.instance_name(left / ".")
    assert module.instance_name(left) != module.instance_name(right)
    assert module.instance_name(left).startswith("LocalTavern.Desktop.v1.")
