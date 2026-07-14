import threading
import time

import pytest

from core.library_lock import LibraryRWLock


def test_shared_lock_allows_parallel_readers():
    lock = LibraryRWLock()
    both_inside = threading.Event()
    release = threading.Event()
    entered = 0
    guard = threading.Lock()

    def reader():
        nonlocal entered
        with lock.shared():
            with guard:
                entered += 1
                if entered == 2:
                    both_inside.set()
            release.wait(1)

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert both_inside.wait(1)
    release.set()
    for thread in threads:
        thread.join(1)
        assert not thread.is_alive()


def test_waiting_writer_blocks_new_reader_until_exclusive_finishes():
    lock = LibraryRWLock()
    first_reader_release = threading.Event()
    writer_waiting = threading.Event()
    writer_inside = threading.Event()
    writer_release = threading.Event()
    second_reader_inside = threading.Event()

    def first_reader():
        with lock.shared():
            first_reader_release.wait(1)

    def writer():
        writer_waiting.set()
        with lock.exclusive():
            writer_inside.set()
            writer_release.wait(1)

    def second_reader():
        with lock.shared():
            second_reader_inside.set()

    first = threading.Thread(target=first_reader)
    exclusive = threading.Thread(target=writer)
    second = threading.Thread(target=second_reader)
    first.start()
    exclusive.start()
    assert writer_waiting.wait(1)
    time.sleep(0.02)
    second.start()
    assert not second_reader_inside.wait(0.05)
    first_reader_release.set()
    assert writer_inside.wait(1)
    assert not second_reader_inside.is_set()
    writer_release.set()
    assert second_reader_inside.wait(1)
    for thread in (first, exclusive, second):
        thread.join(1)
        assert not thread.is_alive()


def test_reentrant_modes_and_upgrade_rejection():
    lock = LibraryRWLock()
    with lock.shared():
        with lock.shared():
            with pytest.raises(RuntimeError, match="升级"):
                lock.acquire_exclusive()
    with lock.exclusive():
        with lock.exclusive():
            with lock.shared():
                pass

