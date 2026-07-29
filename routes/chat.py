"""聊天与模型切换路由。"""
import asyncio
import json
import logging
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from core.active_turns import assert_write_allowed
from core.chat_turns import (
    TurnEventLogLimitExceeded,
    TurnNotFound,
    TurnRuntime,
    get_turn_coordinator,
)
from core.model_provider import ProviderError
from core.provider_registry import get_provider_registry
from core.secret_store import SecretStoreError
from core.character_loader import load_character, load_user_profile, load_worldbook
from core.config import (
    CHAT_GENERATION_MAX_SECONDS,
    CHAT_OUTPUT_MAX_BYTES,
    MAX_MESSAGES_IN_SAVE,
)
from core.message_commands import (
    MessageCommandError,
    MessageNotFound,
    RegenerationWouldRewriteHistory,
    attach_original_reply_as_alternative,
    plan_message_regeneration,
    restore_regeneration_original,
)
from core.prompt_assembler import (
    PromptAssembler,
    PromptBudgetExceeded,
    PromptTemplateInvalid,
)
from core.response_parser import (
    build_response_presentation,
    check_voice_confusion,
    parse_response,
)
from core.roleplay_policy import (
    ACTION_UNRESOLVED_SKIPPED,
    ACTION_WRITEBACK_APPLIED,
    ACTION_WRITEBACK_SKIPPED,
    WARNING_AMBIGUOUS_CHARACTER_IDENTITY,
    WARNING_MUTED_CHARACTER_OUTPUT,
    WARNING_UNKNOWN_CHARACTER_IDENTITY,
    build_roleplay_context,
    decrement_silence_counters,
    resolve_character_id,
)
from core.summary_lifecycle import (
    schedule_summary_generation,
    shutdown_summary_tasks,
)
from core.worldbook_policy import WorldbookValidationError
from core.session_manager import (
    RevisionConflict,
    aload_session,
    append_history,
    get_session_store,
    mutate_session,
    trim_history,
    trim_snapshot_payload,
)
from routes.common import (
    ChatRequest,
    RegenerateRequest,
    _json_object,
    _norm_save,
    _norm_project,
    _initialize_session_from_profiles,
    _expected_revision,
    _raise_revision_conflict,
    apply_character_state,
)
from routes.providers import raise_provider_error

logger = logging.getLogger(__name__)
router = APIRouter()


class _OutputLimitExceeded(RuntimeError):
    pass
_prompt_assembler = PromptAssembler()


def _assemble_generation_prompt_sync(
    *,
    project: str,
    session: dict,
    user_text: str,
    history_override: list[dict] | None,
    context_info: dict,
    num_predict: int,
):
    """在线程内读取角色资料并完成完整提示词预算计算。"""

    characters = [
        load_character(project, character_id)
        for character_id in session.get("characters_state", {})
    ]
    characters = [character for character in characters if character]
    roleplay_context = build_roleplay_context(
        characters,
        session.get("characters_state", {}),
        session.get("roleplay_policy"),
    )
    user_profile = load_user_profile(project)
    worldbook_entries = load_worldbook(project)
    history = (
        deepcopy(history_override)
        if history_override is not None
        else deepcopy(session.get("message_history", []))
    )
    assembly = _prompt_assembler.assemble(
        user_input=user_text,
        characters=characters,
        characters_state=session.get("characters_state", {}),
        scene_meta=session.get("scene_meta", {}),
        user_profile=user_profile,
        worldbook_entries=worldbook_entries,
        history=history,
        summaries=session.get("summaries", []),
        context_limit=context_info["context_limit"],
        context_limit_source=context_info["source"],
        num_predict=num_predict,
        manual_worldbook_ids=session.get("manual_worldbook_ids", []),
        roleplay_context=roleplay_context,
    )
    return assembly, characters, roleplay_context


async def _assemble_generation_prompt(**kwargs):
    return await asyncio.to_thread(_assemble_generation_prompt_sync, **kwargs)
_PARSED_SESSION_DELTA_FIELDS = (
    "scene_meta",
    "characters_state",
    "roleplay_policy",
    "current_provider",
    "current_model",
)


async def shutdown_chat_background_tasks() -> None:
    await shutdown_summary_tasks()


def _validated_parameters(req: ChatRequest | RegenerateRequest) -> dict:
    params = {
        "temperature": 0.8,
        "num_predict": 4096,
        "think": True,
        "top_p": None,
        "top_k": None,
    }
    if req.temperature is not None:
        try:
            value = float(req.temperature)
            if 0.0 <= value <= 2.0:
                params["temperature"] = value
            else:
                logger.warning(
                    "temperature=%s 超出范围 [0.0, 2.0]，使用默认值 0.8",
                    req.temperature,
                )
        except (TypeError, ValueError):
            logger.warning("无效 temperature 值: %s，使用默认值 0.8", req.temperature)
    if req.num_predict is not None:
        try:
            value = int(req.num_predict)
            if 1 <= value <= 32768:
                params["num_predict"] = value
            else:
                logger.warning(
                    "num_predict=%s 超出范围 [1, 32768]，使用默认值 4096",
                    req.num_predict,
                )
        except (TypeError, ValueError):
            logger.warning("无效 num_predict 值: %s，使用默认值 4096", req.num_predict)
    if req.think is not None:
        params["think"] = bool(req.think)
    if req.top_p is not None:
        try:
            value = float(req.top_p)
            if 0.0 <= value <= 1.0:
                params["top_p"] = value
            else:
                logger.warning(
                    "top_p=%s 超出范围 [0.0, 1.0]，不使用 top_p",
                    req.top_p,
                )
        except (TypeError, ValueError):
            logger.warning("无效 top_p 值: %s，不使用 top_p", req.top_p)
    if req.top_k is not None:
        try:
            value = int(req.top_k)
            if 0 <= value <= 1000:
                params["top_k"] = value
            else:
                logger.warning(
                    "top_k=%s 超出范围 [0, 1000]，不使用 top_k",
                    req.top_k,
                )
        except (TypeError, ValueError):
            logger.warning("无效 top_k 值: %s，不使用 top_k", req.top_k)
    return params


