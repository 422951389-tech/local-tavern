from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID, uuid4

import pytest

from core.import_validation import validate_import_json
from core.relationship_edges import (
    RelationshipEdgeError,
    reconcile_relationship_evidence,
    relationship_key,
    upsert_relationship_edge,
    validate_relationship_edges,
)
from core.session_manager import trim_history


def _message(index: int) -> dict:
    return {
        "id": str(UUID(int=index + 1)),
        "role": "user" if index % 2 == 0 else "assistant",
        "content": f"消息 {index}",
        "pinned": False,
        "in_prompt": True,
    }


def _session(message_count: int = 60) -> dict:
    return {
        "characters_state": {
            "alpha": {"name": "阿尔法"},
            "beta": {"name": "贝塔"},
            "gamma": {"name": "伽马"},
        },
        "message_history": [_message(index) for index in range(message_count)],
        "relationship_edges": [],
    }


def _edge(
    relation_type: str = "盟友",
    *,
    source: str = "alpha",
    target: str = "beta",
    strength: int = 50,
    evidence: list[str] | None = None,
) -> dict:
    return {
        "source_character_id": source,
        "target_character_id": target,
        "relation_type": relation_type,
        "strength": strength,
        "evidence_message_ids": evidence if evidence is not None else [str(UUID(int=1))],
        "updated_at": "2026-07-22T12:00:00+08:00",
    }


def test_relationship_key_normalizes_nfkc_casefold_and_direction():
    assert relationship_key("alpha", "beta", "  Ｓｔｒａße  ") == (
        "alpha",
        "beta",
        "strasse",
    )
    assert relationship_key("beta", "alpha", "STRASSE") != relationship_key(
        "alpha",
        "beta",
        "Straße",
    )


def test_relationship_validation_enforces_types_references_and_uniqueness():
    session = _session()
    session["relationship_edges"] = [_edge("  Ｓｔｒａße  ")]
    normalized = validate_relationship_edges(session)
    assert normalized[0]["relation_type"] == "Straße"

    for invalid_strength in (True, 1.0, "1", -1, 101):
        with pytest.raises(RelationshipEdgeError, match="严格整数"):
            validate_relationship_edges(session, [_edge(strength=invalid_strength)])
    for invalid in (
        _edge(source="alpha", target="alpha"),
        _edge(source="missing"),
        _edge(evidence=[str(uuid4())]),
        _edge(evidence=[str(UUID(int=1)), str(UUID(int=1))]),
        _edge(evidence=[]),
        _edge(evidence=[str(UUID(int=index + 1)) for index in range(21)]),
    ):
        with pytest.raises(RelationshipEdgeError):
            validate_relationship_edges(session, [invalid])

    with pytest.raises(RelationshipEdgeError, match="复合键"):
        validate_relationship_edges(session, [_edge("Straße"), _edge("STRASSE")])


@pytest.mark.parametrize(
    "relation_type",
    ["盟\u202e友", "盟\u200b友", "A\u0085B", "\ud800", "\udfff"],
)
def test_relation_type_rejects_unicode_controls_formats_and_surrogates(relation_type):
    with pytest.raises(RelationshipEdgeError, match="控制、格式或代理"):
        validate_relationship_edges(_session(), [_edge(relation_type)])


def test_relationship_validation_enforces_edge_and_distinct_evidence_limits():
    session = _session(message_count=60)
    two_hundred = [
        _edge(f"关系 {index}", evidence=[str(UUID(int=(index % 50) + 1))])
        for index in range(200)
    ]
    assert len(validate_relationship_edges(session, two_hundred)) == 200
    with pytest.raises(RelationshipEdgeError, match="最多 200"):
        validate_relationship_edges(session, [*two_hundred, _edge("第 201 条")])

    fifty_one = [
        _edge(f"证据 {index}", evidence=[str(UUID(int=index + 1))])
        for index in range(51)
    ]
    with pytest.raises(RelationshipEdgeError, match="最多引用 50"):
        validate_relationship_edges(session, fifty_one)


def test_upsert_replaces_composite_key_atomically_and_detects_conflict():
    session = _session()
    session["relationship_edges"] = [_edge("盟友"), _edge("竞争", target="gamma")]
    updated = _edge("挚友", target="gamma", strength=90)
    with pytest.raises(RelationshipEdgeError, match="已存在"):
        upsert_relationship_edge(
            session,
            _edge("竞争", target="gamma"),
            original_key={
                "source_character_id": "alpha",
                "target_character_id": "beta",
                "relation_type": "盟友",
            },
        )
    assert [edge["relation_type"] for edge in session["relationship_edges"]] == ["盟友", "竞争"]

    result = upsert_relationship_edge(
        session,
        updated,
        original_key={
            "source_character_id": "alpha",
            "target_character_id": "beta",
            "relation_type": "盟友",
        },
    )
    assert result["relation_type"] == "挚友"
    assert {edge["relation_type"] for edge in session["relationship_edges"]} == {"挚友", "竞争"}


