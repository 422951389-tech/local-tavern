from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import pytest
import yaml

from core import active_turns
from core.character_loader import ensure_project, get_worldbook_path
from core.import_validation import validate_import_json
from core.session_manager import load_session
from core.worldbook_policy import MAX_MANUAL_WORLDBOOK_IDS


def _project_dir(isolated_paths, project: str):
    ensure_project(project)
    return isolated_paths["projects"] / project


def _write_worldbook(isolated_paths, project: str, filename: str, data: dict):
    directory = _project_dir(isolated_paths, project) / "worldbook"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_schema_and_legacy_get_are_normalized_without_disk_write(
    app_client,
    isolated_paths,
):
    project = "worldbook_legacy_get"
    path = _write_worldbook(
        isolated_paths,
        project,
        "legacy.yaml",
        {
            "id": "legacy",
            "title": "旧条目",
            "content": "旧事实",
            "custom": {"faction": "青龙"},
        },
    )
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    response = await app_client.get("/api/worldbook", params={"project": project})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "entries": [{
            "id": "legacy",
            "title": "旧条目",
            "content": "旧事实",
            "custom": {"faction": "青龙"},
            "enabled": True,
            "activation": "always",
            "keywords": [],
            "priority": 0,
        }],
    }
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime

    schema_response = await app_client.get("/api/schema/worldbook")
    assert schema_response.status_code == 200
    schema = schema_response.json()
    fields = {
        field["key"]: field
        for group in schema["groups"]
        for field in group["fields"]
    }
    assert set(fields) == {
        "id", "title", "enabled", "activation", "keywords", "priority", "content",
    }
    assert [item["value"] for item in fields["activation"]["options"]] == [
        "always", "keywords", "manual",
    ]
    assert fields["priority"]["min"] == -1_000_000
    assert fields["priority"]["max"] == 1_000_000


