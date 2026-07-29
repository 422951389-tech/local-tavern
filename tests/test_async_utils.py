import asyncio
import threading

import pytest

from core.async_utils import acquire_critical, await_critical, run_sync_critical


@pytest.mark.asyncio
async def test_run_sync_critical_waits_through_repeated_cancellation():
    started = threading.Event()
    release = threading.Event()

    def blocking_write():
        started.set()
        assert release.wait(5)
        return "committed"

    task = asyncio.create_task(run_sync_critical(blocking_write))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_await_critical_can_finish_cleanup_before_propagating_cancel():
    worker_started = asyncio.Event()
    worker_done = asyncio.Event()
    cleanup_done = asyncio.Event()

    async def operation():
        try:
            worker_started.set()
            await worker_done.wait()
            return "restored"
        finally:
            cleanup_done.set()

    task = asyncio.create_task(await_critical(operation()))
    await worker_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert not task.done()
    assert not cleanup_done.is_set()

    worker_done.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleanup_done.is_set()


@pytest.mark.asyncio
async def test_acquire_critical_releases_resource_when_cancelled_during_acquire():
    acquired = asyncio.Event()
    return_resource = asyncio.Event()
    cleaned = asyncio.Event()
    resource = object()

    async def acquire():
        acquired.set()
        await return_resource.wait()
        return resource

    async def cleanup(value):
        assert value is resource
        cleaned.set()

    task = asyncio.create_task(acquire_critical(acquire(), cleanup))
    await acquired.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert not task.done()

    return_resource.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
