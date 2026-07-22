"""消息操作、历史快照与总结重生成路由。"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from uuid import UUID, uuid4

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
from core.relationship_edges import (
    RelationshipEdgeError,
    reconcile_relationship_evidence,
    validate_relationship_edges,
)
from core.summary_lifecycle import (
    SummaryValidationError,
    annotate_summary_task_state,
    is_summary_generation_active,
    recompute_summary_error,
    schedule_summary_generation,
    validated_summary_patch,
)
from routes.common import (
    MessageAction,
    _expected_revision,
    _norm_project,
    _norm_save,
    _raise_revision_conflict,
)


router = APIRouter()


def _required_summary_id(body: dict) -> str:
    value = body.get("summary_id")
    if not isinstance(value, str) or not value:
        raise HTTPException(
            400,
            detail={
                "code": "summary_id_required",
                "message": "摘要命令必须提供稳定 summary_id",
            },
        )
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            400,
            detail={
                "code": "summary_id_invalid",
                "message": "summary_id 必须是 UUID",
            },
        ) from exc


def _require_summary_request_body(value: object) -> dict:
    if not isinstance(value, dict):
        raise HTTPException(
            400,
            detail={
                "code": "summary_request_invalid",
                "message": "摘要请求体必须是 JSON 对象",
            },
        )
    return value


def _find_summary(session: dict, summary_id: str) -> dict:
    target = next(
        (
            item
            for item in session.get("summaries", [])
            if item.get("id") == summary_id
        ),
        None,
    )
    if target is None:
        raise HTTPException(
            404,
            detail={
                "code": "summary_not_found",
                "message": "指定摘要不存在",
                "summary_id": summary_id,
            },
        )
    return target


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
    project = _norm_project(project)
    save = _norm_save(save)
    session = await aload_session(project, save)
    try:
        session["relationship_edges"] = validate_relationship_edges(session)
    except RelationshipEdgeError as exc:
        raise HTTPException(422, detail=exc.as_detail()) from exc
    return annotate_summary_task_state(session, project, save)


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
    if not req.message_id:
        raise HTTPException(
            400,
            detail={
                "code": "message_id_required",
                "message": "消息命令必须提供稳定 message_id",
            },
        )

    def apply_action(session: dict, context) -> dict:
        try:
            position, message = resolve_message(
                session,
                message_id=req.message_id,
            )
        except IndexError as exc:
            raise HTTPException(400, str(exc)) from exc

        history = session.setdefault("message_history", [])
        result: dict = {}
        if req.action == "delete":
            context.snapshot("snapshot", session)
            history.pop(position)
            reconcile_relationship_evidence(session)
        elif req.action == "edit":
            if req.content is None:
                raise HTTPException(400, "缺少 content")
            message["content"] = req.content
        elif req.action == "toggle_in_prompt":
            if req.in_prompt is None:
                raise HTTPException(400, "缺少 in_prompt")
            message["in_prompt"] = req.in_prompt
        elif req.action == "truncate":
            context.snapshot("snapshot", session)
            session["message_history"] = [
                *history[:position],
                *(
                    item
                    for item in history[position:]
                    if item.get("pinned")
                ),
            ]
            reconcile_relationship_evidence(session)
        elif req.action == "toggle_pinned":
            toggle_pinned(
                session,
                message_id=req.message_id,
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
    replacement.pop("_snapshot_at", None)
    replacement.pop("_snapshot_type", None)

    def restore(session: dict, context) -> None:
        context.checkpoint(
            "restore",
            metadata={"snapshot_filename": filename},
        )
        session.clear()
        session.update(deepcopy(replacement))

    try:
        mutation = await mutate_session(
            project,
            save,
            expected_revision,
            restore,
        )
        return {
            "session": mutation.session,
            "recovery_id": (
                mutation.recovery_ids[-1]
                if mutation.recovery_ids
                else None
            ),
        }
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)


@router.post("/api/session/summary/regenerate", status_code=202)
async def api_regenerate_summary(req: Request):
    body = _require_summary_request_body(await req.json())
    project = _norm_project(body.get("project", "默认项目"))
    save = _norm_save(body.get("save", "默认存档"))
    expected_revision = _expected_revision(body)
    summary_id = _required_summary_id(body)
    session = await aload_session(project, save)
    _precheck_revision(session, expected_revision)
    target = _find_summary(session, summary_id)
    current_generation_id = target.get("generation_id")
    if target.get("status") == "pending" and is_summary_generation_active(
        project,
        save,
        summary_id,
        current_generation_id if isinstance(current_generation_id, str) else None,
    ):
        raise HTTPException(
            409,
            detail={
                "code": "summary_generation_pending",
                "message": "该摘要正在生成，请等待当前任务完成",
                "summary_id": summary_id,
            },
        )

    source_snapshot_id = target.get("source_snapshot_id")
    if not isinstance(source_snapshot_id, str) or not source_snapshot_id:
        raise HTTPException(
            409,
            detail={
                "code": "summary_source_unlinked",
                "message": "该摘要没有绑定原文 trim 快照，不能重生成",
                "summary_id": summary_id,
            },
        )
    if sum(
        1
        for item in session.get("summaries", [])
        if item.get("source_snapshot_id") == source_snapshot_id
    ) != 1:
        raise HTTPException(
            409,
            detail={
                "code": "summary_source_ambiguous",
                "message": "多个摘要绑定了同一原文 trim 快照",
                "summary_id": summary_id,
            },
        )
    try:
        snapshot_path, snapshot_type = resolve_snapshot_path(
            project,
            save,
            source_snapshot_id,
            allowed_types=("trim",),
        )
    except ValueError as exc:
        raise HTTPException(
            409,
            detail={
                "code": "summary_source_invalid",
                "message": "摘要绑定的原文快照标识无效",
                "summary_id": summary_id,
            },
        ) from exc
    if not snapshot_path.is_file():
        raise HTTPException(
            409,
            detail={
                "code": "summary_source_missing",
                "message": "摘要绑定的原文 trim 快照已缺失",
                "summary_id": summary_id,
                "source_snapshot_id": source_snapshot_id,
            },
        )
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(
            422,
            detail={
                "code": "summary_source_corrupt",
                "message": "摘要绑定的原文 trim 快照无法读取",
                "summary_id": summary_id,
            },
        ) from exc
    if not isinstance(snapshot, dict):
        raise HTTPException(
            422,
            detail={
                "code": "summary_source_corrupt",
                "message": "摘要绑定的原文 trim 快照格式无效",
                "summary_id": summary_id,
            },
        )
    _validate_snapshot_owner(snapshot, project, save, snapshot_type)
    snapshot_summary_id = snapshot.get("summary_id")
    if snapshot_summary_id != summary_id:
        raise HTTPException(
            409,
            detail={
                "code": "summary_source_mismatch",
                "message": "原文 trim 快照绑定了其他摘要",
                "summary_id": summary_id,
            },
        )
    dropped = snapshot.get("dropped_messages", [])
    if (
        not isinstance(dropped, list)
        or not dropped
        or any(not isinstance(item, dict) for item in dropped)
    ):
        raise HTTPException(
            422,
            detail={
                "code": "summary_source_empty",
                "message": "摘要绑定的 trim 快照没有有效被截消息",
                "summary_id": summary_id,
            },
        )

    model = session.get("current_model", "")
    if not model:
        available = await get_client().list_models()
        model = available[0] if available else ""
    if not model:
        raise HTTPException(400, "存档未指定模型，无法重生成总结")

    generation_id = str(uuid4())
    requested_at = datetime.now().astimezone().isoformat()

    def mark_pending(current: dict, context) -> dict:
        del context
        item = _find_summary(current, summary_id)
        attempt = item.get("generation_attempt", 0)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
            attempt = 0
        item.update({
            "status": "pending",
            "generation_id": generation_id,
            "generation_attempt": attempt + 1,
            "requested_at": requested_at,
            "source_status": "available",
            "error": None,
        })
        item.pop("failed", None)
        recompute_summary_error(current)
        return deepcopy(item)

    try:
        mutation = await mutate_session(
            project,
            save,
            expected_revision,
            mark_pending,
        )
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    schedule_summary_generation(
        project,
        save,
        model,
        summary_id,
        generation_id,
        dropped,
    )
    public_session = annotate_summary_task_state(
        mutation.session,
        project,
        save,
    )
    return {
        "accepted": True,
        "summary_id": summary_id,
        "generation_id": generation_id,
        "session": public_session,
    }


@router.patch("/api/session/summary")
async def api_patch_summary(req: Request):
    body = _require_summary_request_body(await req.json())
    project = _norm_project(body.get("project", "默认项目"))
    save = _norm_save(body.get("save", "默认存档"))
    expected_revision = _expected_revision(body)
    summary_id = _required_summary_id(body)

    def patch_summary(session: dict, context) -> None:
        del context
        target = _find_summary(session, summary_id)
        updated = validated_summary_patch(body, target)
        target.update(updated)
        target["status"] = "completed"
        target["content_status"] = "valid"
        target.pop("failed", None)
        target.pop("generation_id", None)
        target["error"] = None
        target["edited_at"] = datetime.now().astimezone().isoformat()
        recompute_summary_error(session)

    try:
        mutation = await mutate_session(
            project,
            save,
            expected_revision,
            patch_summary,
        )
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    except SummaryValidationError as exc:
        raise HTTPException(422, detail=exc.as_detail()) from exc
    return {"ok": True, "session": mutation.session}
