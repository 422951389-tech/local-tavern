"""提示词编辑路由。"""
import asyncio

from fastapi import APIRouter, HTTPException, Request

from core.prompt_editor import (
    VALID_NAMES,
    read_prompt,
    reset_prompt_to_default,
    write_prompt,
)

router = APIRouter()


@router.get("/api/prompts")
async def api_get_prompts():
    return {name: read_prompt(name) for name in VALID_NAMES}


@router.put("/api/prompts/{name}")
async def api_save_prompt(name: str, req: Request):
    body = await req.json()
    content = body.get("content", "")
    if name not in VALID_NAMES:
        raise HTTPException(400, "未知 prompt 名")
    await asyncio.to_thread(write_prompt, name, content)
    return {"saved": True, "name": name}


@router.post("/api/prompts/{name}/reset")
async def api_reset_prompt(name: str):
    if name not in VALID_NAMES:
        raise HTTPException(400, "未知 prompt 名")
    try:
        content = await asyncio.to_thread(reset_prompt_to_default, name)
        return {"reset": True, "name": name, "content": content}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
