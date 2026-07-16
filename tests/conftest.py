"""pytest 会话隔离：应用导入前重定向全部可写路径。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from tests.data_guard import file_manifest, manifest_diff, manifest_digest


REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_DATA_DIR = (REPO_ROOT / "data").resolve(strict=False)
REAL_PROMPTS_DIR = (REPO_ROOT / "prompts").resolve(strict=False)
REAL_BACKUPS_DIR = (REPO_ROOT / "backups").resolve(strict=False)
REAL_LOG_DIR = (REPO_ROOT / "logs").resolve(strict=False)

_TEMP_DIR = tempfile.TemporaryDirectory(prefix="local-tavern-pytest-")
TEST_ROOT = Path(_TEMP_DIR.name).resolve(strict=False)
TEST_DATA_DIR = TEST_ROOT / "data"
TEST_PROMPTS_DIR = TEST_ROOT / "prompts"
TEST_SETTINGS_PATH = TEST_DATA_DIR / "settings.json"
TEST_BACKUPS_DIR = TEST_ROOT / "backups"
TEST_LOG_DIR = TEST_ROOT / "logs"

# 必须先配置环境，再导入任何 core/routes/server 模块。
os.environ["TAVERN_DATA_DIR"] = str(TEST_DATA_DIR)
os.environ["TAVERN_PROJECTS_DIR"] = str(TEST_DATA_DIR / "projects")
os.environ["TAVERN_PROMPTS_DIR"] = str(TEST_PROMPTS_DIR)
os.environ["TAVERN_SETTINGS_PATH"] = str(TEST_SETTINGS_PATH)
os.environ["TAVERN_RECOVERY_DIR"] = str(TEST_DATA_DIR / ".recovery")
os.environ["TAVERN_MIGRATIONS_DIR"] = str(TEST_DATA_DIR / ".migrations")
os.environ["TAVERN_BACKUP_DIR"] = str(TEST_BACKUPS_DIR)
os.environ["TAVERN_LOG_DIR"] = str(TEST_LOG_DIR)
os.environ["TAVERN_OLLAMA_HOST"] = "http://127.0.0.1:1"

TEST_PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
for filename, content in {
    "system.md": "系统测试模板：只使用已定义角色，不得创建角色。",
    "group_chat.md": (
        "场景={{scene_meta_json}}\n"
        "用户={{user_profile_json}}\n"
        "角色={{character_context}}\n"
        "世界书={{worldbook_entries}}\n"
        "历史={{history}}\n"
        "本轮={{user_input}}"
    ),
    "summary.md": "前情提要(必填,1-3句纯文本): ...",
}.items():
    (TEST_PROMPTS_DIR / filename).write_text(content, encoding="utf-8")


REAL_DATA_BEFORE = file_manifest(REAL_DATA_DIR)
REAL_DATA_BEFORE_DIGEST = manifest_digest(REAL_DATA_BEFORE)
REAL_BACKUPS_BEFORE = file_manifest(REAL_BACKUPS_DIR)
REAL_BACKUPS_BEFORE_DIGEST = manifest_digest(REAL_BACKUPS_BEFORE)
REAL_LOGS_BEFORE = file_manifest(REAL_LOG_DIR)
REAL_LOGS_BEFORE_DIGEST = manifest_digest(REAL_LOGS_BEFORE)
_real_data_after: dict[str, str] | None = None
_real_backups_after: dict[str, str] | None = None
_real_logs_after: dict[str, str] | None = None
_cleanup_error = ""


import httpx  # noqa: E402  (路径隔离必须先建立)
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from tests.fakes.fake_ollama import FakeOllamaClient  # noqa: E402


@pytest.fixture(scope="session")
def isolated_paths() -> dict[str, Path]:
    return {
        "root": TEST_ROOT,
        "data": TEST_DATA_DIR,
        "projects": TEST_DATA_DIR / "projects",
        "prompts": TEST_PROMPTS_DIR,
        "settings": TEST_SETTINGS_PATH,
        "recovery": TEST_DATA_DIR / ".recovery",
        "migrations": TEST_DATA_DIR / ".migrations",
        "backups": TEST_BACKUPS_DIR,
        "logs": TEST_LOG_DIR,
        "real_data": REAL_DATA_DIR,
        "real_prompts": REAL_PROMPTS_DIR,
        "real_backups": REAL_BACKUPS_DIR,
        "real_logs": REAL_LOG_DIR,
    }


@pytest.fixture
def fake_ollama(monkeypatch: pytest.MonkeyPatch) -> FakeOllamaClient:
    from core import ollama_client

    fake = FakeOllamaClient()
    monkeypatch.setattr(ollama_client, "_client_instance", fake)
    return fake


@pytest_asyncio.fixture
async def app_client(fake_ollama: FakeOllamaClient):
    from server import app

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def seed_project():
    from core.character_loader import ensure_project, save_character

    def _seed(project: str) -> str:
        ensure_project(project)
        save_character(project, "test_character", {
            "id": "test_character",
            "name": "测试角色",
            "active": True,
            "tagline": "仅用于自动化测试",
            "initial_stats": {
                "affinity": 40,
                "mood": "平静",
                "posture": "站立",
            },
            "appearance": {"outfit": "测试服"},
        })
        return project

    return _seed


def pytest_report_header() -> list[str]:
    return [
        f"isolated data root: {TEST_DATA_DIR}",
        f"real data guard: {len(REAL_DATA_BEFORE)} files / {REAL_DATA_BEFORE_DIGEST}",
        f"real backups guard: {len(REAL_BACKUPS_BEFORE)} files / {REAL_BACKUPS_BEFORE_DIGEST}",
        f"real logs guard: {len(REAL_LOGS_BEFORE)} files / {REAL_LOGS_BEFORE_DIGEST}",
    ]


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    global _real_data_after, _real_backups_after, _real_logs_after, _cleanup_error

    _real_data_after = file_manifest(REAL_DATA_DIR)
    _real_backups_after = file_manifest(REAL_BACKUPS_DIR)
    _real_logs_after = file_manifest(REAL_LOG_DIR)
    for label, before, after in (
        ("data", REAL_DATA_BEFORE, _real_data_after),
        ("backups", REAL_BACKUPS_BEFORE, _real_backups_after),
        ("logs", REAL_LOGS_BEFORE, _real_logs_after),
    ):
        if after != before:
            added, removed, changed = manifest_diff(before, after)
            print(
                f"\n真实 {label} 保护失败：新增={added}，删除={removed}，修改={changed}",
                file=sys.stderr,
            )
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    try:
        _TEMP_DIR.cleanup()
    except OSError as exc:
        _cleanup_error = str(exc)
        print(f"\n测试临时目录清理失败：{exc}", file=sys.stderr)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if _real_data_after is None or _real_backups_after is None or _real_logs_after is None:
        return
    for label, before, after in (
        ("data", REAL_DATA_BEFORE, _real_data_after),
        ("backups", REAL_BACKUPS_BEFORE, _real_backups_after),
        ("logs", REAL_LOGS_BEFORE, _real_logs_after),
    ):
        if after == before:
            terminalreporter.write_line(
                f"真实 {label} 保护通过：{len(after)} files / {manifest_digest(after)}"
            )
    if not _cleanup_error:
        terminalreporter.write_line("pytest 临时数据目录已清理")
