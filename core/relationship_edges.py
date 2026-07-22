"""存档级角色—角色关系边及证据生命周期规则。"""
from __future__ import annotations

from datetime import datetime
from typing import Mapping
from unicodedata import category, normalize
from uuid import UUID

from core.path_policy import PathPolicyError, validate_file_id


MAX_RELATIONSHIP_EDGES = 200
MAX_EVIDENCE_PER_EDGE = 20
MAX_DISTINCT_EVIDENCE_MESSAGES = 50
MAX_RELATION_TYPE_LENGTH = 80
MAX_UPDATED_AT_LENGTH = 64


class RelationshipEdgeError(ValueError):
    """可稳定映射到 API 错误契约的关系边错误。"""

    code = "relationship_edge_invalid"

    def __init__(self, message: str, *, field: str = ""):
        super().__init__(message)
        self.field = field

    def as_detail(self) -> dict:
        detail = {"code": self.code, "message": str(self)}
        if self.field:
            detail["field"] = self.field
        return detail


class RelationshipEdgeNotFound(RelationshipEdgeError):
    code = "relationship_edge_not_found"


class RelationshipEdgeConflict(RelationshipEdgeError):
    code = "relationship_edge_conflict"


def normalize_relation_type(value: object) -> str:
    if not isinstance(value, str):
        raise RelationshipEdgeError("关系类型必须是字符串", field="relation_type")
    normalized = normalize("NFKC", value).strip()
    if not normalized:
        raise RelationshipEdgeError("关系类型不能为空", field="relation_type")
    if len(normalized) > MAX_RELATION_TYPE_LENGTH:
        raise RelationshipEdgeError(
            f"关系类型不能超过 {MAX_RELATION_TYPE_LENGTH} 个字符",
            field="relation_type",
        )
    if any(category(character) in {"Cc", "Cf", "Cs"} for character in normalized):
        raise RelationshipEdgeError(
            "关系类型不能包含控制、格式或代理字符",
            field="relation_type",
        )
    return normalized


def relation_type_key(value: object) -> str:
    return normalize_relation_type(value).casefold()


