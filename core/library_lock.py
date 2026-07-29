"""进程内整库读写协调锁。

普通局部事务持共享锁；整库备份、恢复、迁移和项目删除持独占锁。
锁采用写者优先，避免持续聊天写入让灾备操作永久饥饿。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator


class LibraryRWLock:
    """支持同线程重入的同步读写锁；不支持从共享锁升级为独占锁。"""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._readers = 0
        self._reader_depth: dict[int, int] = {}
        self._writer: int | None = None
        self._writer_depth = 0
        self._waiting_writers = 0

    def acquire_shared(self) -> None:
        thread_id = threading.get_ident()
        with self._condition:
            reentrant_reader = self._reader_depth.get(thread_id, 0) > 0
            writer_owned = self._writer == thread_id
            while (self._writer is not None and not writer_owned) or (
                self._waiting_writers > 0 and not reentrant_reader and not writer_owned
            ):
                self._condition.wait()
                reentrant_reader = self._reader_depth.get(thread_id, 0) > 0
                writer_owned = self._writer == thread_id
            self._readers += 1
            self._reader_depth[thread_id] = self._reader_depth.get(thread_id, 0) + 1

    def release_shared(self) -> None:
        thread_id = threading.get_ident()
        with self._condition:
            depth = self._reader_depth.get(thread_id, 0)
            if depth < 1:
                raise RuntimeError("当前线程未持有整库共享锁")
            if depth == 1:
                del self._reader_depth[thread_id]
            else:
                self._reader_depth[thread_id] = depth - 1
            self._readers -= 1
            if self._readers == 0:
                self._condition.notify_all()

    def acquire_exclusive(self) -> None:
        thread_id = threading.get_ident()
        with self._condition:
            if self._writer == thread_id:
                self._writer_depth += 1
                return
            if self._reader_depth.get(thread_id, 0):
                raise RuntimeError("禁止把整库共享锁升级为独占锁")
            self._waiting_writers += 1
            try:
                while self._writer is not None or self._readers:
                    self._condition.wait()
                self._writer = thread_id
                self._writer_depth = 1
            finally:
                self._waiting_writers -= 1

    def release_exclusive(self) -> None:
        thread_id = threading.get_ident()
        with self._condition:
            if self._writer != thread_id or self._writer_depth < 1:
                raise RuntimeError("当前线程未持有整库独占锁")
            self._writer_depth -= 1
            if self._writer_depth == 0:
                self._writer = None
                self._condition.notify_all()

    @contextmanager
    def shared(self) -> Iterator[None]:
        self.acquire_shared()
        try:
            yield
        finally:
            self.release_shared()

    @contextmanager
    def shared_write(self) -> Iterator[None]:
        """共享写锁；锁内复核维护代次，阻断跨恢复边界的旧写入。"""

        self.acquire_shared()
        try:
            from core.active_turns import assert_global_write_allowed

            assert_global_write_allowed()
            yield
        finally:
            self.release_shared()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        self.acquire_exclusive()
        try:
            from core.active_turns import assert_global_write_allowed

            # 所有整库独占调用均会创建、恢复、迁移或删除持久化数据。
            # 在真正取得锁后复核维护代次，阻断恢复前已排队的旧请求。
            assert_global_write_allowed()
            yield
        finally:
            self.release_exclusive()


library_lock = LibraryRWLock()
