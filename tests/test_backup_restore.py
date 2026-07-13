from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import warnings
import zipfile
from pathlib import Path

import pytest

from core.backup_store import (
    BackupConflict,
    BackupIntegrityError,
    BackupOperationError,
)
from tests.test_backup_store import (
    active_manifest,
    backup_id,
    backup_zip,
    seed_backup_library,
    source_bytes,
)


def _canonical_fingerprint(root_states: dict, items: list[dict]) -> str:
    canonical = {
        "root_states": root_states,
        "items": [
            {
                "path": item["path"],
                "size": item["size"],
                "sha256": item["sha256"],
            }
            for item in sorted(items, key=lambda current: current["path"])
        ],
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _expected_current_fingerprint(seeded: dict) -> str:
    records = source_bytes(seeded)
    items = [
        {
            "path": path,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for path, payload in sorted(records.items())
    ]
    return _canonical_fingerprint(
        {
            "projects": seeded["projects_root"].is_dir(),
            "prompts": seeded["prompts_dir"].is_dir(),
            "settings": seeded["settings_path"].is_file(),
        },
        items,
    )


def _read_entries(path: Path) -> list[tuple[zipfile.ZipInfo, bytes]]:
    with zipfile.ZipFile(path, "r") as archive:
        return [(info, archive.read(info)) for info in archive.infolist()]


def _rewrite_entries(
    path: Path,
    entries: list[tuple[zipfile.ZipInfo, bytes]],
) -> None:
    staging = path.with_suffix(".rewrite.tmp")
    with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for info, payload in entries:
            archive.writestr(info, payload)
    os.replace(staging, path)


def _append_member(path: Path, info: zipfile.ZipInfo | str, payload: bytes) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(info, payload)


def _corrupt_archive(path: Path, attack: str) -> None:
    entries = _read_entries(path)
    payload_info, payload = next(
        (info, body)
        for info, body in entries
        if info.filename != "manifest.json"
    )
    if attack == "duplicate":
        _append_member(path, payload_info.filename, payload)
    elif attack == "traversal":
        _append_member(path, "../outside.txt", b"escape")
    elif attack == "absolute":
        _append_member(path, "/absolute.txt", b"escape")
    elif attack == "symlink":
        info = zipfile.ZipInfo("projects/alpha/symlink")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        _append_member(path, info, b"characters/hero.yaml")
    elif attack == "missing":
        _rewrite_entries(
            path,
            [
                (info, body)
                for info, body in entries
                if info.filename != payload_info.filename
            ],
        )
    elif attack == "extra":
        _append_member(path, "projects/alpha/unlisted.txt", b"extra")
    elif attack == "truncated":
        body = path.read_bytes()
        path.write_bytes(body[: max(1, len(body) // 2)])
    elif attack == "tampered":
        _rewrite_entries(
            path,
            [
                (info, b"tampered" if info.filename == payload_info.filename else body)
                for info, body in entries
            ],
        )
    else:
        raise AssertionError(attack)


def _replace_payload_with_valid_manifest(
    path: Path,
    logical_path: str,
    payload: bytes,
) -> None:
    entries = _read_entries(path)
    by_name = {info.filename: (info, body) for info, body in entries}
    manifest = json.loads(by_name["manifest.json"][1])
    item = next(item for item in manifest["items"] if item["path"] == logical_path)
    item["size"] = len(payload)
    item["sha256"] = hashlib.sha256(payload).hexdigest()
    manifest["source_fingerprint"] = _canonical_fingerprint(
        manifest["root_states"],
        manifest["items"],
    )
    by_name[logical_path] = (by_name[logical_path][0], payload)
    manifest_payload = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    by_name["manifest.json"] = (by_name["manifest.json"][0], manifest_payload)
    _rewrite_entries(
        path,
        [by_name[name] for name in sorted(by_name)],
    )


@pytest.mark.parametrize(
    "attack",
    [
        "duplicate",
        "traversal",
        "absolute",
        "symlink",
        "missing",
        "extra",
        "truncated",
        "tampered",
    ],
)
def test_hostile_or_corrupt_zip_is_rejected_before_active_library_write(
    tmp_path,
    attack,
):
    seeded = seed_backup_library(tmp_path)
    created = seeded["manager"].create_backup()
    identifier = backup_id(created)
    expected_fingerprint = seeded["manager"].dry_run(identifier)[
        "current_fingerprint"
    ]
    active_before = active_manifest(seeded)
    _corrupt_archive(backup_zip(seeded, identifier), attack)
    backup_files_before = {
        path.relative_to(seeded["backups_root"]).as_posix()
        for path in seeded["backups_root"].rglob("*")
        if path.is_file()
    }

    with pytest.raises(BackupIntegrityError):
        seeded["manager"].restore(
            identifier,
            expected_current_fingerprint=expected_fingerprint,
            confirm_conflicts=True,
        )

    assert active_manifest(seeded) == active_before
    assert {
        path.relative_to(seeded["backups_root"]).as_posix()
        for path in seeded["backups_root"].rglob("*")
        if path.is_file()
    } == backup_files_before


@pytest.mark.parametrize(
    ("logical_path", "invalid_payload"),
    [
        ("settings.json", b'{"broken":'),
        ("projects/alpha/characters/hero.yaml", b"name: [unterminated"),
    ],
)
def test_invalid_json_or_yaml_is_rejected_after_hashes_verify_and_before_write(
    tmp_path,
    logical_path,
    invalid_payload,
):
    seeded = seed_backup_library(tmp_path)
    created = seeded["manager"].create_backup()
    identifier = backup_id(created)
    expected_fingerprint = seeded["manager"].dry_run(identifier)[
        "current_fingerprint"
    ]
    active_before = active_manifest(seeded)
    _replace_payload_with_valid_manifest(
        backup_zip(seeded, identifier),
        logical_path,
        invalid_payload,
    )

    with pytest.raises(BackupIntegrityError) as raised:
        seeded["manager"].restore(
            identifier,
            expected_current_fingerprint=expected_fingerprint,
            confirm_conflicts=True,
        )

    assert raised.value.code == "invalid_payload"
    assert active_manifest(seeded) == active_before


def test_dry_run_reports_changes_conflicts_and_exact_current_fingerprint(tmp_path):
    seeded = seed_backup_library(tmp_path)
    created = seeded["manager"].create_backup()
    hero = seeded["projects_root"] / "alpha" / "characters" / "hero.yaml"
    hero.write_text("id: hero\nname: 当前版本\n", encoding="utf-8")
    (seeded["prompts_dir"] / "group_chat.md").unlink()
    extra = seeded["projects_root"] / "beta" / "user.yaml"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("id: user\nname: 额外项目\n", encoding="utf-8")
    before = active_manifest(seeded)

    result = seeded["manager"].dry_run(backup_id(created))

    changes = {item["path"]: item["action"] for item in result["changes"]}
    assert changes["projects/alpha/characters/hero.yaml"] == "replace"
    assert changes["prompts/group_chat.md"] == "create"
    assert changes["projects/beta/user.yaml"] == "remove"
    assert {
        item["path"] for item in result["conflicts"]
    } == {
        "projects/alpha/characters/hero.yaml",
        "projects/beta/user.yaml",
    }
    assert result["current_fingerprint"] == _expected_current_fingerprint(seeded)
    assert active_manifest(seeded) == before


def test_stale_expected_fingerprint_rejects_restore_without_any_write(tmp_path):
    seeded = seed_backup_library(tmp_path)
    created = seeded["manager"].create_backup()
    identifier = backup_id(created)
    stale = seeded["manager"].dry_run(identifier)["current_fingerprint"]
    target = seeded["projects_root"] / "alpha" / "user.yaml"
    target.write_text("id: user\nname: newer\n", encoding="utf-8")
    active_before = active_manifest(seeded)
    backups_before = {
        item["backup_id"] for item in seeded["manager"].list_backups()
    }

    with pytest.raises(BackupConflict) as raised:
        seeded["manager"].restore(
            identifier,
            expected_current_fingerprint=stale,
            confirm_conflicts=True,
        )

    assert raised.value.code == "current_changed"
    assert active_manifest(seeded) == active_before
    assert {
        item["backup_id"] for item in seeded["manager"].list_backups()
    } == backups_before


def test_deleted_fixture_library_restores_byte_exact_with_pre_restore_backup(tmp_path):
    seeded = seed_backup_library(tmp_path)
    expected = active_manifest(seeded)
    created = seeded["manager"].create_backup(reason="restore source")
    identifier = backup_id(created)
    shutil.rmtree(seeded["projects_root"])
    shutil.rmtree(seeded["prompts_dir"])
    seeded["settings_path"].unlink()

    dry_run = seeded["manager"].dry_run(identifier)
    result = seeded["manager"].restore(
        identifier,
        expected_current_fingerprint=dry_run["current_fingerprint"],
        confirm_conflicts=True,
    )

    assert result["restored"] is True
    assert result["backup_id"] == identifier
    assert result["pre_restore_backup_id"]
    assert active_manifest(seeded) == expected
    by_id = {
        item["backup_id"]: item for item in seeded["manager"].list_backups()
    }
    assert by_id[result["pre_restore_backup_id"]]["kind"] == "pre_restore"


def _mutate_all_roots(seeded: dict) -> None:
    (seeded["projects_root"] / "alpha" / "user.yaml").write_text(
        "id: user\nname: 当前用户\n",
        encoding="utf-8",
    )
    (seeded["prompts_dir"] / "system.md").write_text(
        "当前系统模板",
        encoding="utf-8",
    )
    seeded["settings_path"].write_text(
        json.dumps({"current_project": "changed"}),
        encoding="utf-8",
    )


def test_nth_root_switch_failure_rolls_back_entire_active_library(
    tmp_path,
    monkeypatch,
):
    seeded = seed_backup_library(tmp_path)
    created = seeded["manager"].create_backup()
    identifier = backup_id(created)
    _mutate_all_roots(seeded)
    active_before = active_manifest(seeded)
    current = seeded["manager"].dry_run(identifier)["current_fingerprint"]
    manager = seeded["manager"]
    original_replace = manager._replace_path
    active_roots = {
        seeded["projects_root"].resolve(strict=False),
        seeded["prompts_dir"].resolve(strict=False),
        seeded["settings_path"].resolve(strict=False),
    }
    root_switch_calls = 0

    def fail_third_root_switch(source, target):
        nonlocal root_switch_calls
        resolved_source = Path(source).resolve(strict=False)
        resolved_target = Path(target).resolve(strict=False)
        if resolved_source in active_roots or resolved_target in active_roots:
            root_switch_calls += 1
            if root_switch_calls == 3:
                raise OSError("injected root switch failure")
        return original_replace(source, target)

    monkeypatch.setattr(manager, "_replace_path", fail_third_root_switch)

    with pytest.raises(BackupOperationError) as raised:
        manager.restore(
            identifier,
            expected_current_fingerprint=current,
            confirm_conflicts=True,
        )

    assert raised.value.code == "restore_failed"
    assert root_switch_calls >= 3
    assert active_manifest(seeded) == active_before


def test_terminal_journal_failure_rolls_back_entire_active_library(
    tmp_path,
    monkeypatch,
):
    seeded = seed_backup_library(tmp_path)
    created = seeded["manager"].create_backup()
    identifier = backup_id(created)
    _mutate_all_roots(seeded)
    active_before = active_manifest(seeded)
    current = seeded["manager"].dry_run(identifier)["current_fingerprint"]
    manager = seeded["manager"]
    original_write_journal = manager._write_journal

    def fail_terminal_journal(path, journal):
        if journal.get("status") == "committed":
            raise OSError("injected terminal journal failure")
        return original_write_journal(path, journal)

    monkeypatch.setattr(manager, "_write_journal", fail_terminal_journal)

    with pytest.raises(BackupOperationError) as raised:
        manager.restore(
            identifier,
            expected_current_fingerprint=current,
            confirm_conflicts=True,
        )

    assert raised.value.code == "restore_failed"
    assert active_manifest(seeded) == active_before
