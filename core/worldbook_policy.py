"""WORLD-1 世界书 Schema、触发匹配与稳定排序的纯领域规则。"""
from __future__ import annotations

import math
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from typing import Iterable
from uuid import UUID

from core.config import MAX_TURNS_IN_PROMPT
from core.path_policy import PathPolicyError, validate_file_id


ACTIVATIONS = frozenset({"always", "keywords", "manual", "scene"})
WORLDBOOK_CATEGORIES = frozenset({
    "general",
    "location",
    "faction",
    "rule",
    "history",
    "culture",
    "item",
    "secret",
})
WORLDBOOK_VISIBILITIES = frozenset({"public", "discovered", "hidden"})
WORLDBOOK_KNOWLEDGE_SCOPES = frozenset({"global", "narrator", "characters"})
MAX_WORLDBOOK_KEYWORDS = 64
MAX_WORLDBOOK_KEYWORD_LENGTH = 128
MIN_WORLDBOOK_PRIORITY = -1_000_000
MAX_WORLDBOOK_PRIORITY = 1_000_000
MAX_MANUAL_WORLDBOOK_IDS = 500
MAX_WORLDBOOK_TITLE_LENGTH = 2_000
MAX_WORLDBOOK_CONTENT_LENGTH = 200_000
MAX_WORLDBOOK_SUMMARY_LENGTH = 2_000
MAX_WORLDBOOK_LINKS = 100
MAX_WORLDBOOK_ALIAS_LENGTH = 256
WORLDBOOK_CONTROL_FIELDS = frozenset({
    "enabled",
    "activation",
    "keywords",
    "priority",
    "linked_character_ids",
    "linked_entry_ids",
    "location_aliases",
    # WORLD-1 前的旧控制字段只为无损兼容保留，不再影响触发或进入 Prompt。
    "keys",
    "constant",
    "position",
})


class WorldbookValidationError(ValueError):
    """可安全映射到 API 的世界书 Schema/选择错误。"""

    code = "worldbook_invalid"

    def __init__(self, violations: Iterable[str]):
        normalized = tuple(sorted({str(item) for item in violations if str(item)}))
        self.violations = normalized or ("worldbook:invalid",)
        super().__init__("世界书条目或手动选择不符合 Schema")

    def as_detail(self) -> dict:
        return {
            "code": self.code,
            "message": str(self),
            "violations": list(self.violations),
        }


@dataclass(frozen=True)
class WorldbookCandidate:
    """一次 Prompt 组装中的世界书触发结果，不持有上下文正文。"""

    entry_id: str
    entry: dict
    activation: str
    enabled: bool
    priority: int
    activated: bool
    trigger: str
    matched_keywords: tuple[str, ...]
    matched_sources: tuple[dict, ...]
    hit_count: int
    recency_distance: int
    rank: int | None
    reason: str
    title: str
    category: str
    summary: str
    visibility: str
    knowledge_scope: str

    def diagnostic(self) -> dict:
        return {
            "id": self.entry_id,
            "activation": self.activation,
            "enabled": self.enabled,
            "priority": self.priority,
            "trigger": self.trigger,
            "matched_keywords": list(self.matched_keywords),
            "matched_sources": deepcopy(list(self.matched_sources)),
            "hit_count": self.hit_count,
            "recency_distance": self.recency_distance,
            "rank": self.rank,
            "activated": self.activated,
            "kept": False,
            "reason": self.reason,
            "title": self.title,
            "category": self.category,
            "summary": self.summary,
            "visibility": self.visibility,
            "knowledge_scope": self.knowledge_scope,
        }


@dataclass(frozen=True)
class _MatchSource:
    scope: str
    ref: str
    text: str
    turn_distance: int

    def diagnostic(self) -> dict:
        return {
            "scope": self.scope,
            "ref": self.ref,
            "turn_distance": self.turn_distance,
        }


