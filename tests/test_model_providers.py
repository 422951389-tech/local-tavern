from __future__ import annotations

import asyncio
import gzip
import json
import threading

import httpx
import pytest

import core.ollama_client as ollama_module
import core.provider_registry as provider_registry_module
import core.providers.http_base as http_base_module
from core.model_provider import (
    ModelProvider,
    ProviderError,
    ProviderValidationError,
    validate_cloud_base_url,
)
from core.provider_registry import ProviderRegistry, ProviderRegistryError
from core.providers.anthropic import AnthropicProvider
from core.providers.ollama import OllamaProvider
from core.providers.openai_compatible import OpenAICompatibleProvider
from core.secret_store import SecretStore, default_app_data_dir


def PUBLIC_RESOLVER(_host: str) -> tuple[str, ...]:
    return ("93.184.216.34",)


class ReversingProtector:
    prefix = b"protected-v1:"

    def protect(self, plaintext: bytes) -> bytes:
        return self.prefix + plaintext[::-1]

    def unprotect(self, protected: bytes) -> bytes:
        if not protected.startswith(self.prefix):
            raise ValueError("invalid test ciphertext")
        return protected[len(self.prefix):][::-1]


class FakeOllama:
    def __init__(self, label: str) -> None:
        self.label = label

    async def list_models(self):
        return [self.label]

    async def chat_stream(self, **_kwargs):
        yield {"type": "content", "content": self.label}
        yield {"type": "done", "content": ""}

    async def summarize_once(self, *_args, **_kwargs):
        return f"summary:{self.label}"

    async def close(self):
        return None


class TrackingCloudProvider:
    capabilities = OpenAICompatibleProvider.capabilities
    instances: list[TrackingCloudProvider] = []
    failing_credentials: set[str] = set()

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

    async def close(self):
        self.close_calls += 1
        if self.credential in type(self).failing_credentials:
            raise RuntimeError("intentional close failure")


def test_cloud_url_validation_blocks_ssrf_and_ambiguous_components():
    blocked = [
        "http://api.example.com/v1",
        "https://user:password@api.example.com/v1",
        "https://127.0.0.1/v1",
        "https://10.0.0.8/v1",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/v1",
        "https://metadata.google.internal/v1",
        "https://api.example.com/v1?key=secret",
        "https://api.example.com/v1#fragment",
        "https://api.example.com/v1/../admin",
    ]
    for url in blocked:
        with pytest.raises(ProviderValidationError):
            validate_cloud_base_url(url, resolver=PUBLIC_RESOLVER)

    with pytest.raises(ProviderValidationError, match="公网"):
        validate_cloud_base_url(
            "https://rebinding.example/v1",
            resolver=lambda _host: ("127.0.0.1",),
        )
    assert validate_cloud_base_url(
        "https://API.EXAMPLE.com/v1/",
        resolver=PUBLIC_RESOLVER,
    ) == "https://api.example.com/v1"


@pytest.mark.asyncio
async def test_connect_phase_rebinding_is_rejected_before_transport_or_credentials(
    monkeypatch,
):
    resolutions = iter((
        ("93.184.216.34",),
        ("127.0.0.1",),
    ))
    resolver_calls: list[str] = []
    transport_calls: list[tuple] = []

    def rebinding_resolver(host: str) -> tuple[str, ...]:
        resolver_calls.append(host)
        return next(resolutions)

    def reject_transport(*args, **kwargs):
        transport_calls.append((args, kwargs))
        raise AssertionError("私网二次解析后不得创建底层 transport")

    monkeypatch.setattr(
        http_base_module,
        "_PinnedAsyncHTTPTransport",
        reject_transport,
    )
    credential = "sk-must-not-reach-connect"
    provider = OpenAICompatibleProvider(
        "https://rebinding.example/v1",
        credential,
        resolver=rebinding_resolver,
    )

    with pytest.raises(ProviderValidationError) as raised:
        await provider.list_models()

    assert raised.value.code == "provider_url_private_forbidden"
    assert credential not in str(raised.value)
    assert resolver_calls == ["rebinding.example", "rebinding.example"]
    assert transport_calls == []
    assert provider._client is None


