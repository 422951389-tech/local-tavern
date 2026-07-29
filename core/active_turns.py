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
_active_api_reads = 0
_maintenance: tuple[str, str] | None = None
_maintenance_generation = 0
_last_maintenance_operation: str | None = None
_project_generations: dict[str, int] = {}
_last_project_operations: dict[str, str] = {}
_current_turn_id: ContextVar[str | None] = ContextVar(
    "local_tavern_current_turn_id",
    default=None,
)
_current_maintenance_token: ContextVar[str | None] = ContextVar(
    "local_tavern_current_maintenance_token",
    default=None,
)
_request_maintenance_generation: ContextVar[int | None] = ContextVar(
    "local_tavern_request_maintenance_generation",
    default=None,
)
_request_project_generations: ContextVar[dict[str, int] | None] = ContextVar(
    "local_tavern_request_project_generations",
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
            (project, save, turn_id) for (project, save), turn_id in _active.items()
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
        if _active_api_reads:
            raise TurnMaintenanceConflict("active_api_reads")
        if _active:
            (project, save), turn_id = next(iter(_active.items()))
            raise ActiveTurnConflict(project, save, turn_id)
        _maintenance = (token, operation)
        _current_maintenance_token.set(token)
    return token


def register_api_read() -> None:
    """登记业务 API 读取；与维护启动在同一锁内互斥。"""

    global _active_api_reads
    with _lock:
        if _maintenance is not None:
            raise TurnMaintenanceConflict(_maintenance[1])
        _active_api_reads += 1


def unregister_api_read() -> None:
    global _active_api_reads
    with _lock:
        if _active_api_reads > 0:
            _active_api_reads -= 1


def end_maintenance(token: str) -> None:
    global _maintenance, _maintenance_generation, _last_maintenance_operation
    with _lock:
        if _maintenance is not None and _maintenance[0] == token:
            _last_maintenance_operation = _maintenance[1]
            _maintenance = None
            _maintenance_generation += 1
    if _current_maintenance_token.get() == token:
        _current_maintenance_token.set(None)


def maintenance_operation() -> str | None:
    with _lock:
        return _maintenance[1] if _maintenance is not None else None


def maintenance_generation() -> int:
    with _lock:
        return _maintenance_generation


def advance_project_write_generation(project: str, operation: str) -> None:
    """项目整体删除/恢复提交后推进边界，拒绝此前已排队的同项目写入。"""

    if not isinstance(project, str) or not project:
        raise ValueError("项目 ID 不能为空")
    if not isinstance(operation, str) or not operation:
        raise ValueError("项目维护操作不能为空")
    owner_token = _current_maintenance_token.get()
    with _lock:
        if _maintenance is not None and owner_token != _maintenance[0]:
            raise TurnMaintenanceConflict(_maintenance[1])
        _project_generations[project] = _project_generations.get(project, 0) + 1
        _last_project_operations[project] = operation


@contextmanager
def request_maintenance_generation_context() -> Iterator[None]:
    """冻结请求进入时的维护代次，用于拒绝跨恢复边界的旧写请求。"""

    with _lock:
        generation = _maintenance_generation
        project_generations = dict(_project_generations)
    token = _request_maintenance_generation.set(generation)
    project_token = _request_project_generations.set(project_generations)
    try:
        yield
    finally:
        _request_project_generations.reset(project_token)
        _request_maintenance_generation.reset(token)


def assert_global_write_allowed() -> None:
    """在取得整库共享锁后调用，维护拥有者可执行恢复内部写入。"""

    request_generation = _request_maintenance_generation.get()
    owner_token = _current_maintenance_token.get()
    with _lock:
        maintenance = _maintenance
        generation = _maintenance_generation
        last_operation = _last_maintenance_operation
    if maintenance is not None and owner_token != maintenance[0]:
        raise TurnMaintenanceConflict(maintenance[1])
    if (
        request_generation is not None
        and request_generation != generation
        and not (maintenance is not None and owner_token == maintenance[0])
    ):
        raise TurnMaintenanceConflict(last_operation or "recent_maintenance")


def _assert_project_generation_allowed(project: str) -> None:
    request_generations = _request_project_generations.get()
    if request_generations is None:
        return
    with _lock:
        generation = _project_generations.get(project, 0)
        operation = _last_project_operations.get(project, "recent_project_change")
    if request_generations.get(project, 0) != generation:
        raise TurnMaintenanceConflict(operation)


def assert_write_allowed(project: str, save: str) -> None:
    assert_global_write_allowed()
    _assert_project_generation_allowed(project)
    with _lock:
        turn_id = _active.get((project, save))
    if turn_id and _current_turn_id.get() != turn_id:
        raise ActiveTurnConflict(project, save, turn_id)


def assert_project_write_allowed(project: str) -> None:
    assert_global_write_allowed()
    _assert_project_generation_allowed(project)
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
    global _active_api_reads, _maintenance, _maintenance_generation
    global _last_maintenance_operation
    with _lock:
        _active.clear()
        _active_api_reads = 0
        _maintenance = None
        _maintenance_generation = 0
        _last_maintenance_operation = None
        _project_generations.clear()
        _last_project_operations.clear()
    _current_maintenance_token.set(None)
    _request_maintenance_generation.set(None)
    _request_project_generations.set(None)