def normalize_match_text(value: object) -> str:
    """以 NFKC + casefold 生成跨中英文的确定性字面匹配文本。"""

    if not isinstance(value, str):
        return ""
    return unicodedata.normalize("NFKC", value).casefold()


def _safe_entry_id(value: object) -> str | None:
    try:
        return validate_file_id(value, label="世界书条目 ID")
    except PathPolicyError:
        return None


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _normalized_id_list(
    value: object,
    *,
    field: str,
    violations: list[str],
) -> list[str]:
    if not isinstance(value, list):
        violations.append(f"{field}:expected_array")
        return []
    if len(value) > MAX_WORLDBOOK_LINKS:
        violations.append(f"{field}:too_many")
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        safe = _safe_entry_id(item)
        if safe is None:
            violations.append(f"{field}:item_invalid")
            continue
        if safe in seen:
            continue
        seen.add(safe)
        result.append(safe)
    return result


def _normalized_aliases(value: object, violations: list[str]) -> list[str]:
    if not isinstance(value, list):
        violations.append("location_aliases:expected_array")
        return []
    if len(value) > MAX_WORLDBOOK_LINKS:
        violations.append("location_aliases:too_many")
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            violations.append("location_aliases:item_expected_string")
            continue
        alias = item.strip()
        canonical = normalize_match_text(alias)
        if not alias:
            violations.append("location_aliases:item_empty")
        elif len(alias) > MAX_WORLDBOOK_ALIAS_LENGTH:
            violations.append("location_aliases:item_too_long")
        elif _has_control_character(alias):
            violations.append("location_aliases:item_control_character")
        elif canonical not in seen:
            seen.add(canonical)
            result.append(alias)
    return result