@pytest.mark.asyncio
async def test_pinned_transport_preserves_port_host_and_tls_sni():
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = http_base_module._PinnedAsyncHTTPTransport(
        "api.example.com",
        ("93.184.216.34",),
    )
    original = transport._transport
    await original.aclose()
    transport._transport = httpx.MockTransport(handler)
    try:
        response = await transport.handle_async_request(httpx.Request(
            "GET",
            "https://api.example.com:8443/v1",
            headers={"Authorization": "Bearer credential"},
        ))
        assert response.status_code == 200
    finally:
        await transport.aclose()

    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "https://93.184.216.34:8443/v1"
    assert request.headers["host"] == "api.example.com:8443"
    assert request.extensions["sni_hostname"] == "api.example.com"


def test_secret_store_is_external_atomic_and_never_writes_plaintext(tmp_path, monkeypatch):
    local_app_data = tmp_path / "LocalAppData"
    monkeypatch.delenv("TAVERN_PROVIDER_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    assert default_app_data_dir() == local_app_data / "LocalTavern"

    secret_path = default_app_data_dir() / "provider-secrets.json"
    store = SecretStore(secret_path, protector=ReversingProtector())
    api_key = "sk-test-DO-NOT-ECHO"
    store.set("cloud-main", api_key)

    assert store.get("cloud-main") == api_key
    assert store.configured("cloud-main") is True
    assert store.list_keys() == ("cloud-main",)
    assert api_key not in secret_path.read_text(encoding="utf-8")
    assert not list(secret_path.parent.glob("*.tmp"))
    assert store.delete("cloud-main") is True
    assert store.get("cloud-main") is None


@pytest.mark.asyncio
async def test_registry_environment_override_is_isolated_and_read_only(tmp_path, monkeypatch):
    isolated = (tmp_path / "isolated-provider-data").resolve()
    monkeypatch.setenv("TAVERN_PROVIDER_DATA_DIR", str(isolated))
    monkeypatch.delenv("TAVERN_PROVIDER_CONFIG_PATH", raising=False)
    monkeypatch.delenv("TAVERN_PROVIDER_SECRETS_PATH", raising=False)
    store = SecretStore(protector=ReversingProtector())
    registry = ProviderRegistry(secret_store=store)
    try:
        assert registry.config_path == isolated / "providers.json"
        assert registry.secret_store.path == isolated / "provider-secrets.json"
        assert registry.list_configs()[0]["provider_id"] == "ollama"
        assert not isolated.exists()
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_saved_cloud_config_can_load_offline_and_revalidates_before_use(tmp_path):
    root = tmp_path / "LocalAppData" / "LocalTavern"
    secret_store = SecretStore(
        root / "provider-secrets.json",
        protector=ReversingProtector(),
    )
    online = ProviderRegistry(
        root / "providers.json",
        secret_store=secret_store,
        resolver=PUBLIC_RESOLVER,
    )
    config = online.build_config(
        "cloud-main",
        kind="openai_compatible",
        name="Cloud Main",
        base_url="https://api.example.com/v1",
        models=["model-a"],
        context_limit=65_536,
    )
    await online.upsert_config(config)
    await online.set_credential("cloud-main", "sk-private")
    await online.close()

    offline = ProviderRegistry(
        root / "providers.json",
        secret_store=secret_store,
        resolver=lambda _host: (),
    )
    try:
        assert offline.public_config("cloud-main")["models"] == ["model-a"]
        assert offline.public_config("cloud-main")["context_limit"] == 65_536
        with pytest.raises(ProviderValidationError, match="公网"):
            offline.resolve("cloud-main")
    finally:
        await offline.close()


@pytest.mark.asyncio
async def test_ollama_adapter_resolves_the_injected_global_client_dynamically(monkeypatch):
    provider = OllamaProvider()
    assert isinstance(provider, ModelProvider)

    first = FakeOllama("first")
    second = FakeOllama("second")
    monkeypatch.setattr(ollama_module, "_client_instance", first)
    assert await provider.list_models() == ["first"]
    monkeypatch.setattr(ollama_module, "_client_instance", second)
    assert await provider.list_models() == ["second"]
    assert [
        event
        async for event in provider.chat_stream(
            "model",
            [{"role": "user", "content": "test"}],
            think=False,
            num_predict=12,
            temperature=0.1,
            top_p=0.8,
            top_k=20,
            num_ctx=4096,
        )
    ][0]["content"] == "second"


@pytest.mark.asyncio
async def test_openai_compatible_models_stream_keepalive_done_and_summary_are_native():
    requests: list[tuple[httpx.Request, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        requests.append((request, payload))
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gpt-b"}, {"id": "gpt-a"}]})
        assert request.url.path == "/v1/chat/completions"
        if payload["stream"]:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=(
                    ": keep-alive\n\n"
                    "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"思\",\"content\":\"答\"},\"finish_reason\":null}]}\n\n"
                    "data: [DONE]\n\n"
                ),
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "<thinking>丢弃</thinking>摘要"}}]},
        )

    provider = OpenAICompatibleProvider(
        "https://api.example.com/v1",
        "sk-private",
        resolver=PUBLIC_RESOLVER,
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await provider.list_models() == ["gpt-a", "gpt-b"]
        events = [
            event
            async for event in provider.chat_stream(
                "gpt-a",
                [{"role": "user", "content": "你好"}],
            )
        ]
        summary = await provider.summarize_once(
            "gpt-a",
            [{"role": "user", "content": "旧对话"}],
        )
    finally:
        await provider.close()

    assert events == [
        {"type": "keepalive", "content": ""},
        {"type": "thinking", "content": "思"},
        {"type": "content", "content": "答"},
        {"type": "done", "content": ""},
    ]
    assert summary == "摘要"
    assert all(request.headers["authorization"] == "Bearer sk-private" for request, _ in requests)
    assert requests[1][1]["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_openai_error_is_redacted_and_redirect_is_not_followed():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
            text="SECRET-UPSTREAM-BODY",
        )

    provider = OpenAICompatibleProvider(
        "https://api.example.com/v1",
        "sk-private",
        resolver=PUBLIC_RESOLVER,
        transport=httpx.MockTransport(handler),
    )
    try:
        events = [
            event
            async for event in provider.chat_stream(
                "gpt-a",
                [{"role": "user", "content": "test"}],
            )
        ]
    finally:
        await provider.close()

    assert len(requests) == 1
    assert events[0]["code"] == "provider_http_error"
    assert events[0]["http_status"] == 307
    assert "SECRET" not in json.dumps(events, ensure_ascii=False)
    assert "sk-private" not in json.dumps(events, ensure_ascii=False)


@pytest.mark.asyncio
async def test_json_response_declared_and_decompressed_limits_share_stable_error_code():
    declared_limit = http_base_module.PROVIDER_RESPONSE_MAX_BYTES + 1

    def declared_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "content-length": str(declared_limit),
            },
            content=b"{}",
        )

    declared = OpenAICompatibleProvider(
        "https://api.example.com/v1",
        "sk-private",
        resolver=PUBLIC_RESOLVER,
        transport=httpx.MockTransport(declared_handler),
    )
    try:
        with pytest.raises(ProviderError) as raised_declared:
            await declared.list_models()
    finally:
        await declared.close()
    assert raised_declared.value.code == "provider_response_too_large"

    raw = json.dumps(
        {"data": [], "padding": "x" * http_base_module.PROVIDER_RESPONSE_MAX_BYTES},
        separators=(",", ":"),
    ).encode("utf-8")
    compressed = gzip.compress(raw)
    assert len(compressed) < http_base_module.PROVIDER_RESPONSE_MAX_BYTES

    def compressed_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",
                "content-length": str(len(compressed)),
            },
            content=compressed,
        )

    decompressed = OpenAICompatibleProvider(
        "https://api.example.com/v1",
        "sk-private",
        resolver=PUBLIC_RESOLVER,
        transport=httpx.MockTransport(compressed_handler),
    )
    try:
        with pytest.raises(ProviderError) as raised_decompressed:
            await decompressed.list_models()
    finally:
        await decompressed.close()
    assert raised_decompressed.value.code == "provider_response_too_large"


