from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

import core.provider_registry as provider_registry_module
from core.model_provider import ProviderCapabilities, ProviderError
from core.provider_registry import ProviderRegistry, reset_provider_registry_for_testing
from core.secret_store import SecretStore
from core import summary_lifecycle


def _public_resolver(_host: str) -> tuple[str, ...]:
    return ("93.184.216.34",)


class _Protector:
    prefix = b"provider-integration:"

    def protect(self, plaintext: bytes) -> bytes:
        return self.prefix + plaintext[::-1]

    def unprotect(self, protected: bytes) -> bytes:
        if not protected.startswith(self.prefix):
            raise ValueError("invalid test ciphertext")
        return protected[len(self.prefix):][::-1]


async def _registry(tmp_path, handler) -> ProviderRegistry:
    root = tmp_path / "provider-data"
    registry = ProviderRegistry(
        root / "providers.json",
        secret_store=SecretStore(
            root / "provider-secrets.json",
            protector=_Protector(),
        ),
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )
    config = registry.build_config(
        "cloud-main",
        kind="openai_compatible",
        name="Cloud Main",
        base_url="https://api.example.com/v1",
        models=["cloud-model"],
        context_limit=32_768,
    )
    await registry.upsert_config(config)
    await registry.set_credential("cloud-main", "sk-integration-private")
    return registry


async def _wait_terminal(client, turn_id: str) -> dict:
    for _attempt in range(100):
        turn = (await client.get(f"/api/chat/turns/{turn_id}")).json()
        if turn["status"] in {"completed", "failed", "cancelled"}:
            return turn
        await asyncio.sleep(0.01)
    raise AssertionError("云端 turn 未进入终态")


class _TurnProvider:
    def __init__(self, *, runtime_error: ProviderError | None = None):
        self.runtime_error = runtime_error

    async def get_context_limit(self, _model: str) -> dict[str, object]:
        return {"context_limit": 32_768, "source": "provider_config"}

    async def chat_stream(self, **_kwargs):
        if self.runtime_error is not None:
            raise self.runtime_error
        yield {"type": "content", "content": "云端首轮完成"}
        yield {"type": "done", "content": ""}


class _TurnRegistry:
    def __init__(self, provider: _TurnProvider):
        self.provider = provider
        self.lease_calls: list[str] = []
        self.leases: list[_TurnLease] = []

    def lease(self, provider_id: str):
        self.lease_calls.append(provider_id)
        lease = _TurnLease(self.provider)
        self.leases.append(lease)
        return lease

    async def acquire_lease(self, provider_id: str):
        return self.lease(provider_id)


class _TurnLease:
    def __init__(self, provider: _TurnProvider):
        self.provider = provider
        self.config = SimpleNamespace(models=("cloud-model",))
        self.release_calls = 0

    async def release(self):
        self.release_calls += 1


class _BlockingProvider:
    capabilities = ProviderCapabilities()
    instances: list[_BlockingProvider] = []
    stream_started: asyncio.Event
    stream_release: asyncio.Event
    summary_started: asyncio.Event
    summary_release: asyncio.Event

    def __init__(
        self,
        base_url,
        credential,
        *,
        configured_models=(),
        context_limit=32_768,
        **_kwargs,
    ):
        self.base_url = base_url
        self.credential = credential
        self.configured_models = tuple(configured_models)
        self.context_limit = context_limit
        self.close_calls = 0
        type(self).instances.append(self)

    async def list_models(self):
        return list(self.configured_models)

    async def get_context_limit(self, _model):
        return {"context_limit": self.context_limit, "source": "provider_config"}

    async def chat_stream(self, **_kwargs):
        type(self).stream_started.set()
        await type(self).stream_release.wait()
        yield {
            "type": "content",
            "content": f"在途实例凭据={self.credential}",
        }
        yield {"type": "done", "content": ""}

    async def summarize_once(self, _model, _dropped):
        type(self).summary_started.set()
        await type(self).summary_release.wait()
        return f"前情提要: 在途摘要凭据={self.credential}"

    async def close(self):
        self.close_calls += 1


def _reset_blocking_provider() -> None:
    _BlockingProvider.instances = []
    _BlockingProvider.stream_started = asyncio.Event()
    _BlockingProvider.stream_release = asyncio.Event()
    _BlockingProvider.summary_started = asyncio.Event()
    _BlockingProvider.summary_release = asyncio.Event()


class _ForbiddenOllamaRegistry:
    def resolve(self, provider_id: str):
        raise AssertionError(f"显式云端首轮不应访问 {provider_id}")

    async def list_models(self, provider_id: str) -> list[str]:
        raise AssertionError(f"显式云端首轮不应枚举 {provider_id}")


