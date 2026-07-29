import asyncio
import time

import pytest

from routes import chat, messages, projects, settings


async def _assert_event_loop_remains_responsive(awaitable):
    started = time.perf_counter()
    task = asyncio.create_task(awaitable)
    await asyncio.sleep(0.02)
    elapsed = time.perf_counter() - started
    result = await task
    assert elapsed < 0.1
    return result


@pytest.mark.asyncio
async def test_settings_read_runs_off_the_event_loop(monkeypatch):
    def slow_read():
        time.sleep(0.15)
        return {"think": True}

    monkeypatch.setattr(settings, "_read_settings", slow_read)
    assert await _assert_event_loop_remains_responsive(settings.api_get_settings()) == {
        "think": True
    }


@pytest.mark.asyncio
async def test_project_listing_runs_off_the_event_loop(monkeypatch):
    def slow_list():
        time.sleep(0.15)
        return ["project"]

    monkeypatch.setattr(projects, "list_projects", slow_list)
    assert await _assert_event_loop_remains_responsive(
        projects.api_list_projects()
    ) == {"projects": ["project"]}


@pytest.mark.asyncio
async def test_history_scan_runs_off_loop_and_paginates(monkeypatch):
    entries = [{"filename": f"save.{index}.json"} for index in range(7)]

    def slow_history(_history_dir, _save):
        time.sleep(0.15)
        return entries

    monkeypatch.setattr(messages, "_history_entries_sync", slow_history)
    result = await _assert_event_loop_remains_responsive(
        messages.api_list_history(
            project="project",
            save="save",
            offset=2,
            limit=3,
        )
    )
    assert result == {
        "snapshots": entries[2:5],
        "total": 7,
        "offset": 2,
        "limit": 3,
        "has_more": True,
    }


@pytest.mark.asyncio
async def test_chat_prompt_loading_and_budgeting_run_off_event_loop(monkeypatch):
    expected = ("assembly", ["character"], {"muted_ids": []})

    def slow_assembly(**_kwargs):
        time.sleep(0.15)
        return expected

    monkeypatch.setattr(chat, "_assemble_generation_prompt_sync", slow_assembly)
    result = await _assert_event_loop_remains_responsive(
        chat._assemble_generation_prompt(
            project="project",
            session={},
            user_text="hello",
            history_override=None,
            context_info={"context_limit": 4096, "source": "test"},
            num_predict=128,
        )
    )
    assert result == expected