@pytest.mark.asyncio
async def test_sse_oversized_line_and_total_emit_provider_response_too_large():
    oversized_line = (
        b"data: "
        + b"x" * (http_base_module.PROVIDER_SSE_LINE_MAX_BYTES + 1)
        + b"\n\n"
    )

    def line_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=oversized_line,
        )

    openai = OpenAICompatibleProvider(
        "https://api.example.com/v1",
        "sk-private",
        resolver=PUBLIC_RESOLVER,
        transport=httpx.MockTransport(line_handler),
    )
    try:
        line_events = [
            event
            async for event in openai.chat_stream(
                "gpt-test",
                [{"role": "user", "content": "test"}],
            )
        ]
    finally:
        await openai.close()
    assert line_events == [{
        "type": "error",
        "code": "provider_response_too_large",
        "content": "Provider 响应超过本地安全上限",
    }]

    record = b"event: ping\ndata: {}\n\n"
    oversized_total = record * (
        http_base_module.PROVIDER_RESPONSE_MAX_BYTES // len(record) + 1
    )

    def total_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=oversized_total,
        )

    anthropic = AnthropicProvider(
        "https://api.anthropic.example",
        "anthropic-private",
        resolver=PUBLIC_RESOLVER,
        transport=httpx.MockTransport(total_handler),
    )
    try:
        total_events = [
            event
            async for event in anthropic.chat_stream(
                "claude-test",
                [{"role": "user", "content": "test"}],
            )
        ]
    finally:
        await anthropic.close()
    assert total_events == [{
        "type": "error",
        "code": "provider_response_too_large",
        "content": "Provider 响应超过本地安全上限",
    }]


