"""持久聊天 turn 与可重放事件流。

每个 turn 使用独立目录保存原子元数据和追加式事件日志。事件先落盘，再更新
元数据；进程异常退出后可从日志识别终态，未终结 turn 会标记为
``server_restarted``，不会静默恢复生成。
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
import json
import logging
from pathlib import Path
import shutil
from typing import Awaitable, Callable
from uuid import UUID, uuid4

from core import active_turns
from core.config import DATA_DIR
from core.library_lock import library_lock
from core.session_store import atomic_write


ACTIVE_STATUSES = frozenset({"pending", "streaming"})
TERMINAL_STATUSES = frozenset({"completed", "cancelled", "failed"})
MAX_EVENT_LOG_BYTES = 128 * 1024 * 1024
MAX_EVENT_BYTES = 16 * 1024 * 1024
logger = logging.getLogger(__name__)


class TurnNotFound(FileNotFoundError):
    pass


def _now() -> str:
    return datetime.now().astimezone().isoformat()


async def _await_critical(awaitable):
    """等待不可取消的异步临界区；外层取消不留下半提交。"""
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        return await task


async def _run_sync_critical(callback, *args, **kwargs):
    """同步磁盘动作在线程中收口后再传播取消。"""
    task = asyncio.create_task(asyncio.to_thread(callback, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def _validate_turn_id(turn_id: str) -> str:
    try:
        parsed = UUID(str(turn_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("无效 turn_id") from exc
    canonical = str(parsed)
    if canonical != str(turn_id).lower():
        raise ValueError("无效 turn_id")
    return canonical


def _public_turn(turn: dict) -> dict:
    result = {
        key: deepcopy(value)
        for key, value in turn.items()
        if key not in {"events"}
    }
    result["events_url"] = f"/api/chat/turns/{turn['turn_id']}/events"
    result["cancel_url"] = f"/api/chat/turns/{turn['turn_id']}/cancel"
    return result


class TurnStore:
    def __init__(self, root: Path | None = None):
        self.root = (root or (DATA_DIR / ".chat-turns")).resolve(strict=False)

    def _turn_dir(self, turn_id: str) -> Path:
        return self.root / _validate_turn_id(turn_id)

    def _meta_path(self, turn_id: str) -> Path:
        return self._turn_dir(turn_id) / "meta.json"

    def _events_dir(self, turn_id: str) -> Path:
        return self._turn_dir(turn_id) / "events"

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))

    def create(self, payload: dict) -> dict:
        turn_id = _validate_turn_id(payload["turn_id"])
        turn_dir = self._turn_dir(turn_id)
        with library_lock.shared():
            turn_dir.mkdir(parents=True, exist_ok=False)
            meta = deepcopy(payload)
            meta["last_event_id"] = 0
            meta["event_count"] = 0
            self._write_json(turn_dir / "meta.json", meta)
            (turn_dir / "events").mkdir()
        return meta

    def abort_unaccepted(self, turn_id: str) -> None:
        turn_dir = self._turn_dir(turn_id)
        with library_lock.shared():
            if turn_dir.is_dir():
                shutil.rmtree(turn_dir)

    def _read_events(self, turn_id: str) -> list[dict]:
        events_dir = self._events_dir(turn_id)
        if not events_dir.is_dir():
            return []
        events: list[dict] = []
        total_size = 0
        paths = sorted(events_dir.glob("*.json"))
        for number, path in enumerate(paths, start=1):
            expected_name = f"{number:012d}.json"
            if path.name != expected_name:
                raise ValueError("turn 事件文件序号不连续")
            size = path.stat().st_size
            total_size += size
            if size > MAX_EVENT_BYTES or total_size > MAX_EVENT_LOG_BYTES:
                raise ValueError("turn 事件日志超过安全上限")
            try:
                event = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"turn 事件 {number} 损坏") from exc
            if not isinstance(event, dict) or event.get("id") != number:
                raise ValueError("turn 事件序号不连续")
            events.append(event)
        return events

    def read_events_after(
        self,
        turn_id: str,
        after: int,
        through: int,
    ) -> list[dict]:
        turn_id = _validate_turn_id(turn_id)
        if after < 0 or through < after:
            raise ValueError("turn 事件范围无效")
        events: list[dict] = []
        total_size = 0
        with library_lock.shared():
            events_dir = self._events_dir(turn_id)
            for number in range(after + 1, through + 1):
                path = events_dir / f"{number:012d}.json"
                if not path.is_file():
                    raise ValueError("turn 事件文件序号不连续")
                size = path.stat().st_size
                total_size += size
                if size > MAX_EVENT_BYTES or total_size > MAX_EVENT_LOG_BYTES:
                    raise ValueError("turn 事件日志超过安全上限")
                try:
                    event = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"turn 事件 {number} 损坏") from exc
                if not isinstance(event, dict) or event.get("id") != number:
                    raise ValueError("turn 事件序号不连续")
                events.append(event)
        return events

    def load(self, turn_id: str, *, with_events: bool = False) -> dict:
        with library_lock.shared():
            return self._load_unlocked(turn_id, with_events=with_events)

    def _load_unlocked(self, turn_id: str, *, with_events: bool = False) -> dict:
        turn_id = _validate_turn_id(turn_id)
        path = self._meta_path(turn_id)
        if not path.is_file():
            raise TurnNotFound(f"turn {turn_id} 不存在")
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError("turn 元数据损坏") from exc
        if not isinstance(meta, dict) or meta.get("turn_id") != turn_id:
            raise ValueError("turn 元数据归属不匹配")
        events = self._read_events(turn_id)
        meta["last_event_id"] = events[-1]["id"] if events else 0
        meta["event_count"] = len(events)
        meta["content"] = "".join(
            str(event.get("content", ""))
            for event in events
            if event.get("type") == "content"
        )
        meta["thinking"] = "".join(
            str(event.get("content", ""))
            for event in events
            if event.get("type") == "thinking"
        )
        if events and events[-1].get("type") == "terminal":
            meta["status"] = events[-1]["status"]
            meta["completed_at"] = events[-1].get("created_at")
            meta["error"] = events[-1].get("error")
        if with_events:
            meta["events"] = events
        return meta

    def update(self, turn: dict) -> dict:
        payload = {key: deepcopy(value) for key, value in turn.items() if key != "events"}
        with library_lock.shared():
            self._write_json(self._meta_path(turn["turn_id"]), payload)
        return payload

    def append_event(self, turn: dict, event: dict) -> tuple[dict, dict]:
        if turn.get("status") in TERMINAL_STATUSES:
            raise RuntimeError("终态 turn 不能追加事件")
        stored = deepcopy(event)
        stored["id"] = int(turn.get("last_event_id", 0)) + 1
        stored.setdefault("created_at", _now())
        updated = deepcopy(turn)
        updated.pop("events", None)
        updated["last_event_id"] = stored["id"]
        updated["event_count"] = int(updated.get("event_count", 0)) + 1
        updated["updated_at"] = stored["created_at"]
        if stored.get("type") == "terminal":
            status = stored.get("status")
            if status not in TERMINAL_STATUSES:
                raise ValueError("无效 turn 终态")
            updated["status"] = status
            updated["completed_at"] = stored["created_at"]
            updated["error"] = deepcopy(stored.get("error"))
        elif updated.get("status") == "pending":
            updated["status"] = "streaming"
            updated["started_at"] = stored["created_at"]

        with library_lock.shared():
            path = self._events_dir(turn["turn_id"]) / f"{stored['id']:012d}.json"
            self._write_json(path, stored)
            self._write_json(self._meta_path(turn["turn_id"]), updated)
        return updated, stored

    def list_turn_ids(self) -> list[str]:
        with library_lock.shared():
            if not self.root.is_dir():
                return []
            result: list[str] = []
            for path in self.root.iterdir():
                if not path.is_dir():
                    continue
                try:
                    result.append(_validate_turn_id(path.name))
                except ValueError:
                    continue
            return sorted(result)


class TurnRuntime:
    def __init__(self, coordinator: "TurnCoordinator", turn_id: str):
        self.coordinator = coordinator
        self.turn_id = turn_id

    async def emit(self, event: dict, **turn_updates) -> dict:
        return await self.coordinator._emit(self.turn_id, event, turn_updates)

    async def terminal(
        self,
        status: str,
        *,
        error: dict | None = None,
        **turn_updates,
    ) -> dict:
        return await self.coordinator._terminal(
            self.turn_id,
            status,
            error=error,
            turn_updates=turn_updates,
        )


TurnWorker = Callable[[TurnRuntime], Awaitable[None]]


class TurnCoordinator:
    def __init__(self, store: TurnStore | None = None):
        self.store = store or TurnStore()
        self._lock = asyncio.Lock()
        self._recovery_lock = asyncio.Lock()
        self._recovered = False
        self._conditions: dict[str, asyncio.Condition] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._records: dict[str, dict] = {}

    def _condition(self, turn_id: str) -> asyncio.Condition:
        condition = self._conditions.get(turn_id)
        if condition is None:
            condition = asyncio.Condition()
            self._conditions[turn_id] = condition
        return condition

    @staticmethod
    def _final_message_metadata(turn: dict, error: dict) -> dict:
        completed_at = _now()
        return {
            "turn_id": turn["turn_id"],
            "status": "failed",
            "error": deepcopy(error),
            "timestamps": {
                "created_at": turn.get("created_at") or completed_at,
                "completed_at": completed_at,
            },
            "pinned": False,
        }

    async def _recover_session(self, turn: dict, error: dict) -> int | None:
        from core.session_manager import aload_session, append_history, mutate_session
        from core.session_store import RevisionConflict

        project = turn["project"]
        save = turn["save"]
        for _attempt in range(3):
            session = await aload_session(project, save)
            history = session.get("message_history", [])
            has_user = any(
                message.get("turn_id") == turn["turn_id"]
                and message.get("role") == "user"
                for message in history
            )
            if not has_user and turn.get("accepted_revision") is None:
                return None

            def reconcile(current: dict, context) -> None:
                del context
                messages = current.setdefault("message_history", [])
                user_message = next(
                    (
                        message
                        for message in messages
                        if message.get("turn_id") == turn["turn_id"]
                        and message.get("role") == "user"
                    ),
                    None,
                )
                metadata = self._final_message_metadata(turn, error)
                if user_message is None:
                    user_message = append_history(
                        current,
                        "user",
                        turn.get("user_input", ""),
                        metadata={**metadata, "in_prompt": True},
                    )
                else:
                    user_message.update({**metadata, "in_prompt": True})

                content = turn.get("content", "")
                thinking = turn.get("thinking", "")
                assistant = next(
                    (
                        message
                        for message in messages
                        if message.get("turn_id") == turn["turn_id"]
                        and message.get("role") == "assistant"
                    ),
                    None,
                )
                if (content or thinking) and assistant is None:
                    append_history(
                        current,
                        "assistant",
                        content,
                        thinking,
                        metadata={**metadata, "in_prompt": False},
                    )
                elif assistant is not None:
                    assistant.update({**metadata, "in_prompt": False})

            try:
                mutation = await mutate_session(
                    project,
                    save,
                    session.get("revision", 0),
                    reconcile,
                )
                return mutation.session["revision"]
            except RevisionConflict:
                continue
        raise RuntimeError("重启 turn 的存档收口连续发生 revision 冲突")

    async def ensure_recovered(self) -> int:
        if self._recovered:
            return 0
        async with self._recovery_lock:
            if self._recovered:
                return 0
            recovered = 0
            turn_ids = await _run_sync_critical(self.store.list_turn_ids)
            for turn_id in turn_ids:
                try:
                    turn = await _run_sync_critical(self.store.load, turn_id)
                    if turn.get("status") not in ACTIVE_STATUSES:
                        continue
                    error = {
                        "code": "server_restarted",
                        "message": "服务进程在 turn 完成前重启",
                    }
                    revision = await _await_critical(
                        self._recover_session(turn, error)
                    )
                    updates = {}
                    if revision is not None:
                        updates["session_revision"] = revision
                    await self._terminal(
                        turn_id,
                        "failed",
                        error=error,
                        turn_updates=updates,
                    )
                    recovered += 1
                except Exception:
                    logger.exception("恢复中断 turn %s 失败，继续检查其他 turn", turn_id)
            self._recovered = True
            return recovered

    async def reconcile_after_restore(self) -> int:
        async with self._recovery_lock:
            self._recovered = False
        return await self.ensure_recovered()

    async def start(
        self,
        *,
        project: str,
        save: str,
        expected_revision: int,
        user_input: str,
        model: str,
        parameters: dict,
        initial_session: dict,
        accepted_callback: Callable[[dict], None],
        worker: TurnWorker,
    ) -> dict:
        turn_id = str(uuid4())
        created_at = _now()
        record = {
            "schema_version": 1,
            "turn_id": turn_id,
            "project": project,
            "save": save,
            "expected_revision": expected_revision,
            "user_input": user_input,
            "model": model,
            "parameters": deepcopy(parameters),
            "status": "pending",
            "created_at": created_at,
            "updated_at": created_at,
            "started_at": None,
            "completed_at": None,
            "cancel_requested_at": None,
            "content": "",
            "thinking": "",
            "error": None,
        }
        await self.ensure_recovered()
        async with self._lock:
            try:
                stored = await _run_sync_critical(self.store.create, record)
                from core.session_manager import get_session_store

                acceptance = await _await_critical(get_session_store().accept_chat_turn(
                    project,
                    save,
                    expected_revision,
                    turn_id=turn_id,
                    user_input=user_input,
                    created_at=created_at,
                    initial_session=initial_session,
                ))
            except BaseException:
                active_turns.unregister(project, save, turn_id)
                with suppress(Exception):
                    await _run_sync_critical(self.store.abort_unaccepted, turn_id)
                raise
            stored["accepted_revision"] = acceptance.session["revision"]
            stored["user_message_id"] = acceptance.value
            stored = await _run_sync_critical(self.store.update, stored)
            accepted_callback(deepcopy(acceptance.session))
            self._records[turn_id] = deepcopy(stored)
            task = asyncio.create_task(
                self._run_worker(stored, worker),
                name=f"chat-turn-{turn_id}",
            )
            self._tasks[turn_id] = task
        return _public_turn(stored)

    async def _run_worker(self, turn: dict, worker: TurnWorker) -> None:
        turn_id = turn["turn_id"]
        runtime = TurnRuntime(self, turn_id)
        try:
            with active_turns.turn_write_context(turn_id):
                await worker(runtime)
        except asyncio.CancelledError:
            await asyncio.shield(runtime.terminal(
                "cancelled",
                error={"code": "cancelled", "message": "turn 已取消"},
            ))
        except Exception as exc:
            await runtime.terminal(
                "failed",
                error={"code": "internal_error", "message": str(exc)},
            )
        finally:
            final = await _run_sync_critical(self.store.load, turn_id)
            if final.get("status") in ACTIVE_STATUSES:
                await runtime.terminal(
                    "failed",
                    error={
                        "code": "missing_terminal_event",
                        "message": "turn 工作器未写入终态",
                    },
                )
                final = await _run_sync_critical(self.store.load, turn_id)
            active_turns.unregister(final["project"], final["save"], turn_id)
            self._tasks.pop(turn_id, None)
            self._records.pop(turn_id, None)
            self._conditions.pop(turn_id, None)

    async def _notify(self, turn_id: str) -> None:
        condition = self._condition(turn_id)
        async with condition:
            condition.notify_all()

    async def _emit(self, turn_id: str, event: dict, turn_updates: dict) -> dict:
        async with self._lock:
            turn = deepcopy(self._records.get(turn_id))
            if turn is None:
                turn = await _run_sync_critical(self.store.load, turn_id)
            if turn.get("status") in TERMINAL_STATUSES:
                return event
            turn.update(deepcopy(turn_updates))
            turn, stored = await _run_sync_critical(
                self.store.append_event,
                turn,
                event,
            )
            self._records[turn_id] = deepcopy(turn)
        await self._notify(turn_id)
        return stored

    async def _terminal(
        self,
        turn_id: str,
        status: str,
        *,
        error: dict | None,
        turn_updates: dict,
    ) -> dict:
        if status not in TERMINAL_STATUSES:
            raise ValueError("无效 turn 终态")
        async with self._lock:
            turn = deepcopy(self._records.get(turn_id))
            if turn is None:
                turn = await _run_sync_critical(self.store.load, turn_id)
            if turn.get("status") in TERMINAL_STATUSES:
                return turn
            turn.update(deepcopy(turn_updates))
            turn, _stored = await _run_sync_critical(
                self.store.append_event,
                turn,
                {"type": "terminal", "status": status, "error": deepcopy(error)},
            )
            self._records[turn_id] = deepcopy(turn)
        active_turns.unregister(turn["project"], turn["save"], turn_id)
        await self._notify(turn_id)
        return turn

    async def get(self, turn_id: str) -> dict:
        await self.ensure_recovered()
        async with self._lock:
            turn = deepcopy(self._records.get(turn_id))
            if turn is None:
                turn = await _run_sync_critical(self.store.load, turn_id)
        return _public_turn(turn)

    async def events(self, turn_id: str, after: int = 0):
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError("after 必须是非负整数")
        await self.ensure_recovered()
        condition = self._condition(turn_id)
        while True:
            timed_out = False
            async with condition:
                async with self._lock:
                    turn = deepcopy(self._records.get(turn_id))
                    if turn is None:
                        turn = await _run_sync_critical(self.store.load, turn_id)
                    through = int(turn.get("last_event_id", 0))
                    pending = await _run_sync_critical(
                        self.store.read_events_after,
                        turn_id,
                        after,
                        through,
                    )
                terminal = turn.get("status") in TERMINAL_STATUSES
                if not pending and not terminal:
                    try:
                        await asyncio.wait_for(condition.wait(), timeout=15.0)
                    except TimeoutError:
                        timed_out = True
            if not pending and not terminal:
                if timed_out:
                    yield {"type": "keepalive", "id": after}
                continue
            for event in pending:
                after = event["id"]
                yield event
            if terminal:
                return

    async def cancel(self, turn_id: str) -> dict:
        await self.ensure_recovered()
        async with self._lock:
            turn = deepcopy(self._records.get(turn_id))
            if turn is None:
                turn = await _run_sync_critical(self.store.load, turn_id)
            if turn.get("status") in TERMINAL_STATUSES:
                return _public_turn(turn)
            turn["cancel_requested_at"] = _now()
            turn = await _run_sync_critical(self.store.update, turn)
            self._records[turn_id] = deepcopy(turn)
            task = self._tasks.get(turn_id)
            if task is not None and not task.done():
                task.cancel()
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task
        final = await _run_sync_critical(self.store.load, turn_id)
        if final.get("status") in ACTIVE_STATUSES:
            final = await self._terminal(
                turn_id,
                "cancelled",
                error={"code": "cancelled", "message": "turn 已取消"},
                turn_updates={},
            )
        if task is not None and task.done():
            self._tasks.pop(turn_id, None)
        if final.get("status") in TERMINAL_STATUSES:
            self._records.pop(turn_id, None)
        return _public_turn(final)

    async def shutdown(self) -> None:
        turn_ids = [
            turn_id
            for turn_id, task in self._tasks.items()
            if not task.done()
        ]
        if turn_ids:
            await asyncio.gather(
                *(self.cancel(turn_id) for turn_id in turn_ids),
                return_exceptions=True,
            )


_coordinators: dict[str, TurnCoordinator] = {}


def get_turn_coordinator() -> TurnCoordinator:
    root = (DATA_DIR / ".chat-turns").resolve(strict=False)
    try:
        loop_key = str(id(asyncio.get_running_loop()))
    except RuntimeError:
        loop_key = "no-loop"
    key = f"{root}|{loop_key}"
    coordinator = _coordinators.get(key)
    if coordinator is None:
        coordinator = TurnCoordinator(TurnStore(root))
        _coordinators[key] = coordinator
    return coordinator


def clear_turn_coordinators_for_testing() -> None:
    _coordinators.clear()
    active_turns.clear_for_testing()