async def _prepare_turn(
    req: ChatRequest | RegenerateRequest,
    *,
    turn_kind: str = "chat",
    user_text_override: str | None = None,
    session_override: dict | None = None,
    history_override: list[dict] | None = None,
) -> dict:
    raw_user_text = (
        user_text_override
        if user_text_override is not None
        else getattr(req, "user_input", "")
    )
    user_text = (raw_user_text or "").strip()
    if not user_text:
        raise HTTPException(400, "用户输入不能为空")
    project = _norm_project(req.project)
    save = _norm_save(req.save)
    session = (
        deepcopy(session_override)
        if session_override is not None
        else await aload_session(project, save)
    )
    current_revision = session.get("revision", 0)
    if current_revision != req.expected_revision:
        _raise_revision_conflict(
            RevisionConflict(req.expected_revision, current_revision, session)
        )

    if not session.get("characters_state") and not session.get("message_history"):
        await _initialize_session_from_profiles(
            session,
            project,
            requested_provider=req.provider,
            requested_model=req.model,
        )

    session_provider = session.get("current_provider") or "ollama"
    provider_id = req.provider or session_provider
    if not isinstance(provider_id, str):
        raise HTTPException(400, "Provider 必须是字符串")
    model = req.model
    if not model and provider_id == session_provider:
        model = session.get("current_model")
    if not model:
        raise HTTPException(400, "未指定模型")

    try:
        provider_lease = await get_provider_registry().acquire_lease(provider_id)
    except (ProviderError, SecretStoreError) as exc:
        raise_provider_error(exc)

    try:
        provider = provider_lease.provider
        available = (
            sorted(set(provider_lease.config.models))
            if provider_lease.config.models
            else await provider.list_models()
        )
        if model not in available:
            raise HTTPException(
                400,
                detail={
                    "code": "provider_model_unavailable",
                    "message": "所选模型在当前 Provider 中不可用",
                    "provider_id": provider_id,
                    "models": available,
                },
            )

        params = _validated_parameters(req)

        context_info = await provider.get_context_limit(model)
        try:
            assembly, characters, roleplay_context = await _assemble_generation_prompt(
                project=project,
                session=session,
                user_text=user_text,
                history_override=history_override,
                context_info=context_info,
                num_predict=params["num_predict"],
            )
        except (
            PromptBudgetExceeded,
            PromptTemplateInvalid,
            WorldbookValidationError,
        ) as exc:
            raise HTTPException(422, detail=exc.as_detail()) from exc
        # 与预算计算共用同一有效窗口，禁止 Ollama 按更小默认 num_ctx 静默截断。
        params["num_ctx"] = assembly.diagnostics["context_limit"]

        return {
            "project": project,
            "save": save,
            "user_text": user_text,
            "expected_revision": req.expected_revision,
            "provider": provider_id,
            "provider_client": provider,
            "provider_lease": provider_lease,
            "model": model,
            "params": params,
            "session": deepcopy(session),
            "characters": characters,
            "roleplay_context": deepcopy(roleplay_context),
            "turn_kind": turn_kind,
            "messages": assembly.messages,
            "prompt_diagnostics": assembly.diagnostics,
        }
    except (ProviderError, SecretStoreError) as exc:
        await provider_lease.release()
        raise_provider_error(exc)
    except BaseException:
        await provider_lease.release()
        raise


def _message_metadata(
    turn_id: str,
    status: str,
    error: dict | None,
    *,
    in_prompt: bool,
    created_at: str,
    turn_kind: str,
    roleplay_warnings: list[dict] | None = None,
    context_diagnostics: dict | None = None,
    generation_telemetry: dict | None = None,
) -> dict:
    metadata = {
        "turn_id": turn_id,
        "turn_kind": turn_kind,
        "status": status,
        "error": deepcopy(error),
        "timestamps": {
            "created_at": created_at,
            "completed_at": datetime.now().astimezone().isoformat(),
        },
        "in_prompt": in_prompt,
    }
    if roleplay_warnings is not None:
        metadata["roleplay_warnings"] = deepcopy(roleplay_warnings)
    if context_diagnostics is not None:
        metadata["context_diagnostics"] = deepcopy(context_diagnostics)
    if generation_telemetry is not None:
        metadata["generation_telemetry"] = deepcopy(generation_telemetry)
    return metadata


