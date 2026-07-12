"""消息操作、历史快照与总结重生成路由。"""
import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Query

from core.ollama_client import get_client
from core.session_manager import (
    aload_session,
    save_session,
    toggle_pinned,
    count_pinned,
    _saves_dir,
    resolve_snapshot_path,
)
from routes.common import (
    MessageAction,
    _norm_save,
    _norm_project,
    _save_snapshot,
)

router = APIRouter()


def _validate_snapshot_owner(snapshot: dict, project: str, save: str, snapshot_type: str) -> None:
    session_id = snapshot.get("session_id")
    if session_id is not None and session_id != save:
        raise HTTPException(400, "快照内容不属于当前存档")
    snapshot_project = snapshot.get("project")
    if snapshot_project is not None and snapshot_project != project:
        raise HTTPException(400, "快照内容不属于当前项目")
    declared_type = snapshot.get("_snapshot_type")
    if declared_type and declared_type != snapshot_type:
        raise HTTPException(400, "快照内容类型与文件名不一致")


@router.get("/api/session")
async def api_get_session(project: str = Query("默认项目"), save: str = Query("默认存档")):
    save = _norm_save(save)
    project = _norm_project(project)
    return await aload_session(project, save)


@router.patch("/api/session")
async def api_patch_session(req: MessageAction, project: str = Query("默认项目"), save: str = Query("默认存档")):
    save = _norm_save(save)
    project = _norm_project(project)
    session = await aload_session(project, save)
    history = session.setdefault("message_history", [])

    if req.action == "delete":
        if req.index is None or req.index < 0 or req.index >= len(history):
            raise HTTPException(400, "无效的 index")
        history.pop(req.index)
    elif req.action == "edit":
        if req.index is None or req.index < 0 or req.index >= len(history):
            raise HTTPException(400, "无效的 index")
        if req.content is None:
            raise HTTPException(400, "缺少 content")
        history[req.index]["content"] = req.content
    elif req.action == "toggle_in_prompt":
        if req.index is None or req.index < 0 or req.index >= len(history):
            raise HTTPException(400, "无效的 index")
        history[req.index]["in_prompt"] = bool(req.in_prompt)
    elif req.action == "truncate":
        if req.index is None:
            raise HTTPException(400, "缺少 index")
        await _save_snapshot(session, project)
        session["message_history"] = history[:req.index]
    elif req.action == "snapshot":
        await _save_snapshot(session, project)
        return {"snapshotted": True}
    elif req.action == "toggle_pinned":
        if req.index is None or req.index < 0 or req.index >= len(history):
            raise HTTPException(400, "无效的 index")
        try:
            session = toggle_pinned(project, save, req.index)
        except IndexError:
            raise HTTPException(400, "index 超出消息历史范围")
        pinned_count = count_pinned(session)
        await save_session(session, project, save)
        return {"pinned_count": pinned_count, "index": req.index, "session": session}
    else:
        raise HTTPException(400, f"未知 action: {req.action}")

    await save_session(session, project, save)
    return session


@router.get("/api/session/history")
async def api_list_history(project: str = Query("默认项目"), save: str = Query("默认存档")):
    save = _norm_save(save)
    project = _norm_project(project)
    d = _saves_dir(project) / ".history"
    if not d.exists():
        return {"snapshots": []}
    snapshots = []
    for p in sorted(d.glob(f"{save}.*.json"), reverse=True):
        try:
            stem = p.stem
            if ".trim." in stem:
                snap_type = "trim"
                ts_str = stem.rsplit(".", 1)[-1]
            elif ".reset." in stem:
                snap_type = "reset"
                ts_str = stem.rsplit(".", 1)[-1]
            else:
                snap_type = "snapshot"
                ts_str = stem.rsplit(".", 1)[-1] if "." in stem else ""
            snapshots.append({
                "filename": p.name, "timestamp": ts_str, "type": snap_type,
                "modified_at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
            })
        except Exception:
            continue
    return {"snapshots": snapshots}


