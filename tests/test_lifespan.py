from __future__ import annotations

import asyncio

import pytest

import server
from core import chat_turns


class _Manager:
    def __init__(self, pending=None, error: Exception | None = None):
        self.pending = [] if pending is None else pending
        self.error = error

    def pending_restore_ids(self):
        if self.error is not None:
            raise self.error
        return list(self.pending)


class _Coordinator:
    def __init__(self, recovery_error: Exception | None = None):
        self.recovery_error = recovery_error
        self.recovered = 0
        self.closed = 0

    async def ensure_recovered(self):
        self.recovered += 1
        if self.recovery_error is not None:
            raise self.recovery_error

    async def shutdown(self):
        self.closed += 1


class _Client:
    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1


def _wire(monkeypatch, *, manager, coordinator=None, schedule=False):
    released: list[dict] = []
    client = _Client()
    stopped = asyncio.Event()
    background_closed: list[bool] = []

    monkeypatch.setattr(
        server,
        "claim_pid_file",
        lambda: {"pid": 1234, "token": "test"},
    )
    monkeypatch.setattr(server, "release_pid_file", lambda metadata: released.append(metadata))
    monkeypatch.setattr(server.backups, "get_backup_manager", lambda: manager)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "BACKUP_SCHEDULE_ENABLED", schedule)
    if coordinator is not None:
        monkeypatch.setattr(chat_turns, "get_turn_coordinator", lambda: coordinator)

    async def stop_monitor(_metadata):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def close_background():
        background_closed.append(True)

    async def scheduler(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(server, "wait_for_stop_request", stop_monitor)
    monkeypatch.setattr(server.chat, "shutdown_chat_background_tasks", close_background)
    monkeypatch.setattr(server, "run_backup_scheduler", scheduler)
    return released, client, stopped, background_closed


@pytest.mark.asyncio
async def test_lifespan_normal_shutdown_releases_every_resource(monkeypatch):
    coordinator = _Coordinator()
    released, client, stopped, background_closed = _wire(
        monkeypatch,
        manager=_Manager(),
        coordinator=coordinator,
        schedule=True,
    )
    async with server.lifespan(server.app):
        await asyncio.sleep(0)

    assert coordinator.recovered == 1
    assert coordinator.closed == 1
    assert client.closed == 1
    assert stopped.is_set()
    assert background_closed == [True]
    assert released == [{"pid": 1234, "token": "test"}]


@pytest.mark.asyncio
async def test_pending_restore_query_failure_still_releases_pid(monkeypatch):
    released, client, stopped, background_closed = _wire(
        monkeypatch,
        manager=_Manager(error=RuntimeError("restore lookup failed")),
    )
    with pytest.raises(RuntimeError, match="restore lookup failed"):
        async with server.lifespan(server.app):
            pass

    assert client.closed == 1
    assert stopped.is_set()
    assert background_closed == [True]
    assert len(released) == 1


@pytest.mark.asyncio
async def test_turn_recovery_failure_still_releases_pid(monkeypatch):
    coordinator = _Coordinator(RuntimeError("turn recovery failed"))
    released, client, stopped, background_closed = _wire(
        monkeypatch,
        manager=_Manager(),
        coordinator=coordinator,
    )
    with pytest.raises(RuntimeError, match="turn recovery failed"):
        async with server.lifespan(server.app):
            pass

    assert coordinator.recovered == 1
    assert coordinator.closed == 1
    assert client.closed == 1
    assert stopped.is_set()
    assert background_closed == [True]
    assert len(released) == 1


@pytest.mark.asyncio
async def test_scheduler_task_creation_failure_cleans_started_monitor(monkeypatch):
    coordinator = _Coordinator()
    released, client, stopped, background_closed = _wire(
        monkeypatch,
        manager=_Manager(),
        coordinator=coordinator,
        schedule=True,
    )
    real_create_task = asyncio.create_task
    calls = 0

    def fail_second_create(coroutine):
        nonlocal calls
        calls += 1
        if calls == 2:
            coroutine.close()
            raise RuntimeError("scheduler task creation failed")
        return real_create_task(coroutine)

    monkeypatch.setattr(server.asyncio, "create_task", fail_second_create)
    with pytest.raises(RuntimeError, match="scheduler task creation failed"):
        async with server.lifespan(server.app):
            pass

    assert coordinator.closed == 1
    assert client.closed == 1
    assert stopped.is_set()
    assert background_closed == [True]
    assert len(released) == 1


@pytest.mark.asyncio
async def test_cleanup_continues_after_coordinator_cancellation(monkeypatch):
    released: list[dict] = []
    background_closed: list[bool] = []
    client = _Client()

    class CancellingCoordinator:
        async def shutdown(self):
            raise asyncio.CancelledError()

    async def close_background():
        background_closed.append(True)

    monkeypatch.setattr(server, "release_pid_file", released.append)
    monkeypatch.setattr(server.chat, "shutdown_chat_background_tasks", close_background)
    monkeypatch.setattr(server, "get_client", lambda: client)

    with pytest.raises(asyncio.CancelledError):
        await server._cleanup_lifespan(
            {"pid": 1234, "token": "test"},
            stop_monitor=None,
            backup_scheduler=None,
            turn_coordinator=CancellingCoordinator(),
        )

    assert background_closed == [True]
    assert client.closed == 1
    assert released == [{"pid": 1234, "token": "test"}]


@pytest.mark.asyncio
async def test_external_cleanup_cancellation_still_closes_every_resource(monkeypatch):
    released: list[dict] = []
    background_closed: list[bool] = []
    client = _Client()
    monitor_started = asyncio.Event()
    monitor_received_cancel = asyncio.Event()

    async def stop_monitor():
        monitor_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            monitor_received_cancel.set()
            await asyncio.Event().wait()

    async def close_background():
        background_closed.append(True)

    monkeypatch.setattr(server, "release_pid_file", released.append)
    monkeypatch.setattr(server.chat, "shutdown_chat_background_tasks", close_background)
    monkeypatch.setattr(server, "get_client", lambda: client)

    monitor = asyncio.create_task(stop_monitor())
    await monitor_started.wait()
    cleanup = asyncio.create_task(
        server._cleanup_lifespan(
            {"pid": 1234, "token": "test"},
            stop_monitor=monitor,
            backup_scheduler=None,
            turn_coordinator=None,
        )
    )
    await monitor_received_cancel.wait()
    cleanup.cancel()

    with pytest.raises(asyncio.CancelledError):
        await cleanup

    assert monitor.cancelled()
    assert background_closed == [True]
    assert client.closed == 1
    assert released == [{"pid": 1234, "token": "test"}]


@pytest.mark.asyncio
async def test_desktop_lifespan_skips_process_guard_but_keeps_recovery_scheduler_and_cleanup(
    monkeypatch,
):
    coordinator = _Coordinator()
    client = _Client()
    scheduler_started = asyncio.Event()
    scheduler_stopped = asyncio.Event()
    background_closed: list[bool] = []
    providers_closed: list[bool] = []

    monkeypatch.setattr(server, "DESKTOP_MODE", True)
    monkeypatch.setattr(
        server,
        "claim_pid_file",
        lambda: (_ for _ in ()).throw(AssertionError("桌面模式不得登记 PID")),
    )
    monkeypatch.setattr(
        server,
        "release_pid_file",
        lambda _metadata: (_ for _ in ()).throw(AssertionError("桌面模式不得释放 PID 文件")),
    )

    async def reject_stop_monitor(_metadata):
        raise AssertionError("桌面模式不得启动 stop monitor")

    async def scheduler(*_args, **_kwargs):
        scheduler_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            scheduler_stopped.set()

    async def close_background():
        background_closed.append(True)

    async def close_providers():
        providers_closed.append(True)

    monkeypatch.setattr(server, "wait_for_stop_request", reject_stop_monitor)
    monkeypatch.setattr(server.backups, "get_backup_manager", lambda: _Manager())
    monkeypatch.setattr(server, "run_backup_scheduler", scheduler)
    monkeypatch.setattr(server, "BACKUP_SCHEDULE_ENABLED", True)
    monkeypatch.setattr(server, "get_client", lambda: client)
    monkeypatch.setattr(server, "close_provider_registry", close_providers)
    monkeypatch.setattr(server.chat, "shutdown_chat_background_tasks", close_background)
    monkeypatch.setattr(chat_turns, "get_turn_coordinator", lambda: coordinator)

    async with server.lifespan(server.app):
        await asyncio.wait_for(scheduler_started.wait(), timeout=1)
        assert coordinator.recovered == 1

    assert scheduler_stopped.is_set()
    assert coordinator.closed == 1
    assert background_closed == [True]
    assert providers_closed == [True]
    assert client.closed == 1