def _generation_telemetry(
    prepared: dict,
    content: str,
    thinking: str,
    status: str,
    error: dict | None,
) -> dict:
    started = prepared.get("_generation_started_monotonic")
    now = asyncio.get_running_loop().time()
    latency_ms = (
        max(0, round((now - started) * 1000))
        if isinstance(started, (int, float)) and not isinstance(started, bool)
        else None
    )
    diagnostics = prepared.get("prompt_diagnostics", {})
    input_tokens = (
        diagnostics.get("estimated_prompt_tokens")
        if isinstance(diagnostics, dict)
        else None
    )
    output_text = f"{thinking}{content}"
    return {
        "schema_version": 1,
        "provider": prepared.get("provider"),
        "model": prepared.get("model"),
        "status": status,
        "error_code": error.get("code") if isinstance(error, dict) else None,
        "latency_ms": latency_ms,
        "input_tokens_estimated": input_tokens,
        "output_tokens_estimated": _prompt_assembler.estimator.estimate_text(
            output_text
        ),
        "output_bytes": len(output_text.encode("utf-8")),
        "token_source": "local_estimator",
        "finished_at": datetime.now().astimezone().isoformat(),
    }


def _finalize_turn_messages(
    session: dict,
    prepared: dict,
    turn_id: str,
    content: str,
    thinking: str,
    status: str,
    error: dict | None,
    *,
    trim: bool = True,
    roleplay_warnings: list[dict] | None = None,
    presentation: dict | None = None,
    generation_telemetry: dict | None = None,
) -> tuple[dict, dict | None, list[dict]]:
    user_message = next(
        (
            message
            for message in session.setdefault("message_history", [])
            if message.get("turn_id") == turn_id and message.get("role") == "user"
        ),
        None,
    )
    if user_message is None:
        raise RuntimeError("accepted turn 缺少 pending user message")
    if prepared.get("turn_kind") == "regenerate" and status != "completed":
        restored = restore_regeneration_original(session, user_message)
        return user_message, restored, []
    created_at = (
        user_message.get("timestamps", {}).get("created_at")
        or datetime.now().astimezone().isoformat()
    )
    user_message.update(_message_metadata(
        turn_id,
        status,
        error,
        in_prompt=True,
        created_at=created_at,
        turn_kind=prepared.get("turn_kind", "chat"),
    ))
    assistant_message = None
    if status == "completed" or content or thinking:
        assistant_message = append_history(
            session,
            "assistant",
            content,
            thinking,
            metadata=_message_metadata(
                turn_id,
                status,
                error,
                in_prompt=status == "completed",
                created_at=created_at,
                turn_kind=prepared.get("turn_kind", "chat"),
                roleplay_warnings=roleplay_warnings,
                context_diagnostics=prepared.get("prompt_diagnostics"),
                generation_telemetry=generation_telemetry,
            ),
        )
        if presentation is not None:
            assistant_message["presentation"] = deepcopy(presentation)
        if prepared.get("turn_kind") == "regenerate":
            attach_original_reply_as_alternative(
                session,
                user_message,
                assistant_message,
            )
    dropped = (
        trim_history(session, max_messages=MAX_MESSAGES_IN_SAVE)
        if trim
        else []
    )
    return user_message, assistant_message, dropped


def _apply_parsed_state(
    session: dict,
    parsed: dict,
    roleplay_context: dict,
) -> list[dict]:
    roleplay_warnings: list[dict] = []
    muted_ids = {
        character_id
        for character_id in roleplay_context.get("muted_ids", [])
        if isinstance(character_id, str)
    }
    if parsed.get("scene_meta"):
        for key, value in parsed["scene_meta"].items():
            if not value or key == "user_line":
                continue
            if key == "time_weather":
                parts = value.split("/", 1)
                session["scene_meta"]["time"] = parts[0].strip()
                if len(parts) == 2:
                    session["scene_meta"]["weather"] = parts[1].strip()
            elif key in session["scene_meta"]:
                session["scene_meta"][key] = value

    for character in parsed.get("characters", []):
        if not isinstance(character, dict):
            continue
        resolved = resolve_character_id(character.get("name"), roleplay_context)
        status = resolved.get("status")
        if status == "ambiguous":
            roleplay_warnings.append({
                "code": WARNING_AMBIGUOUS_CHARACTER_IDENTITY,
                "action": ACTION_UNRESOLVED_SKIPPED,
                "candidate_ids": sorted({
                    candidate
                    for candidate in resolved.get("candidate_ids", [])
                    if isinstance(candidate, str) and candidate
                }),
            })
            continue
        if status != "matched":
            roleplay_warnings.append({
                "code": WARNING_UNKNOWN_CHARACTER_IDENTITY,
                "action": ACTION_UNRESOLVED_SKIPPED,
            })
            continue

        character_id = resolved.get("character_id")
        state = session.get("characters_state", {}).get(character_id)
        if not isinstance(character_id, str) or not isinstance(state, dict):
            roleplay_warnings.append({
                "code": WARNING_UNKNOWN_CHARACTER_IDENTITY,
                "action": ACTION_UNRESOLVED_SKIPPED,
            })
            continue
        if character_id in muted_ids:
            strict = roleplay_context.get("strict_muted_writeback") is True
            roleplay_warnings.append({
                "code": WARNING_MUTED_CHARACTER_OUTPUT,
                "action": (
                    ACTION_WRITEBACK_SKIPPED if strict else ACTION_WRITEBACK_APPLIED
                ),
                "character_id": character_id,
            })
            if strict:
                character["affinity"] = state.get("affinity", 0)
                continue
        apply_character_state(state, character)
        character["affinity"] = state.get("affinity", character.get("affinity", 0))

    parsed["roleplay_warnings"] = deepcopy(roleplay_warnings)
    return roleplay_warnings


