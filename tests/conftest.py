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

_TEMP_DIR = tempfile.TemporaryDirectory(prefix="local-tavern-pytest-")
TEST_ROOT = Path(_TEMP_DIR.name).resolve(strict=False)
TEST_DATA_DIR = TEST_ROOT / "data"
TEST_PROMPTS_DIR = TEST_ROOT / "prompts"
TEST_SETTINGS_PATH = TEST_DATA_DIR / "settings.json"

# 必须先配置环境，再导入任何 core/routes/server 模块。
os.environ["TAVERN_DATA_DIR"] = str(TEST_DATA_DIR)
os.environ["TAVERN_PROJECTS_DIR"] = str(TEST_DATA_DIR / "projects")
os.environ["TAVERN_PROMPTS_DIR"] = str(TEST_PROMPTS_DIR)
os.environ["TAVERN_SETTINGS_PATH"] = str(TEST_SETTINGS_PATH)
os.environ["TAVERN_OLLAMA_HOST"] = "http://127.0.0.1:1"

TEST_PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
for filename, content in {
    "system.md": "系统测试模板\n{{character_list}}\n{{user_profile}}\n{{worldbook_entries}}\n{{format_example}}",
    "group_chat.md": "{{history}}\n{{scene_meta_json}}\n{{characters_state_json}}\n用户：{{user_input}}",
    "summary.md": "前情提要(必填,1-3句纯文本): ...",
}.items():
    (TEST_PROMPTS_DIR / filename).write_text(content, encoding="utf-8")


REAL_DATA_BEFORE = file_manifest(REAL_DATA_DIR)
REAL_DATA_BEFORE_DIGEST = manifest_digest(REAL_DATA_BEFORE)
_real_data_after: dict[str, str] | None = None
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
        "real_data": REAL_DATA_DIR,
        "real_prompts": REAL_PROMPTS_DIR,
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
    ]


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    global _real_data_after, _cleanup_error

    _real_data_after = file_manifest(REAL_DATA_DIR)
    if _real_data_after != REAL_DATA_BEFORE:
        added, removed, changed = manifest_diff(REAL_DATA_BEFORE, _real_data_after)
        print(
            f"\n真实 data 保护失败：新增={added}，删除={removed}，修改={changed}",
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
    if _real_data_after is None:
        return
    after_digest = manifest_digest(_real_data_after)
    if _real_data_after == REAL_DATA_BEFORE:
        terminalreporter.write_line(
            f"真实 data 保护通过：{len(_real_data_after)} files / {after_digest}"
        )
    if not _cleanup_error:
        terminalreporter.write_line("pytest 临时数据目录已清理")