@pytest.mark.asyncio
async def test_anthropic_native_stream_fallback_models_and_summary():
    requests: list[tuple[httpx.Request, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        requests.append((request, payload))
        if request.url.path == "/v1/models":
            return httpx.Response(404, json={"type": "error"})
        assert request.url.path == "/v1/messages"
        if payload["stream"]:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=(
                    "event: ping\ndata: {\"type\":\"ping\"}\n\n"
                    "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"delta\":{\"type\":\"thinking_delta\",\"thinking\":\"分析\"}}\n\n"
                    "event: unknown_future_event\ndata: {\"type\":\"unknown_future_event\",\"secret\":\"ignored\"}\n\n"
                    "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"delta\":{\"type\":\"signature_delta\",\"signature\":\"ignored\"}}\n\n"
                    "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"delta\":{\"type\":\"text_delta\",\"text\":\"正文\"}}\n\n"
                    "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
                ),
            )
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "Anthropic 摘要"}]},
        )

    provider = AnthropicProvider(
        "https://api.anthropic.example",
        "anthropic-private",
        configured_models=("claude-configured",),
        resolver=PUBLIC_RESOLVER,
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await provider.list_models() == ["claude-configured"]
        events = [
            event
            async for event in provider.chat_stream(
                "claude-configured",
                [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "hello"},
                ],
            )
        ]
        summary = await provider.summarize_once(
            "claude-configured",
            [{"role": "assistant", "content": "old"}],
        )
    finally:
        await provider.close()

    assert events == [
        {"type": "keepalive", "content": ""},
        {"type": "thinking", "content": "分析"},
        {"type": "content", "content": "正文"},
        {"type": "done", "content": ""},
    ]
    assert summary == "Anthropic 摘要"
    stream_request, stream_payload = requests[1]
    assert stream_request.headers["x-api-key"] == "anthropic-private"
    assert stream_request.headers["anthropic-version"] == "2023-06-01"
    assert stream_payload["system"] == "system"
    assert "thinking" not in stream_payload


