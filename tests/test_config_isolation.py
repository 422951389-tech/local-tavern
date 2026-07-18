import hashlib

import pytest

from core import character_loader, config, ollama_client, prompt_editor


def test_all_mutable_paths_use_the_pytest_sandbox(isolated_paths):
    assert config.DATA_DIR == isolated_paths["data"]
    assert config.PROJECTS_DIR == isolated_paths["projects"]
    assert config.PROMPTS_DIR == isolated_paths["prompts"]
    assert config.SETTINGS_PATH == isolated_paths["settings"]
    assert config.RECOVERY_DIR == isolated_paths["recovery"]
    assert config.MIGRATIONS_DIR == isolated_paths["migrations"]
    assert config.BACKUPS_DIR == isolated_paths["backups"]
    assert config.LOG_DIR == isolated_paths["logs"]
    assert config.LOG_FILE == isolated_paths["log_file"]
    assert config.PID_PATH == isolated_paths["pid"]
    assert config.STOP_REQUEST_PATH == isolated_paths["stop_request"]
    assert config.HOST == "127.0.0.1"
    assert config.PORT == 8765
    assert config.ALLOW_REMOTE is False
    assert config.LOG_MAX_BYTES == 5 * 1024 * 1024
    assert config.LOG_BACKUP_COUNT == 5
    assert config.OLLAMA_HEALTH_TIMEOUT_MS == 2000
    assert character_loader.OLD_DATA_DIR == isolated_paths["data"]
    assert prompt_editor.PROMPTS_DIR == isolated_paths["prompts"]
    assert config.OLLAMA_HOST == "http://127.0.0.1:1"
    assert ollama_client.OLLAMA_HOST == config.OLLAMA_HOST
    assert config.DATA_DIR != isolated_paths["real_data"]
    assert config.PROMPTS_DIR != isolated_paths["real_prompts"]
    assert config.BACKUPS_DIR != isolated_paths["real_backups"]
    assert config.LOG_DIR != isolated_paths["real_logs"]


@pytest.mark.asyncio
async def test_settings_and_prompts_write_only_to_sandbox(app_client, isolated_paths):
    real_system = isolated_paths["real_prompts"] / "system.md"
    real_summary = isolated_paths["real_prompts"] / "summary.md"
    real_hash_before = hashlib.sha256(real_system.read_bytes()).hexdigest()
    real_summary_hash_before = hashlib.sha256(real_summary.read_bytes()).hexdigest()
    original_summary = (isolated_paths["prompts"] / "summary.md").read_text(encoding="utf-8")

    response = await app_client.put("/api/settings", json={"temperature": 0.25, "ignored": "x"})
    assert response.status_code == 200
    assert isolated_paths["settings"].exists()
    assert isolated_paths["settings"].is_relative_to(isolated_paths["root"])

    response = await app_client.put("/api/prompts/system", json={"content": "隔离后的系统提示词"})
    assert response.status_code == 200
    assert (isolated_paths["prompts"] / "system.md").read_text(encoding="utf-8") == "隔离后的系统提示词"
    assert hashlib.sha256(real_system.read_bytes()).hexdigest() == real_hash_before

    prompts = await app_client.get("/api/prompts")
    assert prompts.status_code == 200
    assert set(prompts.json()) == {"system", "group_chat", "summary"}
    saved = await app_client.put(
        "/api/prompts/summary",
        json={"content": "隔离后的摘要模板"},
    )
    assert saved.status_code == 200
    assert (isolated_paths["prompts"] / "summary.md").read_text(encoding="utf-8") == "隔离后的摘要模板"
    reset = await app_client.post("/api/prompts/summary/reset")
    assert reset.status_code == 200
    assert reset.json()["content"] == original_summary
    assert (isolated_paths["prompts"] / "summary.md").read_text(encoding="utf-8") == original_summary
    assert hashlib.sha256(real_summary.read_bytes()).hexdigest() == real_summary_hash_before
