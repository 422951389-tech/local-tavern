"""用户档案路由。"""
from fastapi import APIRouter, HTTPException, Request, Query

from core.character_loader import load_user_profile, save_user_profile, delete_user_profile
from core.session_manager import RevisionConflict, mutate_session
from routes.common import _norm_save, _norm_project, _raise_revision_conflict

router = APIRouter()


@router.get("/api/user")
async def api_get_user(project: str = Query("默认项目")):
    project = _norm_project(project)
    return load_user_profile(project)


@router.put("/api/user")
async def api_save_user(req: Request, project: str = Query("默认项目")):
    project = _norm_project(project)
    body = await req.json()
    data = body.get("data", {})
    try:
        save_user_profile(project, data)
        return {"saved": True, "id": data.get("id", "user")}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/api/user")
async def api_delete_user(
    project: str = Query("默认项目"),
    save: str = Query(None),
    expected_revision: int = Query(..., ge=0),
):
    project = _norm_project(project)
    save_id = _norm_save(save) if save else "默认存档"

    def remove_user(session: dict, context) -> None:
        if not delete_user_profile(project, session):
            raise HTTPException(404, "用户档案不存在")

    try:
        mutation = await mutate_session(
            project,
            save_id,
            expected_revision,
            remove_user,
        )
    except RevisionConflict as exc:
        _raise_revision_conflict(exc)
    return {"deleted": True, "session": mutation.session}