@pytest.mark.asyncio
async def test_anthropic_stream_error_is_redacted_and_not_retried():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                "event: error\n"
                "data: {\"type\":\"error\",\"error\":{\"message\":\"UPSTREAM-SECRET\"}}\n\n"
            ),
        )

    provider = AnthropicProvider(
        "https://api.anthropic.example",
        "anthropic-private",
        resolver=PUBLIC_RESOLVER,
        transport=httpx.MockTransport(handler),
    )
    try:
        events = [
            event
            async for event in provider.chat_stream(
                "claude-test",
                [{"role": "user", "content": "test"}],
            )
        ]
    finally:
        await provider.close()

    assert calls == 1
    assert events == [{
        "type": "error",
        "code": "provider_stream_error",
        "content": "Provider 流式响应失败",
    }]
    assert "UPSTREAM-SECRET" not in json.dumps(events, ensure_ascii=False)
    assert "anthropic-private" not in json.dumps(events, ensure_ascii=False)


@pytest.mark.asyncio
async def test_registry_keeps_config_and_secret_separate(tmp_path):
    secret_store = SecretStore(
        tmp_path / "local-app-data" / "provider-secrets.json",
        protector=ReversingProtector(),
    )
    registry = ProviderRegistry(
        tmp_path / "local-app-data" / "providers.json",
        secret_store=secret_store,
        resolver=PUBLIC_RESOLVER,
    )
    config = registry.build_config(
        "cloud-main",
        kind="openai_compatible",
        name="Cloud Main",
        base_url="https://api.example.com/v1",
        models=["model-a"],
        context_limit=65_536,
        preset="custom",
    )
    try:
        public = await registry.upsert_config(config)
        assert public["has_credential"] is False
        assert public["context_limit"] == 65_536
        await registry.set_credential("cloud-main", "sk-registry-private")
        public = registry.public_config("cloud-main")
        assert public["has_credential"] is True
        assert "credential" not in public and "api_key" not in public
        assert "sk-registry-private" not in registry.config_path.read_text(encoding="utf-8")
        assert "sk-registry-private" not in secret_store.path.read_text(encoding="utf-8")
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_delete_config_never_leaves_reusable_orphan_secret_on_config_write_failure(
    tmp_path,
    monkeypatch,
):
    secret_store = SecretStore(
        tmp_path / "local-app-data" / "provider-secrets.json",
        protector=ReversingProtector(),
    )
    registry = ProviderRegistry(
        tmp_path / "local-app-data" / "providers.json",
        secret_store=secret_store,
        resolver=PUBLIC_RESOLVER,
    )
    config = registry.build_config(
        "cloud-main",
        kind="openai_compatible",
        name="Cloud Main",
        base_url="https://api.example.com/v1",
        models=["model-a"],
    )
    await registry.upsert_config(config)
    await registry.set_credential("cloud-main", "sk-delete-me")
    registry.resolve("cloud-main")

    original_persist = registry._persist

    def fail_persist():
        raise ProviderRegistryError(
            "provider_config_write_failed",
            "Provider 配置保存失败",
        )

    monkeypatch.setattr(registry, "_persist", fail_persist)
    try:
        with pytest.raises(ProviderRegistryError, match="配置保存失败"):
            await registry.delete_config("cloud-main")

        assert registry.get_config("cloud-main") == config
        assert secret_store.get("cloud-main") is None
        with pytest.raises(ProviderRegistryError, match="凭据未配置"):
            registry.resolve("cloud-main")

        monkeypatch.setattr(registry, "_persist", original_persist)
        assert await registry.delete_config("cloud-main") is True
        with pytest.raises(ProviderRegistryError, match="不存在"):
            registry.get_config("cloud-main")
    finally:
        await registry.close()


