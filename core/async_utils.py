"""异步临界区工具。

``asyncio.to_thread`` 已开始的同步工作无法被任务取消终止。这里把外层取消
延迟到临界区完整收口后再传播，避免异步锁、维护门禁或清理责任提前释放。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar


T = TypeVar("T")


async def await_critical(
    awaitable: Awaitable[T],
    *,
    propagate_cancellation: bool = True,
) -> T:
    """等待临界任务真正结束，并抵抗外层任务的重复取消。

    ``asyncio.shield`` 只保护内层任务不被一次取消连带终止；外层仍会立即收到
    ``CancelledError``。因此必须循环 shield，直到内层任务进入终态，再恢复外层
    的取消语义。内层异常优先传播，确保后台失败不会变成无人读取的异常。
    """

    task = asyncio.ensure_future(awaitable)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc

    result = task.result()
    if cancellation is not None and propagate_cancellation:
        raise cancellation
    return result


async def run_sync_critical(
    callback: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """在线程中执行同步临界区，完成后再传播外层取消。"""

    return await await_critical(asyncio.to_thread(callback, *args, **kwargs))


async def acquire_critical(
    awaitable: Awaitable[T],
    cleanup: Callable[[T], Awaitable[object]],
) -> T:
    """取得异步资源；若取得期间被取消，先回收资源再传播取消。"""

    task = asyncio.ensure_future(awaitable)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc

    resource = task.result()
    if cancellation is not None:
        await await_critical(
            cleanup(resource),
            propagate_cancellation=False,
        )
        raise cancellation
    return resource
