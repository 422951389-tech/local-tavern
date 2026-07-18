"""多角色发言节奏的防御式归一、身份解析与倒计时工具。"""
from __future__ import annotations

import unicodedata
from typing import Any


DEFAULT_CHATTINESS = 50
MIN_CHATTINESS = 0
MAX_CHATTINESS = 100
MIN_SILENT_TURNS = 0
MAX_SILENT_TURNS = 999

WARNING_MUTED_CHARACTER_OUTPUT = "muted_character_output"
WARNING_AMBIGUOUS_CHARACTER_IDENTITY = "ambiguous_character_identity"
WARNING_UNKNOWN_CHARACTER_IDENTITY = "unknown_character_identity"

ACTION_WRITEBACK_APPLIED = "writeback_applied"
ACTION_WRITEBACK_SKIPPED = "writeback_skipped"
ACTION_UNRESOLVED_SKIPPED = "unresolved_skipped"


def _strict_bounded_int(value: object, minimum: int, maximum: int, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    if value < minimum or value > maximum:
        return default
    return value


def normalize_chattiness(value: object) -> int:
    """把旧卡或不可信内存值收敛到安全默认值。"""
    return _strict_bounded_int(value, MIN_CHATTINESS, MAX_CHATTINESS, DEFAULT_CHATTINESS)


def validate_chattiness(value: object) -> int:
    """校验写入侧 chattiness；不接受 bool、字符串或浮点数。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("chattiness 必须是 0 到 100 的整数")
    if value < MIN_CHATTINESS or value > MAX_CHATTINESS:
        raise ValueError("chattiness 必须是 0 到 100 的整数")
    return value


def normalize_silent_turns(value: object) -> int:
    """旧存档纯读兼容：无效倒计时只在内存中按 0 处理。"""
    return _strict_bounded_int(value, MIN_SILENT_TURNS, MAX_SILENT_TURNS, 0)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _identity_key(value: object) -> str:
    text = _text(value)
    return unicodedata.normalize("NFKC", text).casefold() if text else ""


def _normalized_aliases(raw_aliases: object, state_name: str, display_name: str) -> list[str]:
    values: list[object] = list(raw_aliases) if isinstance(raw_aliases, list) else []
    if state_name:
        values.append(state_name)
    aliases: list[str] = []
    seen: set[str] = {_identity_key(display_name)}
    for value in values:
        alias = _text(value)
        key = _identity_key(alias)
        if not key or key in seen:
            continue
        seen.add(key)
        aliases.append(alias)
    return aliases


def build_roleplay_context(
    characters: object,
    characters_state: object,
    policy: object = None,
) -> dict[str, Any]:
    """构造不含角色正文的严格 JSON 发言上下文。"""
    raw_characters = characters if isinstance(characters, (list, tuple)) else []
    states = characters_state if isinstance(characters_state, dict) else {}
    strict = isinstance(policy, dict) and policy.get("strict_muted_writeback") is True
    normalized_characters: list[dict[str, Any]] = []
    speakable_ids: list[str] = []
    muted_ids: list[str] = []
    seen_ids: set[str] = set()

    for raw_character in raw_characters:
        if not isinstance(raw_character, dict):
            continue
        character_id = _text(raw_character.get("id"))
        if not character_id or character_id in seen_ids:
            continue
        seen_ids.add(character_id)
        raw_state = states.get(character_id)
        state = raw_state if isinstance(raw_state, dict) else {}
        card_name = _text(raw_character.get("name"))
        state_name = _text(state.get("name"))
        display_name = card_name or state_name or character_id
        silent_turns = normalize_silent_turns(state.get("remaining_silent_turns", 0))
        may_speak = silent_turns == 0
        normalized_characters.append({
            "id": character_id,
            "name": display_name,
            "aliases": _normalized_aliases(raw_character.get("aliases"), state_name, display_name),
            "chattiness": normalize_chattiness(raw_character.get("chattiness", DEFAULT_CHATTINESS)),
            "remaining_silent_turns": silent_turns,
            "may_speak": may_speak,
        })
        (speakable_ids if may_speak else muted_ids).append(character_id)

    return {
        "strict_muted_writeback": strict,
        "characters": normalized_characters,
        "speakable_ids": speakable_ids,
        "muted_ids": muted_ids,
    }


def resolve_character_id(value: object, context: object) -> dict[str, Any]:
    """按稳定 ID、显示名和唯一别名解析角色；碰撞时绝不猜测。"""
    key = _identity_key(value)
    raw_characters = context.get("characters", []) if isinstance(context, dict) else []
    candidates: set[str] = set()
    if key and isinstance(raw_characters, list):
        for character in raw_characters:
            if not isinstance(character, dict):
                continue
            character_id = _text(character.get("id"))
            if not character_id:
                continue
            identities: list[object] = [character_id, character.get("name")]
            aliases = character.get("aliases")
            if isinstance(aliases, list):
                identities.extend(aliases)
            if any(_identity_key(identity) == key for identity in identities):
                candidates.add(character_id)

    candidate_ids = sorted(candidates)
    if len(candidate_ids) == 1:
        return {"status": "matched", "character_id": candidate_ids[0]}
    if candidate_ids:
        return {"status": "ambiguous", "candidate_ids": candidate_ids}
    return {"status": "unknown", "candidate_ids": []}


def decrement_silence_counters(session: object) -> list[str]:
    """原地递减所有严格正整数倒计时，返回稳定排序的变更角色 ID。"""
    if not isinstance(session, dict):
        return []
    raw_states = session.get("characters_state")
    if not isinstance(raw_states, dict):
        return []
    changed: list[str] = []
    for character_id in sorted(key for key in raw_states if isinstance(key, str)):
        state = raw_states.get(character_id)
        if not isinstance(state, dict):
            continue
        value = state.get("remaining_silent_turns")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            continue
        state["remaining_silent_turns"] = value - 1
        changed.append(character_id)
    return changed
