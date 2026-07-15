"""进程内活动聊天 turn 写入门禁。

服务固定单 worker 运行。turn 生成期间，只有该 turn 自己可以提交对应存档，
其余写命令统一失败，避免生成完成时用旧 revision 覆盖用户操作。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import RLock
from typing import Iterator
from uuid import uuid4


@dataclass
class ActiveTurnConflict(RuntimeError):
    project: str
    save: str
    turn_id: str

    def as_detail(self) -> dict:
        return {
            "code": "active_turn_conflict",
            "message": "当前存档正在生成回复，请先取消或等待生成结束",
            "project": self.project,
            "save": self.save,
            "turn_id": self.turn_id,
        }


@dataclass
class TurnMaintenanceConflict(RuntimeError):
    operation: str

    def as_detail(self) -> dict:
        return {
            "code": "turn_maintenance",
            "message": "整库维护期间不能启动聊天或执行其他写入",
            "operation": self.operation,
        }


_lock = RLock()
_active: dict[tuple[str, str], str] = {}
_maintenance: tuple[str, str] | None = None
_current_turn_id: ContextVar[str | None] = ContextVar(
    "local_tavern_current_turn_id",
    default=None,
)


def register(project: str, save: str, turn_id: str) -> None:
    key = (project, save)
    with _lock:
        if _maintenance is not None:
            raise TurnMaintenanceConflict(_maintenance[1])
        existing = _active.get(key)
        if existing and existing != turn_id:
            raise ActiveTurnConflict(project, save, existing)
        _active[key] = turn_id


def unregister(project: str, save: str, turn_id: str) -> None:
    key = (project, save)
    with _lock:
        if _active.get(key) == turn_id:
            _active.pop(key, None)


def active_turn_id(project: str, save: str) -> str | None:
    with _lock:
        return _active.get((project, save))


def active_turns_for_project(project: str) -> tuple[str, ...]:
    with _lock:
        return tuple(
            turn_id
            for (active_project, _save), turn_id in _active.items()
            if active_project == project
        )


def all_active_turns() -> tuple[tuple[str, str, str], ...]:
    with _lock:
        return tuple(
            (project, save, turn_id)
            for (project, save), turn_id in _active.items()
        )


def assert_no_active_turns() -> None:
    active = all_active_turns()
    if active:
        project, save, turn_id = active[0]
        raise ActiveTurnConflict(project, save, turn_id)


def begin_maintenance(operation: str) -> str:
    global _maintenance
    token = str(uuid4())
    with _lock:
        if _maintenance is not None:
            raise TurnMaintenanceConflict(_maintenance[1])
        if _active:
            (project, save), turn_id = next(iter(_active.items()))
            raise ActiveTurnConflict(project, save, turn_id)
        _maintenance = (token, operation)
    return token


def end_maintenance(token: str) -> None:
    global _maintenance
    with _lock:
        if _maintenance is not None and _maintenance[0] == token:
            _maintenance = None


def maintenance_operation() -> str | None:
    with _lock:
        return _maintenance[1] if _maintenance is not None else None


def assert_write_allowed(project: str, save: str) -> None:
    with _lock:
        turn_id = _active.get((project, save))
    if turn_id and _current_turn_id.get() != turn_id:
        raise ActiveTurnConflict(project, save, turn_id)


def assert_project_write_allowed(project: str) -> None:
    current = _current_turn_id.get()
    with _lock:
        conflict = next(
            (
                (save, turn_id)
                for (active_project, save), turn_id in _active.items()
                if active_project == project and turn_id != current
            ),
            None,
        )
    if conflict:
        save, turn_id = conflict
        raise ActiveTurnConflict(project, save, turn_id)


@contextmanager
def turn_write_context(turn_id: str | None) -> Iterator[None]:
    token = _current_turn_id.set(turn_id)
    try:
        yield
    finally:
        _current_turn_id.reset(token)


def clear_for_testing() -> None:
    global _maintenance
    with _lock:
        _active.clear()
        _maintenance = None
