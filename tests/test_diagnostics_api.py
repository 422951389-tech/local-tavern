import json
from uuid import UUID

import pytest

from core import diagnostics
from core.diagnostics import recent_error_codes, turn_store_summary


def test_recent_error_codes_never_returns_raw_log_messages(tmp_path):
    secret = "sk-test-secret-value"
    prompt = "这是绝不能进入支持包的 Prompt 正文"
    log_file = tmp_path / "tavern.log"
    log_file.write_text(
        "2026-07-29T12:00:00 [ERROR] routes.chat: "
        f"code=provider_failed api_key={secret} prompt={prompt}\n",
        encoding="utf-8",
    )

    result = recent_error_codes(log_file)
    serialized = json.dumps(result, ensure_ascii=False)

    assert result == [{
        "timestamp": "2026-07-29T12:00:00",
        "level": "ERROR",
        "component": "routes.chat",
        "code": "provider_failed",
    }]
    assert secret not in serialized
    assert prompt not in serialized


def test_turn_store_summary_exposes_counts_without_turn_identity(tmp_path):
    root = tmp_path / ".chat-turns"
    active = root / "11111111-1111-4111-8111-111111111111"
    terminal = root / "22222222-2222-4222-8222-222222222222"
    active.mkdir(parents=True)
    terminal.mkdir(parents=True)
    (active / "meta.json").write_text(
        json.dumps({"status": "streaming", "event_log_bytes": 10}),
        encoding="utf-8",
    )
    (terminal / "meta.json").write_text(
        json.dumps({"status": "completed", "event_log_bytes": 20}),
        encoding="utf-8",
    )

    result = turn_store_summary(tmp_path)

    assert result == {
        "status": "ok",
        "total": 2,
        "active": 1,
        "terminal": 1,
        "invalid": 0,
        "event_log_bytes": 30,
    }
    assert "11111111" not in json.dumps(result)


def test_local_diagnostics_exposes_safe_desktop_renderer_metadata(monkeypatch):
    monkeypatch.setenv("TAVERN_DESKTOP_RENDERER_ACTIVE", "software")
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --secret-flag")

    result = diagnostics.local_diagnostics()

    assert result["desktop_rendering"] == {
        "mode": "software",
        "gpu_acceleration": False,
        "restart_required_after_change": True,
    }
    assert "chromium" not in json.dumps(result).casefold()
    assert "secret-flag" not in json.dumps(result)


def test_frozen_release_manifest_is_resolved_next_to_distribution(
    tmp_path,
    monkeypatch,
):
    executable = tmp_path / "release" / "LocalTavern" / "LocalTavern.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(diagnostics.sys, "frozen", True, raising=False)
    monkeypatch.setattr(diagnostics.sys, "executable", str(executable))

    assert diagnostics._release_manifest_path(tmp_path / "user-data") == (
        tmp_path / "release" / "release-manifest.json"
    )


@pytest.mark.asyncio
async def test_support_bundle_api_is_attachment_and_uses_strict_allowlist(
    app_client,
):
    response = await app_client.get("/api/diagnostics/support-bundle")

    assert response.status_code == 200, response.text
    assert str(UUID(response.headers["x-correlation-id"])) == response.headers[
        "x-correlation-id"
    ]
    assert response.headers["cache-control"] == "no-store"
    assert "attachment" in response.headers["content-disposition"]
    payload = response.json()
    assert payload["schema_version"] == 1
    assert payload["providers"][0]["id"] == "ollama"
    assert set(payload) == {
        "schema_version",
        "generated_at",
        "application",
        "runtime",
        "release",
        "turns",
        "recent_errors",
        "health",
        "providers",
        "backups",
        "desktop_rendering",
    }
    serialized = json.dumps(payload, ensure_ascii=False).casefold()
    for forbidden in (
        "api_key",
        "credential_value",
        "base_url",
        "message_history",
        "prompt_body",
        "worldbook_entries",
    ):
        assert forbidden not in serialized
