"""短期总结的验证、后台生成与按稳定 ID 的并发收口。"""
from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from datetime import datetime
from typing import Any

from core.active_turns import ActiveTurnConflict
from core.model_provider import ProviderError
from core.provider_registry import ProviderLease, get_provider_registry
from core.secret_store import SecretStoreError
from core.session_manager import RevisionConflict, aload_session, mutate_session
from core.summary_parser import parse_summary


logger = logging.getLogger(__name__)

_EMPTY_SUMMARY_MARKERS = {"无", "无。", "—", "-", "（总结为空）"}

MAX_SUMMARY_RAW_LENGTH = 32_768
MAX_SUMMARY_TEXT_LENGTH = 2_000
MAX_SUMMARY_TIME_LENGTH = 300
MAX_SUMMARY_ITEMS = 5
MAX_SUMMARY_ITEM_LENGTH = 200
SUMMARY_APPLY_RETRY_SECONDS = 300.0
SUMMARY_APPLY_RETRY_INTERVAL = 0.1

_summary_tasks: dict[tuple[str, str, str, str], asyncio.Task] = {}


class SummaryValidationError(ValueError):
    """摘要内容或命令不满足公开合约。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_detail(self) -> dict:
        return {"code": self.code, "message": self.message}


def _validate_plain_text(
    value: Any,
    *,
    field: str,
    max_length: int,
    required: bool = False,
) -> str:
    if not isinstance(value, str):
        raise SummaryValidationError(
            "summary_field_type_invalid",
            f"{field} 必须是字符串",
        )
    normalized = value.strip()
    if required and (not normalized or normalized in _EMPTY_SUMMARY_MARKERS):
        raise SummaryValidationError(
            "summary_text_required",
            "前情提要不能为空",
        )
    if len(normalized) > max_length:
        raise SummaryValidationError(
            "summary_field_too_long",
            f"{field} 不能超过 {max_length} 个字符",
        )
    if any(ord(char) < 32 and char not in {"\n", "\t"} for char in normalized):
        raise SummaryValidationError(
            "summary_field_control_character",
            f"{field} 不能包含控制字符",
        )
    return normalized


def _validate_items(value: Any, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise SummaryValidationError(
            "summary_field_type_invalid",
            f"{field} 必须是数组",
        )
    if len(value) > MAX_SUMMARY_ITEMS:
        raise SummaryValidationError(
            "summary_items_too_many",
            f"{field} 最多 {MAX_SUMMARY_ITEMS} 条",
        )
    result: list[str] = []
    for index, item in enumerate(value, start=1):
        normalized = _validate_plain_text(
            item,
            field=f"{field}[{index}]",
            max_length=MAX_SUMMARY_ITEM_LENGTH,
            required=True,
        )
        result.append(normalized)
    return result


def validate_generated_summary(raw: Any) -> dict:
    """严格验证模型输出；空值和超长值进入 failed，不伪造占位正文。"""
    if not isinstance(raw, str) or not raw.strip():
        raise SummaryValidationError(
            "summary_output_empty",
            "模型返回了空总结",
        )
    if len(raw) > MAX_SUMMARY_RAW_LENGTH:
        raise SummaryValidationError(
            "summary_output_too_long",
            f"模型总结不能超过 {MAX_SUMMARY_RAW_LENGTH} 个字符",
        )
    parsed = parse_summary(raw)
    return {
        "text": _validate_plain_text(
            parsed.get("text"),
            field="text",
            max_length=MAX_SUMMARY_TEXT_LENGTH,
            required=True,
        ),
        "time": _validate_plain_text(
            parsed.get("time"),
            field="time",
            max_length=MAX_SUMMARY_TIME_LENGTH,
        ),
        "facts": _validate_items(parsed.get("facts"), field="facts"),
        "relations": _validate_items(parsed.get("relations"), field="relations"),
    }


def validated_summary_patch(body: dict, current: dict) -> dict:
    """校验人工编辑，并拒绝不会改变任何字段的提交。"""
    editable = ("text", "time", "facts", "relations")
    provided = [field for field in editable if field in body]
    if not provided:
        raise SummaryValidationError(
            "summary_patch_empty",
            "摘要编辑至少需要提供一个可编辑字段",
        )

    candidate = {
        "text": current.get("text", ""),
        "time": current.get("time", ""),
        "facts": deepcopy(current.get("facts", [])),
        "relations": deepcopy(current.get("relations", [])),
    }
    for field in provided:
        if field == "text":
            candidate[field] = _validate_plain_text(
                body[field],
                field=field,
                max_length=MAX_SUMMARY_TEXT_LENGTH,
                required=True,
            )
        elif field == "time":
            candidate[field] = _validate_plain_text(
                body[field],
                field=field,
                max_length=MAX_SUMMARY_TIME_LENGTH,
            )
        else:
            candidate[field] = _validate_items(body[field], field=field)

    candidate["text"] = _validate_plain_text(
        candidate["text"],
        field="text",
        max_length=MAX_SUMMARY_TEXT_LENGTH,
        required=True,
    )
    candidate["time"] = _validate_plain_text(
        candidate["time"],
        field="time",
        max_length=MAX_SUMMARY_TIME_LENGTH,
    )
    candidate["facts"] = _validate_items(candidate["facts"], field="facts")
    candidate["relations"] = _validate_items(
        candidate["relations"],
        field="relations",
    )

    if all(candidate[field] == current.get(field, "" if field in {"text", "time"} else []) for field in editable):
        raise SummaryValidationError(
            "summary_no_changes",
            "摘要内容没有变化",
        )
    return candidate


def recompute_summary_error(session: dict) -> None:
    """兼容旧顶层字段，同时避免一个任务成功后清掉其他失败段。"""
    latest_error = ""
    for item in reversed(session.get("summaries", [])):
        if item.get("status") == "failed" and item.get("error"):
            latest_error = f"总结生成失败: {item['error']}"
            break
    session["summary_error"] = latest_error


def is_summary_generation_active(
    project: str,
    save: str,
    summary_id: str,
    generation_id: str | None = None,
) -> bool:
    for key, task in tuple(_summary_tasks.items()):
        if task.done():
            continue
        if key[:3] != (project, save, summary_id):
            continue
        if generation_id is None or key[3] == generation_id:
            return True
    return False


def annotate_summary_task_state(session: dict, project: str, save: str) -> dict:
    """给公开响应添加进程内任务事实；该字段不写回磁盘。"""
    for item in session.get("summaries", []):
        summary_id = item.get("id")
        generation_id = item.get("generation_id")
        item["generation_active"] = bool(
            isinstance(summary_id, str)
            and is_summary_generation_active(
                project,
                save,
                summary_id,
                generation_id if isinstance(generation_id, str) else None,
            )
        )
    return session


async def _apply_generation_update(
    project: str,
    save: str,
    summary_id: str,
    generation_id: str,
    replacement: dict,
) -> bool:
    """只更新自己的 pending 版本；其他任务、人工编辑和新重试会使其失效。"""
    deadline = asyncio.get_running_loop().time() + SUMMARY_APPLY_RETRY_SECONDS
    while True:
        session = await aload_session(project, save)
        target = next(
            (
                item
                for item in session.get("summaries", [])
                if item.get("id") == summary_id
            ),
            None,
        )
        if (
            target is None
            or target.get("status") != "pending"
            or target.get("generation_id") != generation_id
        ):
            return False

        def apply(current: dict, context) -> bool:
            del context
            item = next(
                (
                    candidate
                    for candidate in current.setdefault("summaries", [])
                    if candidate.get("id") == summary_id
                ),
                None,
            )
            if (
                item is None
                or item.get("status") != "pending"
                or item.get("generation_id") != generation_id
            ):
                return False
            item.update(deepcopy(replacement))
            if item.get("status") == "completed":
                item.pop("failed", None)
            elif item.get("status") == "failed":
                item["failed"] = True
            recompute_summary_error(current)
            return True

        try:
            mutation = await mutate_session(
                project,
                save,
                session.get("revision", 0),
                apply,
            )
            return bool(mutation.value)
        except RevisionConflict:
            continue
        except ActiveTurnConflict:
            if asyncio.get_running_loop().time() >= deadline:
                logger.warning(
                    "总结 %s 的生成结果因 active turn 超时未写入，保留 pending 供人工重试",
                    summary_id,
                )
                return False
            await asyncio.sleep(SUMMARY_APPLY_RETRY_INTERVAL)


def _public_error(exc: Exception) -> str:
    """只公开受控异常文本，未分类异常的原文仅进入内部日志。"""
    if isinstance(exc, (SummaryValidationError, ProviderError, SecretStoreError)):
        return exc.message[:500]
    return "摘要生成失败，请重试"


async def _generate_summary(
    project: str,
    save: str,
    model: str,
    summary_id: str,
    generation_id: str,
    dropped: list[dict],
    *,
    provider: str = "ollama",
    provider_lease: ProviderLease | None = None,
) -> None:
    lease = provider_lease
    try:
        if lease is None:
            lease = await get_provider_registry().acquire_lease(provider)
        raw = await lease.provider.summarize_once(model, dropped)
        parsed = validate_generated_summary(raw)
        replacement = {
            **parsed,
            "status": "completed",
            "content_status": "valid",
            "completed_at": datetime.now().astimezone().isoformat(),
            "error": None,
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if isinstance(exc, (SummaryValidationError, ProviderError, SecretStoreError)):
            logger.warning(
                "短期总结失败，不影响基础对话 code=%s",
                exc.code,
            )
        else:
            logger.exception("短期总结发生未分类异常，不影响基础对话")
        replacement = {
            "status": "failed",
            "failed": True,
            "completed_at": datetime.now().astimezone().isoformat(),
            "error": _public_error(exc),
        }
    finally:
        if lease is not None:
            await lease.release()
    await _apply_generation_update(
        project,
        save,
        summary_id,
        generation_id,
        replacement,
    )


async def schedule_summary_generation(
    project: str,
    save: str,
    model: str,
    summary_id: str,
    generation_id: str,
    dropped: list[dict],
    *,
    provider: str = "ollama",
    provider_lease: ProviderLease | None = None,
    retain_provider_lease: bool = False,
) -> asyncio.Task:
    """同一生成版本只启动一个任务，并把单一实例租约移交给任务。"""
    key = (project, save, summary_id, generation_id)
    existing = _summary_tasks.get(key)
    if existing is not None and not existing.done():
        if provider_lease is not None and not retain_provider_lease:
            await provider_lease.release()
        return existing
    if provider_lease is None:
        task_lease = await get_provider_registry().acquire_lease(provider)
    elif retain_provider_lease:
        task_lease = provider_lease.retain()
    else:
        task_lease = provider_lease
    try:
        task = asyncio.create_task(
            _generate_summary(
                project,
                save,
                model,
                summary_id,
                generation_id,
                deepcopy(dropped),
                provider=provider,
                provider_lease=task_lease,
            ),
            name=f"summary-{summary_id}-{generation_id}",
        )
    except BaseException:
        await task_lease.release()
        raise
    _summary_tasks[key] = task

    def discard(done: asyncio.Task) -> None:
        if _summary_tasks.get(key) is done:
            _summary_tasks.pop(key, None)

    task.add_done_callback(discard)
    return task


async def shutdown_summary_tasks() -> None:
    tasks = [task for task in _summary_tasks.values() if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _summary_tasks.clear()
