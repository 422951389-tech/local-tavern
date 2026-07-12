import asyncio

import httpx
import pytest

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
async def test_bad_ndjson_chunk_is_skipped_without_losing_valid_events():
    body = (
        b"not-json\n"
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


@pytest.mark.asyncio
async def test_http_error_becomes_stream_error_event():
    transport = httpx.MockTransport(lambda request: httpx.Response(503, text="offline"))
    client = OllamaClient("http://ollama.invalid")
    client._client = httpx.AsyncClient(base_url=client.host, transport=transport)
    try:
        events = await _collect(client)
    finally:
        await client.close()

    assert events == [{"type": "error", "content": "HTTP 503: offline"}]


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
