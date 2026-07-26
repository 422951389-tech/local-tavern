from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from tools import node_dependency_lock as node_lock
from tools.node_dependency_lock import (
    NodeLockValidationError,
    read_json,
    validate_installed_tree,
    validate_lock,
    validate_manifest,
    verify_environment,
)


def manifest() -> dict:
    return {
        "name": "local-tavern-dev-tools",
        "version": "0.0.0",
        "private": True,
        "engines": {"node": "24.15.0", "npm": "11.12.1"},
        "packageManager": "npm@11.12.1",
        "overrides": {"playwright-core": "1.61.1"},
        "devDependencies": {
            "@axe-core/playwright": "4.12.1",
            "playwright": "1.61.1",
        },
    }


def artifact(version: str) -> dict:
    return {
        "version": version,
        "resolved": f"https://registry.npmjs.org/pkg/-/pkg-{version}.tgz",
        "integrity": f"sha512-{base64.b64encode(b'x' * 64).decode('ascii')}",
        "dev": True,
    }


def lock() -> dict:
    direct = manifest()["devDependencies"]
    fsevents = artifact("2.3.2")
    fsevents.update({"optional": True, "os": ["darwin"]})
    return {
        "name": "local-tavern-dev-tools",
        "version": "0.0.0",
        "lockfileVersion": 3,
        "requires": True,
        "packages": {
            "": {
                "name": "local-tavern-dev-tools",
                "version": "0.0.0",
                "devDependencies": direct,
            },
            "node_modules/@axe-core/playwright": artifact("4.12.1"),
            "node_modules/axe-core": artifact("4.12.1"),
            "node_modules/fsevents": fsevents,
            "node_modules/playwright": artifact("1.61.1"),
            "node_modules/playwright-core": artifact("1.61.1"),
        },
    }


def test_manifest_and_lock_require_an_exact_dev_only_cross_platform_closure():
    direct = validate_manifest(manifest())
    locked = validate_lock(lock(), direct)
    assert locked == {
        "@axe-core/playwright": "4.12.1",
        "axe-core": "4.12.1",
        "fsevents": "2.3.2",
        "playwright": "1.61.1",
        "playwright-core": "1.61.1",
    }


def test_manifest_rejects_ranges_production_dependencies_and_unsorted_names():
    ranged = manifest()
    ranged["devDependencies"]["playwright"] = "^1.61.1"
    with pytest.raises(NodeLockValidationError, match="exact version"):
        validate_manifest(ranged)
    production = manifest()
    production["dependencies"] = {"playwright": "1.61.1"}
    with pytest.raises(NodeLockValidationError, match="production"):
        validate_manifest(production)
    null_production = manifest()
    null_production["dependencies"] = None
    with pytest.raises(NodeLockValidationError, match="production"):
        validate_manifest(null_production)
    unsorted = manifest()
    unsorted["devDependencies"] = {
        "playwright": "1.61.1",
        "@axe-core/playwright": "4.12.1",
    }
    with pytest.raises(NodeLockValidationError, match="sorted"):
        validate_manifest(unsorted)
    missing_override = manifest()
    missing_override.pop("overrides")
    with pytest.raises(NodeLockValidationError, match="overrides"):
        validate_manifest(missing_override)


def test_lock_rejects_non_registry_missing_integrity_and_extra_packages():
    direct = validate_manifest(manifest())
    invalid = lock()
    invalid["packages"]["node_modules/playwright"]["resolved"] = "https://example.test/p.tgz"
    with pytest.raises(NodeLockValidationError, match="public npm registry"):
        validate_lock(invalid, direct)
    invalid = lock()
    invalid["packages"]["node_modules/playwright"].pop("integrity")
    with pytest.raises(NodeLockValidationError, match="SHA-512"):
        validate_lock(invalid, direct)
    invalid = lock()
    invalid["packages"]["node_modules/playwright"]["integrity"] = "sha512-eA=="
    with pytest.raises(NodeLockValidationError, match="invalid SHA-512"):
        validate_lock(invalid, direct)
    invalid = lock()
    invalid["packages"]["node_modules/extra"] = artifact("1.0.0")
    with pytest.raises(NodeLockValidationError, match="extra"):
        validate_lock(invalid, direct)
    invalid = lock()
    invalid["packages"][""]["dependencies"] = None
    with pytest.raises(NodeLockValidationError, match="production"):
        validate_lock(invalid, direct)
    invalid = lock()
    invalid["packages"]["node_modules/fsevents"].pop("optional")
    with pytest.raises(NodeLockValidationError, match="optional"):
        validate_lock(invalid, direct)
    invalid = lock()
    invalid["packages"]["node_modules/playwright-core"]["version"] = "1.62.0"
    with pytest.raises(NodeLockValidationError, match="versions differ"):
        validate_lock(invalid, direct)


def test_installed_tree_must_match_only_direct_versions():
    direct = validate_manifest(manifest())
    tree = {
        "dependencies": {
            "@axe-core/playwright": {"version": "4.12.1"},
            "playwright": {"version": "1.61.1"},
        }
    }
    assert validate_installed_tree(tree, direct) == direct
    tree["dependencies"]["playwright"]["extraneous"] = True
    with pytest.raises(NodeLockValidationError, match="not clean"):
        validate_installed_tree(tree, direct)


def test_environment_requires_exact_tools_and_clean_direct_tree(monkeypatch, tmp_path):
    direct = validate_manifest(manifest())
    versions = {"node.exe": "24.15.0", "npm.cmd": "11.12.1"}
    tree = {
        "dependencies": {
            "@axe-core/playwright": {"version": "4.12.1"},
            "playwright": {"version": "1.61.1"},
        }
    }

    monkeypatch.setattr(
        node_lock,
        "_run_version",
        lambda command, _argument, _label: versions[command],
    )
    monkeypatch.setattr(
        node_lock.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(tree),
        ),
    )
    assert verify_environment(tmp_path, direct, "node.exe", "npm.cmd") == {
        "node": "24.15.0",
        "npm": "11.12.1",
        "direct_packages": direct,
    }

    versions["npm.cmd"] = "11.12.0"
    with pytest.raises(NodeLockValidationError, match="environment differs"):
        verify_environment(tmp_path, direct, "node.exe", "npm.cmd")


def test_json_reader_rejects_duplicate_keys(tmp_path):
    path = tmp_path / "package.json"
    path.write_text('{"name":"a","name":"b"}', encoding="utf-8")
    with pytest.raises(NodeLockValidationError, match="duplicate key"):
        read_json(path)
    path.write_text(json.dumps(manifest()), encoding="utf-8")
    assert read_json(path)["private"] is True