def _validate_character_id(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise RelationshipEdgeError("角色 ID 必须是字符串", field=field)
    try:
        return validate_file_id(value, label="角色 ID")
    except (PathPolicyError, TypeError, ValueError) as exc:
        raise RelationshipEdgeError(str(exc), field=field) from exc


def relationship_key(
    source_character_id: object,
    target_character_id: object,
    relation_type: object,
) -> tuple[str, str, str]:
    return (
        _validate_character_id(source_character_id, field="source_character_id"),
        _validate_character_id(target_character_id, field="target_character_id"),
        relation_type_key(relation_type),
    )


def relationship_key_from_mapping(value: object) -> tuple[str, str, str]:
    if not isinstance(value, Mapping):
        raise RelationshipEdgeError("关系键必须是对象")
    return relationship_key(
        value.get("source_character_id"),
        value.get("target_character_id"),
        value.get("relation_type"),
    )


def validate_updated_at(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RelationshipEdgeError("updated_at 必须是非空 ISO 时间", field="updated_at")
    if len(value) > MAX_UPDATED_AT_LENGTH:
        raise RelationshipEdgeError(
            f"updated_at 不能超过 {MAX_UPDATED_AT_LENGTH} 个字符",
            field="updated_at",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RelationshipEdgeError("updated_at 必须是有效 ISO 时间", field="updated_at") from exc
    if parsed.tzinfo is None or "T" not in value:
        raise RelationshipEdgeError("updated_at 必须包含时区与时间", field="updated_at")
    return value


def _canonical_message_id(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise RelationshipEdgeError("证据消息 ID 必须是 UUID 字符串", field=field)
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise RelationshipEdgeError("证据消息 ID 必须是 UUID", field=field) from exc


def _session_character_ids(session: Mapping) -> set[str]:
    states = session.get("characters_state")
    if not isinstance(states, dict):
        return set()
    return {
        character_id
        for character_id, state in states.items()
        if isinstance(character_id, str) and isinstance(state, dict)
    }


def _session_message_ids(session: Mapping) -> set[str]:
    history = session.get("message_history")
    if not isinstance(history, list):
        return set()
    ids: set[str] = set()
    for message in history:
        if not isinstance(message, dict):
            continue
        try:
            message_id = _canonical_message_id(message.get("id"), field="message_history.id")
        except RelationshipEdgeError:
            continue
        ids.add(message_id)
    return ids


def validate_relationship_edge(
    session: Mapping,
    edge: object,
    *,
    allow_missing_evidence: bool = False,
) -> dict:
    if not isinstance(edge, Mapping):
        raise RelationshipEdgeError("关系边必须是对象")
    required = {
        "source_character_id",
        "target_character_id",
        "relation_type",
        "strength",
        "evidence_message_ids",
        "updated_at",
    }
    if set(edge) != required:
        missing = sorted(required - set(edge))
        extra = sorted(set(edge) - required)
        if missing:
            raise RelationshipEdgeError(f"关系边缺少字段：{', '.join(missing)}")
        raise RelationshipEdgeError(f"关系边包含未知字段：{', '.join(extra)}")

    source = _validate_character_id(edge.get("source_character_id"), field="source_character_id")
    target = _validate_character_id(edge.get("target_character_id"), field="target_character_id")
    if source == target:
        raise RelationshipEdgeError("关系边禁止自环", field="target_character_id")
    character_ids = _session_character_ids(session)
    if source not in character_ids:
        raise RelationshipEdgeError("来源角色不属于当前存档", field="source_character_id")
    if target not in character_ids:
        raise RelationshipEdgeError("目标角色不属于当前存档", field="target_character_id")

    strength = edge.get("strength")
    if isinstance(strength, bool) or not isinstance(strength, int) or not 0 <= strength <= 100:
        raise RelationshipEdgeError("关系强度必须是 0 到 100 的严格整数", field="strength")

    raw_evidence = edge.get("evidence_message_ids")
    if not isinstance(raw_evidence, list):
        raise RelationshipEdgeError("证据消息必须是数组", field="evidence_message_ids")
    if not 1 <= len(raw_evidence) <= MAX_EVIDENCE_PER_EDGE:
        raise RelationshipEdgeError(
            f"每条关系边必须包含 1 到 {MAX_EVIDENCE_PER_EDGE} 条证据消息",
            field="evidence_message_ids",
        )
    evidence: list[str] = []
    seen_evidence: set[str] = set()
    current_message_ids = _session_message_ids(session)
    for raw_id in raw_evidence:
        message_id = _canonical_message_id(raw_id, field="evidence_message_ids")
        if message_id in seen_evidence:
            raise RelationshipEdgeError("同一关系边不能重复引用证据消息", field="evidence_message_ids")
        if not allow_missing_evidence and message_id not in current_message_ids:
            raise RelationshipEdgeError("证据消息不属于当前存档", field="evidence_message_ids")
        seen_evidence.add(message_id)
        evidence.append(message_id)

    return {
        "source_character_id": source,
        "target_character_id": target,
        "relation_type": normalize_relation_type(edge.get("relation_type")),
        "strength": strength,
        "evidence_message_ids": evidence,
        "updated_at": validate_updated_at(edge.get("updated_at")),
    }


def validate_relationship_edges(
    session: Mapping,
    edges: object | None = None,
    *,
    allow_missing_evidence: bool = False,
) -> list[dict]:
    raw_edges = session.get("relationship_edges", []) if edges is None else edges
    if not isinstance(raw_edges, list):
        raise RelationshipEdgeError("relationship_edges 必须是数组")
    if len(raw_edges) > MAX_RELATIONSHIP_EDGES:
        raise RelationshipEdgeError(f"每个存档最多 {MAX_RELATIONSHIP_EDGES} 条关系边")

    normalized_edges: list[dict] = []
    seen_keys: set[tuple[str, str, str]] = set()
    all_evidence: set[str] = set()
    for raw_edge in raw_edges:
        edge = validate_relationship_edge(
            session,
            raw_edge,
            allow_missing_evidence=allow_missing_evidence,
        )
        key = relationship_key_from_mapping(edge)
        if key in seen_keys:
            raise RelationshipEdgeError("关系边复合键不能重复")
        seen_keys.add(key)
        all_evidence.update(edge["evidence_message_ids"])
        if len(all_evidence) > MAX_DISTINCT_EVIDENCE_MESSAGES:
            raise RelationshipEdgeError(
                f"每个存档最多引用 {MAX_DISTINCT_EVIDENCE_MESSAGES} 条不同证据消息",
                field="evidence_message_ids",
            )
        normalized_edges.append(edge)
    return normalized_edges


def relationship_evidence_ids(session: Mapping) -> set[str]:
    """返回自动 trim 应保护的证据 ID；对手工损坏数据执行硬上限。"""
    current_message_ids = _session_message_ids(session)
    protected: set[str] = set()
    raw_edges = session.get("relationship_edges", [])
    if not isinstance(raw_edges, list):
        return protected
    for edge in raw_edges[:MAX_RELATIONSHIP_EDGES]:
        if not isinstance(edge, dict):
            continue
        evidence = edge.get("evidence_message_ids")
        if not isinstance(evidence, list):
            continue
        for raw_id in evidence[:MAX_EVIDENCE_PER_EDGE]:
            try:
                message_id = _canonical_message_id(raw_id, field="evidence_message_ids")
            except RelationshipEdgeError:
                continue
            if message_id in current_message_ids:
                protected.add(message_id)
            if len(protected) >= MAX_DISTINCT_EVIDENCE_MESSAGES:
                return protected
    return protected


def upsert_relationship_edge(
    session: dict,
    edge: object,
    *,
    original_key: object | None = None,
) -> dict:
    current = validate_relationship_edges(session)
    candidate = validate_relationship_edge(session, edge)
    candidate_key = relationship_key_from_mapping(candidate)
    original = relationship_key_from_mapping(original_key) if original_key is not None else None

    retained: list[dict] = []
    replaced = False
    for current_edge in current:
        key = relationship_key_from_mapping(current_edge)
        if original is not None and key == original:
            replaced = True
            continue
        if original is None and key == candidate_key:
            replaced = True
            continue
        retained.append(current_edge)
    if original is not None and not replaced:
        raise RelationshipEdgeNotFound("待编辑的关系边不存在")
    if any(relationship_key_from_mapping(item) == candidate_key for item in retained):
        raise RelationshipEdgeConflict("目标关系边复合键已存在")
    retained.append(candidate)
    session["relationship_edges"] = validate_relationship_edges(session, retained)
    return candidate


def delete_relationship_edge(session: dict, key_value: object) -> dict:
    target_key = relationship_key_from_mapping(key_value)
    current = validate_relationship_edges(session)
    retained: list[dict] = []
    deleted: dict | None = None
    for edge in current:
        if relationship_key_from_mapping(edge) == target_key:
            deleted = edge
        else:
            retained.append(edge)
    if deleted is None:
        raise RelationshipEdgeNotFound("关系边不存在")
    session["relationship_edges"] = retained
    return deleted


def reconcile_relationship_evidence(session: dict, *, updated_at: str | None = None) -> bool:
    current = validate_relationship_edges(session, allow_missing_evidence=True)
    current_message_ids = _session_message_ids(session)
    changed = False
    retained: list[dict] = []
    timestamp = updated_at or datetime.now().astimezone().isoformat()
    for edge in current:
        evidence = [
            message_id
            for message_id in edge["evidence_message_ids"]
            if message_id in current_message_ids
        ]
        if evidence == edge["evidence_message_ids"]:
            retained.append(edge)
            continue
        changed = True
        if not evidence:
            continue
        retained.append({**edge, "evidence_message_ids": evidence, "updated_at": timestamp})
    if changed:
        session["relationship_edges"] = validate_relationship_edges(session, retained)
    return changed


def remove_incident_relationship_edges(session: dict, character_id: str) -> bool:
    raw_edges = session.get("relationship_edges", [])
    if not isinstance(raw_edges, list):
        session["relationship_edges"] = []
        return True
    retained = [
        edge
        for edge in raw_edges
        if not isinstance(edge, dict)
        or (
            edge.get("source_character_id") != character_id
            and edge.get("target_character_id") != character_id
        )
    ]
    if len(retained) == len(raw_edges):
        return False
    session["relationship_edges"] = retained
    return True
