from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from desktop.environment import (
    build_paths,
    migrate_legacy_data,
    paths_touch_root,
    prepare_desktop_environment,
)


DESKTOP_ENV_NAMES = (
    "TAVERN_BASE_DIR",
    "TAVERN_DATA_DIR",
    "TAVERN_SETTINGS_PATH",
    "TAVERN_PROJECTS_DIR",
    "TAVERN_WEB_DIR",
    "TAVERN_PROMPTS_DIR",
    "TAVERN_RECOVERY_DIR",
    "TAVERN_MIGRATIONS_DIR",
    "TAVERN_BACKUP_DIR",
    "TAVERN_LOG_DIR",
    "TAVERN_LOG_FILE",
    "TAVERN_PID_PATH",
    "TAVERN_STOP_REQUEST_PATH",
    "TAVERN_PROVIDER_DATA_DIR",
    "TAVERN_PROVIDER_CONFIG_PATH",
    "TAVERN_PROVIDER_SECRETS_PATH",
    "TAVERN_ALLOW_REMOTE",
    "TAVERN_HOST",
    "TAVERN_PORT",
    "TAVERN_DESKTOP_MODE",
)


def _clear_desktop_environment(monkeypatch) -> None:
    for name in DESKTOP_ENV_NAMES:
        # 先通过 monkeypatch 写入哨兵，确保被测代码随后直接写 os.environ
        # 时，测试结束仍能恢复原值或删除原本不存在的键。
        monkeypatch.setenv(name, "__desktop_test_isolation__")
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delitem(sys.modules, "core.config", raising=False)


def test_prepare_desktop_environment_uses_user_writable_paths_and_imports_once(
    monkeypatch,
    tmp_path,
):
    _clear_desktop_environment(monkeypatch)
    resources = tmp_path / "bundle"
    shipped_prompts = resources / "prompts"
    shipped_prompts.mkdir(parents=True)
    (shipped_prompts / "system.md").write_text("打包默认提示词", encoding="utf-8")
    (shipped_prompts / "group_chat.md").write_text("打包群聊模板", encoding="utf-8")
    legacy = tmp_path / "legacy"
    (legacy / "data" / "projects").mkdir(parents=True)
    (legacy / "data" / "projects" / "save.json").write_text("legacy", encoding="utf-8")
    (legacy / "backups").mkdir(parents=True)
    (legacy / "backups" / "backup.json").write_text("backup", encoding="utf-8")
    (legacy / "prompts").mkdir(parents=True)
    (legacy / "prompts" / "system.md").write_text("旧版用户提示词", encoding="utf-8")
    user_root = tmp_path / "user"

    paths = prepare_desktop_environment(
        resources,
        user_root=user_root,
        legacy_root=legacy,
    )

    assert paths.user_root == user_root.resolve()
    assert paths.resource_root == resources.resolve()
    assert (paths.projects_dir / "save.json").read_text(encoding="utf-8") == "legacy"
    assert (paths.backups_dir / "backup.json").read_text(encoding="utf-8") == "backup"
    assert (paths.prompts_dir / "system.md").read_text(encoding="utf-8") == "旧版用户提示词"
    assert (paths.prompts_dir / "group_chat.md").read_text(encoding="utf-8") == "打包群聊模板"
    assert os.environ["TAVERN_DESKTOP_MODE"] == "true"
    assert os.environ["TAVERN_ALLOW_REMOTE"] == "false"
    assert os.environ["TAVERN_DATA_DIR"] == str(paths.data_dir)
    assert os.environ["TAVERN_WEB_DIR"] == str(resources / "web")
    assert os.environ["TAVERN_PID_PATH"] == str(paths.runtime_dir / "desktop.pid")
    assert os.environ["TAVERN_PROVIDER_DATA_DIR"] == str(paths.user_root)
    assert os.environ["TAVERN_PROVIDER_CONFIG_PATH"] == str(paths.user_root / "providers.json")
    assert os.environ["TAVERN_PROVIDER_SECRETS_PATH"] == str(
        paths.user_root / "provider-secrets.json"
    )
    assert all(
        Path(os.environ[name]).is_absolute()
        for name in (
            "TAVERN_DATA_DIR",
            "TAVERN_PROJECTS_DIR",
            "TAVERN_PROMPTS_DIR",
            "TAVERN_BACKUP_DIR",
            "TAVERN_LOG_DIR",
        )
    )


