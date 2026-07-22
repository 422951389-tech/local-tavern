"""外部存档 JSON 的大小、结构和字段类型验证。"""
from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import UUID

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
    model_validator,
)

from core.path_policy import (
    PathPolicyError,
    validate_file_id,
    validate_snapshot_filename,
)
from core.relationship_edges import (
    MAX_DISTINCT_EVIDENCE_MESSAGES,
    MAX_EVIDENCE_PER_EDGE,
    MAX_RELATIONSHIP_EDGES,
    MAX_RELATION_TYPE_LENGTH,
    RelationshipEdgeError,
    normalize_relation_type,
    relationship_key,
    validate_updated_at,
)
from core.worldbook_policy import MAX_MANUAL_WORLDBOOK_IDS


MAX_IMPORT_BYTES = 8 * 1024 * 1024
MAX_MESSAGES = 10_000
MAX_MESSAGE_TEXT = 200_000
MAX_SHORT_TEXT = 2_000
MAX_CHARACTERS = 500
MAX_SUMMARIES = 2_000

ShortText = Annotated[StrictStr, Field(max_length=MAX_SHORT_TEXT)]
MessageText = Annotated[StrictStr, Field(max_length=MAX_MESSAGE_TEXT)]
SummaryText = Annotated[StrictStr, Field(max_length=2_000)]
SummaryTime = Annotated[StrictStr, Field(max_length=300)]
SummaryItem = Annotated[StrictStr, Field(min_length=1, max_length=200)]
Affinity = Annotated[StrictInt | StrictFloat, Field(ge=0, le=100)]
Revision = Annotated[StrictInt, Field(ge=0)]
SilentTurns = Annotated[StrictInt, Field(ge=0, le=999)]
RelationshipStrength = Annotated[StrictInt, Field(ge=0, le=100)]
RelationshipType = Annotated[
    StrictStr,
    Field(min_length=1, max_length=MAX_RELATION_TYPE_LENGTH),
]


class ImportModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class ImportedMessage(ImportModel):
    id: StrictStr = ""
    role: Literal["user", "assistant"]
    content: MessageText
    thinking: MessageText = ""
    pinned: StrictBool = False
    in_prompt: StrictBool = True

    @field_validator("id")
    @classmethod
    def validate_message_id(cls, value: str) -> str:
        if not value:
            return value
        try:
            return str(UUID(value))
        except ValueError as exc:
            raise ValueError("消息 id 必须是 UUID") from exc


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
    remaining_silent_turns: SilentTurns = 0


class ImportedRoleplayPolicy(ImportModel):
    strict_muted_writeback: StrictBool = False


class ImportedRelationshipEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_character_id: StrictStr
    target_character_id: StrictStr
    relation_type: RelationshipType
    strength: RelationshipStrength
    evidence_message_ids: Annotated[
        list[StrictStr],
        Field(min_length=1, max_length=MAX_EVIDENCE_PER_EDGE),
    ]
    updated_at: StrictStr

    @field_validator("source_character_id", "target_character_id")
    @classmethod
    def validate_character_id(cls, value: str) -> str:
        try:
            return validate_file_id(value, label="关系角色 ID")
        except PathPolicyError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("relation_type", mode="before")
    @classmethod
    def validate_relation_type(cls, value: object) -> str:
        try:
            return normalize_relation_type(value)
        except RelationshipEdgeError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("evidence_message_ids")
    @classmethod
    def validate_evidence_message_ids(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for message_id in value:
            try:
                canonical = str(UUID(message_id))
            except ValueError as exc:
                raise ValueError("证据消息 ID 必须是 UUID") from exc
            if canonical in seen:
                raise ValueError("同一关系边不能重复引用证据消息")
            seen.add(canonical)
            normalized.append(canonical)
        return normalized

    @field_validator("updated_at")
    @classmethod
    def validate_edge_updated_at(cls, value: str) -> str:
        try:
            return validate_updated_at(value)
        except RelationshipEdgeError as exc:
            raise ValueError(str(exc)) from exc


class ImportedSummary(ImportModel):
    id: StrictStr = ""
    source_snapshot_id: StrictStr | None = None
    status: Literal["pending", "completed", "failed"] = "completed"
    generation_id: StrictStr | None = None
    generation_attempt: Annotated[StrictInt, Field(ge=0)] = 0
    text: SummaryText = ""
    time: SummaryTime = ""
    facts: list[SummaryItem] = Field(default_factory=list, max_length=5)
    relations: list[SummaryItem] = Field(default_factory=list, max_length=5)
    created_at: ShortText = ""
    failed: StrictBool = False
    error: ShortText | None = None

    @field_validator("id", "generation_id")
    @classmethod
    def validate_optional_uuid(cls, value: str | None) -> str | None:
        if not value:
            return value
        try:
            return str(UUID(value))
        except ValueError as exc:
            raise ValueError("摘要 id 与 generation_id 必须是 UUID") from exc


class ImportedSession(ImportModel):
    session_id: StrictStr
    name: ShortText = ""
    project: ShortText = ""
    revision: Revision = 0
    created_at: ShortText = ""
    updated_at: ShortText = ""
    current_model: ShortText = ""
    scene_meta: ImportedSceneMeta = Field(default_factory=ImportedSceneMeta)
    user_status: ImportedUserStatus = Field(default_factory=ImportedUserStatus)
    characters_state: dict[StrictStr, ImportedCharacterState] = Field(
        default_factory=dict,
        max_length=MAX_CHARACTERS,
    )
    roleplay_policy: ImportedRoleplayPolicy = Field(
        default_factory=ImportedRoleplayPolicy,
    )
    relationship_edges: list[ImportedRelationshipEdge] = Field(
        default_factory=list,
        max_length=MAX_RELATIONSHIP_EDGES,
    )
    manual_worldbook_ids: list[StrictStr] = Field(
        default_factory=list,
        max_length=MAX_MANUAL_WORLDBOOK_IDS,
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

    @field_validator("manual_worldbook_ids")
    @classmethod
    def validate_manual_worldbook_ids(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for entry_id in value:
            try:
                validated = validate_file_id(entry_id, label="手动世界书条目 ID")
            except PathPolicyError as exc:
                raise ValueError(str(exc)) from exc
            if validated in seen:
                raise ValueError("手动世界书条目 ID 不能重复")
            seen.add(validated)
            normalized.append(validated)
        return sorted(normalized)

    @model_validator(mode="after")
    def validate_summary_identity(self) -> "ImportedSession":
        seen_ids: set[str] = set()
        seen_sources: set[str] = set()
        for summary in self.summaries:
            if summary.id:
                if summary.id in seen_ids:
                    raise ValueError("摘要 id 不能重复")
                seen_ids.add(summary.id)
            source = summary.source_snapshot_id
            if not source:
                continue
            try:
                _filename, kind = validate_snapshot_filename(
                    self.session_id,
                    source,
                )
            except PathPolicyError as exc:
                raise ValueError(f"摘要 source_snapshot_id 无效: {exc}") from exc
            if kind != "trim":
                raise ValueError("摘要 source_snapshot_id 必须指向 trim 快照")
            if source in seen_sources:
                raise ValueError("多个摘要不能绑定同一个 trim 快照")
            seen_sources.add(source)

        if self.relationship_edges:
            message_ids: set[str] = set()
            for message in self.message_history:
                if not message.id:
                    raise ValueError("含关系边的存档要求每条消息都有 UUID")
                if message.id in message_ids:
                    raise ValueError("含关系边的存档禁止重复消息 UUID")
                message_ids.add(message.id)

            character_ids = set(self.characters_state)
            seen_relationship_keys: set[tuple[str, str, str]] = set()
            distinct_evidence: set[str] = set()
            for edge in self.relationship_edges:
                if edge.source_character_id == edge.target_character_id:
                    raise ValueError("关系边禁止自环")
                if edge.source_character_id not in character_ids:
                    raise ValueError("关系边来源角色不属于当前存档")
                if edge.target_character_id not in character_ids:
                    raise ValueError("关系边目标角色不属于当前存档")
                key = relationship_key(
                    edge.source_character_id,
                    edge.target_character_id,
                    edge.relation_type,
                )
                if key in seen_relationship_keys:
                    raise ValueError("关系边复合键不能重复")
                seen_relationship_keys.add(key)
                for message_id in edge.evidence_message_ids:
                    if message_id not in message_ids:
                        raise ValueError("关系边证据消息不属于当前存档")
                    distinct_evidence.add(message_id)
                    if len(distinct_evidence) > MAX_DISTINCT_EVIDENCE_MESSAGES:
                        raise ValueError(
                            f"每个存档最多引用 {MAX_DISTINCT_EVIDENCE_MESSAGES} 条不同证据消息"
                        )
        return self


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
