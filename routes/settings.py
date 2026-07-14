"""设置路由。"""
import asyncio
import json
from fastapi import APIRouter, Request

from core.config import SETTINGS_PATH
from core.session_store import atomic_write

router = APIRouter()


@router.get("/api/settings")
async def api_get_settings():
    if not SETTINGS_PATH.exists():
        return {"temperature": 0.8, "top_p": 0.9, "top_k": 40, "num_predict": 4096, "think": True}
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"temperature": 0.8, "top_p": 0.9, "top_k": 40, "num_predict": 4096, "think": True}


@router.put("/api/settings")
async def api_save_settings(req: Request):
    body = await req.json()
    data = body.get("data", body)
    allowed = {"temperature", "top_p", "top_k", "num_predict", "think"}
    filtered = {k: v for k, v in data.items() if k in allowed}
    content = json.dumps(filtered, ensure_ascii=False, indent=2)
    await asyncio.to_thread(atomic_write, SETTINGS_PATH, content)
    return {"saved": True}
