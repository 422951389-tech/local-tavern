from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from desktop import journey
from desktop import main as desktop_main
from desktop.journey import (
    DesktopJourneyError,
    JOURNEY_ASSISTANT_MARKER,
    JOURNEY_ENVIRONMENT_FLAG,
    JOURNEY_MODEL,
    JOURNEY_ROOT_PREFIX,
    JourneyProviderRegistry,
    journey_probe_script,
    validate_journey_paths,
)
from tools import desktop_journey_smoke
from tools.desktop_journey_smoke import (
    DesktopJourneySmokeError,
    _build_command,
    _build_isolated_environment,
    _validate_isolated_root,
    _validate_result,
    run_journey,
)


def _isolated_root() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix=JOURNEY_ROOT_PREFIX)


def _valid_result(pid: int, stage: str) -> dict:
    return {
        "schema_version": 1,
        "pid": pid,
        "stage": stage,
        "ready": True,
        "page_loaded": True,
        "api_live": True,
        "project_created": True,
        "saves_created": True,
        "chat_completed": True,
        "refresh_verified": True,
        "save_switch_verified": True,
        "persistence_verified": stage == "verify",
        "message_count": 2,
        "screenshot_saved": True,
        "runtime": "in_process_asgi",
        "transport": "qwebchannel",
        "scheme": "tavern://app",
        "tcp_listener_started": False,
        "off_the_record": True,
        "error": "",
    }


def test_desktop_hook_requires_flag_and_paths_inside_dedicated_temp_root(monkeypatch):
    with _isolated_root() as raw_root:
        root = Path(raw_root).resolve()
        result = root / "result.json"
        screenshot = root / "screen.png"
        for name in journey._JOURNEY_WRITABLE_ENVIRONMENTS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("TAVERN_BASE_DIR", str(root))
        monkeypatch.delenv(JOURNEY_ENVIRONMENT_FLAG, raising=False)
        with pytest.raises(DesktopJourneyError, match="环境开关"):
            validate_journey_paths(result, screenshot)

        monkeypatch.setenv(JOURNEY_ENVIRONMENT_FLAG, "1")
        assert validate_journey_paths(result, screenshot) == root
        with pytest.raises(DesktopJourneyError, match="截图路径"):
            validate_journey_paths(result, root.parent / "escaped.png")
        monkeypatch.setenv("TAVERN_PROJECTS_DIR", str(root.parent / "real-projects"))
        with pytest.raises(DesktopJourneyError, match="可写路径越界"):
            validate_journey_paths(result, screenshot)


def test_harness_rejects_non_dedicated_or_non_temp_roots(tmp_path):
    with pytest.raises(DesktopJourneySmokeError, match="隔离契约"):
        _validate_isolated_root(tmp_path)
    outside = Path("C:/local-tavern")
    if outside.is_dir():
        with pytest.raises(DesktopJourneySmokeError, match="隔离契约"):
            _validate_isolated_root(outside)


def test_isolated_environment_removes_inherited_tavern_paths(monkeypatch):
    monkeypatch.setenv("TAVERN_DATA_DIR", "C:/Users/zcw/real-data")
    monkeypatch.setenv("TAVERN_UNRELATED_TEST_LEAK", "must-be-removed")
    with _isolated_root() as raw_root:
        root = Path(raw_root).resolve()
        environment = _build_isolated_environment(root)

    assert "TAVERN_UNRELATED_TEST_LEAK" not in environment
    assert environment[JOURNEY_ENVIRONMENT_FLAG] == "1"
    assert environment["TAVERN_ALLOW_REMOTE"] == "false"
    assert environment["TAVERN_OLLAMA_HOST"] == "http://127.0.0.1:1"
    for name in desktop_journey_smoke._PATH_ENVIRONMENTS:
        assert Path(environment[name]).is_relative_to(root)


def test_command_exposes_journey_hook_without_network_or_real_user_paths(tmp_path):
    executable = tmp_path / "LocalTavern.exe"
    result = tmp_path / "result.json"
    screenshot = tmp_path / "screen.png"
    command = _build_command(
        executable,
        result_path=result,
        screenshot_path=screenshot,
        stage="seed",
        hold_ms=2500,
    )
    assert command == [
        str(executable),
        "--journey-test",
        str(result),
        "--journey-screenshot",
        str(screenshot),
        "--journey-stage",
        "seed",
        "--journey-hold-ms",
        "2500",
    ]
    with pytest.raises(DesktopJourneySmokeError, match="阶段无效"):
        _build_command(
            executable,
            result_path=result,
            screenshot_path=screenshot,
            stage="unknown",
            hold_ms=2500,
        )


