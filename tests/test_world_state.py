from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest

from core.world_state import (
    WorldStateValidationError,
    add_world_change,
    discover_world_entries,
    normalize_world_state,
    remove_world_change,
    set_discovered_world_entries,
    update_world_change,
)


def test_legacy_world_state_defaults_without_mutating_input():
    raw = {}
    before = deepcopy(raw)

    normalized = normalize_world_state(raw)

    assert raw == before
    assert normalized == {
        "schema_version": 1,
        "discovered_entry_ids": [],
        "changes": [],
    }


def test_world_change_is_traceable_to_messages_and_reversible():
    message_id = "11111111-1111-4111-8111-111111111111"
    state = add_world_change(
        {},
        category="faction",
        title="花园守卫转向",
        detail="守卫开始协助主角。",
        related_entry_ids=["glass_palace"],
        evidence_message_ids=[message_id],
    )

    assert len(state["changes"]) == 1
    change = state["changes"][0]
    assert str(UUID(change["id"])) == change["id"]
    assert change["evidence_message_ids"] == [message_id]
    assert remove_world_change(state, change["id"])["changes"] == []


def test_discoveries_are_deduplicated_and_invalid_evidence_is_rejected():
    assert discover_world_entries({}, ["lore", "lore", "palace"])[
        "discovered_entry_ids"
    ] == ["lore", "palace"]

    with pytest.raises(WorldStateValidationError):
        add_world_change(
            {},
            category="event",
            title="无来源事件",
            detail="无效",
            evidence_message_ids=["not-a-uuid"],
        )


def test_discoveries_can_be_replaced_and_world_changes_can_be_edited():
    message_id = "11111111-1111-4111-8111-111111111111"
    state = set_discovered_world_entries({}, ["palace", "garden"])
    state = set_discovered_world_entries(state, ["garden"])
    assert state["discovered_entry_ids"] == ["garden"]

    state = add_world_change(
        state,
        category="event",
        title="守卫封锁花园",
        related_entry_ids=["garden"],
        evidence_message_ids=[message_id],
    )
    change = state["changes"][0]
    updated = update_world_change(
        state,
        change["id"],
        category="faction",
        title="守卫解除封锁",
        detail="通行恢复。",
        status="retconned",
        related_entry_ids=["palace"],
        evidence_message_ids=[message_id],
    )
    assert updated["changes"][0]["title"] == "守卫解除封锁"
    assert updated["changes"][0]["status"] == "retconned"
    assert updated["changes"][0]["created_at"] == change["created_at"]

    with pytest.raises(WorldStateValidationError):
        update_world_change(state, str(UUID(int=0)), category="event", title="不存在")
