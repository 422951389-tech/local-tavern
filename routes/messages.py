"""消息操作、历史快照与总结重生成路由。"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request

from core.ollama_client import get_client
from core.session_manager import (
    RevisionConflict,
    _saves_dir,
    aload_session,
    count_pinned,
    mutate_session,
    resolve_message,
    resolve_snapshot_path,
    snapshot_session,
    toggle_pinned,
)
from routes.common import (
    MessageAction,
    _expected_revision,
    _norm_project,
    _norm_save,
    _raise_revision_conflict,
)


router = APIRouter()


def _validate_snapshot_owner(
    snapshot: dict,
    project: str,
    save: str,
    snapshot_type: str,
) -> None:
    session_id = snapshot.get("session_id")
    if session_id is not None and session_id != save:
        raise HTTPException(400, "快照内容不属于当前存档")
    snapshot_project = snapshot.get("project")
    if snapshot_project is not None and snapshot_project != project:
        raise HTTPException(400, "快照内容不属于当前项目")
    declared_type = snapshot.get("_snapshot_type")
    if declared_type and declared_type != snapshot_type:
        raise HTTPException(400, "快照内容类型与文件名不一致")


def _precheck_revision(session: dict, expected_revision: int) -> None:
    current = session.get("revision", 0)
    if current != expected_revision:
        _raise_revision_conflict(
            RevisionConflict(expected_revision, current, session)
        )


@router.get("/api/session")
async def api_get_session(
    project: str = Query("默认项目"),
    save: str = Query("默认存档"),
):
    return await aload_session(_norm_project(project), _norm_save(save))


@router.patch("/api/session")
async def api_patch_session(
    req: MessageAction,
    project: str = Query("默认项目"),
    save: str = Query("默认存档"),
):
    project = _norm_project(project)
    save = _norm_save(save)

    if req.action == "snapshot":
        try:
            session, path = await snapshot_session(
                project,
                save,
                req.expected_revision,
            )
            return {
                "snapshotted": True,
                "filename": path.name,
                "session": session,
            }
        except RevisionConflict as exc:
            _raise_revision_conflict(exc)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    allowed_actions = {
        "delete",
        "edit",
        "toggle_in_prompt",
        "truncate",
        "toggle_pinned",
    }
    if req.action not in allowed_actions:
        raise HTTPException(400, f"未知 action: {req.action}")

    def apply_action(session: dict, context) -> dict:
        try:
            position, message = resolve_message(
                session,
                message_id=req.message_id,
                index=req.index,
            )
        except IndexError as exc:
            raise HTTPException(400, str(exc)) from exc

        history = session.setdefault("message_history", [])
        result: dict = {}
        if req.action == "delete":
            history.pop(position)
        elif req.action == "edit":
            if req.content is None:
                raise HTTPException(400, "缺少 content")
            message["content"] = req.content
        elif req.action == "toggle_in_prompt":
            message["in_prompt"] = bool(req.in_prompt)
        elif req.action == "truncate":
            context.snapshot("snapshot", session)
            session["message_history"] = history[:position]
        elif req.action == "toggle_pinned":
            toggle_pinned(
                session,
                message_id=req.message_id,
                index=req.index,
            )
            result["pinned_count"] = count_pinned(session)
            result["message_id"] = message.get("id")
        return result

    try:
        mutation = await mutate_session(
            project,
            save,
            req.expected_revision,
            apply_action,
        )
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)

    if req.action == "toggle_pinned":
        return {
            **mutation.value,
            "session": mutation.session,
        }
    return mutation.session


@router.get("/api/session/history")
async def api_list_history(
    project: str = Query("默认项目"),
    save: str = Query("默认存档"),
):
    save = _norm_save(save)
    project = _norm_project(project)
    history_dir = _saves_dir(project) / ".history"
    if not history_dir.exists():
        return {"snapshots": []}
    snapshots = []
    for path in sorted(history_dir.glob(f"{save}.*.json"), reverse=True):
        try:
            if ".trim." in path.name:
                snapshot_type = "trim"
                timestamp = path.name.split(".trim.", 1)[1].removesuffix(".json")
            elif ".reset." in path.name:
                snapshot_type = "reset"
                timestamp = path.name.split(".reset.", 1)[1].removesuffix(".json")
            else:
                snapshot_type = "snapshot"
                timestamp = path.name[len(save) + 1:].removesuffix(".json")
            snapshots.append({
                "filename": path.name,
                "timestamp": timestamp,
                "type": snapshot_type,
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
            })
        except (OSError, ValueError):
            continue
    return {"snapshots": snapshots}


@router.get("/api/session/snapshot")
async def api_get_snapshot(
    project: str = Query("默认项目"),
    save: str = Query("默认存档"),
    filename: str = Query(""),
):
    if not filename:
        raise HTTPException(400, "缺少 filename")
    save = _norm_save(save)
    project = _norm_project(project)
    try:
        path, snapshot_type = resolve_snapshot_path(project, save, filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.exists():
        raise HTTPException(404, "快照不存在")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(400, f"快照读取失败: {exc}") from exc
    _validate_snapshot_owner(snapshot, project, save, snapshot_type)
    messages = (
        snapshot.get("dropped_messages", [])
        if snapshot_type == "trim"
        else snapshot.get("message_history", [])
    )
    return {
        "filename": filename,
        "snapshot_type": snapshot_type,
        "session_id": snapshot.get("session_id", save),
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
        "messages": messages,
    }


@router.post("/api/session/restore")
async def api_restore_snapshot(req: Request):
    body = await req.json()
    project = _norm_project(body.get("project", "默认项目"))
    save = _norm_save(body.get("save", "默认存档"))
    expected_revision = _expected_revision(body)
    filename = body.get("filename")
    if not filename:
        raise HTTPException(400, "缺少 filename")
    try:
        path, snapshot_type = resolve_snapshot_path(
            project,
            save,
            filename,
            allowed_types=("snapshot", "reset"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.exists():
        raise HTTPException(404, "快照不存在")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(400, f"快照读取失败: {exc}") from exc
    _validate_snapshot_owner(snapshot, project, save, snapshot_type)
    replacement = deepcopy(snapshot)

    def restore(session: dict, context) -> None:
        session.clear()
        session.update(deepcopy(replacement))

    try:
        return (
            await mutate_session(
                project,
                save,
                expected_revision,
                restore,
            )
        ).session
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)


@router.post("/api/session/summary/regenerate")
async def api_regenerate_summary(req: Request):
    body = await req.json()
    project = _norm_project(body.get("project", "默认项目"))
    save = _norm_save(body.get("save", "默认存档"))
    expected_revision = _expected_revision(body)
    session = await aload_session(project, save)
    _precheck_revision(session, expected_revision)
    if not session.get("summaries"):
        raise HTTPException(400, "尚无已生成的总结可重生成（summaries 为空）")

    history_dir = _saves_dir(project) / ".history"
    trim_files = (
        sorted(history_dir.glob(f"{save}.trim.*.json"), reverse=True)
        if history_dir.exists()
        else []
    )
    if not trim_files:
        raise HTTPException(404, "无可用的 trim 快照，无法重生成总结")
    snapshot = json.loads(trim_files[0].read_text(encoding="utf-8"))
    dropped = snapshot.get("dropped_messages", [])
    if not dropped:
        raise HTTPException(400, "trim 快照中无被截消息，无法重生成总结")

    model = session.get("current_model", "")
    if not model:
        available = await get_client().list_models()
        model = available[0] if available else ""
    if not model:
        raise HTTPException(400, "存档未指定模型，无法重生成总结")

    try:
        raw = await get_client().summarize_once(model, dropped)
        from core.summary_parser import parse_summary

        parsed = parse_summary(raw)
    except Exception as exc:
        error_text = str(exc)

        def record_error(current: dict, context) -> None:
            current["summary_error"] = f"重生成总结失败: {error_text}"

        try:
            failed = await mutate_session(
                project,
                save,
                expected_revision,
                record_error,
            )
        except RevisionConflict as conflict:
            _raise_revision_conflict(conflict)
        return {"error": error_text, "session": failed.session}

    new_item = {
        "text": parsed["text"],
        "time": parsed["time"],
        "facts": parsed["facts"],
        "relations": parsed["relations"],
        "created_at": datetime.now().isoformat(),
    }

    def replace_summary(current: dict, context) -> None:
        summaries = current.setdefault("summaries", [])
        if not summaries:
            raise HTTPException(409, "总结已被其他操作移除")
        summaries[-1] = deepcopy(new_item)
        current["summary_error"] = ""

    try:
        mutation = await mutate_session(
            project,
            save,
            expected_revision,
            replace_summary,
        )
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    return {"ok": True, "session": mutation.session}


@router.patch("/api/session/summary")
async def api_patch_summary(req: Request):
    body = await req.json()
    project = _norm_project(body.get("project", "默认项目"))
    save = _norm_save(body.get("save", "默认存档"))
    expected_revision = _expected_revision(body)
    index = body.get("index")
    if isinstance(index, bool) or not isinstance(index, int):
        raise HTTPException(400, "缺少或无效 index")

    def patch_summary(session: dict, context) -> None:
        summaries = session.setdefault("summaries", [])
        if index < 0 or index >= len(summaries):
            raise HTTPException(400, f"index {index} 超出 summaries 范围 ({len(summaries)})")
        target = summaries[index]
        if body.get("text") is not None:
            target["text"] = body["text"]
        if body.get("time") is not None:
            target["time"] = body["time"]
        if isinstance(body.get("facts"), list):
            target["facts"] = [str(fact)[:200] for fact in body["facts"]]
        if isinstance(body.get("relations"), list):
            target["relations"] = [str(relation)[:200] for relation in body["relations"]]
        target.pop("failed", None)
        target.pop("error", None)
        target["edited_at"] = datetime.now().isoformat()

    try:
        mutation = await mutate_session(
            project,
            save,
            expected_revision,
            patch_summary,
        )
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    return {"ok": True, "session": mutation.session}