def test_result_contract_requires_refresh_save_switch_restart_and_no_tcp():
    _validate_result(_valid_result(4242, "seed"), pid=4242, stage="seed")
    _validate_result(_valid_result(5252, "verify"), pid=5252, stage="verify")

    failed = _valid_result(4242, "seed")
    failed["save_switch_verified"] = False
    with pytest.raises(DesktopJourneySmokeError, match="save_switch_verified"):
        _validate_result(failed, pid=4242, stage="seed")

    listener = _valid_result(4242, "seed")
    listener["tcp_listener_started"] = True
    with pytest.raises(DesktopJourneySmokeError, match="TCP"):
        _validate_result(listener, pid=4242, stage="seed")


@pytest.mark.asyncio
async def test_fake_provider_is_in_process_and_deterministic():
    registry = JourneyProviderRegistry()
    assert await registry.list_models("desktop-journey-fake") == [JOURNEY_MODEL]
    lease = registry.lease("desktop-journey-fake")
    events = [
        event
        async for event in lease.provider.chat_stream(
            JOURNEY_MODEL,
            [{"role": "user", "content": "hello"}],
        )
    ]
    assert events == [
        {"type": "content", "content": JOURNEY_ASSISTANT_MARKER},
        {"type": "done", "content": ""},
    ]
    await lease.release()
    await registry.close()


def test_injected_script_drives_formal_api_refresh_and_ui_save_switch():
    source = journey_probe_script("seed")
    for required in (
        "TavernDesktopTransport.fetch",
        "'/api/projects'",
        "'/api/sessions'",
        "'/api/chat/turns'",
        "'/health/live'",
        "location.reload()",
        "save_switch_verified",
        "current-session-label",
    ):
        assert required in source
    assert "http://" not in source
    assert "https://" not in source


def test_two_stage_runner_reuses_one_isolated_root_and_preserves_screenshots(
    monkeypatch,
    tmp_path,
):
    executable = tmp_path / "LocalTavern.exe"
    executable.write_bytes(b"fixture")
    artifacts = tmp_path / "artifacts"
    roots: list[Path] = []

    def fake_launch(
        _executable,
        *,
        root,
        stage,
        timeout_seconds,
        hold_ms,
    ):
        del timeout_seconds, hold_ms
        roots.append(root)
        screenshot = root / f"{stage}.png"
        screenshot.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        return {
            "stage": stage,
            "status": "passed",
            "message_count": 2,
            "refresh_verified": True,
            "save_switch_verified": True,
            "persistence_verified": stage == "verify",
            "tcp_listener_samples": [[], []],
            "process_exited": True,
        }, screenshot

    monkeypatch.setattr(desktop_journey_smoke, "_launch_stage", fake_launch)
    result = run_journey(
        executable,
        artifacts_dir=artifacts,
        timeout_seconds=5,
        hold_ms=1500,
    )

    assert result["status"] == "passed"
    assert result["restart_persistence_verified"] is True
    assert len(roots) == 2 and roots[0] == roots[1]
    assert roots[0].name.startswith(JOURNEY_ROOT_PREFIX)
    assert (artifacts / "desktop-journey-seed.png").is_file()
    assert (artifacts / "desktop-journey-verify.png").is_file()
    assert (artifacts / "desktop-journey-result.json").is_file()


def test_install_fake_registry_refuses_normal_launch(monkeypatch):
    monkeypatch.delenv(JOURNEY_ENVIRONMENT_FLAG, raising=False)
    with pytest.raises(DesktopJourneyError, match="普通桌面启动"):
        journey.install_journey_provider_registry()


def test_desktop_cli_refuses_journey_hook_without_environment_flag(monkeypatch, tmp_path):
    monkeypatch.delenv(JOURNEY_ENVIRONMENT_FLAG, raising=False)
    with pytest.raises(SystemExit) as stopped:
        desktop_main.main([
            "--journey-test",
            str((tmp_path / "result.json").resolve()),
            "--journey-screenshot",
            str((tmp_path / "screen.png").resolve()),
            "--journey-stage",
            "seed",
        ])
    assert stopped.value.code == 2