@pytest.mark.asyncio
async def test_explicit_cloud_first_turn_never_probes_ollama(
    app_client,
    seed_project,
    monkeypatch,
):
    import routes.chat as chat_routes
    import routes.common as common_routes

    registry = _TurnRegistry(_TurnProvider())
    monkeypatch.setattr(chat_routes, "get_provider_registry", lambda: registry)
    monkeypatch.setattr(
        common_routes,
        "get_provider_registry",
        lambda: _ForbiddenOllamaRegistry(),
    )
    project = seed_project("provider_explicit_cloud_first_turn")

    created = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": "默认存档",
        "user_input": "首轮直接使用云端",
        "provider": "cloud-main",
        "model": "cloud-model",
        "expected_revision": 0,
    })

    assert created.status_code == 202, created.text
    terminal = await _wait_terminal(app_client, created.json()["turn_id"])
    assert terminal["status"] == "completed", terminal
    assert registry.lease_calls == ["cloud-main"]
    assert registry.leases[0].release_calls == 1


@pytest.mark.asyncio
async def test_provider_initialization_error_keeps_stable_public_code(
    app_client,
    seed_project,
    monkeypatch,
):
    import routes.chat as chat_routes

    provider = _TurnProvider(runtime_error=ProviderError(
        "provider_host_unresolved",
        "Provider 主机无法解析",
    ))
    registry = _TurnRegistry(provider)
    monkeypatch.setattr(chat_routes, "get_provider_registry", lambda: registry)
    project = seed_project("provider_runtime_initialization_error")

    created = await app_client.post("/api/chat/turns", json={
        "project": project,
        "save": "默认存档",
        "user_input": "触发请求前 DNS 校验",
        "provider": "cloud-main",
        "model": "cloud-model",
        "expected_revision": 0,
    })

    assert created.status_code == 202, created.text
    terminal = await _wait_terminal(app_client, created.json()["turn_id"])
    assert terminal["status"] == "failed"
    assert terminal["error"] == {
        "code": "provider_host_unresolved",
        "message": "Provider 主机无法解析",
    }
    session = (await app_client.get("/api/session", params={
        "project": project,
        "save": "默认存档",
    })).json()
    assert session["message_history"][0]["error"] == terminal["error"]


@pytest.mark.asyncio
async def test_turn_start_failure_releases_prepared_provider_lease(
    seed_project,
    monkeypatch,
):
    import routes.chat as chat_routes

    registry = _TurnRegistry(_TurnProvider())

    class FailingCoordinator:
        async def start(self, **_kwargs):
            raise RuntimeError("coordinator start failed")

    monkeypatch.setattr(chat_routes, "get_provider_registry", lambda: registry)
    monkeypatch.setattr(chat_routes, "get_turn_coordinator", lambda: FailingCoordinator())
    project = seed_project("provider_lease_start_failure")
    request = chat_routes.ChatRequest(
        project=project,
        save="默认存档",
        user_input="启动失败",
        provider="cloud-main",
        model="cloud-model",
        expected_revision=0,
    )

    with pytest.raises(RuntimeError, match="coordinator start failed"):
        await chat_routes._start_turn(request)
    assert registry.lease_calls == ["cloud-main"]
    assert registry.leases[0].release_calls == 1


@pytest.mark.asyncio
async def test_cloud_provider_turn_uses_configured_context_and_persists_selection(
    app_client,
    seed_project,
    tmp_path,
):
    requests: list[tuple[httpx.Request, dict | None]] = []
    content = (
        "📍 测试室 | ⏱️ 白天\n"
        "🎯 [主线] 任务名称：验证云端\n"
        "📌 当前场景：连接完成\n"
        "➡️ 下一目标：继续测试\n"
        "👤 测试用户\n"
        "📖 场景旁白\n云端适配器返回了结构化正文。\n"
        "💡 行动建议\n- 继续"
    )

    def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        requests.append((request, payload))
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                "data: "
                + json.dumps({
                    "choices": [{
                        "delta": {"content": content},
                        "finish_reason": None,
                    }]
                }, ensure_ascii=False)
                + "\n\ndata: [DONE]\n\n"
            ),
        )

    registry = await _registry(tmp_path, upstream)
    await reset_provider_registry_for_testing(registry)
    project = seed_project("provider_cloud_turn")
    try:
        created = await app_client.post("/api/chat/turns", json={
            "project": project,
            "save": "默认存档",
            "user_input": "调用云端",
            "provider": "cloud-main",
            "model": "cloud-model",
            "expected_revision": 0,
        })
        assert created.status_code == 202, created.text
        turn = created.json()
        assert turn["provider"] == "cloud-main"
        assert turn["prompt_diagnostics"]["context_limit"] == 32_768
        assert turn["prompt_diagnostics"]["context_limit_source"] == "provider_config"
        assert turn["prompt_diagnostics"]["input_budget_tokens"] > 0

        terminal = await _wait_terminal(app_client, turn["turn_id"])
        assert terminal["status"] == "completed", terminal
        assert len(requests) == 1
        assert requests[0][1]["model"] == "cloud-model"
        assert requests[0][1]["max_tokens"] == 4096
        assert requests[0][0].headers["authorization"] == "Bearer sk-integration-private"

        session = (await app_client.get("/api/session", params={
            "project": project,
            "save": "默认存档",
        })).json()
        assert session["current_provider"] == "cloud-main"
        assert session["current_model"] == "cloud-model"
        assert session["message_history"][-1]["content"] == content

        reset = await app_client.post("/api/session/reset", json={
            "project": project,
            "save": "默认存档",
            "expected_revision": session["revision"],
        })
        assert reset.status_code == 200, reset.text
        reset_session = reset.json()["session"]
        assert reset_session["current_provider"] == "cloud-main"
        assert reset_session["current_model"] == "cloud-model"
        assert reset_session["message_history"] == []

        mismatched_override = await app_client.post("/api/chat/turns", json={
            "project": project,
            "save": "默认存档",
            "user_input": "不能复用其他 Provider 的模型",
            "provider": "ollama",
            "expected_revision": reset_session["revision"],
        })
        assert mismatched_override.status_code == 400
        assert mismatched_override.json()["error"]["message"] == "未指定模型"
    finally:
        await reset_provider_registry_for_testing(None)