@pytest.mark.asyncio
async def test_put_is_strict_preserves_unknown_fields_and_never_creates_project(
    app_client,
    isolated_paths,
):
    project = "worldbook_strict_put"
    project_dir = _project_dir(isolated_paths, project)
    payload = {
        "data": {
            "id": "manual_entry",
            "title": "手动条目",
            "enabled": True,
            "activation": "manual",
            "keywords": [],
            "priority": 7,
            "content": "可选事实",
            "facts": {"owner": "测试势力"},
        },
    }
    saved = await app_client.put(
        "/api/worldbook/manual_entry",
        params={"project": project},
        json=payload,
    )
    assert saved.status_code == 200, saved.text
    on_disk = yaml.safe_load(
        (project_dir / "worldbook" / "manual_entry.yaml").read_text(encoding="utf-8")
    )
    assert on_disk["facts"] == {"owner": "测试势力"}
    assert on_disk["activation"] == "manual"

    before = (project_dir / "worldbook" / "manual_entry.yaml").read_bytes()
    invalid_cases = [
        {"data": {**payload["data"], "enabled": "false"}},
        {"data": {**payload["data"], "priority": True}},
        {"data": {**payload["data"], "activation": "keywords", "keywords": []}},
        {"data": {**payload["data"], "id": "different_id"}},
        {"data": {**payload["data"], "content": "x" * 200_001}},
    ]
    for invalid in invalid_cases:
        response = await app_client.put(
            "/api/worldbook/manual_entry",
            params={"project": project},
            json=invalid,
        )
        assert response.status_code == 422, response.text
        assert (project_dir / "worldbook" / "manual_entry.yaml").read_bytes() == before

    for invalid_body in ([], {}, {"data": payload["data"], "extra": True}):
        response = await app_client.put(
            "/api/worldbook/manual_entry",
            params={"project": project},
            json=invalid_body,
        )
        assert response.status_code == 422, response.text
        assert (project_dir / "worldbook" / "manual_entry.yaml").read_bytes() == before

    ghost = "worldbook_ghost_project"
    ghost_dir = isolated_paths["projects"] / ghost
    response = await app_client.put(
        "/api/worldbook/entry",
        params={"project": ghost},
        json={"data": {"id": "entry", "content": "不得写入"}},
    )
    assert response.status_code == 404, response.text
    assert not ghost_dir.exists()
    listed = await app_client.get("/api/worldbook", params={"project": ghost})
    assert listed.status_code == 404, listed.text
    assert not ghost_dir.exists()

    turn_id = str(uuid4())
    active_turns.register(project, "默认存档", turn_id)
    try:
        blocked = await app_client.put(
            "/api/worldbook/blocked_entry",
            params={"project": project},
            json={"data": {"id": "blocked_entry", "content": "不得并发写入"}},
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["error"]["code"] == "active_turn_conflict"
        assert not (project_dir / "worldbook" / "blocked_entry.yaml").exists()
    finally:
        active_turns.unregister(project, "默认存档", turn_id)


@pytest.mark.asyncio
async def test_top_level_yml_is_read_saved_and_deleted_in_place(
    app_client,
    isolated_paths,
):
    project = "worldbook_yml_compat"
    path = _write_worldbook(
        isolated_paths,
        project,
        "legacy_yml.yml",
        {"id": "legacy_yml", "content": "旧扩展事实"},
    )

    listed = await app_client.get("/api/worldbook", params={"project": project})
    assert listed.status_code == 200, listed.text
    assert listed.json()["entries"][0]["activation"] == "always"

    saved = await app_client.put(
        "/api/worldbook/legacy_yml",
        params={"project": project},
        json={
            "data": {
                "id": "legacy_yml",
                "activation": "manual",
                "enabled": True,
                "keywords": [],
                "priority": 1,
                "content": "更新事实",
            },
        },
    )
    assert saved.status_code == 200, saved.text
    assert path.is_file()
    assert not path.with_suffix(".yaml").exists()
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["content"] == "更新事实"
    assert get_worldbook_path(project, "legacy_yml") == path

    deleted = await app_client.delete(
        "/api/worldbook/legacy_yml",
        params={"project": project},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] is True
    assert not path.exists()

    restored = await app_client.post(
        f"/api/recovery/{deleted.json()['recovery_id']}/restore",
        json={},
    )
    assert restored.status_code == 200, restored.text
    assert path.is_file()
    assert not path.with_suffix(".yaml").exists()
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["content"] == "更新事实"


@pytest.mark.asyncio
async def test_yaml_yml_collision_is_rejected_without_modification(
    app_client,
    isolated_paths,
):
    project = "worldbook_extension_collision"
    yaml_path = _write_worldbook(
        isolated_paths,
        project,
        "same.yaml",
        {"id": "same", "content": "yaml"},
    )
    yml_path = _write_worldbook(
        isolated_paths,
        project,
        "same.yml",
        {"id": "same", "content": "yml"},
    )
    before = (yaml_path.read_bytes(), yml_path.read_bytes())

    listed = await app_client.get("/api/worldbook", params={"project": project})
    assert listed.status_code == 422, listed.text
    assert listed.json()["error"]["code"] == "data_corrupt"

    saved = await app_client.put(
        "/api/worldbook/same",
        params={"project": project},
        json={"data": {"id": "same", "content": "new"}},
    )
    assert saved.status_code == 400, saved.text
    assert (yaml_path.read_bytes(), yml_path.read_bytes()) == before


@pytest.mark.asyncio
async def test_yml_restore_rejects_sibling_yaml_without_overwrite(
    app_client,
    isolated_paths,
):
    project = "worldbook_restore_extension_collision"
    yml_path = _write_worldbook(
        isolated_paths,
        project,
        "same.yml",
        {"id": "same", "content": "old yml"},
    )
    deleted = await app_client.delete(
        "/api/worldbook/same",
        params={"project": project},
    )
    assert deleted.status_code == 200, deleted.text
    assert not yml_path.exists()

    yaml_path = _write_worldbook(
        isolated_paths,
        project,
        "same.yaml",
        {"id": "same", "content": "new yaml"},
    )
    before = yaml_path.read_bytes()
    restored = await app_client.post(
        f"/api/recovery/{deleted.json()['recovery_id']}/restore",
        json={},
    )
    assert restored.status_code == 409, restored.text
    assert restored.json()["detail"]["code"] == "target_changed"
    assert yaml_path.read_bytes() == before
    assert not yml_path.exists()


@pytest.mark.asyncio
async def test_corrupt_yml_quarantine_restore_keeps_exact_extension(
    app_client,
    isolated_paths,
):
    project = "worldbook_yml_quarantine"
    project_dir = _project_dir(isolated_paths, project)
    source = project_dir / "worldbook" / "broken.yml"
    raw = b"activation: [broken"
    source.write_bytes(raw)

    corrupt = await app_client.get("/api/worldbook", params={"project": project})
    assert corrupt.status_code == 422, corrupt.text
    error = corrupt.json()["error"]
    assert error["fingerprint"] == hashlib.sha256(raw).hexdigest()

    quarantined = await app_client.post(
        "/api/recovery/quarantine",
        json={
            "entity_type": "worldbook",
            "project": project,
            "entity_id": "broken",
            "fingerprint": error["fingerprint"],
        },
    )
    assert quarantined.status_code == 200, quarantined.text
    recovery = quarantined.json()["recovery"]
    assert recovery["source_relpath"].endswith("/worldbook/broken.yml")
    assert not source.exists()

    sibling = source.with_suffix(".yaml")
    sibling.write_text("id: broken\ncontent: sibling\n", encoding="utf-8")
    blocked = await app_client.post(
        f"/api/recovery/{recovery['recovery_id']}/restore",
        json={},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "target_exists"
    assert sibling.is_file()
    assert not source.exists()

    sibling.unlink()
    restored = await app_client.post(
        f"/api/recovery/{recovery['recovery_id']}/restore",
        json={},
    )
    assert restored.status_code == 200, restored.text
    assert source.read_bytes() == raw
    assert not sibling.exists()


@pytest.mark.asyncio
async def test_manual_selection_is_cas_scoped_and_all_rejections_are_zero_write(
    app_client,
    isolated_paths,
):
    project = "worldbook_manual_selection"
    project_dir = _project_dir(isolated_paths, project)
    for entry_id, activation, enabled in (
        ("manual_b", "manual", True),
        ("manual_a", "manual", True),
        ("always_entry", "always", True),
        ("disabled_manual", "manual", False),
    ):
        _write_worldbook(
            isolated_paths,
            project,
            f"{entry_id}.yaml",
            {
                "id": entry_id,
                "activation": activation,
                "enabled": enabled,
                "keywords": [],
                "priority": 0,
                "content": entry_id,
            },
        )

    save_path = project_dir / "saves" / "默认存档.json"
    selected = await app_client.patch(
        "/api/session/worldbook/manual",
        json={
            "project": project,
            "save": "默认存档",
            "expected_revision": 0,
            "entry_ids": ["manual_b", "manual_a"],
        },
    )
    assert selected.status_code == 200, selected.text
    session = selected.json()["session"]
    assert session["manual_worldbook_ids"] == ["manual_a", "manual_b"]
    assert session["revision"] == 1
    assert json.loads(save_path.read_text(encoding="utf-8"))[
        "manual_worldbook_ids"
    ] == ["manual_a", "manual_b"]

    invalid_entry_sets = [
        ["missing_entry"],
        ["manual_a", "manual_a"],
        ["always_entry"],
        ["disabled_manual"],
        ["bad id"],
    ]
    for entry_ids in invalid_entry_sets:
        before = save_path.read_bytes()
        response = await app_client.patch(
            "/api/session/worldbook/manual",
            json={
                "project": project,
                "save": "默认存档",
                "expected_revision": 1,
                "entry_ids": entry_ids,
            },
        )
        assert response.status_code == 422, response.text
        assert save_path.read_bytes() == before

    before = save_path.read_bytes()
    conflict = await app_client.patch(
        "/api/session/worldbook/manual",
        json={
            "project": project,
            "save": "默认存档",
            "expected_revision": 0,
            "entry_ids": [],
        },
    )
    assert conflict.status_code == 409, conflict.text
    assert save_path.read_bytes() == before

    turn_id = str(uuid4())
    active_turns.register(project, "默认存档", turn_id)
    try:
        blocked = await app_client.patch(
            "/api/session/worldbook/manual",
            json={
                "project": project,
                "save": "默认存档",
                "expected_revision": 1,
                "entry_ids": [],
            },
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["error"]["code"] == "active_turn_conflict"
        assert save_path.read_bytes() == before
    finally:
        active_turns.unregister(project, "默认存档", turn_id)

    deleted = await app_client.delete(
        "/api/worldbook/manual_a",
        params={"project": project},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["affected_saves"] == []
    dormant = load_session(project, "默认存档")
    assert dormant["revision"] == 1
    assert dormant["manual_worldbook_ids"] == ["manual_a", "manual_b"]

    retained = await app_client.patch(
        "/api/session/worldbook/manual",
        json={
            "project": project,
            "save": "默认存档",
            "expected_revision": 1,
            # manual_a 已删除，但它属于当前 Session 的既有休眠引用，允许原样保留。
            "entry_ids": ["manual_b", "manual_a"],
        },
    )
    assert retained.status_code == 200, retained.text
    retained_session = retained.json()["session"]
    assert retained_session["revision"] == 2
    assert retained_session["manual_worldbook_ids"] == ["manual_a", "manual_b"]

    reject_new_dormant = await app_client.patch(
        "/api/session/worldbook/manual",
        json={
            "project": project,
            "save": "默认存档",
            "expected_revision": 2,
            "entry_ids": ["manual_a", "manual_b", "never_selected_missing"],
        },
    )
    assert reject_new_dormant.status_code == 422, reject_new_dormant.text
    assert load_session(project, "默认存档") == retained_session

    restored = await app_client.post(
        f"/api/recovery/{deleted.json()['recovery_id']}/restore",
        json={},
    )
    assert restored.status_code == 200, restored.text
    after_restore = load_session(project, "默认存档")
    assert after_restore["revision"] == 2
    assert after_restore["manual_worldbook_ids"] == ["manual_a", "manual_b"]


@pytest.mark.asyncio
async def test_manual_selection_cannot_cross_projects_or_create_missing_save(
    app_client,
    isolated_paths,
):
    project_a = "worldbook_scope_a"
    project_b = "worldbook_scope_b"
    _write_worldbook(
        isolated_paths,
        project_a,
        "only_a.yaml",
        {
            "id": "only_a",
            "activation": "manual",
            "enabled": True,
            "keywords": [],
            "priority": 0,
            "content": "A",
        },
    )
    project_b_dir = _project_dir(isolated_paths, project_b)
    default_path = project_b_dir / "saves" / "默认存档.json"
    before = default_path.read_bytes()

    cross_project = await app_client.patch(
        "/api/session/worldbook/manual",
        json={
            "project": project_b,
            "save": "默认存档",
            "expected_revision": 0,
            "entry_ids": ["only_a"],
        },
    )
    assert cross_project.status_code == 422, cross_project.text
    assert default_path.read_bytes() == before

    missing_save = await app_client.patch(
        "/api/session/worldbook/manual",
        json={
            "project": project_b,
            "save": "不存在存档",
            "expected_revision": 0,
            "entry_ids": [],
        },
    )
    assert missing_save.status_code == 404, missing_save.text
    assert not (project_b_dir / "saves" / "不存在存档.json").exists()


def test_legacy_session_manual_ids_normalize_in_memory_without_write(isolated_paths):
    project = "worldbook_legacy_session"
    project_dir = _project_dir(isolated_paths, project)
    path = project_dir / "saves" / "默认存档.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["manual_worldbook_ids"] = ["z_entry", "bad id", 7, "a_entry", "z_entry"]
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    before = path.read_bytes()

    session = load_session(project, "默认存档")
    assert session["manual_worldbook_ids"] == ["a_entry", "z_entry"]
    assert path.read_bytes() == before


def test_import_manual_worldbook_ids_are_strict_bounded_and_sorted():
    base = {"session_id": "import_worldbook", "message_history": []}
    valid = validate_import_json(json.dumps({
        **base,
        "manual_worldbook_ids": ["z_entry", "a_entry"],
    }))
    assert valid["manual_worldbook_ids"] == ["a_entry", "z_entry"]

    for invalid in (
        "not-an-array",
        ["duplicate", "duplicate"],
        ["bad id"],
        [1],
        [f"entry_{index}" for index in range(MAX_MANUAL_WORLDBOOK_IDS + 1)],
    ):
        with pytest.raises(ValueError, match="manual_worldbook_ids|手动世界书"):
            validate_import_json(json.dumps({
                **base,
                "manual_worldbook_ids": invalid,
            }))