def normalize_worldbook_entry(
    entry: object,
    entry_id: str | None = None,
    strict: bool = False,
) -> dict:
    """返回不修改输入的 WORLD-1 规范条目。

    ``strict=False`` 只允许旧条目缺少 WORLD-1 字段；任何显式错型仍拒绝，
    从而避免聊天链路静默改变用户意图。常用正文字段始终校验类型，
    ``strict=True`` 还校验其长度，供 CRUD 保存边界复用。
    """

    violations: list[str] = []
    if not isinstance(entry, dict):
        raise WorldbookValidationError(["entry:expected_object"])
    normalized = deepcopy(entry)

    path_id = None
    if entry_id is not None:
        path_id = _safe_entry_id(entry_id)
        if path_id is None:
            violations.append("id:path_invalid")

    raw_body_id = normalized.get("id")
    body_id = None
    if raw_body_id not in (None, ""):
        body_id = _safe_entry_id(raw_body_id)
        if body_id is None:
            violations.append("id:invalid")
    resolved_id = path_id or body_id
    if resolved_id is None:
        violations.append("id:required")
    elif path_id is not None and body_id is not None and path_id != body_id:
        violations.append("id:path_mismatch")

    raw_enabled = normalized.get("enabled", True)
    if not isinstance(raw_enabled, bool):
        violations.append("enabled:expected_boolean")

    raw_activation = normalized.get("activation", "always")
    if not isinstance(raw_activation, str) or raw_activation not in ACTIVATIONS:
        violations.append("activation:invalid")

    raw_priority = normalized.get("priority", 0)
    if isinstance(raw_priority, bool) or not isinstance(raw_priority, int):
        violations.append("priority:expected_integer")
    elif not MIN_WORLDBOOK_PRIORITY <= raw_priority <= MAX_WORLDBOOK_PRIORITY:
        violations.append("priority:out_of_range")

    raw_keywords = normalized.get("keywords", [])
    cleaned_keywords: list[str] = []
    if not isinstance(raw_keywords, list):
        violations.append("keywords:expected_array")
    elif len(raw_keywords) > MAX_WORLDBOOK_KEYWORDS:
        violations.append("keywords:too_many")
    else:
        seen_keywords: set[str] = set()
        for raw_keyword in raw_keywords:
            if not isinstance(raw_keyword, str):
                violations.append("keywords:item_expected_string")
                continue
            keyword = raw_keyword.strip()
            if not keyword:
                violations.append("keywords:item_empty")
                continue
            if len(keyword) > MAX_WORLDBOOK_KEYWORD_LENGTH:
                violations.append("keywords:item_too_long")
                continue
            if _has_control_character(keyword):
                violations.append("keywords:item_control_character")
                continue
            canonical = normalize_match_text(keyword)
            if canonical in seen_keywords:
                violations.append("keywords:item_duplicate")
                continue
            seen_keywords.add(canonical)
            cleaned_keywords.append(keyword)

    if raw_activation == "keywords" and not cleaned_keywords:
        violations.append("keywords:required_for_activation")

    raw_category = normalized.get("category", "general")
    if not isinstance(raw_category, str) or raw_category not in WORLDBOOK_CATEGORIES:
        violations.append("category:invalid")
    raw_visibility = normalized.get("visibility", "public")
    if (
        not isinstance(raw_visibility, str)
        or raw_visibility not in WORLDBOOK_VISIBILITIES
    ):
        violations.append("visibility:invalid")
    raw_knowledge_scope = normalized.get("knowledge_scope", "global")
    if (
        not isinstance(raw_knowledge_scope, str)
        or raw_knowledge_scope not in WORLDBOOK_KNOWLEDGE_SCOPES
    ):
        violations.append("knowledge_scope:invalid")

    known_by_character_ids = _normalized_id_list(
        normalized.get("known_by_character_ids", []),
        field="known_by_character_ids",
        violations=violations,
    )
    linked_character_ids = _normalized_id_list(
        normalized.get("linked_character_ids", []),
        field="linked_character_ids",
        violations=violations,
    )
    linked_entry_ids = _normalized_id_list(
        normalized.get("linked_entry_ids", []),
        field="linked_entry_ids",
        violations=violations,
    )
    location_aliases = _normalized_aliases(
        normalized.get("location_aliases", []),
        violations,
    )
    if raw_knowledge_scope == "characters" and not known_by_character_ids:
        violations.append("known_by_character_ids:required_for_scope")
    if raw_activation == "scene" and not (
        linked_character_ids or location_aliases
    ):
        violations.append("scene_links:required_for_activation")

    for field, maximum in (
        ("title", MAX_WORLDBOOK_TITLE_LENGTH),
        ("summary", MAX_WORLDBOOK_SUMMARY_LENGTH),
        ("content", MAX_WORLDBOOK_CONTENT_LENGTH),
    ):
        value = normalized.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            violations.append(f"{field}:expected_string")
        elif strict and len(value) > maximum:
            violations.append(f"{field}:too_long")

    if violations:
        raise WorldbookValidationError(violations)

    normalized["id"] = resolved_id
    normalized["enabled"] = raw_enabled
    normalized["activation"] = raw_activation
    normalized["keywords"] = cleaned_keywords
    normalized["priority"] = raw_priority
    normalized["category"] = raw_category
    normalized["summary"] = str(normalized.get("summary") or "")
    normalized["visibility"] = raw_visibility
    normalized["knowledge_scope"] = raw_knowledge_scope
    normalized["known_by_character_ids"] = known_by_character_ids
    normalized["linked_character_ids"] = linked_character_ids
    normalized["linked_entry_ids"] = linked_entry_ids
    normalized["location_aliases"] = location_aliases
    return normalized


def worldbook_prompt_payload(entry: dict) -> dict:
    """去掉触发控制字段，只返回允许进入 Prompt 的事实字段。"""

    return {
        key: deepcopy(value)
        for key, value in entry.items()
        if key not in WORLDBOOK_CONTROL_FIELDS and value not in (None, "", [], {})
    }