def _capture_presentation_baseline(
    session: dict,
    parsed: dict,
    roleplay_context: dict,
) -> dict:
    """冻结写回前的可展示状态，用于刷新后还原本轮变化。"""
    previous_affinities: list[object] = []
    character_moods: list[object] = []
    states = session.get("characters_state", {})
    states = states if isinstance(states, dict) else {}
    for character in parsed.get("characters", []):
        previous = None
        mood = None
        if isinstance(character, dict):
            resolved = resolve_character_id(character.get("name"), roleplay_context)
            character_id = resolved.get("character_id")
            state = states.get(character_id) if isinstance(character_id, str) else None
            if resolved.get("status") == "matched" and isinstance(state, dict):
                previous = state.get("affinity")
                mood = state.get("mood")
        previous_affinities.append(previous)
        character_moods.append(mood)
    return {
        "previous_affinities": previous_affinities,
        "character_moods": character_moods,
        "scene_meta": deepcopy(session.get("scene_meta", {})),
    }


def _presentation_scene_changes(before: object, after: object) -> list[dict]:
    previous = before if isinstance(before, dict) else {}
    current = after if isinstance(after, dict) else {}
    changes = []
    for key in ("location", "time", "weather"):
        old_value = previous.get(key)
        new_value = current.get(key)
        old_text = old_value.strip() if isinstance(old_value, str) else ""
        new_text = new_value.strip() if isinstance(new_value, str) else ""
        if new_text and old_text != new_text:
            changes.append({"key": key, "value": new_text})
    return changes


async def _commit_partial(
    prepared: dict,
    turn_id: str,
    content: str,
    thinking: str,
    status: str,
    error: dict,
    generation_telemetry: dict,
) -> dict | None:
    session = deepcopy(prepared["session"])
    _user, _assistant, _dropped = _finalize_turn_messages(
        session,
        prepared,
        turn_id,
        content,
        thinking,
        status,
        error,
        trim=False,
        generation_telemetry=generation_telemetry,
    )

    def commit_partial(current: dict, context) -> None:
        del context
        current.clear()
        current.update(deepcopy(session))

    mutation = await mutate_session(
        prepared["project"],
        prepared["save"],
        prepared["expected_revision"],
        commit_partial,
    )
    return mutation.session


async def _reconcile_failed_turn_on_current(
    prepared: dict,
    turn_id: str,
    content: str,
    thinking: str,
    error: dict,
    generation_telemetry: dict,
) -> dict | None:
    """CAS 冲突后只收口本 turn 消息，不覆盖并发命令留下的其他字段。"""
    for _attempt in range(3):
        session = await aload_session(prepared["project"], prepared["save"])
        if not any(
            message.get("turn_id") == turn_id
            and message.get("role") == "user"
            for message in session.get("message_history", [])
        ):
            return None

        def reconcile(current: dict, context) -> None:
            del context
            user_message = next(
                (
                    message
                    for message in current.setdefault("message_history", [])
                    if message.get("turn_id") == turn_id
                    and message.get("role") == "user"
                ),
                None,
            )
            if user_message is None:
                return
            if prepared.get("turn_kind") == "regenerate":
                restore_regeneration_original(current, user_message)
                return
            created_at = (
                user_message.get("timestamps", {}).get("created_at")
                or datetime.now().astimezone().isoformat()
            )
            user_message.update(_message_metadata(
                turn_id,
                "failed",
                error,
                in_prompt=True,
                created_at=created_at,
                turn_kind=prepared.get("turn_kind", "chat"),
            ))
            assistant = next(
                (
                    message
                    for message in current["message_history"]
                    if message.get("turn_id") == turn_id
                    and message.get("role") == "assistant"
                ),
                None,
            )
            if content or thinking:
                metadata = _message_metadata(
                    turn_id,
                    "failed",
                    error,
                    in_prompt=False,
                    created_at=created_at,
                    turn_kind=prepared.get("turn_kind", "chat"),
                    context_diagnostics=prepared.get("prompt_diagnostics"),
                    generation_telemetry=generation_telemetry,
                )
                if assistant is None:
                    append_history(
                        current,
                        "assistant",
                        content,
                        thinking,
                        metadata=metadata,
                    )
                else:
                    assistant.update(metadata)

        try:
            mutation = await mutate_session(
                prepared["project"],
                prepared["save"],
                session.get("revision", 0),
                reconcile,
            )
            return mutation.session
        except RevisionConflict:
            continue
    return None