def test_desktop_migration_never_overwrites_existing_user_data(monkeypatch, tmp_path):
    _clear_desktop_environment(monkeypatch)
    resources = tmp_path / "bundle"
    user_root = tmp_path / "user"
    paths = build_paths(resources, user_root)
    paths.data_dir.mkdir(parents=True)
    paths.backups_dir.mkdir(parents=True)
    paths.prompts_dir.mkdir(parents=True)
    (paths.data_dir / "save.json").write_text("current", encoding="utf-8")
    (paths.backups_dir / "backup.json").write_text("current-backup", encoding="utf-8")
    (paths.prompts_dir / "system.md").write_text("current-prompt", encoding="utf-8")

    legacy = tmp_path / "legacy"
    (legacy / "data").mkdir(parents=True)
    (legacy / "backups").mkdir(parents=True)
    (legacy / "prompts").mkdir(parents=True)
    (legacy / "data" / "save.json").write_text("old", encoding="utf-8")
    (legacy / "backups" / "backup.json").write_text("old-backup", encoding="utf-8")
    (legacy / "prompts" / "system.md").write_text("old-prompt", encoding="utf-8")
    (legacy / "prompts" / "legacy-only.md").write_text("legacy-only", encoding="utf-8")

    assert migrate_legacy_data(paths, legacy) == {"data": False, "backups": False}
    assert (paths.data_dir / "save.json").read_text(encoding="utf-8") == "current"
    assert (paths.backups_dir / "backup.json").read_text(encoding="utf-8") == "current-backup"
    assert (paths.prompts_dir / "system.md").read_text(encoding="utf-8") == "current-prompt"
    assert (paths.prompts_dir / "legacy-only.md").read_text(encoding="utf-8") == "legacy-only"


def test_desktop_environment_preserves_explicit_provider_paths_but_forces_web_root(
    monkeypatch,
    tmp_path,
):
    _clear_desktop_environment(monkeypatch)
    resources = tmp_path / "bundle"
    user_root = tmp_path / "user"
    provider_data = tmp_path / "provider-data"
    provider_config = tmp_path / "provider-config" / "providers.json"
    provider_secrets = tmp_path / "provider-secrets" / "provider-secrets.json"
    monkeypatch.setenv("TAVERN_PROVIDER_DATA_DIR", str(provider_data))
    monkeypatch.setenv("TAVERN_PROVIDER_CONFIG_PATH", str(provider_config))
    monkeypatch.setenv("TAVERN_PROVIDER_SECRETS_PATH", str(provider_secrets))
    monkeypatch.setenv("TAVERN_WEB_DIR", str(tmp_path / "untrusted-web"))

    prepare_desktop_environment(resources, user_root=user_root)

    assert os.environ["TAVERN_PROVIDER_DATA_DIR"] == str(provider_data)
    assert os.environ["TAVERN_PROVIDER_CONFIG_PATH"] == str(provider_config)
    assert os.environ["TAVERN_PROVIDER_SECRETS_PATH"] == str(provider_secrets)
    assert os.environ["TAVERN_WEB_DIR"] == str(resources / "web")


@pytest.mark.parametrize(
    "environment_name",
    [
        "TAVERN_BASE_DIR",
        "TAVERN_DATA_DIR",
        "TAVERN_SETTINGS_PATH",
        "TAVERN_PROJECTS_DIR",
        "TAVERN_PROMPTS_DIR",
        "TAVERN_RECOVERY_DIR",
        "TAVERN_MIGRATIONS_DIR",
        "TAVERN_BACKUP_DIR",
        "TAVERN_LOG_DIR",
        "TAVERN_LOG_FILE",
        "TAVERN_PID_PATH",
        "TAVERN_STOP_REQUEST_PATH",
        "TAVERN_PROVIDER_DATA_DIR",
        "TAVERN_PROVIDER_CONFIG_PATH",
        "TAVERN_PROVIDER_SECRETS_PATH",
    ],
)
def test_isolated_smoke_guard_checks_every_explicit_writable_path(
    monkeypatch,
    tmp_path,
    environment_name,
):
    _clear_desktop_environment(monkeypatch)
    paths = build_paths(tmp_path / "bundle", tmp_path / "isolated-user")
    legacy_root = tmp_path / "legacy"
    assert paths_touch_root(paths, legacy_root) is False

    monkeypatch.setenv(environment_name, str(legacy_root / "nested"))

    assert paths_touch_root(paths, legacy_root) is True


def test_prepare_desktop_environment_must_precede_core_config_import(
    monkeypatch,
    tmp_path,
):
    sentinel = object()
    monkeypatch.setitem(sys.modules, "core.config", sentinel)
    with pytest.raises(RuntimeError, match="导入 core.config 前"):
        prepare_desktop_environment(tmp_path / "bundle", user_root=tmp_path / "user")