def _scene_sources(scene_meta: object, *, recency: int) -> list[_MatchSource]:
    sources: list[_MatchSource] = []

    def visit(value: object, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key in sorted(value, key=lambda item: str(item)):
                if isinstance(key, str):
                    visit(value[key], (*path, key))
            return
        if isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, (*path, str(index)))
            return
        if value is None or isinstance(value, (dict, list)):
            return
        if isinstance(value, float) and not math.isfinite(value):
            return
        if not isinstance(value, (str, int, float, bool)):
            return
        text = normalize_match_text(str(value))
        if text:
            ref = "scene:" + (".".join(path) if path else "root")
            sources.append(_MatchSource("scene", ref, text, recency))

    visit(scene_meta, ())
    return sources


def _history_turn_groups(history: object) -> list[list[tuple[int, dict]]]:
    groups: list[list[tuple[int, dict]]] = []
    group_by_turn_id: dict[str, list[tuple[int, dict]]] = {}
    legacy_group: list[tuple[int, dict]] | None = None
    for index, message in enumerate(history if isinstance(history, list) else []):
        if not isinstance(message, dict):
            continue
        if message.get("role") not in {"user", "assistant"}:
            continue
        if message.get("in_prompt", True) is False:
            continue
        if not isinstance(message.get("content"), str):
            continue
        turn_id = message.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            group = group_by_turn_id.get(turn_id)
            if group is None:
                group = []
                group_by_turn_id[turn_id] = group
                groups.append(group)
            group.append((index, message))
            legacy_group = None
            continue
        if message.get("role") == "user" or legacy_group is None:
            legacy_group = []
            groups.append(legacy_group)
        legacy_group.append((index, message))
        if message.get("role") == "assistant":
            legacy_group = None
    return groups


def _history_sources(history: object, *, max_turns: int) -> list[_MatchSource]:
    groups = _history_turn_groups(history)
    recent_groups = groups[-max_turns:] if max_turns > 0 else []
    sources: list[_MatchSource] = []
    for turn_distance, group in enumerate(reversed(recent_groups), start=1):
        for index, message in group:
            text = normalize_match_text(message.get("content"))
            if not text:
                continue
            message_id = message.get("id")
            try:
                safe_ref = str(UUID(message_id)) if isinstance(message_id, str) else ""
            except ValueError:
                safe_ref = ""
            if not safe_ref:
                safe_ref = f"legacy-{index + 1}"
            sources.append(_MatchSource(
                "history",
                f"history:{safe_ref}",
                text,
                turn_distance,
            ))
    return sources


def _character_sources(
    characters: object,
    characters_state: object,
    *,
    recency: int,
) -> list[_MatchSource]:
    cards: dict[str, dict] = {}
    for card in characters if isinstance(characters, list) else []:
        if not isinstance(card, dict):
            continue
        cid = card.get("id")
        if isinstance(cid, str) and cid:
            cards[cid] = card
    states = characters_state if isinstance(characters_state, dict) else {}
    sources: list[_MatchSource] = []
    for cid in sorted(states, key=lambda value: (str(value).casefold(), str(value))):
        if not isinstance(cid, str) or not cid:
            continue
        state = states.get(cid)
        card = cards.get(cid, {})
        aliases: list[str] = [cid]
        card_name = card.get("name") if isinstance(card, dict) else None
        if isinstance(card_name, str) and card_name:
            aliases.append(card_name)
        raw_aliases = card.get("aliases", []) if isinstance(card, dict) else []
        if isinstance(raw_aliases, list):
            aliases.extend(alias for alias in raw_aliases if isinstance(alias, str) and alias)
        state_name = state.get("name") if isinstance(state, dict) else None
        if isinstance(state_name, str) and state_name:
            aliases.append(state_name)
        seen_text: set[str] = set()
        for alias in aliases:
            text = normalize_match_text(alias)
            if not text or text in seen_text:
                continue
            seen_text.add(text)
            sources.append(_MatchSource(
                "character",
                f"character:{cid}",
                text,
                recency,
            ))
    return sources


