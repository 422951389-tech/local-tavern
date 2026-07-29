"""提示词编辑路由。"""

import asyncio

from fastapi import APIRouter, HTTPException, Request

from core.prompt_editor import (
    VALID_NAMES,
    read_prompt,
    reset_prompt_to_default,
    write_prompt,
)
from routes.common import _json_object

router = APIRouter()


def _read_all_prompts() -> dict[str, str]:
    return {name: read_prompt(name) for name in VALID_NAMES}


@router.get("/api/prompts")
async def api_get_prompts():
    return await asyncio.to_thread(_read_all_prompts)


@router.put("/api/prompts/{name}")
async def api_save_prompt(name: str, req: Request):
    body = await _json_object(req)
    content = body.get("content", "")
    if not isinstance(content, str):
        raise HTTPException(
            400,
            detail={
                "code": "invalid_request_body",
                "message": "prompt content 必须是字符串",
            },
        )
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
