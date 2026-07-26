from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from core import config
import routes.migrations as migration_routes
from tests.data_guard import file_manifest


class SimulatedHardExit(BaseException):
    pass


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_legacy_api(target_project: str) -> dict:
    character = config.DATA_DIR / "characters" / "迁移接口角色.yaml"
    worldbook = config.DATA_DIR / "worldbook" / "迁移接口世界.yml"
    user = config.DATA_DIR / "user" / "迁移接口用户.yaml"
    save = config.DATA_DIR / "saves" / "迁移接口存档.json"
    history = (
        config.DATA_DIR
        / "saves"
        / ".history"
        / "迁移接口存档.snapshot.20260713.json"
    )
    _write_text(
        character,
        yaml.safe_dump(
            {"id": "迁移接口角色", "name": "接口机密角色", "active": True},
            allow_unicode=True,
            sort_keys=False,
        ),
    )
    _write_text(
        worldbook,
        yaml.safe_dump(
            {"id": "迁移接口世界", "title": "接口世界", "content": "接口机密正文"},
            allow_unicode=True,
            sort_keys=False,
        ),
    )
    _write_text(user, "id: user\nname: 接口用户\n")
    _write_text(
        save,
        json.dumps(
            {
                "project": target_project,
                "session_id": "迁移接口存档",
                "revision": 1,
                "message_history": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    _write_text(
        history,
        json.dumps(
            {
                "project": target_project,
                "session_id": "迁移接口存档",
                "revision": 1,
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    return {
        "character": character,
        "worldbook": worldbook,
        "user": user,
        "save": save,
        "history": history,
    }


def _error(response) -> dict:
    body = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert isinstance(error, dict), body
    return error


def _assert_private(response) -> None:
    assert str(config.DATA_DIR) not in response.text
    assert str(config.PROJECTS_DIR) not in response.text
    assert "接口机密正文" not in response.text
    assert "接口机密角色" not in response.text


@pytest.mark.asyncio
async def test_plan_api_has_exact_envelope_and_is_pure_read(app_client):
    target_project = "迁移接口计划项目"
    _seed_legacy_api(target_project)
    data_before = file_manifest(config.DATA_DIR)
    prompts_before = file_manifest(config.PROMPTS_DIR)

    response = await app_client.post(
        "/api/migrations/legacy/plan",
        json={"target_project": target_project},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"plan"}
    plan = body["plan"]
    assert plan["target_project"] == target_project
    assert len(plan["plan_id"]) == 64
    assert plan["can_apply"] is True
    assert file_manifest(config.DATA_DIR) == data_before
    assert file_manifest(config.PROMPTS_DIR) == prompts_before


@pytest.mark.asyncio
async def test_apply_and_get_api_have_exact_migration_envelopes(app_client):
    target_project = "迁移接口执行项目"
    seeded = _seed_legacy_api(target_project)
    planned_response = await app_client.post(
        "/api/migrations/legacy/plan",
        json={"target_project": target_project},
    )
    assert planned_response.status_code == 200, planned_response.text
    plan = planned_response.json()["plan"]

    applied_response = await app_client.post(
        f"/api/migrations/legacy/{plan['plan_id']}/apply",
        json={
            "target_project": target_project,
            "expected_source_fingerprint": plan["source_fingerprint"],
            "expected_target_fingerprint": plan["target_fingerprint"],
        },
    )

    assert applied_response.status_code == 200, applied_response.text
    applied_body = applied_response.json()
    assert set(applied_body) == {"migration"}
    migration = applied_body["migration"]
    assert migration["applied"] is True
    assert migration["plan_id"] == plan["plan_id"]
    assert migration["migration_id"] == plan["plan_id"]
    assert migration["backup_id"]
    assert (
        config.PROJECTS_DIR
        / target_project
        / "characters"
        / seeded["character"].name
    ).read_bytes() == seeded["character"].read_bytes()

    get_response = await app_client.get(
        f"/api/migrations/{migration['migration_id']}"
    )
    assert get_response.status_code == 200, get_response.text
    get_body = get_response.json()
    assert set(get_body) == {"migration"}
    assert get_body["migration"] == migration


@pytest.mark.asyncio
async def test_apply_source_conflict_returns_409_private_error_and_zero_write(
    app_client,
):
    target_project = "迁移接口冲突项目"
    seeded = _seed_legacy_api(target_project)
    planned_response = await app_client.post(
        "/api/migrations/legacy/plan",
        json={"target_project": target_project},
    )
    assert planned_response.status_code == 200, planned_response.text
    plan = planned_response.json()["plan"]
    seeded["character"].write_text(
        "id: 迁移接口角色\nname: 接口机密角色-并发改动\n",
        encoding="utf-8",
    )
    data_before = file_manifest(config.DATA_DIR)
    prompts_before = file_manifest(config.PROMPTS_DIR)

    response = await app_client.post(
        f"/api/migrations/legacy/{plan['plan_id']}/apply",
        json={
            "target_project": target_project,
            "expected_source_fingerprint": plan["source_fingerprint"],
            "expected_target_fingerprint": plan["target_fingerprint"],
        },
    )

    assert response.status_code == 409, response.text
    assert _error(response)["code"] == "source_changed"
    _assert_private(response)
    assert file_manifest(config.DATA_DIR) == data_before
    assert file_manifest(config.PROMPTS_DIR) == prompts_before


@pytest.mark.asyncio
async def test_recover_api_resumes_prepared_hard_exit_with_structured_status(
    app_client,
    monkeypatch,
):
    target_project = "迁移接口恢复项目"
    _seed_legacy_api(target_project)
    planned_response = await app_client.post(
        "/api/migrations/legacy/plan",
        json={"target_project": target_project},
    )
    assert planned_response.status_code == 200, planned_response.text
    plan = planned_response.json()["plan"]
    service = migration_routes.get_migration_service(target_project)
    original = service._install_staged_file
    crashed = False

    def install_then_crash(*args, **kwargs):
        nonlocal crashed
        original(*args, **kwargs)
        if not crashed:
            crashed = True
            raise SimulatedHardExit("injected hard exit")

    monkeypatch.setattr(service, "_install_staged_file", install_then_crash)
    with pytest.raises(SimulatedHardExit):
        service.apply(
            plan["plan_id"],
            expected_source_fingerprint=plan["source_fingerprint"],
            expected_target_fingerprint=plan["target_fingerprint"],
        )

    response = await app_client.post(
        f"/api/migrations/{plan['plan_id']}/recover",
        json={"target_project": target_project, "action": "resume"},
    )

    assert response.status_code == 200, response.text
    assert set(response.json()) == {"migration"}
    migration = response.json()["migration"]
    assert migration["applied"] is True
    assert migration["status"] == "applied"
    assert migration["migration_version"] == 2
    assert migration["input_hashes"]["plan"] == plan["plan_id"]
    assert migration["output_hashes"]["target"] == plan["result_target_fingerprint"]


@pytest.mark.asyncio
async def test_recover_api_rejects_invalid_action_with_structured_error(app_client):
    response = await app_client.post(
        f"/api/migrations/{'0' * 64}/recover",
        json={"target_project": "默认项目", "action": "erase"},
    )

    assert response.status_code == 400, response.text
    assert _error(response) == {
        "schema_version": 1,
        "code": "invalid_migration_request",
        "message": "迁移请求体无效",
        "details": {},
    }
