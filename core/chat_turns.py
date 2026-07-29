"""持久聊天 turn 与可重放事件流。

每个 turn 使用独立目录保存原子元数据和追加式事件日志。事件先落盘，再更新
元数据；进程异常退出后可从日志识别终态，未终结 turn 会标记为
``server_restarted``，不会静默恢复生成。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
import shutil
from typing import Awaitable, Callable
from uuid import UUID, uuid4

from core import active_turns
from core.async_utils import await_critical, run_sync_critical
from core.config import (
    DATA_DIR,
    TURN_EVENT_LOG_MAX_BYTES,
    TURN_EVENT_MAX_BYTES,
    TURN_RETENTION_COUNT,
    TURN_RETENTION_DAYS,
    TURN_TERMINAL_EVENT_RESERVE_BYTES,
)
from core.library_lock import library_lock
from core.session_store import MutationResult, atomic_write


ACTIVE_STATUSES = frozenset({"pending", "streaming"})
TERMINAL_STATUSES = frozenset({"completed", "cancelled", "failed"})
logger = logging.getLogger(__name__)


class TurnNotFound(FileNotFoundError):
    pass


class TurnEventLogLimitExceeded(RuntimeError):
    """单个 turn 的追加事件将超过持久化安全边界。"""


def _now() -> str:
    return datetime.now().astimezone().isoformat()


async def _await_critical(awaitable):
    """等待不可取消的异步临界区；外层取消不留下半提交。"""
    return await await_critical(awaitable, propagate_cancellation=False)


async def _run_sync_critical(callback, *args, **kwargs):
    """同步磁盘动作在线程中收口后再传播取消。"""
    return await run_sync_critical(callback, *args, **kwargs)


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
        key: deepcopy(value) for key, value in turn.items() if key not in {"events"}
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

    @staticmethod
    def _payload_size(payload: dict) -> int:
        return len(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))

    def create(self, payload: dict) -> dict:
        turn_id = _validate_turn_id(payload["turn_id"])
        turn_kind = payload.get("turn_kind", "chat")
        if turn_kind not in {"chat", "regenerate"}:
            raise ValueError("无效 turn_kind")
        turn_dir = self._turn_dir(turn_id)
        with library_lock.shared_write():
            turn_dir.mkdir(parents=True, exist_ok=False)
            meta = deepcopy(payload)
            meta["turn_kind"] = turn_kind
            meta["last_event_id"] = 0
            meta["event_count"] = 0
            meta["event_log_bytes"] = 0
            self._write_json(turn_dir / "meta.json", meta)
            (turn_dir / "events").mkdir()
        return meta

    def abort_unaccepted(self, turn_id: str) -> None:
        turn_dir = self._turn_dir(turn_id)
        with library_lock.shared_write():
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
            if size > TURN_EVENT_MAX_BYTES or total_size > TURN_EVENT_LOG_MAX_BYTES:
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
                if size > TURN_EVENT_MAX_BYTES or total_size > TURN_EVENT_LOG_MAX_BYTES:
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
        # ROLE-1 之前的持久 turn 没有类型字段，按普通聊天兼容。
        meta.setdefault("turn_kind", "chat")
        if meta["turn_kind"] not in {"chat", "regenerate"}:
            raise ValueError("turn 元数据类型无效")
        # Provider 抽象引入前的持久 turn 均由 Ollama 执行。
        meta.setdefault("provider", "ollama")
        events = self._read_events(turn_id)
        meta["last_event_id"] = events[-1]["id"] if events else 0
        meta["event_count"] = len(events)
        meta["event_log_bytes"] = sum(self._payload_size(event) for event in events)
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
        payload = {
            key: deepcopy(value) for key, value in turn.items() if key != "events"
        }
        with library_lock.shared_write():
            self._write_json(self._meta_path(turn["turn_id"]), payload)
        return payload

    def append_event(self, turn: dict, event: dict) -> tuple[dict, dict]:
        if turn.get("status") in TERMINAL_STATUSES:
            raise RuntimeError("终态 turn 不能追加事件")
        stored = deepcopy(event)
        stored["id"] = int(turn.get("last_event_id", 0)) + 1
        stored.setdefault("created_at", _now())
        event_bytes = self._payload_size(stored)
        if event_bytes > TURN_EVENT_MAX_BYTES:
            raise TurnEventLogLimitExceeded("turn 单事件超过安全上限")
        current_bytes = turn.get("event_log_bytes", 0)
        if (
            isinstance(current_bytes, bool)
            or not isinstance(current_bytes, int)
            or current_bytes < 0
        ):
            raise ValueError("turn 事件日志字节计数损坏")
        total_bytes = current_bytes + event_bytes
        is_terminal = stored.get("type") == "terminal"
        write_limit = (
            TURN_EVENT_LOG_MAX_BYTES
            if is_terminal
            else (TURN_EVENT_LOG_MAX_BYTES - TURN_TERMINAL_EVENT_RESERVE_BYTES)
        )
        if total_bytes > write_limit:
            raise TurnEventLogLimitExceeded("turn 事件日志超过安全上限")
        updated = deepcopy(turn)
        updated.pop("events", None)
        updated["last_event_id"] = stored["id"]
        updated["event_count"] = int(updated.get("event_count", 0)) + 1
        updated["event_log_bytes"] = total_bytes
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

        with library_lock.shared_write():
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

    @staticmethod
    def _retention_timestamp(meta: dict, path: Path) -> datetime:
        for key in ("completed_at", "updated_at", "created_at"):
            raw = meta.get(key)
            if not isinstance(raw, str) or not raw.strip():
                continue
            try:
                value = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if value.tzinfo is None:
                value = value.astimezone()
            return value
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()

    def prepare_recovery(
        self,
        *,
        retention_days: int = TURN_RETENTION_DAYS,
        retention_count: int = TURN_RETENTION_COUNT,
    ) -> tuple[list[str], int]:
        """返回需完整恢复的 turn，并清理越界的已终结日志。

        启动扫描只读取每个 turn 的 meta.json。只有活动态、未知态或元数据
        损坏的目录才会在恢复阶段读取完整事件流，避免历史完成记录导致启动
        时间随事件总量线性膨胀。
        """

        if retention_days < 1 or retention_count < 1:
            raise ValueError("turn 保留策略必须为正整数")
        with library_lock.shared_write():
            if not self.root.is_dir():
                return [], 0
            recovery_ids: list[str] = []
            terminal: list[tuple[datetime, str, Path]] = []
            for path in self.root.iterdir():
                if not path.is_dir():
                    continue
                try:
                    turn_id = _validate_turn_id(path.name)
                except ValueError:
                    continue
                meta_path = path / "meta.json"
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    recovery_ids.append(turn_id)
                    continue
                if not isinstance(meta, dict) or meta.get("turn_id") != turn_id:
                    recovery_ids.append(turn_id)
                    continue
                if meta.get("status") in TERMINAL_STATUSES:
                    terminal.append(
                        (self._retention_timestamp(meta, path), turn_id, path)
                    )
                else:
                    recovery_ids.append(turn_id)

            terminal.sort(key=lambda item: (item[0], item[1]), reverse=True)
            cutoff = datetime.now().astimezone() - timedelta(days=retention_days)
            removed = 0
            for index, (timestamp, _turn_id, path) in enumerate(terminal):
                if index < retention_count and timestamp >= cutoff:
                    continue
                shutil.rmtree(path)
                removed += 1
            return sorted(recovery_ids), removed


class TurnRuntime:
    def __init__(self, coordinator: "TurnCoordinator", turn_id: str):
        self.coordinator = coordinator
        self.turn_id = turn_id
        self.session_committed_revision: int | None = None

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

    async def mark_session_committed(self, session_revision: int) -> dict:
        """记录 Session 已完成提交；此后该 turn 的完成态不可降级。"""

        if (
            isinstance(session_revision, bool)
            or not isinstance(session_revision, int)
            or session_revision < 0
        ):
            raise ValueError("session_revision 必须是非负整数")
        # 先写内存标记；即使 turn 元数据磁盘随后失败，coordinator 仍能依据
        # Session 事实收口为 completed。
        self.session_committed_revision = session_revision
        return await self.coordinator._mark_session_committed(
            self.turn_id,
            session_revision,
        )


TurnWorker = Callable[[TurnRuntime], Awaitable[None]]
TurnAcceptor = Callable[[str, str], Awaitable[MutationResult[str]]]


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
            "turn_kind": turn.get("turn_kind", "chat"),
            "status": "failed",
            "error": deepcopy(error),
            "timestamps": {
                "created_at": turn.get("created_at") or completed_at,
                "completed_at": completed_at,
            },
        }

    @staticmethod
    async def _completed_session_revision(turn: dict) -> int | None:
        """从 Session 判断该 turn 是否已经完成提交，不执行任何写入。"""

        from core.session_manager import aload_session

        session = await aload_session(turn["project"], turn["save"])
        matched = [
            message
            for message in session.get("message_history", [])
            if message.get("turn_id") == turn["turn_id"]
        ]
        has_completed_user = any(
            message.get("role") == "user" and message.get("status") == "completed"
            for message in matched
        )
        has_completed_assistant = any(
            message.get("role") == "assistant" and message.get("status") == "completed"
            for message in matched
        )
        if not (has_completed_user and has_completed_assistant):
            return None
        revision = session.get("revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            return None
        return revision

    async def _complete_authoritative_turn(
        self,
        turn_id: str,
        session_revision: int,
    ) -> dict:
        """尽力写 terminal 事件；日志故障时至少持久化 completed 元数据。"""

        # 事件文件可能已经落盘、仅 meta 写回抛错；先从磁盘重建 last_event_id，
        # 防止用旧内存游标覆盖同序号的 parsed/terminal 事件。
        async with self._lock:
            turn = await _run_sync_critical(self.store.load, turn_id)
            self._records[turn_id] = deepcopy(turn)
        if turn.get("status") == "completed":
            active_turns.unregister(turn["project"], turn["save"], turn_id)
            await self._notify(turn_id)
            return turn

        try:
            return await self._terminal(
                turn_id,
                "completed",
                error=None,
                turn_updates={
                    "session_revision": session_revision,
                    "session_committed_revision": session_revision,
                },
            )
        except Exception:
            logger.exception(
                "turn %s completed terminal 事件写入失败，按 Session 权威态收口",
                turn_id,
            )

        async with self._lock:
            # 不信任异常前的内存副本；append_event 可能已写成 terminal，只在
            # meta 更新阶段抛错，load 会从事件日志重建真实终态。
            turn = await _run_sync_critical(self.store.load, turn_id)
            if turn.get("status") != "completed":
                completed_at = _now()
                turn.update(
                    {
                        "status": "completed",
                        "error": None,
                        "completed_at": completed_at,
                        "updated_at": completed_at,
                        "session_revision": session_revision,
                        "session_committed_revision": session_revision,
                    }
                )
                turn = await _run_sync_critical(self.store.update, turn)
            self._records[turn_id] = deepcopy(turn)
        active_turns.unregister(turn["project"], turn["save"], turn_id)
        await self._notify(turn_id)
        return turn

    async def _recover_session(self, turn: dict, error: dict) -> int | None:
        from core.message_commands import restore_regeneration_original
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
                    had_regeneration_snapshot = isinstance(
                        user_message.get("regeneration_source_snapshot"),
                        dict,
                    )
                    if turn.get("turn_kind") == "regenerate":
                        restore_regeneration_original(current, user_message)
                    if had_regeneration_snapshot:
                        return
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
            turn_ids, pruned = await _run_sync_critical(self.store.prepare_recovery)
            if pruned:
                logger.info("已清理 %s 个过期终态 turn 日志", pruned)
            for turn_id in turn_ids:
                try:
                    turn = await _run_sync_critical(self.store.load, turn_id)
                    if turn.get("status") not in ACTIVE_STATUSES:
                        continue
                    completed_revision = await self._completed_session_revision(turn)
                    if completed_revision is not None:
                        await self._complete_authoritative_turn(
                            turn_id,
                            completed_revision,
                        )
                        recovered += 1
                        continue
                    error = {
                        "code": "server_restarted",
                        "message": "服务进程在 turn 完成前重启",
                    }
                    # 同进程的新事件循环也会触发恢复（测试、嵌入式运行时重建）。
                    # 若旧协调器留下的是同一个 turn 的活动登记，恢复写入仍属于该
                    # turn；带上写入上下文即可放行自身，同时继续拒绝其他活动 turn。
                    with active_turns.turn_write_context(turn_id):
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
                    logger.exception(
                        "恢复中断 turn %s 失败，继续检查其他 turn", turn_id
                    )
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
        accept_command: TurnAcceptor | None = None,
        prompt_diagnostics: dict | None = None,
        turn_kind: str = "chat",
        provider: str = "ollama",
    ) -> dict:
        if turn_kind not in {"chat", "regenerate"}:
            raise ValueError("无效 turn_kind")
        turn_id = str(uuid4())
        created_at = _now()
        record = {
            "schema_version": 2,
            "turn_id": turn_id,
            "project": project,
            "save": save,
            "expected_revision": expected_revision,
            "user_input": user_input,
            "turn_kind": turn_kind,
            "provider": provider,
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
        if prompt_diagnostics is not None:
            record["prompt_diagnostics"] = deepcopy(prompt_diagnostics)
        await self.ensure_recovered()
        async with self._lock:
            try:
                stored = await _run_sync_critical(self.store.create, record)
                if accept_command is None:
                    from core.session_manager import get_session_store

                    acceptance_awaitable = get_session_store().accept_chat_turn(
                        project,
                        save,
                        expected_revision,
                        turn_id=turn_id,
                        user_input=user_input,
                        created_at=created_at,
                        initial_session=initial_session,
                    )
                else:
                    acceptance_awaitable = accept_command(turn_id, created_at)
                acceptance = await _await_critical(acceptance_awaitable)
            except BaseException:
                active_turns.unregister(project, save, turn_id)
                with suppress(Exception):
                    await _run_sync_critical(self.store.abort_unaccepted, turn_id)
                raise
            stored["accepted_revision"] = acceptance.session["revision"]
            stored["user_message_id"] = acceptance.value
            try:
                stored = await _run_sync_critical(self.store.update, stored)
            except (Exception, asyncio.CancelledError):
                # Session 已接受后不能再向客户端伪装成“未接受”。首个事件会把
                # 内存中的完整 turn 元数据重新写回；即使进程立即退出，pending
                # user 的 turn_id 也足以让启动恢复流程确定性收口。
                logger.exception("turn %s 接受元数据回写失败，继续启动 worker", turn_id)
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
            committed_revision = runtime.session_committed_revision
            if committed_revision is None:
                committed_revision = await self._completed_session_revision(turn)
            if committed_revision is not None:
                await asyncio.shield(
                    self._complete_authoritative_turn(
                        turn_id,
                        committed_revision,
                    )
                )
            else:
                await asyncio.shield(
                    runtime.terminal(
                        "cancelled",
                        error={"code": "cancelled", "message": "turn 已取消"},
                    )
                )
        except Exception:
            committed_revision = runtime.session_committed_revision
            if committed_revision is None:
                committed_revision = await self._completed_session_revision(turn)
            if committed_revision is not None:
                logger.exception(
                    "turn %s Session 已完成，忽略完成后事件异常",
                    turn_id,
                )
                await self._complete_authoritative_turn(turn_id, committed_revision)
            else:
                logger.exception("turn %s 工作器发生未分类异常", turn_id)
                await runtime.terminal(
                    "failed",
                    error={
                        "code": "internal_error",
                        "message": "turn 执行失败，请重试",
                    },
                )
        finally:
            final = await _run_sync_critical(self.store.load, turn_id)
            if final.get("status") in ACTIVE_STATUSES:
                committed_revision = runtime.session_committed_revision
                if committed_revision is None:
                    committed_revision = await self._completed_session_revision(final)
                if committed_revision is not None:
                    await self._complete_authoritative_turn(
                        turn_id,
                        committed_revision,
                    )
                else:
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

    async def _mark_session_committed(
        self,
        turn_id: str,
        session_revision: int,
    ) -> dict:
        async with self._lock:
            turn = deepcopy(self._records.get(turn_id))
            if turn is None:
                turn = await _run_sync_critical(self.store.load, turn_id)
            if turn.get("status") in {"cancelled", "failed"}:
                raise RuntimeError("非完成终态 turn 不能登记 Session 完成提交")
            turn["session_committed_revision"] = session_revision
            turn["session_revision"] = session_revision
            turn = await _run_sync_critical(self.store.update, turn)
            self._records[turn_id] = deepcopy(turn)
        return turn

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
        turn_ids = [turn_id for turn_id, task in self._tasks.items() if not task.done()]
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
