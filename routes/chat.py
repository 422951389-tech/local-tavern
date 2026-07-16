"""聊天与模型切换路由。"""
import asyncio
import json
import logging
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from core.chat_turns import TurnNotFound, TurnRuntime, get_turn_coordinator
from core.ollama_client import get_client
from core.character_loader import load_character, load_user_profile, load_worldbook
from core.config import MAX_MESSAGES_IN_SAVE
from core.message_commands import (
    MessageCommandError,
    MessageNotFound,
    plan_message_regeneration,
)
from core.prompt_assembler import (
    PromptAssembler,
    PromptBudgetExceeded,
    PromptTemplateInvalid,
)
from core.response_parser import parse_response, check_voice_confusion
from core.summary_lifecycle import (
    schedule_summary_generation,
    shutdown_summary_tasks,
)
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
    _norm_save,
    _norm_project,
    _initialize_session_from_profiles,
    _expected_revision,
    _raise_revision_conflict,
    apply_character_state,
)

logger = logging.getLogger(__name__)
router = APIRouter()
_prompt_assembler = PromptAssembler()


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
        await _initialize_session_from_profiles(session, project)

    model = req.model or session.get("current_model")
    if not model:
        raise HTTPException(400, "未指定模型")

    ollama = get_client()
    available = await ollama.list_models()
    if model not in available:
        raise HTTPException(400, f"模型 {model} 不可用。可用：{available}")

    params = _validated_parameters(req)

    characters = [load_character(project, cid) for cid in session.get("characters_state", {}).keys()]
    characters = [c for c in characters if c]

    # B2：建立角色卡 name -> cid 反向映射
    char_name_to_cid = {}
    for c in characters:
        cid = c.get("id")
        name = c.get("name", "")
        if cid and name:
            char_name_to_cid[name] = cid

    user_profile = load_user_profile(project)
    all_entries = load_worldbook(project)
    wb_entries = [e for e in all_entries if e.get("enabled", True)]
    history = (
        deepcopy(history_override)
        if history_override is not None
        else session.get("message_history", [])
    )

    context_info = await ollama.get_context_limit(model)
    try:
        assembly = _prompt_assembler.assemble(
            user_input=user_text,
            characters=characters,
            characters_state=session.get("characters_state", {}),
            scene_meta=session.get("scene_meta", {}),
            user_profile=user_profile,
            worldbook_entries=wb_entries,
            history=history,
            summaries=session.get("summaries", []),
            context_limit=context_info["context_limit"],
            context_limit_source=context_info["source"],
            num_predict=params["num_predict"],
        )
    except (PromptBudgetExceeded, PromptTemplateInvalid) as exc:
        raise HTTPException(422, detail=exc.as_detail()) from exc
    # 与预算计算共用同一有效窗口，禁止 Ollama 按更小默认 num_ctx 静默截断。
    params["num_ctx"] = assembly.diagnostics["context_limit"]

    return {
        "project": project,
        "save": save,
        "user_text": user_text,
        "expected_revision": req.expected_revision,
        "model": model,
        "params": params,
        "session": deepcopy(session),
        "characters": characters,
        "char_name_to_cid": char_name_to_cid,
        "messages": assembly.messages,
        "prompt_diagnostics": assembly.diagnostics,
    }


def _message_metadata(
    turn_id: str,
    status: str,
    error: dict | None,
    *,
    in_prompt: bool,
    created_at: str,
) -> dict:
    return {
        "turn_id": turn_id,
        "status": status,
        "error": deepcopy(error),
        "timestamps": {
            "created_at": created_at,
            "completed_at": datetime.now().astimezone().isoformat(),
        },
        "in_prompt": in_prompt,
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
            ),
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
    characters: list[dict],
    char_name_to_cid: dict[str, str],
) -> None:
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
        name = character.get("name", "")
        matched = False
        for state in session.get("characters_state", {}).values():
            if state.get("name") == name:
                apply_character_state(state, character)
                matched = True
                break
        if not matched:
            cid = char_name_to_cid.get(name)
            if cid and cid in session.get("characters_state", {}):
                apply_character_state(session["characters_state"][cid], character)
                matched = True
                logger.info("角色「%s」通过 name→cid(%s) 映射写回状态", name, cid)
        if not matched and name:
            logger.info("解析到角色「%s」但 characters_state 无匹配，已跳过", name)