async def _commit_completed(
    prepared: dict,
    runtime: TurnRuntime,
    content: str,
    thinking: str,
) -> None:
    session = deepcopy(prepared["session"])
    generation_telemetry = _generation_telemetry(
        prepared,
        content,
        thinking,
        "completed",
        None,
    )
    parsed = parse_response(content)
    try:
        warnings = check_voice_confusion(parsed, prepared["characters"])
        if warnings:
            parsed["warnings"] = warnings
    except Exception:
        logger.exception("角色声线校验失败，不阻断 turn 提交")

    presentation_baseline = _capture_presentation_baseline(
        session,
        parsed,
        prepared["roleplay_context"],
    )
    roleplay_warnings = _apply_parsed_state(
        session,
        parsed,
        prepared["roleplay_context"],
    )
    _user, _assistant, dropped = _finalize_turn_messages(
        session,
        prepared,
        runtime.turn_id,
        content,
        thinking,
        "completed",
        None,
        roleplay_warnings=roleplay_warnings,
        presentation=build_response_presentation(
            parsed,
            previous_affinities=presentation_baseline["previous_affinities"],
            character_moods=presentation_baseline["character_moods"],
            scene_changes=_presentation_scene_changes(
                presentation_baseline["scene_meta"],
                session.get("scene_meta"),
            ),
        ),
        generation_telemetry=generation_telemetry,
    )

    session["current_provider"] = prepared["provider"]
    session["current_model"] = prepared["model"]
    summary_id = str(uuid4()) if dropped else None
    summary_generation_id = str(uuid4()) if dropped else None

    def commit_chat(current: dict, context) -> str | None:
        current.clear()
        current.update(deepcopy(session))
        # 起始为 1 的角色已在冻结 Prompt 上下文中保持 muted；只有普通
        # completed chat 在这次唯一 Session commit 内把它递减为 0。
        if prepared.get("turn_kind", "chat") == "chat":
            decrement_silence_counters(current)
        if dropped:
            snapshot_path = context.snapshot(
                "trim",
                trim_snapshot_payload(dropped, summary_id=summary_id),
            )
            current.setdefault("summaries", []).append({
                "id": summary_id,
                "source_snapshot_id": snapshot_path.name,
                "source_status": "available",
                "status": "pending",
                "content_status": "empty",
                "generation_id": summary_generation_id,
                "generation_attempt": 1,
                "provider": prepared["provider"],
                "model": prepared["model"],
                "text": "",
                "time": "",
                "facts": [],
                "relations": [],
                "created_at": datetime.now().astimezone().isoformat(),
                "requested_at": datetime.now().astimezone().isoformat(),
                "error": None,
            })
            return snapshot_path.name
        return None

    mutation = await mutate_session(
        prepared["project"],
        prepared["save"],
        prepared["expected_revision"],
        commit_chat,
    )
    revision = mutation.session["revision"]
    await runtime.mark_session_committed(revision)
    parsed_event = {
        "type": "parsed",
        "parsed": parsed,
        "roleplay_warnings": deepcopy(roleplay_warnings),
        "context_diagnostics": deepcopy(prepared["prompt_diagnostics"]),
        "generation_telemetry": deepcopy(generation_telemetry),
        "revision": revision,
        "session_delta": {
            field: deepcopy(mutation.session[field])
            for field in _PARSED_SESSION_DELTA_FIELDS
        },
    }
    await runtime.emit(parsed_event, session_revision=revision)
    await runtime.terminal(
        "completed",
        session_revision=revision,
        context_diagnostics=deepcopy(prepared["prompt_diagnostics"]),
        generation_telemetry=deepcopy(generation_telemetry),
    )
    if dropped and summary_id and summary_generation_id:
        await schedule_summary_generation(
            prepared["project"],
            prepared["save"],
            prepared["model"],
            summary_id,
            summary_generation_id,
            dropped,
            provider=prepared["provider"],
            provider_lease=prepared["provider_lease"],
            retain_provider_lease=True,
        )


def _revision_error(conflict: RevisionConflict) -> dict:
    return {
        "code": "revision_conflict",
        "message": "turn 提交时存档 revision 已变化",
        "expected_revision": conflict.expected,
        "current_revision": conflict.current,
    }


async def _fail_turn(
    runtime: TurnRuntime,
    prepared: dict,
    content: str,
    thinking: str,
    *,
    status: str,
    error: dict,
) -> None:
    generation_telemetry = _generation_telemetry(
        prepared,
        content,
        thinking,
        status,
        error,
    )
    committed = None
    try:
        committed = await _commit_partial(
            prepared,
            runtime.turn_id,
            content,
            thinking,
            status,
            error,
            generation_telemetry,
        )
    except RevisionConflict as conflict:
        status = "failed"
        error = _revision_error(conflict)
        committed = await _reconcile_failed_turn_on_current(
            prepared,
            runtime.turn_id,
            content,
            thinking,
            error,
            generation_telemetry,
        )
    updates = {}
    if committed is not None:
        updates["session_revision"] = committed["revision"]
    updates["context_diagnostics"] = deepcopy(prepared["prompt_diagnostics"])
    updates["generation_telemetry"] = deepcopy(generation_telemetry)
    await runtime.terminal(status, error=error, **updates)