@pytest.mark.asyncio
async def test_registry_leases_pin_retired_versions_until_release(tmp_path, monkeypatch):
    TrackingCloudProvider.instances = []
    TrackingCloudProvider.failing_credentials = set()
    monkeypatch.setattr(
        provider_registry_module,
        "OpenAICompatibleProvider",
        TrackingCloudProvider,
    )
    secret_store = SecretStore(
        tmp_path / "local-app-data" / "provider-secrets.json",
        protector=ReversingProtector(),
    )
    registry = ProviderRegistry(
        tmp_path / "local-app-data" / "providers.json",
        secret_store=secret_store,
        resolver=PUBLIC_RESOLVER,
    )
    first_config = registry.build_config(
        "cloud-main",
        kind="openai_compatible",
        name="Cloud Main",
        base_url="https://api.example.com/v1",
        models=["model-old"],
    )
    await registry.upsert_config(first_config)
    await registry.set_credential("cloud-main", "key-old")
    old_lease = registry.lease("cloud-main")
    old_provider = old_lease.provider
    assert old_lease.config == first_config

    updated_config = registry.build_config(
        "cloud-main",
        kind="openai_compatible",
        name="Cloud Main Updated",
        base_url="https://api.example.com/v1",
        models=["model-new"],
        context_limit=65_536,
    )
    await registry.upsert_config(updated_config)
    retained_old_lease = old_lease.retain()
    assert retained_old_lease.version == old_lease.version
    assert retained_old_lease.provider is old_provider
    assert old_provider.close_calls == 0
    assert await old_provider.list_models() == ["model-old"]

    config_lease = registry.lease("cloud-main")
    config_provider = config_lease.provider
    assert config_lease.version > old_lease.version
    assert config_provider is not old_provider
    assert config_provider.configured_models == ("model-new",)
    assert config_provider.context_limit == 65_536
    assert config_provider.credential == "key-old"

    await registry.set_credential("cloud-main", "key-new")
    assert config_provider.close_calls == 0
    credential_lease = registry.lease("cloud-main")
    credential_provider = credential_lease.provider
    assert credential_lease.version > config_lease.version
    assert credential_provider.credential == "key-new"

    rebound_config = registry.build_config(
        "cloud-main",
        kind="openai_compatible",
        name="Rebound",
        base_url="https://other.example.com/v1",
        models=["model-new"],
    )
    with pytest.raises(ProviderRegistryError, match="先删除"):
        await registry.upsert_config(rebound_config)
    assert secret_store.get("cloud-main") == "key-new"
    same_version_lease = registry.lease("cloud-main")
    assert same_version_lease.provider is credential_provider
    await same_version_lease.release()

    await config_lease.release()
    assert config_provider.close_calls == 1
    await old_lease.release()
    assert old_provider.close_calls == 0
    with pytest.raises(RuntimeError, match="已释放"):
        old_lease.retain()
    await retained_old_lease.release()
    assert old_provider.close_calls == 1

    await registry.delete_config("cloud-main")
    assert credential_provider.close_calls == 0
    assert secret_store.get("cloud-main") is None
    await registry.upsert_config(updated_config)
    with pytest.raises(ProviderRegistryError, match="凭据未配置"):
        registry.lease("cloud-main")
    await registry.set_credential("cloud-main", "key-recreated")
    replacement_lease = registry.lease("cloud-main")
    assert replacement_lease.provider.credential == "key-recreated"

    await credential_lease.release()
    assert credential_provider.close_calls == 1
    await replacement_lease.release()
    await registry.close()
    assert replacement_lease.provider.close_calls == 1


@pytest.mark.asyncio
async def test_registry_close_is_best_effort_across_all_instances(tmp_path, monkeypatch):
    TrackingCloudProvider.instances = []
    TrackingCloudProvider.failing_credentials = {"key-fails-close"}
    monkeypatch.setattr(
        provider_registry_module,
        "OpenAICompatibleProvider",
        TrackingCloudProvider,
    )
    secret_store = SecretStore(
        tmp_path / "local-app-data" / "provider-secrets.json",
        protector=ReversingProtector(),
    )
    registry = ProviderRegistry(
        tmp_path / "local-app-data" / "providers.json",
        secret_store=secret_store,
        resolver=PUBLIC_RESOLVER,
    )
    for provider_id, credential in (
        ("fails-close", "key-fails-close"),
        ("closes-cleanly", "key-closes-cleanly"),
    ):
        config = registry.build_config(
            provider_id,
            kind="openai_compatible",
            name=provider_id,
            base_url=f"https://{provider_id}.example.com/v1",
            models=["model"],
        )
        await registry.upsert_config(config)
        await registry.set_credential(provider_id, credential)

    failing_lease = registry.lease("fails-close")
    clean_lease = registry.lease("closes-cleanly")
    await registry.close()

    assert failing_lease.provider.close_calls == 1
    assert clean_lease.provider.close_calls == 1
    await failing_lease.release()
    await clean_lease.release()
    await registry.close()
    assert failing_lease.provider.close_calls == 1
    assert clean_lease.provider.close_calls == 1
    with pytest.raises(ProviderRegistryError, match="已关闭"):
        registry.lease("closes-cleanly")


