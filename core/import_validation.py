"""外部存档 JSON 的大小、结构和字段类型验证。"""
from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)

from core.path_policy import PathPolicyError, validate_file_id


MAX_IMPORT_BYTES = 8 * 1024 * 1024
MAX_MESSAGES = 10_000
MAX_MESSAGE_TEXT = 200_000
MAX_SHORT_TEXT = 2_000
MAX_CHARACTERS = 500
MAX_SUMMARIES = 2_000

ShortText = Annotated[StrictStr, Field(max_length=MAX_SHORT_TEXT)]
MessageText = Annotated[StrictStr, Field(max_length=MAX_MESSAGE_TEXT)]
Affinity = Annotated[StrictInt | StrictFloat, Field(ge=0, le=100)]


class ImportModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class ImportedMessage(ImportModel):
    role: Literal["user", "assistant"]
    content: MessageText
    thinking: MessageText = ""
    pinned: StrictBool = False
    in_prompt: StrictBool = True


class ImportedSceneMeta(ImportModel):
    location: ShortText = ""
    time: ShortText = ""
    weather: ShortText = ""
    main_quest: MessageText = ""
    current_scene: MessageText = ""
    next_goal: MessageText = ""


class ImportedUserStatus(ImportModel):
    name: ShortText = ""
    identity: ShortText = ""
    condition: ShortText = ""
    abilities: list[ShortText] = Field(default_factory=list, max_length=500)


class ImportedCharacterState(ImportModel):
    name: ShortText = ""
    affinity: Affinity = 0
    mood: ShortText = ""
    inner_thought: MessageText = ""
    outfit: MessageText = ""
    posture: MessageText = ""
    dialogue: MessageText = ""


class ImportedSummary(ImportModel):
    text: MessageText = ""
    time: ShortText = ""
    facts: list[MessageText] = Field(default_factory=list, max_length=1_000)
    relations: list[MessageText] = Field(default_factory=list, max_length=1_000)
    created_at: ShortText = ""
    failed: StrictBool = False


class ImportedSession(ImportModel):
    session_id: StrictStr
    name: ShortText = ""
    project: ShortText = ""
    created_at: ShortText = ""
    updated_at: ShortText = ""
    current_model: ShortText = ""
    scene_meta: ImportedSceneMeta = Field(default_factory=ImportedSceneMeta)
    user_status: ImportedUserStatus = Field(default_factory=ImportedUserStatus)
    characters_state: dict[StrictStr, ImportedCharacterState] = Field(
        default_factory=dict,
        max_length=MAX_CHARACTERS,
    )
    message_history: list[ImportedMessage] = Field(
        default_factory=list,
        max_length=MAX_MESSAGES,
    )
    summaries: list[ImportedSummary] = Field(
        default_factory=list,
        max_length=MAX_SUMMARIES,
    )
    summary_error: MessageText = ""

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        try:
            return validate_file_id(value, label="导入存档 session_id")
        except PathPolicyError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("characters_state")
    @classmethod
    def validate_character_ids(
        cls,
        value: dict[str, ImportedCharacterState],
    ) -> dict[str, ImportedCharacterState]:
        for character_id in value:
            try:
                validate_file_id(character_id, label="角色状态 ID")
            except PathPolicyError as exc:
                raise ValueError(str(exc)) from exc
        return value


def validate_import_json(json_str: object) -> dict:
    """校验导入文本，返回保留未知字段的普通 dict。"""
    if not isinstance(json_str, str):
        raise ValueError("导入内容必须是 JSON 字符串")
    byte_count = len(json_str.encode("utf-8"))
    if byte_count > MAX_IMPORT_BYTES:
        raise ValueError(f"导入文件不能超过 {MAX_IMPORT_BYTES // (1024 * 1024)} MiB")
    try:
        raw = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败: {exc.msg}（第 {exc.lineno} 行）") from exc
    if not isinstance(raw, dict):
        raise ValueError("导入 JSON 顶层必须是对象")
    try:
        validated = ImportedSession.model_validate(raw)
    except ValidationError as exc:
        details = []
        for error in exc.errors(include_input=False)[:8]:
            location = ".".join(str(part) for part in error["loc"])
            details.append(f"{location}: {error['msg']}")
        raise ValueError("导入存档字段验证失败：" + "；".join(details)) from exc
    return validated.model_dump(mode="python")