async def _commit_partial(
    prepared: dict,
    turn_id: str,
    content: str,
    thinking: str,
    status: str,
    error: dict,
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
    parsed = parse_response(content)
    try:
        warnings = check_voice_confusion(parsed, prepared["characters"])
        if warnings:
            parsed["warnings"] = warnings
    except Exception:
        logger.exception("角色声线校验失败，不阻断 turn 提交")

    _apply_parsed_state(
        session,
        parsed,
        prepared["characters"],
        prepared["char_name_to_cid"],
    )
    _user, _assistant, dropped = _finalize_turn_messages(
        session,
        prepared,
        runtime.turn_id,
        content,
        thinking,
        "completed",
        None,
    )

    session["current_model"] = prepared["model"]
    summary_id = str(uuid4()) if dropped else None
    summary_generation_id = str(uuid4()) if dropped else None

    def commit_chat(current: dict, context) -> str | None:
        current.clear()
        current.update(deepcopy(session))
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
    parsed_event = {
        "type": "parsed",
        "parsed": parsed,
        "session": mutation.session,
    }
    await runtime.emit(parsed_event, session_revision=mutation.session["revision"])
    await runtime.terminal(
        "completed",
        session_revision=mutation.session["revision"],
    )
    if dropped and summary_id and summary_generation_id:
        schedule_summary_generation(
            prepared["project"],
            prepared["save"],
            prepared["model"],
            summary_id,
            summary_generation_id,
            dropped,
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
    committed = None
    try:
        committed = await _commit_partial(
            prepared,
            runtime.turn_id,
            content,
            thinking,
            status,
            error,
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
        )
    updates = {}
    if committed is not None:
        updates["session_revision"] = committed["revision"]
    await runtime.terminal(status, error=error, **updates)


def _turn_worker(prepared: dict):
    async def run(runtime: TurnRuntime) -> None:
        full_content = ""
        full_thinking = ""
        pending_type = ""
        pending_text = ""
        last_flush = asyncio.get_running_loop().time()
        emitted_stream_event = False

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

        await runtime.emit({"type": "started"})
        try:
            async for chunk in get_client().chat_stream(
                model=prepared["model"],
                messages=prepared["messages"],
                think=prepared["params"]["think"],
                num_predict=prepared["params"]["num_predict"],
                num_ctx=prepared["params"]["num_ctx"],
                temperature=prepared["params"]["temperature"],
                top_p=prepared["params"]["top_p"],
                top_k=prepared["params"]["top_k"],
            ):
                if chunk["type"] == "thinking":
                    full_thinking += chunk["content"]
                    await buffer_stream("thinking", chunk["content"])
                elif chunk["type"] == "content":
                    full_content += chunk["content"]
                    await buffer_stream("content", chunk["content"])
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
        except Exception as exc:
            logger.exception("turn 生成异常")
            await _fail_turn(
                runtime,
                prepared,
                full_content,
                full_thinking,
                status="failed",
                error={"code": "internal_error", "message": str(exc)},
            )

    return run


async def _start_turn(req: ChatRequest) -> dict:
    prepared = await _prepare_turn(req)
    coordinator = get_turn_coordinator()

    def accepted(session: dict) -> None:
        prepared["session"] = session
        prepared["expected_revision"] = session["revision"]

    try:
        return await coordinator.start(
            project=prepared["project"],
            save=prepared["save"],
            expected_revision=prepared["expected_revision"],
            user_input=prepared["user_text"],
            model=prepared["model"],
            parameters=prepared["params"],
            initial_session=prepared["session"],
            accepted_callback=accepted,
            worker=_turn_worker(prepared),
            prompt_diagnostics=prepared["prompt_diagnostics"],
        )
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)


def _raise_message_command_error(exc: MessageCommandError) -> None:
    status_code = 404 if isinstance(exc, MessageNotFound) else 422
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
        user_text_override=plan.user_input,
        session_override=session,
        history_override=plan.prompt_history,
    )
    coordinator = get_turn_coordinator()
    store = get_session_store()

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
        return await coordinator.start(
            project=prepared["project"],
            save=prepared["save"],
            expected_revision=prepared["expected_revision"],
            user_input=prepared["user_text"],
            model=prepared["model"],
            parameters=prepared["params"],
            initial_session=prepared["session"],
            accepted_callback=accepted,
            worker=_turn_worker(prepared),
            accept_command=accept_command,
            prompt_diagnostics=prepared["prompt_diagnostics"],
        )
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    except MessageCommandError as exc:
        _raise_message_command_error(exc)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


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
    body = await req.json()
    project = _norm_project(body.get("project", "默认项目"))
    save = _norm_save(body.get("save", "默认存档"))
    model = body.get("model", "")
    expected_revision = _expected_revision(body)

    def switch_model(session: dict, context) -> None:
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
    return {"current_model": model, "session": mutation.session}
