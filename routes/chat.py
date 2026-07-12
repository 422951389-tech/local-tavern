"""聊天与模型切换路由。"""
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from core.ollama_client import get_client
from core.character_loader import load_character, load_user_profile, load_worldbook
from core.config import MAX_MESSAGES_IN_SAVE
from core.prompt_builder import build_messages
from core.response_parser import parse_response, check_voice_confusion
from core.session_manager import (
    aload_session,
    save_session,
    append_history,
    trim_history,
)
from routes.common import (
    ChatRequest,
    _norm_save,
    _norm_project,
    _initialize_session_from_profiles,
    apply_character_state,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/chat")
async def api_chat(req: ChatRequest):
    user_text = (req.user_input or "").strip()
    if not user_text:
        raise HTTPException(400, "用户输入不能为空")
    project = _norm_project(req.project)
    save = _norm_save(req.save)
    session = await aload_session(project, save)

    if not session.get("characters_state") and not session.get("message_history"):
        await _initialize_session_from_profiles(session, project)

    model = req.model or session.get("current_model")
    if not model:
        raise HTTPException(400, "未指定模型")

    available = await get_client().list_models()
    if model not in available:
        raise HTTPException(400, f"模型 {model} 不可用。可用：{available}")

    params = {"temperature": 0.8, "num_predict": 4096, "think": True, "top_p": None, "top_k": None}
    # 逐个校验参数，无效值记录日志并回退默认值（不静默丢弃）
    if req.temperature is not None:
        try:
            t = float(req.temperature)
            if 0.0 <= t <= 2.0:
                params["temperature"] = t
            else:
                logger.warning("temperature=%s 超出范围 [0.0, 2.0]，使用默认值 0.8", req.temperature)
        except (TypeError, ValueError):
            logger.warning("无效 temperature 值: %s，使用默认值 0.8", req.temperature)
    if req.num_predict is not None:
        try:
            n = int(req.num_predict)
            if 1 <= n <= 32768:
                params["num_predict"] = n
            else:
                logger.warning("num_predict=%s 超出范围 [1, 32768]，使用默认值 4096", req.num_predict)
        except (TypeError, ValueError):
            logger.warning("无效 num_predict 值: %s，使用默认值 4096", req.num_predict)
    if req.think is not None:
        params["think"] = bool(req.think)
    if req.top_p is not None:
        try:
            t = float(req.top_p)
            if 0.0 <= t <= 1.0:
                params["top_p"] = t
            else:
                logger.warning("top_p=%s 超出范围 [0.0, 1.0]，不使用 top_p", req.top_p)
        except (TypeError, ValueError):
            logger.warning("无效 top_p 值: %s，不使用 top_p", req.top_p)
    if req.top_k is not None:
        try:
            k = int(req.top_k)
            if 0 <= k <= 1000:
                params["top_k"] = k
            else:
                logger.warning("top_k=%s 超出范围 [0, 1000]，不使用 top_k", req.top_k)
        except (TypeError, ValueError):
            logger.warning("无效 top_k 值: %s，不使用 top_k", req.top_k)

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
    history = session.get("message_history", [])

    messages = build_messages(
        user_input=user_text,
        characters=characters,
        characters_state=session.get("characters_state", {}),
        scene_meta=session.get("scene_meta", {}),
        user_profile=user_profile,
        worldbook_entries=wb_entries,
        history=history,
        summaries=session.get("summaries", []),
    )

    async def generate():
        full_content = ""
        full_thinking = ""
        assistant_appended = False
        try:
            async for chunk in get_client().chat_stream(
                model=model, messages=messages,
                think=params["think"], num_predict=params["num_predict"],
                temperature=params["temperature"], top_p=params["top_p"], top_k=params["top_k"],
            ):
                if chunk["type"] == "thinking":
                    full_thinking += chunk["content"]
                    yield f"data: {json.dumps({'type': 'thinking', 'content': chunk['content']}, ensure_ascii=False)}\n\n"
                elif chunk["type"] == "content":
                    full_content += chunk["content"]
                    yield f"data: {json.dumps({'type': 'content', 'content': chunk['content']}, ensure_ascii=False)}\n\n"
                elif chunk["type"] == "error":
                    yield f"data: {json.dumps({'type': 'error', 'content': chunk['content']}, ensure_ascii=False)}\n\n"
                    return
                elif chunk["type"] == "done":
                    parsed = parse_response(full_content)
                    try:
                        warnings = check_voice_confusion(parsed, characters)
                        if warnings:
                            parsed["warnings"] = warnings
                    except Exception:
                        pass

                    if parsed.get("scene_meta"):
                        for k, v in parsed["scene_meta"].items():
                            if v and k != "user_line":
                                if k == "time_weather":
                                    session["scene_meta"]["time"] = v.split("/")[0].strip() if "/" in v else v
                                    if "/" in v:
                                        session["scene_meta"]["weather"] = v.split("/")[1].strip()
                                elif k in session["scene_meta"]:
                                    session["scene_meta"][k] = v

                    for c in parsed.get("characters", []):
                        cname = c.get("name", "")
                        matched = False
                        for cid, state in session.get("characters_state", {}).items():
                            if state.get("name") == cname:
                                apply_character_state(state, c)
                                matched = True
                                break
                        if not matched:
                            cid = char_name_to_cid.get(cname)
                            if cid and cid in session.get("characters_state", {}):
                                state = session["characters_state"][cid]
                                apply_character_state(state, c)
                                matched = True
                                logger.info("角色「%s」通过 name→cid(%s) 映射写回状态", cname, cid)
                        if not matched and cname:
                            logger.info("解析到角色「%s」但 characters_state 无匹配，已跳过", cname)

                    if not assistant_appended:
                        append_history(session, "user", user_text)
                        append_history(session, "assistant", full_content, full_thinking)
                        assistant_appended = True

                        new_summary_event = None
                        try:
                            dropped = trim_history(session, max_messages=MAX_MESSAGES_IN_SAVE, project=project)
                            if dropped:
                                try:
                                    raw = await get_client().summarize_once(model, dropped)
                                    from core.summary_parser import parse_summary
                                    parsed_sum = parse_summary(raw)
                                    import_index = len(session.get("summaries", []))
                                    session.setdefault("summaries", []).append({
                                        "text": parsed_sum["text"],
                                        "time": parsed_sum["time"],
                                        "facts": parsed_sum["facts"],
                                        "relations": parsed_sum["relations"],
                                        "created_at": datetime.now().isoformat(),
                                    })
                                    session["summary_error"] = ""
                                    new_summary_event = {"ok": True, "summary": session["summaries"][-1], "index": import_index}
                                except Exception as se:
                                    logger.warning("短期总结失败，不影响聊天: %s", se)
                                    session["summary_error"] = f"总结生成失败: {se}"
                                    import_index = len(session.get("summaries", []))
                                    session.setdefault("summaries", []).append({
                                        "text": "（总结生成失败，原文已归档）",
                                        "time": "",
                                        "facts": [],
                                        "relations": [],
                                        "failed": True,
                                        "created_at": datetime.now().isoformat(),
                                        "error": str(se),
                                    })
                                    new_summary_event = {"ok": False, "error": str(se), "index": import_index}
                        except Exception as se2:
                            logger.warning("短期总结流程异常: %s", se2)
                            session["summary_error"] = f"总结流程异常: {se2}"

                        session["current_model"] = model

                    summary_payload = {
                        "type": "parsed",
                        "parsed": parsed,
                        "session": dict(session),
                    }
                    if new_summary_event:
                        summary_payload["summary_event"] = new_summary_event
                    yield f"data: {json.dumps(summary_payload, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
        except GeneratorExit:
            logger.info("客户端中断 SSE 连接")
        except Exception as e:
            logger.exception("生成异常")
            try:
                yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"
            except (GeneratorExit, Exception):
                pass
        finally:
            try:
                if assistant_appended:
                    await save_session(session, project, save)
            except Exception as save_err:
                logger.error("最终保存失败: %s", save_err)

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
    session = await aload_session(project, save)
    session["current_model"] = model
    await save_session(session, project, save)
    return {"current_model": model}
