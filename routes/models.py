"""模型列表路由。"""
from fastapi import APIRouter

from core.ollama_client import get_client

router = APIRouter()


@router.get("/api/models")
async def api_list_models():
    models = await get_client().list_models()
    return {"models": models}
