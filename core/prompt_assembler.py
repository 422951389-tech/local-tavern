"""带总预算、单次来源注入和安全诊断的 Prompt 组装器。"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from core.config import MAX_TURNS_IN_PROMPT, PROMPTS_DIR, PROMPT_SAFETY_MARGIN
from core.roleplay_policy import build_roleplay_context
from core.token_estimator import DEFAULT_TOKEN_ESTIMATOR, TokenEstimator
from core.worldbook_policy import (
    WorldbookCandidate,
    activate_worldbook_entries,
    worldbook_prompt_payload,
)


_TEMPLATE_TOKEN = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")
_RUNTIME_TEMPLATE_TOKENS = {
    "scene_meta_json",
    "user_profile_json",
    "character_context",
    "worldbook_entries",
    "history",
    "user_input",
}
_CHARACTER_PROFILE_FIELDS = (
    "id",
    "name",
    "aliases",
    "tagline",
    "persona",
    "appearance",
    "voice_tone",
    "speaking_style",
    "catchphrases",
    "abilities",
    "custom",
)
_CHARACTER_STATE_FIELDS = ("affinity", "mood", "outfit", "posture")
_SUMMARY_FIELDS = (
    "id",
    "created_at",
    "text",
    "time",
    "facts",
    "relations",
    "status",
    "source_snapshot_id",
)


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _source_id(value: object, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()[:160]
    return fallback


def _read_template(name: str) -> str:
    path = PROMPTS_DIR / name
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _render_once(template: str, values: dict[str, str]) -> str:
    """只扫描模板本身一次，插入值中的 {{token}} 不参与级联替换。"""

    missing = sorted({match.group(1) for match in _TEMPLATE_TOKEN.finditer(template)} - values.keys())
    if missing:
        raise ValueError(f"Prompt 模板含未提供占位符: {', '.join(missing)}")
    return _TEMPLATE_TOKEN.sub(lambda match: values[match.group(1)], template)


@dataclass(frozen=True)
class PromptAssembly:
    messages: list[dict]
    diagnostics: dict


class PromptBudgetExceeded(ValueError):
    code = "prompt_budget_exceeded"

    def __init__(self, diagnostics: dict):
        self.diagnostics = deepcopy(diagnostics)
        super().__init__("Prompt 必需内容超过模型上下文输入预算")

    def as_detail(self) -> dict:
        return {
            "code": self.code,
            "message": str(self),
            "diagnostics": deepcopy(self.diagnostics),
        }


class PromptTemplateInvalid(ValueError):
    code = "prompt_template_invalid"

    def __init__(self, violations: list[str]):
        self.violations = sorted(set(violations))
        super().__init__("Prompt 模板必须让每个运行时来源恰好保留一个数据槽")

    def as_detail(self) -> dict:
        return {
            "code": self.code,
            "message": str(self),
            "violations": list(self.violations),
        }


class PromptAssembler:
    """按固定优先级装入来源，任何来源正文都只进入最终 messages 一次。"""

    def __init__(
        self,
        estimator: TokenEstimator | None = None,
        *,
        prompts_dir: Path | None = None,
    ) -> None:
        self.estimator = estimator or DEFAULT_TOKEN_ESTIMATOR
        self.prompts_dir = prompts_dir

    def _template(self, name: str) -> str:
        if self.prompts_dir is None:
            return _read_template(name)
        path = self.prompts_dir / name
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    @staticmethod
    def _validate_templates(system_content: str, group_template: str) -> None:
        violations: list[str] = []
        if not system_content.strip():
            violations.append("system.md:empty")
        if not group_template.strip():
            violations.append("group_chat.md:empty")

        system_tokens = [match.group(1) for match in _TEMPLATE_TOKEN.finditer(system_content)]
        for token in sorted(set(system_tokens)):
            violations.append(f"system.md:{token}:runtime_slot_forbidden")

        group_tokens = [match.group(1) for match in _TEMPLATE_TOKEN.finditer(group_template)]
        for token in sorted(_RUNTIME_TEMPLATE_TOKENS):
            count = group_tokens.count(token)
            if count != 1:
                violations.append(f"group_chat.md:{token}:expected_1_found_{count}")
        for token in sorted(set(group_tokens) - _RUNTIME_TEMPLATE_TOKENS):
            violations.append(f"group_chat.md:{token}:unknown_slot")

        if violations:
            raise PromptTemplateInvalid(violations)

    @staticmethod
    def _character_sources(
        characters: list[dict],
        characters_state: dict,
        roleplay_context: dict,
    ) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        roleplay_by_id = {
            item.get("id"): item
            for item in roleplay_context.get("characters", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        for index, character in enumerate(characters):
            if not isinstance(character, dict):
                continue
            cid = _source_id(character.get("id"), f"character-{index + 1}")
            if cid in seen:
                continue
            seen.add(cid)
            payload = {
                key: deepcopy(character[key])
                for key in _CHARACTER_PROFILE_FIELDS
                if key in character and character[key] not in (None, "", [], {})
            }
            roleplay = roleplay_by_id.get(cid, {})
            # 这些控制字段与角色资料共用 character_context 单一槽位，避免
            # 新增动态来源或重复注入。稳定 ID 即使卡片缺字段也必须显式给出。
            payload["id"] = cid
            payload["name"] = str(roleplay.get("name") or payload.get("name") or cid)
            payload["aliases"] = deepcopy(roleplay.get("aliases", payload.get("aliases", [])))
            payload["chattiness"] = int(roleplay.get("chattiness", 50))
            payload["remaining_silent_turns"] = int(
                roleplay.get("remaining_silent_turns", 0)
            )
            payload["may_speak"] = bool(roleplay.get("may_speak", True))
            state = characters_state.get(cid, {}) if isinstance(characters_state, dict) else {}
            if isinstance(state, dict):
                compact_state = {
                    key: deepcopy(state[key])
                    for key in _CHARACTER_STATE_FIELDS
                    if key in state and state[key] not in (None, "")
                }
                if compact_state:
                    # 当前 outfit 已包含卡片初始穿着的演进结果，不再把初始值重复注入。
                    if "outfit" in compact_state and isinstance(payload.get("appearance"), dict):
                        appearance = deepcopy(payload["appearance"])
                        appearance.pop("outfit", None)
                        if appearance:
                            payload["appearance"] = appearance
                        else:
                            payload.pop("appearance", None)
                    payload["state"] = compact_state
            result.append((cid, _compact_json(payload)))
        return result

    @staticmethod
    def _worldbook_sources(
        candidates: list[WorldbookCandidate],
    ) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not candidate.activated:
                continue
            entry_id = candidate.entry_id
            if entry_id in seen:
                continue
            seen.add(entry_id)
            payload = worldbook_prompt_payload(candidate.entry)
            result.append((entry_id, _compact_json(payload)))
        return result

    @staticmethod
    def _summary_sources(summaries: list | None) -> list[tuple[int, str, str]]:
        result: list[tuple[int, str, str]] = []
        seen: set[str] = set()
        for index, summary in enumerate(summaries or []):
            if not isinstance(summary, dict) or not summary.get("text"):
                continue
            if summary.get("content_status") == "empty":
                continue
            summary_id = _source_id(summary.get("id"), f"summary-{index + 1}")
            if summary_id in seen:
                continue
            seen.add(summary_id)
            payload = {
                key: deepcopy(summary[key])
                for key in _SUMMARY_FIELDS
                if key in summary and summary[key] not in (None, "", [], {})
            }
            result.append((index, summary_id, _compact_json(payload)))
        return result

    def assemble(
        self,
        *,
        user_input: str,
        characters: list[dict],
        characters_state: dict,
        scene_meta: dict,
        user_profile: dict,
        worldbook_entries: list[dict],
        history: list[dict],
        summaries: list | None,
        context_limit: int,
        context_limit_source: str,
        num_predict: int,
        manual_worldbook_ids: list[str] | None = None,
        roleplay_context: dict | None = None,
        safety_margin: int = PROMPT_SAFETY_MARGIN,
    ) -> PromptAssembly:
        context_limit = int(context_limit)
        num_predict = int(num_predict)
        safety_margin = int(safety_margin)
        input_budget = context_limit - num_predict - safety_margin

        system_content = self._template("system.md")
        group_template = self._template("group_chat.md")
        self._validate_templates(system_content, group_template)
        effective_roleplay_context = (
            deepcopy(roleplay_context)
            if isinstance(roleplay_context, dict)
            else build_roleplay_context(characters, characters_state)
        )
        character_sources = self._character_sources(
            characters,
            characters_state,
            effective_roleplay_context,
        )
        character_text = "\n---\n".join(text for _cid, text in character_sources)
        if not character_text:
            character_text = "（无活跃角色；本轮严禁创建角色或角色状态卡）"

        profile_payload = {
            key: deepcopy(value)
            for key, value in (user_profile or {}).items()
            if key != "scene_meta" and value not in (None, "", [], {})
        }
        scene_text = _compact_json(scene_meta or {})
        profile_text = _compact_json(profile_payload)

        eligible: list[tuple[int, dict]] = []
        excluded_diagnostics: list[dict] = []
        seen_history_ids: set[str] = set()
        for index, item in enumerate(history or []):
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                continue
            item_id = _source_id(item.get("id"), f"history-{index + 1}")
            if item_id in seen_history_ids:
                excluded_diagnostics.append({
                    "source": "history",
                    "id": item_id,
                    "estimated_tokens": self.estimator.estimate_text(str(item.get("content", ""))),
                    "kept": False,
                    "reason": "duplicate_source_id",
                })
                continue
            seen_history_ids.add(item_id)
            if item.get("in_prompt", True) is False:
                excluded_diagnostics.append({
                    "source": "history",
                    "id": item_id,
                    "estimated_tokens": self.estimator.estimate_text(str(item.get("content", ""))),
                    "kept": False,
                    "reason": "excluded_by_in_prompt",
                })
                continue
            eligible.append((index, item))

        recent_count = MAX_TURNS_IN_PROMPT * 2
        recent = eligible[-recent_count:]
        older = eligible[:-recent_count] if len(eligible) > recent_count else []

        selected_recent: set[int] = set()
        selected_pinned: set[int] = set()
        selected_worldbook: set[int] = set()
        selected_summaries: set[int] = set()
        worldbook_candidates = activate_worldbook_entries(
            worldbook_entries,
            user_input=str(user_input),
            history=history,
            scene_meta=scene_meta,
            characters=characters,
            characters_state=characters_state,
            manual_worldbook_ids=manual_worldbook_ids,
            max_turns=MAX_TURNS_IN_PROMPT,
        )
        worldbook_sources = self._worldbook_sources(worldbook_candidates)
        worldbook_matches = [candidate.diagnostic() for candidate in worldbook_candidates]
        worldbook_match_by_id = {
            item["id"]: item
            for item in worldbook_matches
        }
        summary_sources = self._summary_sources(summaries)

        def history_block() -> str:
            parts: list[str] = []
            kept_summaries = [
                (index, text)
                for index, _sid, text in summary_sources
                if index in selected_summaries
            ]
            if kept_summaries:
                parts.append(
                    "===== 长期记忆（只作已发生背景，不得当作当前事件复述）=====\n"
                    + "\n---\n".join(text for _index, text in sorted(kept_summaries))
                )
            kept_pinned = [
                (index, item)
                for index, item in older
                if index in selected_pinned
            ]
            if kept_pinned:
                serialized = [
                    _compact_json({
                        "role": item.get("role", "user"),
                        "content": str(item.get("content", "")),
                    })
                    for _index, item in sorted(kept_pinned)
                ]
                parts.append("===== 用户钉选的常驻记忆 =====\n" + "\n".join(serialized))
            return "\n\n".join(parts) or "（无可用长期记忆或常驻记忆）"

        def worldbook_block() -> str:
            kept = [
                text
                for index, (_entry_id, text) in enumerate(worldbook_sources)
                if index in selected_worldbook
            ]
            return "\n---\n".join(kept) or "（无预算内匹配条目）"

        def render_messages() -> list[dict]:
            group_content = _render_once(group_template, {
                "scene_meta_json": scene_text,
                "user_profile_json": profile_text,
                "character_context": character_text,
                "worldbook_entries": worldbook_block(),
                "history": history_block(),
                "user_input": str(user_input),
            })
            messages = [{"role": "system", "content": system_content}]
            messages.extend(
                {
                    "role": item.get("role", "user"),
                    "content": str(item.get("content", "")),
                }
                for index, item in recent
                if index in selected_recent
            )
            messages.append({"role": "user", "content": group_content})
            return messages

        sources: list[dict] = [
            {
                "source": "system_rules",
                "id": "system.md",
                "estimated_tokens": self.estimator.estimate_text(system_content),
                "kept": True,
                "reason": "required",
            },
            {
                "source": "group_instructions",
                "id": "group_chat.md",
                "estimated_tokens": self.estimator.estimate_text(group_template),
                "kept": True,
                "reason": "required",
            },
            {
                "source": "scene",
                "id": "scene_meta",
                "estimated_tokens": self.estimator.estimate_text(scene_text),
                "kept": True,
                "reason": "required",
            },
            {
                "source": "user_profile",
                "id": "user_profile",
                "estimated_tokens": self.estimator.estimate_text(profile_text),
                "kept": True,
                "reason": "required",
            },
        ]
        sources.extend({
            "source": "character",
            "id": cid,
            "estimated_tokens": self.estimator.estimate_text(text),
            "kept": True,
            "reason": "required",
        } for cid, text in character_sources)
        sources.append({
            "source": "current_input",
            "id": "current_input",
            "estimated_tokens": self.estimator.estimate_text(str(user_input)),
            "kept": True,
            "reason": "required",
        })
        sources.extend(excluded_diagnostics)

        messages = render_messages()
        estimated_prompt = self.estimator.estimate_messages(messages)

        def diagnostics() -> dict:
            return {
                "schema_version": 2,
                "estimator": self.estimator.estimator_id,
                "context_limit": context_limit,
                "context_limit_source": str(context_limit_source),
                "reserved_output_tokens": num_predict,
                "safety_margin_tokens": safety_margin,
                "input_budget_tokens": max(0, input_budget),
                "estimated_prompt_tokens": estimated_prompt,
                "remaining_input_tokens": max(0, input_budget - estimated_prompt),
                "sources": deepcopy(sources),
                "worldbook_matches": deepcopy(worldbook_matches),
            }

        if input_budget <= 0 or estimated_prompt > input_budget:
            failed_diagnostics = diagnostics()
            for source in failed_diagnostics["sources"]:
                if source["reason"] == "required":
                    source["kept"] = False
                    source["reason"] = "required_exceeds_budget"
            for item in failed_diagnostics["worldbook_matches"]:
                if item["activated"]:
                    item["reason"] = "required_exceeds_budget_before_optional"
            raise PromptBudgetExceeded(failed_diagnostics)

        def try_source(
            *,
            source: str,
            source_id: str,
            text: str,
            select: Callable[[], None],
            unselect: Callable[[], None],
            kept_reason: str,
        ) -> bool:
            nonlocal messages, estimated_prompt
            select()
            candidate_messages = render_messages()
            candidate_total = self.estimator.estimate_messages(candidate_messages)
            kept = candidate_total <= input_budget
            if kept:
                messages = candidate_messages
                estimated_prompt = candidate_total
            else:
                unselect()
            sources.append({
                "source": source,
                "id": source_id,
                "estimated_tokens": self.estimator.estimate_text(text),
                "kept": kept,
                "reason": kept_reason if kept else "budget_exceeded",
            })
            return kept

        # 1. 近期历史：只保留能够装入预算的连续最新后缀。
        recent_desc = list(reversed(recent))
        recent_cutoff = False
        for index, item in recent_desc:
            item_id = _source_id(item.get("id"), f"history-{index + 1}")
            content = str(item.get("content", ""))
            if recent_cutoff:
                sources.append({
                    "source": "history",
                    "id": item_id,
                    "estimated_tokens": self.estimator.estimate_text(content),
                    "kept": False,
                    "reason": "budget_exceeded_after_newer_history",
                })
                continue
            kept = try_source(
                source="history",
                source_id=item_id,
                text=content,
                select=lambda index=index: selected_recent.add(index),
                unselect=lambda index=index: selected_recent.discard(index),
                kept_reason="recent_history",
            )
            if not kept:
                recent_cutoff = True

        # 近期窗口外的普通消息受数量上限裁剪；其中 pinned 再按预算独立竞争。
        older_pinned: list[tuple[int, dict]] = []
        for index, item in older:
            if item.get("pinned"):
                older_pinned.append((index, item))
                continue
            item_id = _source_id(item.get("id"), f"history-{index + 1}")
            sources.append({
                "source": "history",
                "id": item_id,
                "estimated_tokens": self.estimator.estimate_text(str(item.get("content", ""))),
                "kept": False,
                "reason": "outside_count_window",
            })
        for index, item in reversed(older_pinned):
            item_id = _source_id(item.get("id"), f"history-{index + 1}")
            content = str(item.get("content", ""))
            try_source(
                source="pinned",
                source_id=item_id,
                text=content,
                select=lambda index=index: selected_pinned.add(index),
                unselect=lambda index=index: selected_pinned.discard(index),
                kept_reason="pinned_history",
            )

        # 3. 已按 activation、priority、命中数、最近性和稳定 ID 排序的世界书。
        for index, (entry_id, text) in enumerate(worldbook_sources):
            kept = try_source(
                source="worldbook",
                source_id=entry_id,
                text=text,
                select=lambda index=index: selected_worldbook.add(index),
                unselect=lambda index=index: selected_worldbook.discard(index),
                kept_reason="within_budget",
            )
            match = worldbook_match_by_id[entry_id]
            match["kept"] = kept
            match["reason"] = "within_budget" if kept else "budget_exceeded"

        # 4. 摘要按新到旧争取预算，最终仍按时间正序呈现。
        for index, summary_id, text in reversed(summary_sources):
            try_source(
                source="summary",
                source_id=summary_id,
                text=text,
                select=lambda index=index: selected_summaries.add(index),
                unselect=lambda index=index: selected_summaries.discard(index),
                kept_reason="within_budget",
            )

        return PromptAssembly(messages=messages, diagnostics=diagnostics())


__all__ = [
    "PromptAssembler",
    "PromptAssembly",
    "PromptBudgetExceeded",
    "PromptTemplateInvalid",
]
