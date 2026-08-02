from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from desktop import main as desktop_main
from desktop.legacy_guard import LegacyService


class _FakeApplication:
    def __init__(self, _argv) -> None:
        self.names: list[tuple[str, str]] = []

    def setApplicationName(self, value: str) -> None:
        self.names.append(("name", value))

    def setApplicationDisplayName(self, value: str) -> None:
        self.names.append(("display", value))

    def setOrganizationName(self, value: str) -> None:
        self.names.append(("organization", value))


def _install_fake_qt(monkeypatch, warnings: list[tuple] | None = None) -> None:
    package = ModuleType("PySide6")
    package.__path__ = []
    qt_core = ModuleType("PySide6.QtCore")
    qt_widgets = ModuleType("PySide6.QtWidgets")

    class QCoreApplication:
        @staticmethod
        def setAttribute(_attribute) -> None:
            return None

    class QMessageBox:
        @staticmethod
        def warning(*args) -> None:
            if warnings is not None:
                warnings.append(args)

        @staticmethod
        def critical(*_args) -> None:
            return None

    qt_core.QCoreApplication = QCoreApplication
    qt_core.Qt = SimpleNamespace(
        ApplicationAttribute=SimpleNamespace(AA_ShareOpenGLContexts=object()),
    )
    qt_core.QTimer = SimpleNamespace(singleShot=lambda *_args: None)
    qt_widgets.QApplication = _FakeApplication
    qt_widgets.QMessageBox = QMessageBox
    monkeypatch.setitem(sys.modules, "PySide6", package)
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", qt_core)
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", qt_widgets)


def _bootstrap_to_paths(monkeypatch, tmp_path, *, user_root: Path):
    paths = SimpleNamespace(user_root=user_root)
    monkeypatch.setattr(desktop_main, "resolve_resource_root", lambda: tmp_path / "bundle")
    monkeypatch.setattr(desktop_main, "register_tavern_scheme", lambda: None)
    monkeypatch.setattr(
        desktop_main,
        "prepare_desktop_environment",
        lambda *_args, **_kwargs: paths,
    )
    return paths


def test_bootstrap_log_and_smoke_write_failures_degrade_to_stable_exit(
    monkeypatch,
    tmp_path,
    capsys,
):
    stages: list[str] = []

    def record_failed_log(stage: str, _exc: BaseException):
        stages.append(stage)
        return None

    def fail_smoke_write(_path: Path, _result: dict) -> None:
        raise OSError("private filesystem detail")

    monkeypatch.setattr(desktop_main, "_append_bootstrap_log", record_failed_log)
    monkeypatch.setattr(desktop_main, "_write_smoke_result", fail_smoke_write)

    result = desktop_main._fail_before_qt(
        smoke_path=tmp_path / "result.json",
        stage="bootstrap_environment",
        smoke_error="desktop_bootstrap_failed",
        exc=OSError("private bootstrap detail"),
    )

    assert result == 4
    assert stages == ["bootstrap_environment", "smoke_result_write"]
    assert not (tmp_path / "result.json").exists()
    stderr = capsys.readouterr().err
    assert "bootstrap_environment" in stderr
    assert "private bootstrap detail" not in stderr
    assert "private filesystem detail" not in stderr


def test_migration_failure_writes_stable_smoke_error_and_keeps_detail_out(
    monkeypatch,
    tmp_path,
):
    _install_fake_qt(monkeypatch)
    _bootstrap_to_paths(monkeypatch, tmp_path, user_root=Path("C:/local-tavern"))
    monkeypatch.setattr(desktop_main, "paths_touch_root", lambda *_args: True)
    monkeypatch.setattr(desktop_main, "detect_verified_legacy_service", lambda _root: None)
    monkeypatch.setattr(
        desktop_main,
        "migrate_legacy_data",
        lambda *_args: (_ for _ in ()).throw(OSError("private migration path")),
    )
    monkeypatch.setattr(
        desktop_main,
        "_append_bootstrap_log",
        lambda *_args: tmp_path / "bootstrap.log",
    )
    result_path = (tmp_path / "smoke.json").resolve()

    exit_code = desktop_main.main(["--smoke-test", str(result_path)])

    assert exit_code == 4
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["error"] == "desktop_migration_failed"
    assert payload["ready"] is False
    assert payload["api_live"] is False
    assert "private migration path" not in result_path.read_text(encoding="utf-8")