@router.get("/api/session/snapshot")
async def api_get_snapshot(project: str = Query("默认项目"), save: str = Query("默认存档"), filename: str = Query("")):
    """读取单个历史快照的只读内容（snapshot/reset/trim 全支持）。"""
    if not filename:
        raise HTTPException(400, "缺少 filename")
    save = _norm_save(save)
    project = _norm_project(project)
    try:
        path, snap_type = resolve_snapshot_path(project, save, filename)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.exists():
        raise HTTPException(404, "快照不存在")
    try:
        with open(path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(400, f"快照读取失败: {e}")

    _validate_snapshot_owner(snapshot, project, save, snap_type)

    messages = snapshot.get("message_history", [])
    if snap_type == "trim":
        messages = snapshot.get("dropped_messages", [])

    return {
        "filename": filename,
        "snapshot_type": snap_type,
        "session_id": snapshot.get("session_id", save),
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
        "messages": messages,
    }


@router.post("/api/session/restore")
async def api_restore_snapshot(req: Request):
    body = await req.json()
    project = _norm_project(body.get("project", "默认项目"))
    save = _norm_save(body.get("save", "默认存档"))
    filename = body.get("filename")
    if not filename:
        raise HTTPException(400, "缺少 filename")
    try:
        path, snap_type = resolve_snapshot_path(
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
        with open(path, "r", encoding="utf-8") as f:
            snapshot = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise HTTPException(400, f"快照读取失败: {e}")
    _validate_snapshot_owner(snapshot, project, save, snap_type)
    snapshot["session_id"] = save
    snapshot["project"] = project
    snapshot["updated_at"] = datetime.now().isoformat()
    await save_session(snapshot, project, save)
    return snapshot


@router.post("/api/session/summary/regenerate")
async def api_regenerate_summary(req: Request):
    """重新生成最新一段短期总结。"""
    body = await req.json()
    project = _norm_project(body.get("project", "默认项目"))
    save = _norm_save(body.get("save", "默认存档"))
    session = await aload_session(project, save)
    summaries = session.get("summaries", [])
    if not summaries:
        raise HTTPException(400, "尚无已生成的总结可重生成（summaries 为空）")

    d = _saves_dir(project) / ".history"
    if not d.exists():
        raise HTTPException(404, "无可用的 trim 快照，无法重生成总结")

    trim_files = sorted(d.glob(f"{save}.trim.*.json"), reverse=True)
    if not trim_files:
        raise HTTPException(404, "无 trim 快照（该段原文可能已被覆盖），无法重生成总结")
    with open(trim_files[0], "r", encoding="utf-8") as f:
        snap = json.load(f)
    dropped = snap.get("dropped_messages", [])
    if not dropped:
        raise HTTPException(400, "trim 快照中无被截消息，无法重生成总结")

    model = session.get("current_model", "")
    if not model:
        available = await get_client().list_models()
        if available:
            model = available[0]
    if not model:
        raise HTTPException(400, "存档未指定模型，无法重生成总结")

    try:
        raw = await get_client().summarize_once(model, dropped)
        from core.summary_parser import parse_summary
        ps = parse_summary(raw)
    except Exception as e:
        session["summary_error"] = f"重生成总结失败: {e}"
        await save_session(session, project, save)
        return {"error": str(e), "session": session}

    new_item = {
        "text": ps["text"],
        "time": ps["time"],
        "facts": ps["facts"],
        "relations": ps["relations"],
        "created_at": datetime.now().isoformat(),
    }
    if summaries:
        summaries[-1] = new_item
    else:
        summaries.append(new_item)
    session["summaries"] = summaries
    session["summary_error"] = ""
    await save_session(session, project, save)
    return {"ok": True, "session": session}


@router.patch("/api/session/summary")
async def api_patch_summary(req: Request):
    """编辑/修正某个总结条目。手动修改 text/facts/relations，清掉 failed 标记。"""
    body = await req.json()
    project = _norm_project(body.get("project", "默认项目"))
    save = _norm_save(body.get("save", "默认存档"))
    index = body.get("index")
    if index is None or not isinstance(index, int):
        raise HTTPException(400, "缺少或无效 index")

    session = await aload_session(project, save)
    summaries = session.get("summaries", [])
    if index < 0 or index >= len(summaries):
        raise HTTPException(400, f"index {index} 超出 summaries 范围 ({len(summaries)})")

    target = summaries[index]
    if body.get("text") is not None:
        target["text"] = body["text"]
    if body.get("time") is not None:
        target["time"] = body["time"]
    if body.get("facts") is not None:
        if isinstance(body["facts"], list):
            target["facts"] = [str(f)[:200] for f in body["facts"]]
    if body.get("relations") is not None:
        if isinstance(body["relations"], list):
            target["relations"] = [str(r)[:200] for r in body["relations"]]
    # 人工编辑后清掉失败标记
    target.pop("failed", None)
    target.pop("error", None)
    target["edited_at"] = datetime.now().isoformat()

    session["summaries"] = summaries
    await save_session(session, project, save)
    return {"ok": True, "session": session}