@pytest.mark.asyncio
async def test_inflight_chat_keeps_leased_instance_during_config_and_key_update(
    app_client,
    seed_project,
    tmp_path,
    monkeypatch,
):
    _reset_blocking_provider()
    monkeypatch.setattr(
        provider_registry_module,
        "OpenAICompatibleProvider",
        _BlockingProvider,
    )
    registry = await _registry(
        tmp_path,
        lambda _request: httpx.Response(500),
    )
    await reset_provider_registry_for_testing(registry)
    project = seed_project("provider_leased_inflight_chat")
    try:
        created = await app_client.post("/api/chat/turns", json={
            "project": project,
            "save": "默认存档",
            "user_input": "验证在途租约",
            "provider": "cloud-main",
            "model": "cloud-model",
            "expected_revision": 0,
        })
        assert created.status_code == 202, created.text
        await asyncio.wait_for(_BlockingProvider.stream_started.wait(), timeout=1)
        old_provider = _BlockingProvider.instances[0]

        updated = registry.build_config(
            "cloud-main",
            kind="openai_compatible",
            name="Cloud Main Updated",
            base_url="https://api.example.com/v1",
            models=["cloud-model", "cloud-model-new"],
            context_limit=65_536,
        )
        await registry.upsert_config(updated)
        await registry.set_credential("cloud-main", "sk-updated-private")
        assert old_provider.close_calls == 0

        newest_lease = registry.lease("cloud-main")
        assert newest_lease.provider is not old_provider
        assert newest_lease.config == updated
        assert newest_lease.provider.credential == "sk-updated-private"
        await newest_lease.release()

        _BlockingProvider.stream_release.set()
        terminal = await _wait_terminal(app_client, created.json()["turn_id"])
        assert terminal["status"] == "completed", terminal
        session = (await app_client.get("/api/session", params={
            "project": project,
            "save": "默认存档",
        })).json()
        assert session["message_history"][-1]["content"] == (
            "在途实例凭据=sk-integration-private"
        )
        for _attempt in range(100):
            if old_provider.close_calls == 1:
                break
            await asyncio.sleep(0.01)
        assert old_provider.close_calls == 1
    finally:
        _BlockingProvider.stream_release.set()
        await reset_provider_registry_for_testing(None)


@pytest.mark.asyncio
async def test_summary_generation_uses_frozen_cloud_provider(
    tmp_path,
    monkeypatch,
):
    requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "前情提要: 云端摘要"}}]},
        )

    registry = await _registry(tmp_path, upstream)
    await reset_provider_registry_for_testing(registry)
    updates: list[dict] = []

    async def capture_update(
        project,
        save,
        summary_id,
        generation_id,
        replacement,
    ):
        updates.append({
            "project": project,
            "save": save,
            "summary_id": summary_id,
            "generation_id": generation_id,
            "replacement": replacement,
        })
        return True

    monkeypatch.setattr(summary_lifecycle, "_apply_generation_update", capture_update)
    try:
        await summary_lifecycle._generate_summary(
            "project",
            "save",
            "cloud-model",
            "summary-id",
            "generation-id",
            [{"role": "user", "content": "旧对话"}],
            provider="cloud-main",
        )
    finally:
        await reset_provider_registry_for_testing(None)

    assert len(requests) == 1
    assert requests[0].url.path == "/v1/chat/completions"
    assert updates[0]["replacement"]["status"] == "completed"
    assert updates[0]["replacement"]["text"] == "云端摘要"


