from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest
import yaml

from core.backup_store import BackupConflict, BackupManager
from tests.data_guard import file_manifest


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def seed_backup_library(tmp_path: Path, *, retention_count: int = 10) -> dict:
    data_root = tmp_path / "library"
    projects_root = data_root / "projects"
    prompts_dir = tmp_path / "prompts"
    settings_path = data_root / "settings.json"
    backups_root = tmp_path / "backups"

    _write_text(
        projects_root / "alpha" / "characters" / "hero.yaml",
        yaml.safe_dump(
            {"id": "hero", "name": "备份角色", "active": True},
            allow_unicode=True,
            sort_keys=False,
        ),
    )
    _write_text(
        projects_root / "alpha" / "worldbook" / "lore.yaml",
        yaml.safe_dump(
            {"id": "lore", "title": "备份世界书", "content": "正文"},
            allow_unicode=True,
            sort_keys=False,
        ),
    )
    _write_text(
        projects_root / "alpha" / "user.yaml",
        yaml.safe_dump(
            {"id": "user", "name": "备份用户"},
            allow_unicode=True,
            sort_keys=False,
        ),
    )
    _write_text(
        projects_root / "alpha" / "saves" / "main.json",
        json.dumps(
            {
                "project": "alpha",
                "session_id": "main",
                "revision": 3,
                "message_history": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    _write_text(prompts_dir / "system.md", "系统模板\n{{character_list}}")
    _write_text(prompts_dir / "group_chat.md", "群聊模板\n{{history}}")
    _write_text(
        settings_path,
        json.dumps(
            {"current_project": "alpha", "current_save": "main"},
            ensure_ascii=False,
            indent=2,
        ),
    )
    manager = BackupManager(
        backups_root,
        projects_root,
        prompts_dir,
        settings_path,
        retention_days=30,
        retention_count=retention_count,
    )
    return {
        "data_root": data_root,
        "projects_root": projects_root,
        "prompts_dir": prompts_dir,
        "settings_path": settings_path,
        "backups_root": backups_root,
        "manager": manager,
    }


def source_bytes(seeded: dict) -> dict[str, bytes]:
    items: dict[str, bytes] = {}
    for root_name, root in (
        ("projects", seeded["projects_root"]),
        ("prompts", seeded["prompts_dir"]),
    ):
        for path in sorted(root.rglob("*")):
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                items[f"{root_name}/{relative}"] = path.read_bytes()
    items["settings.json"] = seeded["settings_path"].read_bytes()
    return items


def active_manifest(seeded: dict) -> dict[str, str]:
    digest: dict[str, str] = {}
    for logical_path, payload in source_bytes(seeded).items():
        digest[logical_path] = hashlib.sha256(payload).hexdigest()
    return digest


def backup_id(result: dict) -> str:
    value = result.get("backup_id")
    assert isinstance(value, str) and value
    return value


def backup_zip(seeded: dict, backup: str) -> Path:
    matches = [
        path
        for path in seeded["backups_root"].glob("*.zip")
        if backup in path.name
    ]
    assert len(matches) == 1, matches
    return matches[0]


def read_zip_manifest(path: Path) -> tuple[dict, list[zipfile.ZipInfo]]:
    with zipfile.ZipFile(path, "r") as archive:
        infos = archive.infolist()
        manifest = json.loads(archive.read("manifest.json"))
    assert isinstance(manifest, dict)
    return manifest, infos


def test_create_backup_has_stable_exact_manifest_and_verified_payloads(
    tmp_path,
    isolated_paths,
):
    seeded = seed_backup_library(tmp_path)
    expected = source_bytes(seeded)
    real_before = file_manifest(isolated_paths["real_data"])

    result = seeded["manager"].create_backup(reason="测试手工备份")
    identifier = backup_id(result)
    archive_path = backup_zip(seeded, identifier)
    manifest, infos = read_zip_manifest(archive_path)

    names = [info.filename for info in infos]
    assert names == sorted([*expected, "manifest.json"])
    assert len(names) == len(set(names))
    assert manifest["manifest_version"] == 1
    assert manifest["backup_id"] == identifier
    assert manifest["kind"] == "manual"
    assert manifest["reason"] == "测试手工备份"
    assert manifest["schema_version"]
    items = {item["path"]: item for item in manifest["items"]}
    assert set(items) == set(expected)
    with zipfile.ZipFile(archive_path, "r") as archive:
        for logical_path, original in expected.items():
            item = items[logical_path]
            assert item["size"] == len(original)
            assert item["sha256"] == hashlib.sha256(original).hexdigest()
            assert archive.read(logical_path) == original
    assert file_manifest(isolated_paths["real_data"]) == real_before


def test_list_and_dry_run_are_pure_reads(tmp_path):
    seeded = seed_backup_library(tmp_path)
    created = seeded["manager"].create_backup()
    before = file_manifest(tmp_path)

    listed = seeded["manager"].list_backups()
    dry_run = seeded["manager"].dry_run(backup_id(created))

    assert [item["backup_id"] for item in listed] == [backup_id(created)]
    assert isinstance(dry_run["current_fingerprint"], str)
    assert len(dry_run["current_fingerprint"]) == 64
    assert dry_run["changes"] == []
    assert dry_run["conflicts"] == []
    assert file_manifest(tmp_path) == before


def test_source_change_during_packaging_leaves_no_final_zip(tmp_path, monkeypatch):
    seeded = seed_backup_library(tmp_path)
    manager = seeded["manager"]
    source = seeded["projects_root"] / "alpha" / "user.yaml"
    original_write = manager._write_zip_staging

    def mutate_after_staging(*args, **kwargs):
        result = original_write(*args, **kwargs)
        source.write_text("id: user\nname: 打包期间变化\n", encoding="utf-8")
        return result

    monkeypatch.setattr(manager, "_write_zip_staging", mutate_after_staging)

    with pytest.raises(BackupConflict) as raised:
        manager.create_backup()

    assert raised.value.code == "source_changed"
    assert list(seeded["backups_root"].glob("*.zip")) == []
    assert not any(
        path.is_file()
        for path in seeded["backups_root"].rglob("*")
    )


def test_expired_policy_only_marks_backups_and_never_deletes(tmp_path):
    seeded = seed_backup_library(tmp_path, retention_count=1)
    first = seeded["manager"].create_backup(reason="first")
    second = seeded["manager"].create_backup(reason="second")
    zip_before = file_manifest(seeded["backups_root"])

    expired = seeded["manager"].expired_backup_ids()
    listed = seeded["manager"].list_backups()

    assert expired == [backup_id(first)]
    by_id = {item["backup_id"]: item for item in listed}
    assert by_id[backup_id(first)]["expired"] is True
    assert by_id[backup_id(second)]["expired"] is False
    assert file_manifest(seeded["backups_root"]) == zip_before


def test_project_names_are_not_excluded_and_only_internal_receipts_are_skipped(
    tmp_path,
):
    seeded = seed_backup_library(tmp_path)
    expected_payloads = {}
    for project in ("backup", "logs", "cache"):
        path = seeded["projects_root"] / project / "saves" / "main.json"
        payload = json.dumps(
            {"project": project, "session_id": "main", "revision": 0},
            ensure_ascii=False,
        ).encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        expected_payloads[project] = payload
    receipt = seeded["projects_root"] / ".migration-receipts" / "owned.rcpt"
    receipt.parent.mkdir(parents=True)
    receipt.write_bytes(b"internal ownership receipt")

    created = seeded["manager"].create_backup()
    manifest = seeded["manager"].get_verified(backup_id(created)).manifest
    archive_paths = {item["path"] for item in manifest["items"]}

    for project in expected_payloads:
        assert f"projects/{project}/saves/main.json" in archive_paths
        shutil.rmtree(seeded["projects_root"] / project)
    assert not any(".migration-receipts" in path for path in archive_paths)

    dry_run = seeded["manager"].dry_run(backup_id(created))
    seeded["manager"].restore(
        backup_id(created),
        expected_current_fingerprint=dry_run["current_fingerprint"],
        confirm_conflicts=True,
    )

    for project, payload in expected_payloads.items():
        restored = seeded["projects_root"] / project / "saves" / "main.json"
        assert restored.read_bytes() == payload
    assert not receipt.exists()