def _turn_worker(prepared: dict):
    async def run_with_provider(runtime: TurnRuntime) -> None:
        prepared["_generation_started_monotonic"] = (
            asyncio.get_running_loop().time()
        )
        full_content = ""
        full_thinking = ""
        pending_type = ""
        pending_text = ""
        last_flush = asyncio.get_running_loop().time()
        emitted_stream_event = False
        generated_bytes = 0

        async def flush_stream(*, force: bool = False) -> None:
            nonlocal pending_type, pending_text, last_flush, emitted_stream_event
            if not pending_text:
                return
            now = asyncio.get_running_loop().time()
            if (
                not force
                and emitted_stream_event
                and len(pending_text) < 2048
                and now - last_flush < 0.05
            ):
                return
            text = pending_text
            event_type = pending_type
            pending_type = ""
            pending_text = ""
            updates = {
                "thinking": full_thinking,
            } if event_type == "thinking" else {
                "content": full_content,
            }
            await runtime.emit(
                {"type": event_type, "content": text},
                **updates,
            )
            emitted_stream_event = True
            last_flush = now

        async def buffer_stream(event_type: str, text: str) -> None:
            nonlocal pending_type, pending_text
            if pending_type and pending_type != event_type:
                await flush_stream(force=True)
            pending_type = event_type
            pending_text += text
            await flush_stream()

        async def append_stream_text(event_type: str, text: str) -> None:
            nonlocal generated_bytes, full_content, full_thinking
            if not isinstance(text, str):
                raise TypeError("Provider 流内容必须是字符串")
            next_bytes = generated_bytes + len(text.encode("utf-8"))
            if next_bytes > CHAT_OUTPUT_MAX_BYTES:
                raise _OutputLimitExceeded
            generated_bytes = next_bytes
            if event_type == "thinking":
                full_thinking += text
            else:
                full_content += text
            await buffer_stream(event_type, text)

        async def time_limited_provider_stream():
            iterator = prepared["provider_client"].chat_stream(
                model=prepared["model"],
                messages=prepared["messages"],
                think=prepared["params"]["think"],
                num_predict=prepared["params"]["num_predict"],
                num_ctx=prepared["params"]["num_ctx"],
                temperature=prepared["params"]["temperature"],
                top_p=prepared["params"]["top_p"],
                top_k=prepared["params"]["top_k"],
            ).__aiter__()
            deadline = (
                asyncio.get_running_loop().time()
                + CHAT_GENERATION_MAX_SECONDS
            )
            try:
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError
                    try:
                        chunk = await asyncio.wait_for(
                            anext(iterator),
                            timeout=remaining,
                        )
                    except StopAsyncIteration:
                        return
                    yield chunk
            finally:
                close = getattr(iterator, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except RuntimeError:
                        pass

        try:
            await runtime.emit({"type": "started"})
            async for chunk in time_limited_provider_stream():
                if chunk["type"] == "thinking":
                    await append_stream_text("thinking", chunk["content"])
                elif chunk["type"] == "content":
                    await append_stream_text("content", chunk["content"])
                elif chunk["type"] == "error":
                    await flush_stream(force=True)
                    await _fail_turn(
                        runtime,
                        prepared,
                        full_content,
                        full_thinking,
                        status="failed",
                        error={
                            "code": chunk.get("code", "upstream_error"),
                            "message": chunk["content"],
                            **(
                                {"http_status": chunk["http_status"]}
                                if chunk.get("http_status") is not None
                                else {}
                            ),
                        },
                    )
                    return
                elif chunk["type"] == "done":
                    await flush_stream(force=True)
                    completion = asyncio.create_task(
                        _commit_completed(
                            prepared,
                            runtime,
                            full_content,
                            full_thinking,
                        )
                    )
                    try:
                        await asyncio.shield(completion)
                    except asyncio.CancelledError:
                        await completion
                    except RevisionConflict as conflict:
                        await _fail_turn(
                            runtime,
                            prepared,
                            full_content,
                            full_thinking,
                            status="failed",
                            error=_revision_error(conflict),
                        )
                    return

            await flush_stream(force=True)
            await _fail_turn(
                runtime,
                prepared,
                full_content,
                full_thinking,
                status="failed",
                error={
                    "code": "upstream_eof",
                    "message": "上游连接结束但未发送完成标记",
                },
            )
        except _OutputLimitExceeded:
            await flush_stream(force=True)
            await _fail_turn(
                runtime,
                prepared,
                full_content,
                full_thinking,
                status="failed",
                error={
                    "code": "output_limit_exceeded",
                    "message": "模型输出超过本地安全上限，已停止生成",
                    "limit_bytes": CHAT_OUTPUT_MAX_BYTES,
                },
            )
        except TimeoutError:
            await flush_stream(force=True)
            await _fail_turn(
                runtime,
                prepared,
                full_content,
                full_thinking,
                status="failed",
                error={
                    "code": "generation_timeout",
                    "message": "模型生成超过时间上限，已停止生成",
                    "limit_seconds": CHAT_GENERATION_MAX_SECONDS,
                },
            )
        except TurnEventLogLimitExceeded:
            logger.warning("turn %s 事件日志达到安全上限", runtime.turn_id)
            await _fail_turn(
                runtime,
                prepared,
                full_content,
                full_thinking,
                status="failed",
                error={
                    "code": "turn_log_limit_exceeded",
                    "message": "本轮事件日志达到安全上限，已停止生成",
                },
            )
        except asyncio.CancelledError:
            await asyncio.shield(flush_stream(force=True))
            await asyncio.shield(_fail_turn(
                runtime,
                prepared,
                full_content,
                full_thinking,
                status="cancelled",
                error={"code": "cancelled", "message": "turn 已取消"},
            ))
        except RevisionConflict as conflict:
            await runtime.terminal("failed", error=_revision_error(conflict))
        except (ProviderError, SecretStoreError) as exc:
            # Provider 会在异步生成器首次迭代时再次执行 DNS/初始化校验。
            # 这类已分类故障必须保持公开稳定错误码，不能降级成 internal_error。
            logger.warning(
                "turn Provider 初始化失败 provider=%s code=%s",
                prepared["provider"],
                exc.code,
            )
            error = {"code": exc.code, "message": exc.message}
            if isinstance(exc, ProviderError) and exc.http_status is not None:
                error["http_status"] = exc.http_status
            await _fail_turn(
                runtime,
                prepared,
                full_content,
                full_thinking,
                status="failed",
                error=error,
            )
        except Exception:
            # Session completed commit 是权威终态；完成后的 parsed/terminal
            # 日志故障交由 coordinator 以 completed 收口，禁止走失败写回。
            if runtime.session_committed_revision is not None:
                raise
            logger.exception("turn 生成异常")
            await _fail_turn(
                runtime,
                prepared,
                full_content,
                full_thinking,
                status="failed",
                error={"code": "internal_error", "message": "生成失败，请重试"},
            )

    async def run(runtime: TurnRuntime) -> None:
        try:
            await run_with_provider(runtime)
        finally:
            await prepared["provider_lease"].release()

    return run


async def _start_turn(req: ChatRequest) -> dict:
    prepared = await _prepare_turn(req, turn_kind="chat")
    coordinator = get_turn_coordinator()
    handed_off = False

    def accepted(session: dict) -> None:
        prepared["session"] = session
        prepared["expected_revision"] = session["revision"]

    try:
        result = await coordinator.start(
            project=prepared["project"],
            save=prepared["save"],
            expected_revision=prepared["expected_revision"],
            user_input=prepared["user_text"],
            provider=prepared["provider"],
            model=prepared["model"],
            parameters=prepared["params"],
            initial_session=prepared["session"],
            accepted_callback=accepted,
            worker=_turn_worker(prepared),
            prompt_diagnostics=prepared["prompt_diagnostics"],
            turn_kind="chat",
        )
        handed_off = True
        return result
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    finally:
        if not handed_off:
            await prepared["provider_lease"].release()


def _raise_message_command_error(exc: MessageCommandError) -> None:
    if isinstance(exc, MessageNotFound):
        status_code = 404
    elif isinstance(exc, RegenerationWouldRewriteHistory):
        status_code = 409
    else:
        status_code = 422
    raise HTTPException(
        status_code,
        detail={"code": exc.code, "message": str(exc)},
    ) from exc


async def _start_regenerated_turn(req: RegenerateRequest) -> dict:
    project = _norm_project(req.project)
    save = _norm_save(req.save)
    session = await aload_session(project, save)
    current_revision = session.get("revision", 0)
    if current_revision != req.expected_revision:
        _raise_revision_conflict(
            RevisionConflict(req.expected_revision, current_revision, session)
        )
    try:
        plan = plan_message_regeneration(session, req.message_id)
    except MessageCommandError as exc:
        _raise_message_command_error(exc)

    prepared = await _prepare_turn(
        req,
        turn_kind="regenerate",
        user_text_override=plan.user_input,
        session_override=session,
        history_override=plan.prompt_history,
    )
    coordinator = get_turn_coordinator()
    store = get_session_store()
    handed_off = False

    async def accept_command(turn_id: str, created_at: str):
        return await store.accept_regenerated_chat_turn(
            prepared["project"],
            prepared["save"],
            prepared["expected_revision"],
            turn_id=turn_id,
            target_message_id=req.message_id,
            expected_user_input=prepared["user_text"],
            created_at=created_at,
        )

    def accepted(accepted_session: dict) -> None:
        prepared["session"] = accepted_session
        prepared["expected_revision"] = accepted_session["revision"]

    try:
        result = await coordinator.start(
            project=prepared["project"],
            save=prepared["save"],
            expected_revision=prepared["expected_revision"],
            user_input=prepared["user_text"],
            provider=prepared["provider"],
            model=prepared["model"],
            parameters=prepared["params"],
            initial_session=prepared["session"],
            accepted_callback=accepted,
            worker=_turn_worker(prepared),
            accept_command=accept_command,
            prompt_diagnostics=prepared["prompt_diagnostics"],
            turn_kind="regenerate",
        )
        handed_off = True
        return result
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    except MessageCommandError as exc:
        _raise_message_command_error(exc)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    finally:
        if not handed_off:
            await prepared["provider_lease"].release()


@router.post("/api/chat/turns", status_code=202)
async def api_create_turn(req: ChatRequest):
    return await _start_turn(req)


@router.post("/api/chat/turns/regenerate", status_code=202)
async def api_regenerate_turn(req: RegenerateRequest):
    return await _start_regenerated_turn(req)


@router.get("/api/chat/turns/{turn_id}")
async def api_get_turn(turn_id: str):
    try:
        return await get_turn_coordinator().get(turn_id)
    except TurnNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _sse(event: dict) -> str:
    event_type = event.get("type", "message")
    payload = json.dumps(event, ensure_ascii=False)
    return f"id: {event['id']}\nevent: {event_type}\ndata: {payload}\n\n"


@router.get("/api/chat/turns/{turn_id}/events")
async def api_turn_events(turn_id: str, request: Request, after: int = 0):
    header_id = request.headers.get("last-event-id")
    if header_id is not None:
        try:
            after = int(header_id)
        except ValueError as exc:
            raise HTTPException(400, "Last-Event-ID 必须是非负整数") from exc
    if after < 0:
        raise HTTPException(400, "after 必须是非负整数")
    coordinator = get_turn_coordinator()
    try:
        await coordinator.get(turn_id)
    except TurnNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    async def stream():
        try:
            async for event in coordinator.events(turn_id, after):
                if await request.is_disconnected():
                    await coordinator.cancel(turn_id)
                    return
                if event.get("type") == "keepalive":
                    yield ": keep-alive\n\n"
                else:
                    yield _sse(event)
        except asyncio.CancelledError:
            await asyncio.shield(coordinator.cancel(turn_id))
            raise
        finally:
            if await request.is_disconnected():
                await asyncio.shield(coordinator.cancel(turn_id))

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/chat/turns/{turn_id}/cancel")
async def api_cancel_turn(turn_id: str):
    try:
        return await get_turn_coordinator().cancel(turn_id)
    except TurnNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _compat_event(event: dict) -> dict | str | None:
    event_type = event.get("type")
    if event_type == "started":
        return None
    if event_type != "terminal":
        return {key: value for key, value in event.items() if key not in {"id", "created_at"}}
    status = event.get("status")
    if status == "completed":
        return "[DONE]"
    error = event.get("error") or {}
    if error.get("code") == "revision_conflict":
        return {
            "type": "conflict",
            "expected_revision": error.get("expected_revision"),
            "current_revision": error.get("current_revision"),
        }
    return {
        "type": "cancelled" if status == "cancelled" else "error",
        "content": error.get("message", "turn 未完成"),
        "code": error.get("code", "turn_failed"),
    }


@router.post("/api/chat")
async def api_chat(req: ChatRequest, request: Request):
    """兼容一个发布周期的旧 SSE 入口；内部统一走持久 turn。"""
    turn = await _start_turn(req)
    coordinator = get_turn_coordinator()

    async def generate():
        try:
            async for event in coordinator.events(turn["turn_id"]):
                if await request.is_disconnected():
                    await coordinator.cancel(turn["turn_id"])
                    return
                compatible = _compat_event(event)
                if compatible is None:
                    continue
                if compatible == "[DONE]":
                    yield "data: [DONE]\n\n"
                else:
                    yield f"data: {json.dumps(compatible, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            await asyncio.shield(coordinator.cancel(turn["turn_id"]))
            raise
        finally:
            if await request.is_disconnected():
                await asyncio.shield(coordinator.cancel(turn["turn_id"]))

    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/model/switch")
async def api_switch_model(req: Request):
    body = await _json_object(req)
    project = _norm_project(body.get("project", "默认项目"))
    save = _norm_save(body.get("save", "默认存档"))
    model = body.get("model", "")
    if not isinstance(model, str) or not model:
        raise HTTPException(
            400,
            detail={
                "code": "invalid_request_body",
                "message": "model 必须是非空字符串",
            },
        )
    expected_revision = _expected_revision(body)

    assert_write_allowed(project, save)
    session = await aload_session(project, save)
    if session.get("revision", 0) != expected_revision:
        _raise_revision_conflict(
            RevisionConflict(expected_revision, session.get("revision", 0), session)
        )
    provider_id = body.get("provider", session.get("current_provider", "ollama"))
    if not isinstance(provider_id, str):
        raise HTTPException(
            400,
            detail={
                "code": "invalid_request_body",
                "message": "provider 必须是字符串",
            },
        )
    try:
        registry = get_provider_registry()
        available = await registry.list_models(provider_id)
    except (ProviderError, SecretStoreError) as exc:
        raise_provider_error(exc)
    if model not in available:
        raise HTTPException(
            400,
            detail={
                "code": "provider_model_unavailable",
                "message": "所选模型在当前 Provider 中不可用",
                "provider_id": provider_id,
                "models": available,
            },
        )

    def switch_model(session: dict, context) -> None:
        session["current_provider"] = provider_id
        session["current_model"] = model

    try:
        mutation = await mutate_session(
            project,
            save,
            expected_revision,
            switch_model,
        )
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    return {
        "current_provider": provider_id,
        "current_model": model,
        "session": mutation.session,
    }
