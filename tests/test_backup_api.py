from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from core import config
from tests.data_guard import file_manifest


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_api_library(project: str) -> Path:
    project_dir = config.PROJECTS_DIR / project
    _write_text(
        project_dir / "characters" / "backup_api_character.yaml",
        yaml.safe_dump(
            {
                "id": "backup_api_character",
                "name": "API 备份角色",
                "active": True,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
    )
    _write_text(
        project_dir / "worldbook" / "backup_api_world.yaml",
        yaml.safe_dump(
            {
                "id": "backup_api_world",
                "title": "API 备份世界书",
                "content": "正文不得进入错误响应",
            },
            allow_unicode=True,
            sort_keys=False,
        ),
    )
    _write_text(
        project_dir / "user.yaml",
        yaml.safe_dump(
            {"id": "user", "name": "API 备份用户"},
            allow_unicode=True,
            sort_keys=False,
        ),
    )
    _write_text(
        project_dir / "saves" / "main.json",
        json.dumps(
            {
                "project": project,
                "session_id": "main",
                "revision": 1,
                "message_history": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    config.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.SETTINGS_PATH.write_text(
        json.dumps(
            {"current_project": project, "current_save": "main"},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return project_dir


def _error(response) -> dict:
    body = response.json()
    detail = body.get("error") or body.get("detail")
    assert isinstance(detail, dict), body
    return detail


def _assert_no_absolute_path(response) -> None:
    assert str(config.DATA_DIR) not in response.text
    assert str(config.PROJECTS_DIR) not in response.text
    assert str(config.PROMPTS_DIR) not in response.text


def _active_hashes() -> dict[str, dict[str, str] | str | None]:
    settings_hash = (
        hashlib.sha256(config.SETTINGS_PATH.read_bytes()).hexdigest()
        if config.SETTINGS_PATH.is_file()
        else None
    )
    return {
        "projects": file_manifest(config.PROJECTS_DIR),
        "prompts": file_manifest(config.PROMPTS_DIR),
        "settings": settings_hash,
    }


@pytest.mark.asyncio
async def test_create_list_and_dry_run_api_contracts_are_read_only_after_create(
    app_client,
):
    project = "backup_api_contract"
    _seed_api_library(project)

    created_response = await app_client.post(
        "/api/backups",
        json={"reason": "API contract backup"},
    )
    assert created_response.status_code == 200, created_response.text
    created_body = created_response.json()
    assert set(created_body) == {"backup"}
    created = created_body["backup"]
    assert isinstance(created["backup_id"], str) and created["backup_id"]
    assert created["kind"] == "manual"
    assert created["reason"] == "API contract backup"

    before = _active_hashes()
    list_response = await app_client.get("/api/backups")
    assert list_response.status_code == 200, list_response.text
    list_body = list_response.json()
    assert set(list_body) == {"backups"}
    items = list_body["backups"]
    assert created["backup_id"] in {item["backup_id"] for item in items}

    dry_response = await app_client.post(
        f"/api/backups/{created['backup_id']}/dry-run",
    )
    assert dry_response.status_code == 200, dry_response.text
    dry_run = dry_response.json()
    assert dry_run["backup_id"] == created["backup_id"]
    assert len(dry_run["current_fingerprint"]) == 64
    assert dry_run["changes"] == []
    assert dry_run["conflicts"] == []
    assert _active_hashes() == before


@pytest.mark.asyncio
async def test_restore_api_creates_pre_restore_backup_and_preserves_exact_state(
    app_client,
):
    project = "backup_api_restore"
    _seed_api_library(project)
    created_response = await app_client.post(
        "/api/backups",
        json={"reason": "API restore source"},
    )
    assert created_response.status_code == 200, created_response.text
    created_body = created_response.json()
    assert set(created_body) == {"backup"}
    backup = created_body["backup"]
    dry_response = await app_client.post(
        f"/api/backups/{backup['backup_id']}/dry-run",
    )
    assert dry_response.status_code == 200, dry_response.text
    before = _active_hashes()

    restored_response = await app_client.post(
        f"/api/backups/{backup['backup_id']}/restore",
        json={
            "expected_current_fingerprint": dry_response.json()[
                "current_fingerprint"
            ],
            "confirm_conflicts": False,
        },
    )

    assert restored_response.status_code == 200, restored_response.text
    restored = restored_response.json()
    assert restored["restored"] is True
    assert restored["backup_id"] == backup["backup_id"]
    assert restored["pre_restore_backup_id"]
    assert _active_hashes() == before
    list_response = await app_client.get("/api/backups")
    assert list_response.status_code == 200, list_response.text
    list_body = list_response.json()
    assert set(list_body) == {"backups"}
    by_id = {
        item["backup_id"]: item for item in list_body["backups"]
    }
    assert by_id[restored["pre_restore_backup_id"]]["kind"] == "pre_restore"


@pytest.mark.asyncio
async def test_stale_fingerprint_returns_409_and_performs_zero_writes(app_client):
    project = "backup_api_stale"
    project_dir = _seed_api_library(project)
    created_response = await app_client.post(
        "/api/backups",
        json={"reason": "API stale fingerprint"},
    )
    assert created_response.status_code == 200, created_response.text
    created_body = created_response.json()
    assert set(created_body) == {"backup"}
    backup_id = created_body["backup"]["backup_id"]
    dry_response = await app_client.post(f"/api/backups/{backup_id}/dry-run")
    assert dry_response.status_code == 200, dry_response.text
    stale = dry_response.json()["current_fingerprint"]
    target = project_dir / "user.yaml"
    target.write_text("id: user\nname: newer\n", encoding="utf-8")
    data_before = file_manifest(config.DATA_DIR)
    prompts_before = file_manifest(config.PROMPTS_DIR)

    response = await app_client.post(
        f"/api/backups/{backup_id}/restore",
        json={
            "expected_current_fingerprint": stale,
            "confirm_conflicts": True,
        },
    )

    assert response.status_code == 409, response.text
    assert _error(response)["code"] == "current_changed"
    _assert_no_absolute_path(response)
    assert "正文不得进入错误响应" not in response.text
    assert file_manifest(config.DATA_DIR) == data_before
    assert file_manifest(config.PROMPTS_DIR) == prompts_before