def test_normal_launch_with_custom_user_root_still_runs_legacy_guard(
    monkeypatch,
    tmp_path,
):
    warnings: list[tuple] = []
    _install_fake_qt(monkeypatch, warnings)
    custom_root = tmp_path / "custom-user-root"
    _bootstrap_to_paths(monkeypatch, tmp_path, user_root=custom_root)
    checked_roots: list[Path] = []

    def detect(root: Path):
        checked_roots.append(root)
        return LegacyService(pid=4312, port=54321, project_root=root)

    monkeypatch.setattr(desktop_main, "detect_verified_legacy_service", detect)
    monkeypatch.setattr(
        desktop_main,
        "paths_touch_root",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("普通启动不得按 custom root 跳过旧服务检查")
        ),
    )

    exit_code = desktop_main.main([])

    assert exit_code == 3
    assert checked_roots == [desktop_main.LEGACY_ROOT]
    assert len(warnings) == 1


def test_cleanup_releases_single_instance_even_when_runtime_shutdown_fails():
    class FailingRuntime:
        def shutdown(self, *, timeout):
            assert timeout == 30.0
            raise TimeoutError("runtime stuck")

    class Instance:
        released = False

        def release(self):
            self.released = True

    instance = Instance()
    try:
        desktop_main._cleanup_desktop_resources(FailingRuntime(), instance)
    except TimeoutError as exc:
        assert str(exc) == "runtime stuck"
    else:
        raise AssertionError("runtime 关闭失败必须继续上抛")

    assert instance.released is True


def test_desktop_renderer_defaults_to_software_and_preserves_existing_flags(
    monkeypatch,
    tmp_path,
):
    settings_path = tmp_path / "settings.json"
    monkeypatch.delenv("TAVERN_DESKTOP_RENDERER", raising=False)
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "--lang=zh-CN --disable-gpu")

    mode = desktop_main._configure_desktop_renderer(settings_path)

    assert mode == "software"
    assert desktop_main.os.environ["TAVERN_DESKTOP_RENDERER_ACTIVE"] == "software"
    flags = desktop_main.os.environ["QTWEBENGINE_CHROMIUM_FLAGS"].split()
    assert flags.count("--disable-gpu") == 1
    assert "--lang=zh-CN" in flags


def test_desktop_renderer_reads_hardware_setting_and_environment_override(
    monkeypatch,
    tmp_path,
):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"desktop_renderer": "hardware"}),
        encoding="utf-8",
    )
    monkeypatch.delenv("TAVERN_DESKTOP_RENDERER", raising=False)
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "--lang=zh-CN")

    assert desktop_main._configure_desktop_renderer(settings_path) == "hardware"
    assert "--disable-gpu" not in desktop_main.os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]

    monkeypatch.setenv("TAVERN_DESKTOP_RENDERER", "software")
    assert desktop_main._configure_desktop_renderer(settings_path) == "software"
    assert "--disable-gpu" in desktop_main.os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]


def test_desktop_renderer_invalid_or_corrupt_settings_fail_closed_to_software(
    monkeypatch,
    tmp_path,
):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text('{"desktop_renderer":"invalid"}', encoding="utf-8")
    monkeypatch.delenv("TAVERN_DESKTOP_RENDERER", raising=False)
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)

    assert desktop_main._resolve_desktop_renderer(settings_path) == "software"
    settings_path.write_text("{broken", encoding="utf-8")
    assert desktop_main._resolve_desktop_renderer(settings_path) == "software"
