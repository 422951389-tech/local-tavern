from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

import core.migration_service as migration_module
from core.backup_store import BackupManager
from core.migration_service import (
    LegacyMigrationService,
    MigrationConflict,
    MigrationIntegrityError,
    MigrationOperationError,
)
from tests.data_guard import file_manifest


TARGET_PROJECT = "中文迁移项目"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def seed_legacy_library(tmp_path: Path, *, target_project: str = TARGET_PROJECT) -> dict:
    data_root = tmp_path / "data"
    projects_root = data_root / "projects"
    migrations_root = data_root / ".migrations"
    prompts_dir = tmp_path / "prompts"
    settings_path = data_root / "settings.json"
    backups_root = tmp_path / "backups"

    payloads = {
        "characters/勇者.yaml": yaml.safe_dump(
            {"id": "勇者", "name": "中文角色", "active": True},
            allow_unicode=True,
            sort_keys=False,
        ),
        "worldbook/世界设定.yml": yaml.safe_dump(
            {"id": "世界设定", "title": "中文世界", "content": "设定正文"},
            allow_unicode=True,
            sort_keys=False,
        ),
        "user/玩家.yaml": yaml.safe_dump(
            {"id": "user", "name": "中文玩家"},
            allow_unicode=True,
            sort_keys=False,
        ),
        "saves/主存档.json": json.dumps(
            {
                "project": target_project,
                "session_id": "主存档",
                "revision": 1,
                "message_history": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        "saves/.history/主存档.snapshot.20260713.json": json.dumps(
            {"project": target_project, "session_id": "主存档", "revision": 1},
            ensure_ascii=False,
            indent=2,
        ),
    }
    for relative, content in payloads.items():
        _write_text(data_root / relative, content)

    excluded = {
        "characters/_template.yaml": "id: template\n",
        "characters/.hidden.yaml": "id: hidden\n",
        "worldbook/_template_world.yml": "id: template_world\n",
        "saves/.hidden.json": "{}",
        "backup/characters/backup_only.yaml": "id: backup_only\n",
        "backups/worldbook/backup_only.yaml": "id: backup_only\n",
    }
    for relative, content in excluded.items():
        _write_text(data_root / relative, content)

    _write_text(prompts_dir / "system.md", "系统模板")
    _write_text(settings_path, json.dumps({"current_project": target_project}))
    backup_manager = BackupManager(
        backups_root,
        projects_root,
        prompts_dir,
        settings_path,
    )
    service = LegacyMigrationService(
        data_root,
        projects_root,
        migrations_root,
        backup_manager,
        target_project,
    )
    return {
        "data_root": data_root,
        "projects_root": projects_root,
        "migrations_root": migrations_root,
        "prompts_dir": prompts_dir,
        "settings_path": settings_path,
        "backups_root": backups_root,
        "backup_manager": backup_manager,
        "service": service,
        "target_project": target_project,
        "target_dir": projects_root / target_project,
        "payloads": payloads,
        "excluded": excluded,
    }


def _expected_mapping(seeded: dict) -> dict[str, str]:
    project = seeded["target_project"]
    return {
        "characters/勇者.yaml": f"{project}/characters/勇者.yaml",
        "worldbook/世界设定.yml": f"{project}/worldbook/世界设定.yml",
        "user/玩家.yaml": f"{project}/user.yaml",
        "saves/主存档.json": f"{project}/saves/主存档.json",
        "saves/.history/主存档.snapshot.20260713.json": (
            f"{project}/saves/.history/主存档.snapshot.20260713.json"
        ),
    }


def _legacy_bytes(seeded: dict) -> dict[str, bytes]:
    return {
        relative: (seeded["data_root"] / relative).read_bytes()
        for relative in [*seeded["payloads"], *seeded["excluded"]]
    }


def test_plan_is_stable_pure_read_and_maps_only_supported_legacy_files(tmp_path):
    seeded = seed_legacy_library(tmp_path)
    before = file_manifest(tmp_path)

    first = seeded["service"].plan()
    second = seeded["service"].plan()

    assert first == second
    assert len(first["plan_id"]) == 64
    assert first["target_project"] == TARGET_PROJECT
    assert first["can_apply"] is True
    assert first["conflicts"] == []
    assert first["skipped"] == []
    mapping = {item["source"]: item["target"] for item in first["items"]}
    assert mapping == _expected_mapping(seeded)
    for item in first["items"]:
        source = seeded["data_root"] / item["source"]
        payload = source.read_bytes()
        assert item["action"] == "copy"
        assert item["size"] == len(payload)
        assert item["sha256"] == hashlib.sha256(payload).hexdigest()
    assert file_manifest(tmp_path) == before
    assert not seeded["migrations_root"].exists()
    assert not seeded["backups_root"].exists()
    assert not seeded["target_dir"].exists()


def test_same_hash_is_skipped_and_different_hash_is_a_conflict(tmp_path):
    seeded = seed_legacy_library(tmp_path)
    initial = seeded["service"].plan()
    character = next(
        item for item in initial["items"] if item["source"] == "characters/勇者.yaml"
    )
    target = seeded["projects_root"] / Path(character["target"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((seeded["data_root"] / character["source"]).read_bytes())

    skipped = seeded["service"].plan()
    skipped_item = next(
        item for item in skipped["items"] if item["source"] == character["source"]
    )
    assert skipped_item["action"] == "skip_same"
    assert skipped_item in skipped["skipped"]
    assert skipped["conflicts"] == []

    target.write_text("id: 勇者\nname: 已存在的不同角色\n", encoding="utf-8")
    conflicted = seeded["service"].plan()
    conflict = next(
        item for item in conflicted["items"] if item["source"] == character["source"]
    )
    assert conflict["action"] == "conflict"
    assert conflict in conflicted["conflicts"]
    assert conflicted["can_apply"] is False


def test_multiple_visible_user_profiles_are_reported_as_conflict(tmp_path):
    seeded = seed_legacy_library(tmp_path)
    _write_text(seeded["data_root"] / "user" / "另一个用户.yml", "id: user\nname: 二号用户\n")

    plan = seeded["service"].plan()

    assert plan["can_apply"] is False
    assert any(item.get("code") == "multiple_user_profiles" for item in plan["conflicts"])


@pytest.mark.parametrize(
    ("relative", "invalid"),
    [
        ("saves/主存档.json", '{"broken":'),
        ("characters/勇者.yaml", "name: [unterminated"),
    ],
)
def test_invalid_json_or_yaml_is_rejected_during_plan(tmp_path, relative, invalid):
    seeded = seed_legacy_library(tmp_path)
    (seeded["data_root"] / relative).write_text(invalid, encoding="utf-8")
    before = file_manifest(tmp_path)

    with pytest.raises(MigrationIntegrityError) as raised:
        seeded["service"].plan()

    assert raised.value.code == "invalid_source"
    assert file_manifest(tmp_path) == before


def test_reparse_source_is_rejected_during_plan(tmp_path, monkeypatch):
    seeded = seed_legacy_library(tmp_path)
    source = seeded["data_root"] / "characters" / "勇者.yaml"
    original = migration_module._is_reparse

    monkeypatch.setattr(
        migration_module,
        "_is_reparse",
        lambda path: Path(path) == source or original(path),
    )

    with pytest.raises(MigrationIntegrityError) as raised:
        seeded["service"].plan()

    assert raised.value.code == "invalid_source"


def test_apply_creates_verified_backup_copies_exactly_and_preserves_legacy(tmp_path):
    seeded = seed_legacy_library(tmp_path)
    plan = seeded["service"].plan()
    legacy_before = _legacy_bytes(seeded)

    result = seeded["service"].apply(
        plan["plan_id"],
        expected_source_fingerprint=plan["source_fingerprint"],
        expected_target_fingerprint=plan["target_fingerprint"],
    )

    assert result["applied"] is True
    assert result["migration_id"] == plan["plan_id"]
    assert result["plan_id"] == plan["plan_id"]
    assert result["backup_id"]
    assert set(result["copied"]) == {
        item["target"] for item in plan["items"] if item["action"] == "copy"
    }
    assert result["skipped"] == []
    verified = seeded["backup_manager"].get_verified(result["backup_id"])
    assert verified.manifest["kind"] == "pre_migration"
    for item in plan["items"]:
        target = seeded["projects_root"] / Path(item["target"])
        assert target.read_bytes() == (seeded["data_root"] / item["source"]).read_bytes()
        assert hashlib.sha256(target.read_bytes()).hexdigest() == item["sha256"]
    assert _legacy_bytes(seeded) == legacy_before
    assert seeded["service"].plan()["target_fingerprint"] == plan[
        "result_target_fingerprint"
    ]


@pytest.mark.parametrize(
    ("changed_side", "expected_code"),
    [("source", "source_changed"), ("target", "target_changed")],
)
def test_apply_cas_rejects_changed_source_or_target_without_write(
    tmp_path,
    changed_side,
    expected_code,
):
    seeded = seed_legacy_library(tmp_path)
    plan = seeded["service"].plan()
    if changed_side == "source":
        source = seeded["data_root"] / "characters" / "勇者.yaml"
        source.write_text("id: 勇者\nname: 新源\n", encoding="utf-8")
    else:
        target = seeded["target_dir"] / "existing.txt"
        _write_text(target, "并发目标写入")
    target_before = file_manifest(seeded["projects_root"])
    backups_before = file_manifest(seeded["backups_root"])

    with pytest.raises(MigrationConflict) as raised:
        seeded["service"].apply(
            plan["plan_id"],
            expected_source_fingerprint=plan["source_fingerprint"],
            expected_target_fingerprint=plan["target_fingerprint"],
        )

    assert raised.value.code == expected_code
    assert file_manifest(seeded["projects_root"]) == target_before
    assert file_manifest(seeded["backups_root"]) == backups_before


def test_applied_plan_retry_is_idempotent_and_does_not_duplicate_backup(tmp_path):
    seeded = seed_legacy_library(tmp_path)
    plan = seeded["service"].plan()
    first = seeded["service"].apply(
        plan["plan_id"],
        expected_source_fingerprint=plan["source_fingerprint"],
        expected_target_fingerprint=plan["target_fingerprint"],
    )
    backups_before = file_manifest(seeded["backups_root"])

    second = seeded["service"].apply(
        plan["plan_id"],
        expected_source_fingerprint=plan["source_fingerprint"],
        expected_target_fingerprint=plan["target_fingerprint"],
    )

    assert second == first
    assert file_manifest(seeded["backups_root"]) == backups_before


@pytest.mark.parametrize("fault", ["copy", "install", "journal"])
def test_nth_fault_rolls_back_to_original_target_fingerprint(
    tmp_path,
    monkeypatch,
    fault,
):
    seeded = seed_legacy_library(tmp_path)
    _write_text(seeded["target_dir"] / "keep.txt", "原目标内容")
    plan = seeded["service"].plan()
    target_before = file_manifest(seeded["projects_root"])
    legacy_before = _legacy_bytes(seeded)
    service = seeded["service"]
    method_name = {
        "copy": "_copy_to_staging",
        "install": "_install_staged_file",
        "journal": "_write_journal",
    }[fault]
    original = getattr(service, method_name)
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(f"injected {fault} failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(service, method_name, fail_second)

    with pytest.raises(MigrationOperationError) as raised:
        service.apply(
            plan["plan_id"],
            expected_source_fingerprint=plan["source_fingerprint"],
            expected_target_fingerprint=plan["target_fingerprint"],
        )

    assert raised.value.code == "migration_failed"
    assert calls >= 2
    assert file_manifest(seeded["projects_root"]) == target_before
    assert _legacy_bytes(seeded) == legacy_before
    assert seeded["service"].plan()["target_fingerprint"] == plan[
        "target_fingerprint"
    ]