def _manual_ids(value: object) -> tuple[str, ...]:
    """容错读取 Session 中的持久选择。

    新提交的严格校验属于 PATCH API；Prompt 读取必须允许条目删除、禁用或
    改模式后留下 dormant ID，使条目恢复时原选择能恢复生效。
    """

    if not isinstance(value, list):
        return ()
    seen: set[str] = set()
    for item in value:
        item_id = _safe_entry_id(item)
        if item_id is None:
            continue
        seen.add(item_id)
    return tuple(sorted(seen)[:MAX_MANUAL_WORLDBOOK_IDS])


def activate_worldbook_entries(
    entries: object,
    *,
    user_input: str,
    history: list[dict] | None,
    scene_meta: dict | None,
    characters: list[dict] | None,
    characters_state: dict | None,
    manual_worldbook_ids: list[str] | None = None,
    max_turns: int = MAX_TURNS_IN_PROMPT,
) -> list[WorldbookCandidate]:
    """规范化全部条目，计算三态触发并返回稳定排序结果。"""

    if not isinstance(entries, list):
        raise WorldbookValidationError(["entries:expected_array"])
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 0:
        raise WorldbookValidationError(["max_turns:invalid"])

    manual_ids = _manual_ids(manual_worldbook_ids)
    normalized_entries: list[tuple[dict, bool]] = []
    seen_entry_ids: set[str] = set()
    duplicate = False
    for entry in entries:
        legacy_activation = isinstance(entry, dict) and "activation" not in entry
        normalized = normalize_worldbook_entry(entry)
        entry_id = normalized["id"]
        if entry_id in seen_entry_ids:
            duplicate = True
        seen_entry_ids.add(entry_id)
        normalized_entries.append((normalized, legacy_activation))
    if duplicate:
        raise WorldbookValidationError(["id:duplicate"])

    static_recency = max_turns + 1
    match_sources = [
        _MatchSource(
            "current_input",
            "current_input",
            normalize_match_text(user_input),
            0,
        )
    ]
    match_sources.extend(_history_sources(history, max_turns=max_turns))
    scene_sources = _scene_sources(scene_meta, recency=static_recency)
    match_sources.extend(scene_sources)
    match_sources.extend(_character_sources(
        characters,
        characters_state,
        recency=static_recency,
    ))
    match_sources = [source for source in match_sources if source.text]
    active_character_ids: set[str] = set()
    for card in characters if isinstance(characters, list) else []:
        if isinstance(card, dict):
            safe_id = _safe_entry_id(card.get("id"))
            if safe_id is not None:
                active_character_ids.add(safe_id)
    if isinstance(characters_state, dict):
        for character_id in characters_state:
            safe_id = _safe_entry_id(character_id)
            if safe_id is not None:
                active_character_ids.add(safe_id)

    unresolved: list[dict] = []
    for entry, legacy_activation in normalized_entries:
        entry_id = entry["id"]
        enabled = entry["enabled"]
        activation = entry["activation"]
        matched_keywords: list[str] = []
        matched_source_map: dict[tuple[str, str, int], _MatchSource] = {}
        if enabled and activation == "keywords":
            for keyword in entry["keywords"]:
                canonical = normalize_match_text(keyword)
                keyword_sources = [
                    source for source in match_sources if canonical in source.text
                ]
                if not keyword_sources:
                    continue
                matched_keywords.append(keyword)
                for source in keyword_sources:
                    matched_source_map[
                        (source.scope, source.ref, source.turn_distance)
                    ] = source
        elif enabled and activation == "scene":
            for alias in entry["location_aliases"]:
                canonical = normalize_match_text(alias)
                alias_sources = [
                    source for source in scene_sources if canonical in source.text
                ]
                if not alias_sources:
                    continue
                matched_keywords.append(alias)
                for source in alias_sources:
                    matched_source_map[
                        (source.scope, source.ref, source.turn_distance)
                    ] = source
            for character_id in entry["linked_character_ids"]:
                if character_id not in active_character_ids:
                    continue
                matched_keywords.append(f"character:{character_id}")
                source = _MatchSource(
                    "character",
                    f"character:{character_id}",
                    normalize_match_text(character_id),
                    static_recency,
                )
                matched_source_map[
                    (source.scope, source.ref, source.turn_distance)
                ] = source

        matched_sources = tuple(
            source.diagnostic()
            for _key, source in sorted(
                matched_source_map.items(),
                key=lambda item: (
                    item[1].turn_distance,
                    item[1].scope,
                    item[1].ref.casefold(),
                    item[1].ref,
                ),
            )
        )
        hit_count = len(matched_keywords)
        recency_distance = min(
            (source["turn_distance"] for source in matched_sources),
            default=static_recency,
        )

        if not enabled:
            activated = False
            trigger = "disabled"
            reason = "disabled"
        elif activation == "always":
            activated = True
            trigger = "legacy_always" if legacy_activation else "always"
            reason = "activated"
        elif activation == "manual":
            activated = entry_id in manual_ids
            trigger = "manual_selected" if activated else "manual_not_selected"
            reason = "activated" if activated else "manual_not_selected"
        elif activation == "scene":
            activated = hit_count > 0
            trigger = "scene_link_match" if activated else "scene_link_not_matched"
            reason = "activated" if activated else "scene_link_not_matched"
        else:
            activated = hit_count > 0
            trigger = "keyword_match" if activated else "keyword_not_matched"
            reason = "activated" if activated else "keyword_not_matched"

        unresolved.append({
            "entry_id": entry_id,
            "entry": entry,
            "activation": activation,
            "enabled": enabled,
            "priority": entry["priority"],
            "activated": activated,
            "trigger": trigger,
            "matched_keywords": tuple(matched_keywords),
            "matched_sources": matched_sources,
            "hit_count": hit_count,
            "recency_distance": recency_distance,
            "reason": reason,
            "title": str(entry.get("title") or entry_id),
            "category": entry["category"],
            "summary": entry["summary"],
            "visibility": entry["visibility"],
            "knowledge_scope": entry["knowledge_scope"],
        })

    active = sorted(
        (item for item in unresolved if item["activated"]),
        key=lambda item: (
            -item["priority"],
            -item["hit_count"],
            item["recency_distance"],
            item["entry_id"].casefold(),
            item["entry_id"],
        ),
    )
    inactive = sorted(
        (item for item in unresolved if not item["activated"]),
        key=lambda item: (item["entry_id"].casefold(), item["entry_id"]),
    )
    result: list[WorldbookCandidate] = []
    for rank, item in enumerate(active, start=1):
        result.append(WorldbookCandidate(**item, rank=rank))
    result.extend(WorldbookCandidate(**item, rank=None) for item in inactive)
    return result


__all__ = [
    "ACTIVATIONS",
    "MAX_MANUAL_WORLDBOOK_IDS",
    "MAX_WORLDBOOK_CONTENT_LENGTH",
    "MAX_WORLDBOOK_KEYWORD_LENGTH",
    "MAX_WORLDBOOK_KEYWORDS",
    "MAX_WORLDBOOK_PRIORITY",
    "MAX_WORLDBOOK_TITLE_LENGTH",
    "MAX_WORLDBOOK_SUMMARY_LENGTH",
    "MIN_WORLDBOOK_PRIORITY",
    "WORLDBOOK_CONTROL_FIELDS",
    "WORLDBOOK_CATEGORIES",
    "WORLDBOOK_KNOWLEDGE_SCOPES",
    "WORLDBOOK_VISIBILITIES",
    "WorldbookCandidate",
    "WorldbookValidationError",
    "activate_worldbook_entries",
    "normalize_match_text",
    "normalize_worldbook_entry",
    "worldbook_prompt_payload",
]
