import asyncio
import json

import httpx
import pytest

from core.config import PROMPT_CONTEXT_FALLBACK
from core.ollama_client import OllamaClient
from tests.fakes.fake_ollama import FakeOllamaClient


async def _collect(client: OllamaClient) -> list[dict]:
    return [
        event
        async for event in client.chat_stream(
            model="fake-model:latest",
            messages=[{"role": "user", "content": "测试"}],
        )
    ]


@pytest.mark.asyncio
async def test_bad_ndjson_chunk_is_skipped_without_losing_valid_events(caplog):
    body = (
        b"not-json-SECRET_CHAT_BODY\n"
        b'{"message":{"thinking":"test-thinking"},"done":false}\n'
        b'{"message":{"content":"test-content"},"done":false}\n'
        b'{"message":{},"done":true}\n'
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    client = OllamaClient("http://ollama.invalid")
    client._client = httpx.AsyncClient(base_url=client.host, transport=transport)
    try:
        events = await _collect(client)
    finally:
        await client.close()

    assert events == [
        {"type": "thinking", "content": "test-thinking"},
        {"type": "content", "content": "test-content"},
        {"type": "done", "content": ""},
    ]
    assert client._warned_json_parse is True
    assert "SECRET_CHAT_BODY" not in caplog.text
    assert "invalid_ndjson_chunk length=" in caplog.text


@pytest.mark.asyncio
async def test_chat_payload_uses_the_same_num_ctx_as_prompt_budget():
    captured: list[dict] = []

    def chat(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, content=b'{"message":{},"done":true}\n')

    client = OllamaClient("http://ollama.invalid")
    client._client = httpx.AsyncClient(
        base_url=client.host,
        transport=httpx.MockTransport(chat),
    )
    try:
        events = [
            event
            async for event in client.chat_stream(
                model="budget-aligned-model",
                messages=[{"role": "user", "content": "test"}],
                num_predict=512,
                num_ctx=8192,
            )
        ]
    finally:
        await client.close()

    assert events == [{"type": "done", "content": ""}]
    assert captured[0]["options"]["num_predict"] == 512
    assert captured[0]["options"]["num_ctx"] == 8192


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 503])
async def test_http_error_becomes_stream_error_event(status):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, text="offline"))
    client = OllamaClient("http://ollama.invalid")
    client._client = httpx.AsyncClient(base_url=client.host, transport=transport)
    try:
        events = await _collect(client)
    finally:
        await client.close()

    assert events == [{
        "type": "error",
        "code": "upstream_http_error",
        "http_status": status,
        "content": f"HTTP {status}: offline",
    }]


@pytest.mark.asyncio
async def test_network_error_becomes_stream_error_event():
    def fail(request: httpx.Request):
        raise httpx.ConnectError("blocked by test transport", request=request)

    transport = httpx.MockTransport(fail)
    client = OllamaClient("http://ollama.invalid")
    client._client = httpx.AsyncClient(base_url=client.host, transport=transport)
    try:
        events = await _collect(client)
    finally:
        await client.close()

    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert events[0]["code"] == "upstream_network_error"
    assert "blocked by test transport" in events[0]["content"]


@pytest.mark.asyncio
async def test_eof_without_done_is_characterized_explicitly():
    body = b'{"message":{"content":"partial"},"done":false}\n'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    client = OllamaClient("http://ollama.invalid")
    client._client = httpx.AsyncClient(base_url=client.host, transport=transport)
    try:
        events = await _collect(client)
    finally:
        await client.close()

    assert events == [{"type": "content", "content": "partial"}]


@pytest.mark.asyncio
async def test_delayed_fake_stream_propagates_cancellation():
    fake = FakeOllamaClient().configure("normal", delay=60.0)
    stream = fake.chat_stream(
        model="fake-model:latest",
        messages=[{"role": "user", "content": "取消测试"}],
    )
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await stream.aclose()


