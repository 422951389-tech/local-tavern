"""按 Provider 枚举模型。"""
from fastapi import APIRouter, Query

from core.model_provider import ProviderError
from core.provider_registry import get_provider_registry
from core.secret_store import SecretStoreError
from routes.providers import raise_provider_error

router = APIRouter()


@router.get("/api/models")
async def api_list_models(provider: str = Query("ollama", min_length=1, max_length=64)):
    try:
        registry = get_provider_registry()
        models = await registry.list_models(provider)
    except (ProviderError, SecretStoreError) as exc:
        raise_provider_error(exc)
    return {"provider": provider, "models": models}