def test_trim_protects_evidence_without_pinning_and_reconcile_deletes_empty_edge():
    session = _session(message_count=6)
    evidence_id = session["message_history"][0]["id"]
    session["relationship_edges"] = [_edge(evidence=[evidence_id])]
    dropped = trim_history(session, max_messages=2)
    assert [message["id"] for message in dropped] == [
        str(UUID(int=2)),
        str(UUID(int=3)),
        str(UUID(int=4)),
    ]
    protected = next(message for message in session["message_history"] if message["id"] == evidence_id)
    assert protected["pinned"] is False
    assert [message["id"] for message in session["message_history"]] == [
        evidence_id,
        str(UUID(int=5)),
        str(UUID(int=6)),
    ]

    session["message_history"] = [message for message in session["message_history"] if message["id"] != evidence_id]
    assert reconcile_relationship_evidence(session) is True
    assert session["relationship_edges"] == []


def test_import_relationship_edges_is_strict_traceable_and_never_inferred():
    messages = [_message(0), _message(1)]
    base = {
        "session_id": "relationship_import",
        "characters_state": {
            "alpha": {"name": "阿尔法", "affinity": 99, "mood": "亲密"},
            "beta": {"name": "贝塔"},
        },
        "message_history": messages,
        "summaries": [{"relations": ["阿尔法与贝塔关系密切"]}],
    }
    without_edges = validate_import_json(json.dumps(base, ensure_ascii=False))
    assert without_edges["relationship_edges"] == []

    valid_edge = _edge("  Ｓｔｒａße  ", evidence=[messages[0]["id"]])
    imported = validate_import_json(json.dumps({
        **base,
        "relationship_edges": [valid_edge],
    }, ensure_ascii=False))
    assert imported["relationship_edges"][0]["relation_type"] == "Straße"

    invalid_payloads = (
        {**valid_edge, "extra": True},
        {key: value for key, value in valid_edge.items() if key != "updated_at"},
        {**valid_edge, "source_character_id": "missing"},
        {**valid_edge, "evidence_message_ids": [str(uuid4())]},
        {**valid_edge, "strength": True},
    )
    for invalid in invalid_payloads:
        with pytest.raises(ValueError):
            validate_import_json(json.dumps({
                **base,
                "relationship_edges": [invalid],
            }, ensure_ascii=False))

    duplicate_message = {**messages[1], "id": messages[0]["id"]}
    with pytest.raises(ValueError, match="重复消息 UUID"):
        validate_import_json(json.dumps({
            **base,
            "message_history": [messages[0], duplicate_message],
            "relationship_edges": [valid_edge],
        }, ensure_ascii=False))


def test_import_relationship_limits_and_nfkc_composite_uniqueness():
    messages = [_message(index) for index in range(51)]
    base = {
        "session_id": "relationship_import_limits",
        "characters_state": {
            "alpha": {"name": "阿尔法"},
            "beta": {"name": "贝塔"},
        },
        "message_history": messages[:50],
    }
    two_hundred = [
        _edge(
            f"关系 {index}",
            evidence=[messages[index % 50]["id"]],
        )
        for index in range(200)
    ]
    accepted = validate_import_json(json.dumps({
        **base,
        "relationship_edges": two_hundred,
    }, ensure_ascii=False))
    assert len(accepted["relationship_edges"]) == 200
    assert len({
        message_id
        for edge in accepted["relationship_edges"]
        for message_id in edge["evidence_message_ids"]
    }) == 50

    with pytest.raises(ValueError, match="at most 200|最多 200"):
        validate_import_json(json.dumps({
            **base,
            "relationship_edges": [*two_hundred, _edge("第 201 条")],
        }, ensure_ascii=False))

    fifty_one = [
        _edge(f"证据 {index}", evidence=[messages[index]["id"]])
        for index in range(51)
    ]
    with pytest.raises(ValueError, match="最多引用 50"):
        validate_import_json(json.dumps({
            **base,
            "message_history": messages,
            "relationship_edges": fifty_one,
        }, ensure_ascii=False))

    with pytest.raises(ValueError, match="复合键"):
        validate_import_json(json.dumps({
            **base,
            "relationship_edges": [
                _edge("  Ｓｔｒａße  "),
                _edge("STRASSE"),
            ],
        }, ensure_ascii=False))

    for unsafe in ("盟\u202e友", "盟\u200b友", "A\u0085B", "\ud800", "\udfff"):
        with pytest.raises(ValueError, match="控制、格式或代理"):
            validate_import_json(json.dumps({
                **base,
                "relationship_edges": [_edge(unsafe)],
            }, ensure_ascii=True))


def test_updated_at_is_parseable_for_auditing():
    edge = validate_relationship_edges(_session(), [_edge()])[0]
    assert datetime.fromisoformat(edge["updated_at"])
