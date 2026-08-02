"""设置路由。"""

import asyncio
import json
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.api_errors import validation_error_detail
from core.config import SETTINGS_PATH
from core.session_store import atomic_write
from routes.common import _json_object

router = APIRouter()

Temperature = Annotated[float, Field(ge=0.0, le=2.0)]
TopP = Annotated[float, Field(ge=0.0, le=1.0)]
TopK = Annotated[int, Field(ge=0, le=1000)]
NumPredict = Annotated[int, Field(ge=1, le=32768)]
DesktopRenderer = Literal["software", "hardware"]

DEFAULT_SETTINGS = {
    "temperature": 0.8,
    "top_p": 0.9,
    "top_k": 40,
    "num_predict": 4096,
    "think": True,
    "desktop_renderer": "software",
}


class SettingsValues(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    temperature: Temperature = 0.8
    top_p: TopP = 0.9
    top_k: TopK = 40
    num_predict: NumPredict = 4096
    think: bool = True
    desktop_renderer: DesktopRenderer = "software"


def _read_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            return dict(DEFAULT_SETTINGS)
        return {**DEFAULT_SETTINGS, **data}
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_SETTINGS)


@router.get("/api/settings")
async def api_get_settings():
    return await asyncio.to_thread(_read_settings)


@router.put("/api/settings")
async def api_save_settings(req: Request):
    body = await _json_object(req)
    data = body.get("data", body)
    if "data" in body and set(body) != {"data"}:
        raise HTTPException(
            400,
            detail={
                "code": "invalid_request_body",
                "message": "设置包装对象只允许 data 字段",
            },
        )
    if not isinstance(data, dict):
        raise HTTPException(
            400,
            detail={
                "code": "invalid_request_body",
                "message": "设置 data 必须是 JSON 对象",
            },
        )
    try:
        validated = SettingsValues.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(
            422,
            detail=validation_error_detail(exc.errors()),
        ) from None
    filtered = validated.model_dump(include=validated.model_fields_set)
    existing = _read_settings()
    known_existing = {
        key: value
        for key, value in existing.items()
        if key in SettingsValues.model_fields
    }
    merged = {**DEFAULT_SETTINGS, **known_existing, **filtered}
    try:
        validated_values = SettingsValues.model_validate(merged).model_dump()
    except ValidationError as exc:
        raise HTTPException(
            422,
            detail=validation_error_detail(exc.errors()),
        ) from None
    persisted = {**existing, **validated_values}
    content = json.dumps(persisted, ensure_ascii=False, indent=2)
    await asyncio.to_thread(atomic_write, SETTINGS_PATH, content)
    return {"saved": True}
