"""PROMPT-1：单次来源注入、总预算与原子拒绝契约。"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from core.chat_turns import get_turn_coordinator
from core.prompt_assembler import (
    PromptAssembler,
    PromptBudgetExceeded,
    PromptTemplateInvalid,
)
from core.session_manager import (
    append_history,
    get_session_store,
    mutate_session,
)
from core.token_estimator import ConservativeTokenEstimator


REPO_ROOT = Path(__file__).resolve().parents[1]
SAVE = "默认存档"
MODEL = "fake-model:latest"
SOURCE_KEYS = {"source", "id", "estimated_tokens", "kept", "reason"}
FORBIDDEN_GHOSTS = ("角色B", "秧秧", "炽霞")


def _test_assembler(tmp_path: Path) -> PromptAssembler:
    (tmp_path / "system.md").write_text("SYS_SENTINEL", encoding="utf-8")
    (tmp_path / "group_chat.md").write_text(
        "\n".join([
            "GROUP_SENTINEL",
            "scene={{scene_meta_json}}",
            "profile={{user_profile_json}}",
            "characters={{character_context}}",
            "worldbook={{worldbook_entries}}",
            "memory={{history}}",
            "input={{user_input}}",
        ]),
        encoding="utf-8",
    )
    return PromptAssembler(prompts_dir=tmp_path)


def _assemble(
    assembler: PromptAssembler,
    *,
    characters: list[dict] | None = None,
    characters_state: dict | None = None,
    worldbook_entries: list[dict] | None = None,
    history: list[dict] | None = None,
    summaries: list[dict] | None = None,
    user_input: str = "INPUT",
    context_limit: int = 1_048_576,
    num_predict: int = 64,
    safety_margin: int = 128,
):
    return assembler.assemble(
        user_input=user_input,
        characters=characters or [],
        characters_state=characters_state or {},
        scene_meta={"location": "SCENE"},
        user_profile={"name": "PROFILE"},
        worldbook_entries=worldbook_entries or [],
        history=history or [],
        summaries=summaries or [],
        context_limit=context_limit,
        context_limit_source="test_context_limit",
        num_predict=num_predict,
        safety_margin=safety_margin,
    )


def _all_content(assembly) -> str:
    return "\n".join(str(message.get("content", "")) for message in assembly.messages)


def test_conservative_estimator_is_deterministic_for_ascii_chinese_and_emoji():
    estimator = ConservativeTokenEstimator()

    assert estimator.estimate_text("") == 0
    assert estimator.estimate_text("ASCII") == len("ASCII".encode("utf-8"))
    assert estimator.estimate_text("中文") == len("中文".encode("utf-8"))
    assert estimator.estimate_text("🧭") == len("🧭".encode("utf-8"))
    assert estimator.estimate_messages([{"role": "user", "content": "中文🧭"}]) > (
        estimator.estimate_text("中文🧭")
    )


def test_required_prompt_exact_budget_passes_and_one_token_less_rejects(tmp_path):
    assembler = _test_assembler(tmp_path)
    baseline = _assemble(assembler)
    required_tokens = baseline.diagnostics["estimated_prompt_tokens"]
    num_predict = 64
    safety_margin = 128

    exact = _assemble(
        assembler,
        context_limit=required_tokens + num_predict + safety_margin,
        num_predict=num_predict,
        safety_margin=safety_margin,
    )
    assert exact.diagnostics["remaining_input_tokens"] == 0

    with pytest.raises(PromptBudgetExceeded) as caught:
        _assemble(
            assembler,
            context_limit=required_tokens + num_predict + safety_margin - 1,
            num_predict=num_predict,
            safety_margin=safety_margin,
        )
    assert caught.value.code == "prompt_budget_exceeded"
    assert all(
        source["reason"] == "required_exceeds_budget"
        for source in caught.value.diagnostics["sources"]
        if source["source"] in {
            "system_rules",
            "group_instructions",
            "scene",
            "user_profile",
            "current_input",
        }
    )


def test_runtime_template_slots_must_each_appear_exactly_once(tmp_path):
    assembler = _test_assembler(tmp_path)
    group_path = tmp_path / "group_chat.md"
    group_path.write_text(
        group_path.read_text(encoding="utf-8") + "\nduplicate={{user_input}}",
        encoding="utf-8",
    )

    with pytest.raises(PromptTemplateInvalid) as caught:
        _assemble(assembler)

    assert caught.value.code == "prompt_template_invalid"
    assert caught.value.as_detail()["violations"] == [
        "group_chat.md:user_input:expected_1_found_2"
    ]


def test_each_runtime_source_is_injected_once_without_cascading_template_values(tmp_path):
    assembler = _test_assembler(tmp_path)
    assembly = assembler.assemble(
        user_input="INPUT_SENTINEL {{worldbook_entries}}",
        characters=[{
            "id": "character-one",
            "name": "CHARACTER_SENTINEL",
            "persona": "角色字面量 {{user_input}}",
            "appearance": {"outfit": "OUTFIT_SENTINEL"},
        }],
        characters_state={
            "character-one": {
                "mood": "STATE_SENTINEL",
                "outfit": "OUTFIT_SENTINEL",
            },
        },
        scene_meta={
            "location": "SCENE_SENTINEL",
            "literal": "{{character_context}}",
        },
        user_profile={
            "name": "PROFILE_SENTINEL",
            "literal": "{{scene_meta_json}}",
        },
        worldbook_entries=[{
            "id": "world-one",
            "content": "WORLDBOOK_SENTINEL {{history}}",
        }],
        history=[{
            "id": "history-one",
            "role": "assistant",
            "content": "HISTORY_SENTINEL",
        }],
        summaries=[{
            "id": "summary-one",
            "text": "SUMMARY_SENTINEL",
        }],
        context_limit=1_048_576,
        context_limit_source="test_context_limit",
        num_predict=64,
        safety_margin=128,
    )

    content = _all_content(assembly)
    for sentinel in (
        "SYS_SENTINEL",
        "GROUP_SENTINEL",
        "INPUT_SENTINEL",
        "CHARACTER_SENTINEL",
        "STATE_SENTINEL",
        "OUTFIT_SENTINEL",
        "SCENE_SENTINEL",
        "PROFILE_SENTINEL",
        "WORLDBOOK_SENTINEL",
        "HISTORY_SENTINEL",
        "SUMMARY_SENTINEL",
    ):
        assert content.count(sentinel) == 1, sentinel

    # 这些 token 来自插入值；单次模板扫描不能再次解释它们。
    for literal_token in (
        "{{worldbook_entries}}",
        "{{user_input}}",
        "{{character_context}}",
        "{{scene_meta_json}}",
        "{{history}}",
    ):
        assert content.count(literal_token) == 1, literal_token

    assert assembly.diagnostics["estimated_prompt_tokens"] <= assembly.diagnostics[
        "input_budget_tokens"
    ]
    assert all(set(source) == SOURCE_KEYS for source in assembly.diagnostics["sources"])


def test_optional_growth_is_trimmed_with_body_free_diagnostics(tmp_path):
    assembler = _test_assembler(tmp_path)
    baseline = _assemble(assembler)
    num_predict = 64
    safety_margin = 128
    optional_allowance = 750
    input_budget = baseline.diagnostics["estimated_prompt_tokens"] + optional_allowance

    history = [
        {
            "id": f"history-{index}",
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"HISTORY_{index}_" + ("H" * 400),
        }
        for index in range(4)
    ]
    worldbook = [
        {
            "id": f"world-{index}",
            "content": f"WORLD_{index}_" + ("W" * 200),
        }
        for index in range(4)
    ]
    summaries = [
        {
            "id": f"summary-{index}",
            "text": f"SUMMARY_{index}_" + ("S" * 10),
        }
        for index in range(4)
    ]

    assembly = _assemble(
        assembler,
        history=history,
        worldbook_entries=worldbook,
        summaries=summaries,
        context_limit=input_budget + num_predict + safety_margin,
        num_predict=num_predict,
        safety_margin=safety_margin,
    )

    diagnostics = assembly.diagnostics
    assert diagnostics["estimated_prompt_tokens"] <= diagnostics["input_budget_tokens"]
    assert diagnostics["remaining_input_tokens"] >= 0
    assert all(set(source) == SOURCE_KEYS for source in diagnostics["sources"])

    by_source = {
        source_name: [
            item for item in diagnostics["sources"] if item["source"] == source_name
        ]
        for source_name in ("history", "worldbook", "summary")
    }
    for source_name, entries in by_source.items():
        assert entries, source_name
        assert any(entry["kept"] for entry in entries), source_name
        assert any(not entry["kept"] for entry in entries), source_name
        assert any(
            entry["reason"] in {
                "budget_exceeded",
                "budget_exceeded_after_newer_history",
            }
            for entry in entries
            if not entry["kept"]
        ), source_name


@pytest.mark.parametrize("character_count", [0, 1, 4], ids=["zero", "one", "many"])
def test_production_prompt_uses_only_the_zero_one_or_many_real_characters(character_count):
    assembler = PromptAssembler(prompts_dir=REPO_ROOT / "prompts")
    characters = [
        {
            "id": f"actual-{index}",
            "name": f"REAL_CHARACTER_{index}",
            "persona": f"persona-{index}",
        }
        for index in range(character_count)
    ]
    assembly = _assemble(
        assembler,
        characters=characters,
        characters_state={
            character["id"]: {"mood": f"mood-{index}"}
            for index, character in enumerate(characters)
        },
    )
    content = _all_content(assembly)

    for index in range(character_count):
        assert content.count(f"REAL_CHARACTER_{index}") == 1
    assert not any(ghost in content for ghost in FORBIDDEN_GHOSTS)
    if character_count == 0:
        assert "无活跃角色" in content


def test_current_and_latest_default_production_templates_have_no_named_ghosts():
    prompt_dir = REPO_ROOT / "prompts"
    paths = [
        prompt_dir / "system.md",
        prompt_dir / "group_chat.md",
        prompt_dir / "summary.md",
    ]
    for name in ("system", "group_chat", "summary"):
        backups = sorted((prompt_dir / ".default").glob(f"{name}.md.v*-bak"))
        assert backups, f"{name} 缺少版本化默认模板"
        paths.append(backups[-1])
        assert (prompt_dir / f"{name}.md").read_bytes() == backups[-1].read_bytes()

    for path in paths:
        content = path.read_text(encoding="utf-8")
        assert not any(ghost in content for ghost in FORBIDDEN_GHOSTS), path


def test_summary_time_and_only_valid_content_enter_prompt_once(tmp_path):
    assembler = _test_assembler(tmp_path)
    assembly = _assemble(
        assembler,
        summaries=[
            {
                "id": "valid-summary",
                "status": "completed",
                "content_status": "valid",
                "text": "VALID_SUMMARY_TEXT",
                "time": "VALID_SUMMARY_TIME",
                "facts": [],
                "relations": [],
            },
            {
                "id": "empty-pending",
                "status": "pending",
                "content_status": "empty",
                "text": "SHOULD_NOT_ENTER_PROMPT",
            },
        ],
        context_limit=1_048_576,
    )
    content = _all_content(assembly)
    assert content.count("VALID_SUMMARY_TEXT") == 1
    assert content.count("VALID_SUMMARY_TIME") == 1
    assert "SHOULD_NOT_ENTER_PROMPT" not in content


@pytest.mark.asyncio
async def test_chat_budget_rejection_is_422_before_turn_model_or_session_write(
    app_client,
    fake_ollama,
    seed_project,
    monkeypatch,
):
    project = seed_project("prompt_budget_chat_reject")
    store = get_session_store()
    coordinator = get_turn_coordinator()
    before_session = deepcopy(store.read_sync(project, SAVE))
    before_turn_ids = coordinator.store.list_turn_ids()
    before_records = deepcopy(coordinator._records)
    before_chat_calls = deepcopy(fake_ollama.chat_calls)
    write_calls: list[tuple[str, str]] = []
    original_write = store._write_session_sync

    def write_spy(session: dict, write_project: str, save_id: str) -> None:
        write_calls.append((write_project, save_id))
        original_write(session, write_project, save_id)

    monkeypatch.setattr(store, "_write_session_sync", write_spy)
    fake_ollama.context_limits[MODEL] = {
        "context_limit": 4096,
        "source": "forced_too_small",
    }

    response = await app_client.post("/api/chat", json={
        "project": project,
        "save": SAVE,
        "user_input": "预算拒绝不得落盘",
        "model": MODEL,
        "expected_revision": before_session["revision"],
    })

    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "prompt_budget_exceeded"
    assert detail["diagnostics"]["context_limit_source"] == "forced_too_small"
    after_session = store.read_sync(project, SAVE)
    assert after_session == before_session
    assert after_session["revision"] == before_session["revision"]
    assert after_session["message_history"] == before_session["message_history"]
    assert coordinator.store.list_turn_ids() == before_turn_ids
    assert coordinator._records == before_records
    assert fake_ollama.chat_calls == before_chat_calls
    assert write_calls == []


@pytest.mark.asyncio
async def test_regenerate_budget_rejection_preserves_history_revision_and_turns(
    app_client,
    fake_ollama,
    seed_project,
    monkeypatch,
):
    project = seed_project("prompt_budget_regenerate_reject")
    store = get_session_store()
    current = store.read_sync(project, SAVE)
    old_turn_id = str(uuid4())
    message_ids: dict[str, str] = {}

    def seed_history(session: dict, context) -> None:
        del context
        session["current_model"] = MODEL
        user = append_history(
            session,
            "user",
            "需要重新生成的用户输入",
            metadata={"turn_id": old_turn_id, "in_prompt": True},
        )
        assistant = append_history(
            session,
            "assistant",
            "旧回复",
            metadata={"turn_id": old_turn_id, "in_prompt": True},
        )
        message_ids["target"] = assistant["id"]
        message_ids["source"] = user["id"]

    seeded = await mutate_session(
        project,
        SAVE,
        current["revision"],
        seed_history,
    )
    before_session = deepcopy(seeded.session)
    coordinator = get_turn_coordinator()
    before_turn_ids = coordinator.store.list_turn_ids()
    before_records = deepcopy(coordinator._records)
    write_calls: list[tuple[str, str]] = []
    original_write = store._write_session_sync

    def write_spy(session: dict, write_project: str, save_id: str) -> None:
        write_calls.append((write_project, save_id))
        original_write(session, write_project, save_id)

    monkeypatch.setattr(store, "_write_session_sync", write_spy)
    fake_ollama.context_limits[MODEL] = {
        "context_limit": 256,
        "source": "forced_regenerate_too_small",
    }

    response = await app_client.post("/api/chat/turns/regenerate", json={
        "project": project,
        "save": SAVE,
        "message_id": message_ids["target"],
        "model": MODEL,
        "expected_revision": before_session["revision"],
        "num_predict": 256,
    })

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "prompt_budget_exceeded"
    assert store.read_sync(project, SAVE) == before_session
    assert coordinator.store.list_turn_ids() == before_turn_ids
    assert coordinator._records == before_records
    assert fake_ollama.chat_calls == []
    assert write_calls == []