@pytest.mark.asyncio
async def test_cancelled_registry_update_finishes_retired_provider_cleanup(
    tmp_path,
    monkeypatch,
):
    TrackingCloudProvider.instances = []
    TrackingCloudProvider.failing_credentials = set()
    monkeypatch.setattr(
        provider_registry_module,
        "OpenAICompatibleProvider",
        TrackingCloudProvider,
    )
    secret_store = SecretStore(
        tmp_path / "local-app-data" / "provider-secrets.json",
        protector=ReversingProtector(),
    )
    registry = ProviderRegistry(
        tmp_path / "local-app-data" / "providers.json",
        secret_store=secret_store,
        resolver=PUBLIC_RESOLVER,
    )
    config = registry.build_config(
        "cloud-main",
        kind="openai_compatible",
        name="Cloud Main",
        base_url="https://api.example.com/v1",
        models=["model-a"],
    )
    await registry.upsert_config(config)
    await registry.set_credential("cloud-main", "key-old")
    lease = registry.lease("cloud-main")
    retired_provider = lease.provider
    await lease.release()

    entered_persist = threading.Event()
    release_persist = threading.Event()
    original_persist = registry._persist

    def delayed_persist():
        entered_persist.set()
        assert release_persist.wait(5)
        return original_persist()

    monkeypatch.setattr(registry, "_persist", delayed_persist)
    updated = registry.build_config(
        "cloud-main",
        kind="openai_compatible",
        name="Cloud Main Updated",
        base_url="https://api.example.com/v1",
        models=["model-b"],
    )
    task = asyncio.create_task(registry.upsert_config(updated))
    assert await asyncio.to_thread(entered_persist.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()

    release_persist.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert retired_provider.close_calls == 1
    assert registry.get_config("cloud-main") == updated
    await registry.close()


@pytest.mark.asyncio
async def test_provider_lease_release_resists_repeated_cancellation(
    tmp_path,
    monkeypatch,
):
    registry = ProviderRegistry(
        tmp_path / "providers.json",
        secret_store=SecretStore(
            tmp_path / "provider-secrets.json",
            protector=ReversingProtector(),
        ),
        resolver=PUBLIC_RESOLVER,
    )
    lease = registry.lease("ollama")
    started = asyncio.Event()
    finish = asyncio.Event()
    completed = asyncio.Event()
    original_release = registry._release_lease

    async def delayed_release(_slot):
        started.set()
        await finish.wait()
        completed.set()

    monkeypatch.setattr(registry, "_release_lease", delayed_release)
    task = asyncio.create_task(lease.release())
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()
    assert not completed.is_set()

    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()
    await original_release(lease._slot)
    await registry.close()


@pytest.mark.asyncio
async def test_provider_lease_acquire_cancellation_does_not_leak_lease(
    tmp_path,
    monkeypatch,
):
    registry = ProviderRegistry(
        tmp_path / "providers.json",
        secret_store=SecretStore(
            tmp_path / "provider-secrets.json",
            protector=ReversingProtector(),
        ),
        resolver=PUBLIC_RESOLVER,
    )
    lease_created = threading.Event()
    return_lease = threading.Event()
    original_lease = registry.lease
    captured = {}

    def delayed_lease(provider_id):
        lease = original_lease(provider_id)
        captured["lease"] = lease
        lease_created.set()
        assert return_lease.wait(5)
        return lease

    monkeypatch.setattr(registry, "lease", delayed_lease)
    task = asyncio.create_task(registry.acquire_lease("ollama"))
    assert await asyncio.to_thread(lease_created.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()

    return_lease.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert captured["lease"]._released is True
    assert captured["lease"]._slot.leases == 0
    await registry.close()
