"""提示词编辑路由。"""
from fastapi import APIRouter, HTTPException, Request

from core.prompt_editor import read_prompt, write_prompt, reset_prompt_to_default

router = APIRouter()


@router.get("/api/prompts")
async def api_get_prompts():
    return {
        "system": read_prompt("system"),
        "group_chat": read_prompt("group_chat"),
    }


@router.put("/api/prompts/{name}")
async def api_save_prompt(name: str, req: Request):
    body = await req.json()
    content = body.get("content", "")
    if name not in ("system", "group_chat"):
        raise HTTPException(400, "未知 prompt 名")
    write_prompt(name, content)
    return {"saved": True, "name": name}


@router.post("/api/prompts/{name}/reset")
async def api_reset_prompt(name: str):
    if name not in ("system", "group_chat"):
        raise HTTPException(400, "未知 prompt 名")
    try:
        return {"reset": True, "name": name, "content": reset_prompt_to_default(name)}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