@pytest.mark.asyncio
async def test_fake_eof_scenario_has_no_done_event():
    fake = FakeOllamaClient().configure("eof")
    events = [
        event
        async for event in fake.chat_stream(
            model="fake-model:latest",
            messages=[{"role": "user", "content": "EOF 测试"}],
        )
    ]
    assert events == [{"type": "content", "content": "未完成的 fake 响应"}]


@pytest.mark.asyncio
async def test_context_limit_uses_architecture_model_info_and_caches_a_copy():
    requests: list[httpx.Request] = []

    def show(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={
            "model_info": {
                "general.architecture": "llama",
                "llama.context_length": 32768,
                "clip.context_length": 131072,
            },
        })

    client = OllamaClient("http://ollama.invalid")
    client._client = httpx.AsyncClient(
        base_url=client.host,
        transport=httpx.MockTransport(show),
    )
    try:
        first = await client.get_context_limit("model-info-test")
        first["context_limit"] = 1
        second = await client.get_context_limit("model-info-test")
    finally:
        await client.close()

    assert second == {
        "context_limit": 32768,
        "source": "ollama_show_model_info",
    }
    assert len(requests) == 1
    assert requests[0].url.path == "/api/show"
    assert requests[0].method == "POST"


@pytest.mark.asyncio
async def test_concurrent_context_limit_requests_share_one_show_call():
    request_count = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def show(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        entered.set()
        await release.wait()
        return httpx.Response(200, json={
            "model_info": {
                "general.architecture": "qwen2",
                "qwen2.context_length": 32768,
            },
        })

    client = OllamaClient("http://ollama.invalid")
    client._client = httpx.AsyncClient(
        base_url=client.host,
        transport=httpx.MockTransport(show),
    )
    try:
        tasks = [
            asyncio.create_task(client.get_context_limit("shared-model"))
            for _ in range(3)
        ]
        await asyncio.wait_for(entered.wait(), timeout=1)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
    finally:
        await client.close()

    assert results == [{
        "context_limit": 32768,
        "source": "ollama_show_model_info",
    }] * 3
    assert request_count == 1


@pytest.mark.asyncio
async def test_context_limit_parameters_override_is_bounded_by_model_info():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "model_info": {
            "general.architecture": "qwen2",
            "qwen2.context_length": 32768,
        },
        "parameters": "temperature 0.7\nnum_ctx 8192\ntop_p 0.9",
    }))
    client = OllamaClient("http://ollama.invalid")
    client._client = httpx.AsyncClient(base_url=client.host, transport=transport)
    try:
        result = await client.get_context_limit("parameters-test")
    finally:
        await client.close()

    assert result == {
        "context_limit": 8192,
        "source": "ollama_show_parameters",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_factory",
    [
        pytest.param(
            lambda: httpx.Response(503, text="offline"),
            id="http-error",
        ),
        pytest.param(
            lambda: httpx.Response(
                200,
                content=b"not-json",
                headers={"content-type": "application/json"},
            ),
            id="invalid-json",
        ),
        pytest.param(
            lambda: httpx.Response(200, json={
                "model_info": {"general.architecture": "missing"},
                "parameters": "temperature 0.8",
            }),
            id="missing-context-field",
        ),
        pytest.param(
            lambda: httpx.Response(200, json={
                "model_info": {"bad.context_length": True},
                "parameters": "num_ctx 2",
            }),
            id="invalid-context-values",
        ),
    ],
)
async def test_context_limit_metadata_failures_use_and_cache_fallback(response_factory):
    request_count = 0

    def show(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return response_factory()

    client = OllamaClient("http://ollama.invalid")
    client._client = httpx.AsyncClient(
        base_url=client.host,
        transport=httpx.MockTransport(show),
    )
    try:
        first = await client.get_context_limit("fallback-test")
        second = await client.get_context_limit("fallback-test")
    finally:
        await client.close()

    assert first == second == {
        "context_limit": PROMPT_CONTEXT_FALLBACK,
        "source": "fallback_default",
    }
    assert request_count == 1
