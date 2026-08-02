"""存档级世界状态：发现记录与可追溯、可撤销的世界变化。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID, uuid4

from core.path_policy import PathPolicyError, validate_file_id


WORLD_CHANGE_CATEGORIES = frozenset({
    "location",
    "faction",
    "rule",
    "event",
    "item",
    "other",
})
WORLD_CHANGE_STATUSES = frozenset({"active", "resolved", "retconned"})
MAX_WORLD_CHANGES = 500
MAX_WORLD_LINKS = 20
MAX_WORLD_CHANGE_TITLE = 200
MAX_WORLD_CHANGE_DETAIL = 2_000


class WorldStateValidationError(ValueError):
    code = "world_state_invalid"

    def __init__(self, violations: Iterable[str]):
        self.violations = tuple(sorted({str(item) for item in violations if str(item)}))
        super().__init__("世界状态数据不符合 Schema")

    def as_detail(self) -> dict:
        return {
            "code": self.code,
            "message": str(self),
            "violations": list(self.violations),
        }


def _timestamp(value: object, fallback: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _uuid(value: object, field: str, violations: list[str]) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        violations.append(f"{field}:invalid_uuid")
        return ""


def _entry_ids(value: object, field: str, violations: list[str]) -> list[str]:
    if not isinstance(value, list):
        violations.append(f"{field}:expected_array")
        return []
    if len(value) > MAX_WORLD_LINKS:
        violations.append(f"{field}:too_many")
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        try:
            entry_id = validate_file_id(item, label=field)
        except (PathPolicyError, TypeError, ValueError):
            violations.append(f"{field}:item_invalid")
            continue
        if entry_id not in seen:
            seen.add(entry_id)
            result.append(entry_id)
    return result


def _message_ids(value: object, field: str, violations: list[str]) -> list[str]:
    if not isinstance(value, list):
        violations.append(f"{field}:expected_array")
        return []
    if len(value) > MAX_WORLD_LINKS:
        violations.append(f"{field}:too_many")
        return []
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        message_id = _uuid(item, f"{field}[{index}]", violations)
        if message_id and message_id not in seen:
            seen.add(message_id)
            result.append(message_id)
    return result


def normalize_world_state(value: object, *, strict: bool = False) -> dict:
    """规范化存档世界状态；旧存档缺失该字段时得到空状态。"""

    if value is None:
        value = {}
    if not isinstance(value, dict):
        if strict:
            raise WorldStateValidationError(["world_state:expected_object"])
        value = {}
    source = deepcopy(value)
    violations: list[str] = []
    discovered = _entry_ids(
        source.get("discovered_entry_ids", []),
        "discovered_entry_ids",
        violations,
    )
    raw_changes = source.get("changes", [])
    if not isinstance(raw_changes, list):
        violations.append("changes:expected_array")
        raw_changes = []
    elif len(raw_changes) > MAX_WORLD_CHANGES:
        violations.append("changes:too_many")
        raw_changes = raw_changes[:MAX_WORLD_CHANGES]
    now = datetime.now(timezone.utc).isoformat()
    changes: list[dict] = []
    seen_change_ids: set[str] = set()
    for index, raw in enumerate(raw_changes):
        prefix = f"changes[{index}]"
        if not isinstance(raw, dict):
            violations.append(f"{prefix}:expected_object")
            continue
        change_id = _uuid(raw.get("id"), f"{prefix}.id", violations)
        category = raw.get("category", "other")
        if category not in WORLD_CHANGE_CATEGORIES:
            violations.append(f"{prefix}.category:invalid")
        status = raw.get("status", "active")
        if status not in WORLD_CHANGE_STATUSES:
            violations.append(f"{prefix}.status:invalid")
        title = raw.get("title", "")
        detail = raw.get("detail", "")
        if not isinstance(title, str) or not title.strip():
            violations.append(f"{prefix}.title:required")
            title = ""
        elif len(title.strip()) > MAX_WORLD_CHANGE_TITLE:
            violations.append(f"{prefix}.title:too_long")
        if not isinstance(detail, str):
            violations.append(f"{prefix}.detail:expected_string")
            detail = ""
        elif len(detail.strip()) > MAX_WORLD_CHANGE_DETAIL:
            violations.append(f"{prefix}.detail:too_long")
        if change_id in seen_change_ids:
            violations.append("changes:id_duplicate")
        elif change_id:
            seen_change_ids.add(change_id)
        changes.append({
            "id": change_id,
            "category": category,
            "title": title.strip(),
            "detail": detail.strip(),
            "status": status,
            "related_entry_ids": _entry_ids(
                raw.get("related_entry_ids", []),
                f"{prefix}.related_entry_ids",
                violations,
            ),
            "evidence_message_ids": _message_ids(
                raw.get("evidence_message_ids", []),
                f"{prefix}.evidence_message_ids",
                violations,
            ),
            "created_at": _timestamp(raw.get("created_at"), now),
            "updated_at": _timestamp(raw.get("updated_at"), now),
        })
    if violations and strict:
        raise WorldStateValidationError(violations)
    if violations:
        changes = [
            change for change in changes
            if change["id"] and change["title"]
        ]
    return {
        "schema_version": 1,
        "discovered_entry_ids": discovered,
        "changes": changes,
    }


def discover_world_entries(state: object, entry_ids: list[str]) -> dict:
    normalized = normalize_world_state(state, strict=True)
    violations: list[str] = []
    additions = _entry_ids(entry_ids, "discovered_entry_ids", violations)
    if violations:
        raise WorldStateValidationError(violations)
    seen = set(normalized["discovered_entry_ids"])
    for entry_id in additions:
        if entry_id not in seen:
            normalized["discovered_entry_ids"].append(entry_id)
            seen.add(entry_id)
    return normalized


def set_discovered_world_entries(state: object, entry_ids: list[str]) -> dict:
    """以给定集合替换发现记录，允许撤销误标记的发现状态。"""

    normalized = normalize_world_state(state, strict=True)
    violations: list[str] = []
    selected = _entry_ids(entry_ids, "discovered_entry_ids", violations)
    if violations:
        raise WorldStateValidationError(violations)
    normalized["discovered_entry_ids"] = selected
    return normalized


def add_world_change(
    state: object,
    *,
    category: str,
    title: str,
    detail: str = "",
    status: str = "active",
    related_entry_ids: list[str] | None = None,
    evidence_message_ids: list[str] | None = None,
    change_id: str | None = None,
    now: datetime | None = None,
) -> dict:
    normalized = normalize_world_state(state, strict=True)
    if len(normalized["changes"]) >= MAX_WORLD_CHANGES:
        raise WorldStateValidationError(["changes:too_many"])
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    candidate = {
        "id": change_id or str(uuid4()),
        "category": category,
        "title": title,
        "detail": detail,
        "status": status,
        "related_entry_ids": related_entry_ids or [],
        "evidence_message_ids": evidence_message_ids or [],
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    combined = deepcopy(normalized)
    combined["changes"].append(candidate)
    return normalize_world_state(combined, strict=True)


def update_world_change(
    state: object,
    change_id: str,
    *,
    category: str,
    title: str,
    detail: str = "",
    status: str = "active",
    related_entry_ids: list[str] | None = None,
    evidence_message_ids: list[str] | None = None,
    now: datetime | None = None,
) -> dict:
    """完整替换一条世界变化的可编辑字段，并保留创建时间。"""

    normalized = normalize_world_state(state, strict=True)
    violations: list[str] = []
    safe_id = _uuid(change_id, "change_id", violations)
    if violations:
        raise WorldStateValidationError(violations)
    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    found = False
    for index, change in enumerate(normalized["changes"]):
        if change["id"] != safe_id:
            continue
        found = True
        normalized["changes"][index] = {
            **change,
            "category": category,
            "title": title,
            "detail": detail,
            "status": status,
            "related_entry_ids": related_entry_ids or [],
            "evidence_message_ids": evidence_message_ids or [],
            "updated_at": timestamp,
        }
        break
    if not found:
        raise WorldStateValidationError(["change_id:not_found"])
    return normalize_world_state(normalized, strict=True)


def remove_world_change(state: object, change_id: str) -> dict:
    normalized = normalize_world_state(state, strict=True)
    violations: list[str] = []
    safe_id = _uuid(change_id, "change_id", violations)
    if violations:
        raise WorldStateValidationError(violations)
    normalized["changes"] = [
        change for change in normalized["changes"] if change["id"] != safe_id
    ]
    return normalized


__all__ = [
    "WORLD_CHANGE_CATEGORIES",
    "WorldStateValidationError",
    "add_world_change",
    "discover_world_entries",
    "normalize_world_state",
    "remove_world_change",
    "set_discovered_world_entries",
    "update_world_change",
]
