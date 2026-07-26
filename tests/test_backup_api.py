from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from core import config
from routes import backups as backup_routes
from tests.data_guard import file_manifest


class SimulatedProcessExit(BaseException):
    pass


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
    assert set(body) == {"error"}, body
    error = body["error"]
    assert isinstance(error, dict), body
    return error


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


@pytest.mark.asyncio
async def test_retention_and_drill_api_require_explicit_apply_and_persist_result(
    app_client,
):
    project = "backup_api_drill"
    _seed_api_library(project)
    created_response = await app_client.post(
        "/api/backups",
        json={"reason": "API drill source"},
    )
    assert created_response.status_code == 200, created_response.text
    identifier = created_response.json()["backup"]["backup_id"]
    active_before = _active_hashes()

    drill_response = await app_client.post(
        f"/api/backups/{identifier}/drill",
    )
    assert drill_response.status_code == 200, drill_response.text
    drill = drill_response.json()
    assert drill["status"] == "passed"
    assert drill["workspace_cleaned"] is True
    assert _active_hashes() == active_before
    get_response = await app_client.get(
        f"/api/backups/drills/{drill['drill_id']}",
    )
    assert get_response.status_code == 200, get_response.text
    assert get_response.json() == drill
    list_response = await app_client.get("/api/backups/drills")
    assert list_response.status_code == 200, list_response.text
    assert drill["drill_id"] in {
        item["drill_id"] for item in list_response.json()["drills"]
    }

    plan_response = await app_client.get("/api/backups/retention/plan")
    assert plan_response.status_code == 200, plan_response.text
    plan = plan_response.json()
    unconfirmed = await app_client.post(
        "/api/backups/retention/apply",
        json={
            "plan_fingerprint": plan["plan_fingerprint"],
            "confirm": False,
        },
    )
    assert unconfirmed.status_code == 409, unconfirmed.text
    assert _error(unconfirmed)["code"] == "confirmation_required"
    applied = await app_client.post(
        "/api/backups/retention/apply",
        json={
            "plan_fingerprint": plan["plan_fingerprint"],
            "confirm": True,
        },
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True


@pytest.mark.asyncio
async def test_restore_recovery_query_and_apply_api_resolve_switching_state(
    app_client,
    monkeypatch,
):
    project = "backup_api_recovery"
    project_dir = _seed_api_library(project)
    created_response = await app_client.post(
        "/api/backups",
        json={"reason": "API recovery source"},
    )
    assert created_response.status_code == 200, created_response.text
    identifier = created_response.json()["backup"]["backup_id"]
    (project_dir / "user.yaml").write_text(
        "id: user\nname: API recovery current\n",
        encoding="utf-8",
    )
    active_before = _active_hashes()
    manager = backup_routes.get_backup_manager()
    current = manager.dry_run(identifier)["current_fingerprint"]
    original_replace = manager._replace_path

    def exit_after_first_root_move(source, target):
        original_replace(source, target)
        if Path(source) == config.PROJECTS_DIR:
            raise SimulatedProcessExit()

    monkeypatch.setattr(manager, "_replace_path", exit_after_first_root_move)
    with pytest.raises(SimulatedProcessExit):
        manager.restore(
            identifier,
            expected_current_fingerprint=current,
            confirm_conflicts=True,
        )

    list_response = await app_client.get("/api/backups/restores")
    assert list_response.status_code == 200, list_response.text
    restores = list_response.json()["restores"]
    assert len(restores) == 1
    restore_id = restores[0]["restore_id"]
    query_response = await app_client.get(
        f"/api/backups/restores/{restore_id}",
    )
    assert query_response.status_code == 200, query_response.text
    assert query_response.json()["active_state"] == "mixed"

    blocked_write = await app_client.post(
        "/api/projects",
        json={"name": "blocked_during_restore"},
    )
    assert blocked_write.status_code == 503, blocked_write.text
    assert _error(blocked_write)["code"] == "restore_maintenance_required"

    recover_response = await app_client.post(
        f"/api/backups/restores/{restore_id}/recover",
    )
    assert recover_response.status_code == 200, recover_response.text
    recovered = recover_response.json()
    assert recovered["action"] == "rollback"
    assert recovered["status"] == "rolled_back"
    assert _active_hashes() == active_before
