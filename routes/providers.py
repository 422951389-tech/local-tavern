"""Provider 安全配置、凭据状态与连通性测试 API。"""

from __future__ import annotations

import asyncio
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.model_provider import ProviderError, ProviderValidationError
from core.provider_registry import (
    DEFAULT_CLOUD_CONTEXT_LIMIT,
    PROVIDER_PRESETS,
    ProviderConfig,
    ProviderRegistry,
    ProviderRegistryError,
    get_provider_registry,
)
from core.secret_store import SecretStoreError
from core.request_security import host_header_allowed


router = APIRouter()


class ProviderConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    preset: Literal["openai", "deepseek", "siliconflow", "anthropic", "custom"]
    name: str | None = Field(default=None, min_length=1, max_length=80)
    kind: Literal["openai_compatible", "anthropic"] | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    models: list[str] = Field(default_factory=list, max_length=100)
    context_limit: int = Field(
        default=DEFAULT_CLOUD_CONTEXT_LIMIT,
        ge=4_096,
        le=1_048_576,
    )


class ProviderCredentialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    api_key: str = Field(min_length=1, max_length=16_384)


async def _validated_body(request: Request, model_type):
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是对象")
        return model_type.model_validate(payload)
    except (TypeError, ValueError, ValidationError):
        raise HTTPException(
            400,
            detail={
                "code": "invalid_provider_request",
                "message": "Provider 请求体无效",
            },
        ) from None


def _require_same_origin(request: Request) -> None:
    if not host_header_allowed(request.headers.get("host")):
        raise HTTPException(
            403,
            detail={
                "code": "provider_origin_forbidden",
                "message": "Provider 写操作仅接受可信本地同源请求",
            },
        )
    origin = request.headers.get("origin")
    try:
        parsed = urlsplit(origin or "")
    except ValueError:
        parsed = None
    expected = f"{request.url.scheme}://{request.url.netloc}".casefold()
    actual = (
        f"{parsed.scheme}://{parsed.netloc}".casefold()
        if parsed is not None
        and parsed.scheme in {"http", "https"}
        and parsed.netloc
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        else ""
    )
    if not actual or actual != expected:
        raise HTTPException(
            403,
            detail={
                "code": "provider_origin_forbidden",
                "message": "Provider 写操作仅接受同源请求",
            },
        )


def _require_json(request: Request) -> None:
    media_type = (
        request.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    )
    if media_type != "application/json":
        raise HTTPException(
            415,
            detail={
                "code": "provider_content_type_invalid",
                "message": "Provider 写操作必须使用 application/json",
            },
        )


def get_registry() -> ProviderRegistry:
    return get_provider_registry()


def raise_provider_error(exc: Exception) -> None:
    if isinstance(exc, ProviderValidationError):
        status = 400
    elif isinstance(exc, ProviderRegistryError):
        if exc.code == "provider_not_found":
            status = 404
        elif exc.code in {
            "provider_builtin_immutable",
            "provider_credential_missing",
            "provider_credential_not_required",
            "provider_credential_rebind_required",
        }:
            status = 409
        elif exc.code.endswith(("_invalid", "_write_failed")):
            status = 500
        else:
            status = 400
    elif isinstance(exc, SecretStoreError):
        status = (
            400 if exc.code in {"secret_id_invalid", "secret_value_invalid"} else 500
        )
    elif isinstance(exc, ProviderError):
        status = 502
    else:
        raise exc
    detail = (
        exc.as_detail()
        if hasattr(exc, "as_detail")
        else {
            "code": "provider_operation_failed",
            "message": "Provider 操作失败",
        }
    )
    raise HTTPException(status, detail=detail) from exc


def _config_from_request(
    provider_id: str,
    body: ProviderConfigRequest,
    registry: ProviderRegistry,
) -> ProviderConfig:
    preset = PROVIDER_PRESETS[body.preset]
    if body.preset == "custom":
        kind = body.kind or "openai_compatible"
        base_url = body.base_url
        name = body.name or "自定义 Provider"
        if base_url is None:
            raise ProviderValidationError(
                "provider_url_required",
                "自定义 Provider 必须提供 URL",
            )
    else:
        expected_kind = preset["kind"]
        expected_url = preset["base_url"]
        if body.kind is not None and body.kind != expected_kind:
            raise ProviderValidationError(
                "provider_preset_conflict",
                "Provider 预设与类型冲突",
            )
        if body.base_url is not None and body.base_url != expected_url:
            raise ProviderValidationError(
                "provider_preset_conflict",
                "Provider 预设与 URL 冲突",
            )
        kind = str(expected_kind)
        base_url = str(expected_url)
        name = body.name or str(preset["name"])
    return registry.build_config(
        provider_id,
        kind=kind,
        name=name,
        base_url=base_url,
        models=body.models,
        context_limit=body.context_limit,
        preset=body.preset,
    )


@router.get("/api/providers/presets")
async def api_provider_presets():
    return {"presets": get_registry().presets()}


@router.get("/api/providers")
async def api_list_providers():
    try:
        return {
            "providers": await asyncio.to_thread(lambda: get_registry().list_configs())
        }
    except (ProviderError, SecretStoreError) as exc:
        raise_provider_error(exc)


@router.get("/api/providers/{provider_id}")
async def api_get_provider(provider_id: str):
    try:
        return {
            "provider": await asyncio.to_thread(
                lambda: get_registry().public_config(provider_id)
            )
        }
    except (ProviderError, SecretStoreError) as exc:
        raise_provider_error(exc)


@router.put("/api/providers/{provider_id}")
async def api_put_provider(provider_id: str, request: Request):
    _require_same_origin(request)
    _require_json(request)
    body = await _validated_body(request, ProviderConfigRequest)
    registry = await asyncio.to_thread(get_registry)
    try:
        config = await asyncio.to_thread(
            _config_from_request,
            provider_id,
            body,
            registry,
        )
        return {"provider": await registry.upsert_config(config)}
    except (ProviderError, SecretStoreError) as exc:
        raise_provider_error(exc)


@router.delete("/api/providers/{provider_id}")
async def api_delete_provider(provider_id: str, request: Request):
    _require_same_origin(request)
    try:
        await get_registry().delete_config(provider_id)
        return {"deleted": True, "provider_id": provider_id}
    except (ProviderError, SecretStoreError) as exc:
        raise_provider_error(exc)


@router.put("/api/providers/{provider_id}/credential")
async def api_put_provider_credential(provider_id: str, request: Request):
    _require_same_origin(request)
    _require_json(request)
    body = await _validated_body(request, ProviderCredentialRequest)
    try:
        provider = await get_registry().set_credential(provider_id, body.api_key)
        return {
            "credential": {
                "provider_id": provider["provider_id"],
                "configured": provider["has_credential"],
            }
        }
    except (ProviderError, SecretStoreError) as exc:
        raise_provider_error(exc)


@router.delete("/api/providers/{provider_id}/credential")
async def api_delete_provider_credential(provider_id: str, request: Request):
    _require_same_origin(request)
    try:
        await get_registry().delete_credential(provider_id)
        return {"credential": {"provider_id": provider_id, "configured": False}}
    except (ProviderError, SecretStoreError) as exc:
        raise_provider_error(exc)


@router.post("/api/providers/{provider_id}/test")
async def api_test_provider(provider_id: str, request: Request):
    _require_same_origin(request)
    try:
        return {"test": await get_registry().test(provider_id)}
    except (ProviderError, SecretStoreError) as exc:
        raise_provider_error(exc)
