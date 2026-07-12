"""设置路由。"""
import json
from fastapi import APIRouter, Request

from core.config import SETTINGS_PATH

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
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(filtered, f, ensure_ascii=False, indent=2)
    tmp.replace(SETTINGS_PATH)
    return {"saved": True}