@pytest.mark.asyncio
async def test_inflight_summary_retains_chat_provider_version_during_updates(
    tmp_path,
    monkeypatch,
):
    _reset_blocking_provider()
    monkeypatch.setattr(
        provider_registry_module,
        "OpenAICompatibleProvider",
        _BlockingProvider,
    )
    registry = await _registry(
        tmp_path,
        lambda _request: httpx.Response(500),
    )
    await reset_provider_registry_for_testing(registry)
    updates: list[dict] = []

    async def capture_update(
        project,
        save,
        summary_id,
        generation_id,
        replacement,
    ):
        del project, save, summary_id, generation_id
        updates.append(replacement)
        return True

    monkeypatch.setattr(summary_lifecycle, "_apply_generation_update", capture_update)
    source_lease = registry.lease("cloud-main")
    old_provider = source_lease.provider
    try:
        task = await summary_lifecycle.schedule_summary_generation(
            "project",
            "save",
            "cloud-model",
            "summary-lease-id",
            "summary-generation-id",
            [{"role": "user", "content": "旧对话"}],
            provider="cloud-main",
            provider_lease=source_lease,
            retain_provider_lease=True,
        )
        await asyncio.wait_for(_BlockingProvider.summary_started.wait(), timeout=1)
        await source_lease.release()

        updated = registry.build_config(
            "cloud-main",
            kind="openai_compatible",
            name="Cloud Main Summary Updated",
            base_url="https://api.example.com/v1",
            models=["cloud-model"],
            context_limit=65_536,
        )
        await registry.upsert_config(updated)
        await registry.set_credential("cloud-main", "sk-summary-updated")
        assert old_provider.close_calls == 0

        newest_lease = registry.lease("cloud-main")
        assert newest_lease.provider.credential == "sk-summary-updated"
        await newest_lease.release()

        _BlockingProvider.summary_release.set()
        await asyncio.wait_for(task, timeout=1)
        assert updates[0]["status"] == "completed"
        assert updates[0]["text"] == "在途摘要凭据=sk-integration-private"
        assert old_provider.close_calls == 1
    finally:
        _BlockingProvider.summary_release.set()
        await source_lease.release()
        await summary_lifecycle.shutdown_summary_tasks()
        await reset_provider_registry_for_testing(None)


@pytest.mark.asyncio
async def test_anthropic_native_provider_runs_through_persistent_turn_pipeline(
    app_client,
    seed_project,
    tmp_path,
):
    requests: list[tuple[httpx.Request, dict]] = []
    content = (
        "📍 测试室 | ⏱️ 夜晚\n"
        "🎯 [主线] 任务名称：验证 Anthropic\n"
        "📌 当前场景：原生流已接通\n"
        "➡️ 下一目标：完成验收\n"
        "👤 测试用户\n"
        "📖 场景旁白\nAnthropic Messages 流已进入统一 turn。\n"
        "💡 行动建议\n- 完成验收"
    )

    def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append((request, payload))
        assert request.url.path == "/v1/messages"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                "event: content_block_delta\n"
                "data: "
                + json.dumps({
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": content},
                }, ensure_ascii=False)
                + "\n\nevent: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
            ),
        )

    root = tmp_path / "anthropic-provider-data"
    registry = ProviderRegistry(
        root / "providers.json",
        secret_store=SecretStore(
            root / "provider-secrets.json",
            protector=_Protector(),
        ),
        resolver=_public_resolver,
        transport=httpx.MockTransport(upstream),
    )
    config = registry.build_config(
        "anthropic-main",
        kind="anthropic",
        name="Anthropic Main",
        base_url="https://api.anthropic.com",
        models=["claude-test"],
        context_limit=32_768,
        preset="anthropic",
    )
    await registry.upsert_config(config)
    await registry.set_credential("anthropic-main", "anthropic-integration-private")
    await reset_provider_registry_for_testing(registry)
    project = seed_project("provider_anthropic_turn")
    try:
        created = await app_client.post("/api/chat/turns", json={
            "project": project,
            "save": "默认存档",
            "user_input": "调用 Anthropic",
            "provider": "anthropic-main",
            "model": "claude-test",
            "expected_revision": 0,
        })
        assert created.status_code == 202, created.text
        terminal = await _wait_terminal(app_client, created.json()["turn_id"])
        assert terminal["status"] == "completed", terminal
        assert len(requests) == 1
        request, payload = requests[0]
        assert request.headers["x-api-key"] == "anthropic-integration-private"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert payload["model"] == "claude-test"
        assert "thinking" not in payload
    finally:
        await reset_provider_registry_for_testing(None)
