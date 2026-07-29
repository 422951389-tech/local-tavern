from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.api_errors import http_error_response
from core.config import PORT
from core.provider_registry import ProviderRegistry
from core.secret_store import SecretStore
import routes.providers as provider_routes


ORIGIN = f"http://127.0.0.1:{PORT}"


def PUBLIC_RESOLVER(_host: str) -> tuple[str, ...]:
    return ("93.184.216.34",)


class ReversingProtector:
    prefix = b"protected-v1:"

    def protect(self, plaintext: bytes) -> bytes:
        return self.prefix + plaintext[::-1]

    def unprotect(self, protected: bytes) -> bytes:
        return protected[len(self.prefix):][::-1]


def _app(registry: ProviderRegistry, monkeypatch) -> FastAPI:
    monkeypatch.setattr(provider_routes, "get_provider_registry", lambda: registry)
    app = FastAPI()
    app.include_router(provider_routes.router)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_request, exc):
        return http_error_response(exc.status_code, exc.detail, headers=exc.headers)

    return app


def _registry(tmp_path, handler) -> ProviderRegistry:
    root = tmp_path / "LocalAppData" / "LocalTavern"
    return ProviderRegistry(
        root / "providers.json",
        secret_store=SecretStore(
            root / "provider-secrets.json",
            protector=ReversingProtector(),
        ),
        resolver=PUBLIC_RESOLVER,
        transport=httpx.MockTransport(handler),
    )


def _error(response: httpx.Response) -> dict:
    payload = response.json()
    assert set(payload) == {"error"}
    return payload["error"]


@pytest.mark.asyncio
async def test_provider_config_credential_test_and_delete_crud_is_private(tmp_path, monkeypatch):
    upstream_requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "model-b"}, {"id": "model-a"}]})

    registry = _registry(tmp_path, upstream)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(registry, monkeypatch), raise_app_exceptions=False),
        base_url=ORIGIN,
    )
    api_key = "sk-route-DO-NOT-ECHO"
    try:
        presets = await client.get("/api/providers/presets")
        assert presets.status_code == 200
        assert [item["id"] for item in presets.json()["presets"]] == [
            "openai", "deepseek", "siliconflow", "anthropic", "custom",
        ]

        rebinding_origin = await client.put(
            "/api/providers/rebinding",
            json={"preset": "openai"},
            headers={"Host": "evil.example", "Origin": "http://evil.example"},
        )
        assert rebinding_origin.status_code == 403
        assert _error(rebinding_origin)["code"] == "provider_origin_forbidden"

        no_origin = await client.put(
            "/api/providers/cloud-main",
            json={"preset": "openai"},
        )
        assert no_origin.status_code == 403
        assert _error(no_origin)["code"] == "provider_origin_forbidden"

        wrong_type = await client.put(
            "/api/providers/cloud-main",
            content=json.dumps({"preset": "openai"}),
            headers={"Origin": ORIGIN, "Content-Type": "text/plain"},
        )
        assert wrong_type.status_code == 415

        created = await client.put(
            "/api/providers/cloud-main",
            json={"preset": "openai", "models": ["configured-model"]},
            headers={"Origin": ORIGIN},
        )
        assert created.status_code == 200, created.text
        provider = created.json()["provider"]
        assert provider["has_credential"] is False
        assert provider["base_url"] == "https://api.openai.com/v1"
        assert provider["capabilities"]["request_thinking"] is False

        credential = await client.put(
            "/api/providers/cloud-main/credential",
            json={"api_key": api_key},
            headers={"Origin": ORIGIN},
        )
        assert credential.status_code == 200, credential.text
        assert credential.json() == {
            "credential": {"provider_id": "cloud-main", "configured": True}
        }
        assert api_key not in credential.text

        fetched = await client.get("/api/providers/cloud-main")
        assert fetched.status_code == 200
        assert fetched.json()["provider"]["has_credential"] is True
        assert api_key not in fetched.text
        assert api_key not in registry.config_path.read_text(encoding="utf-8")
        assert api_key not in registry.secret_store.path.read_text(encoding="utf-8")

        tested = await client.post(
            "/api/providers/cloud-main/test",
            headers={"Origin": ORIGIN},
        )
        assert tested.status_code == 200, tested.text
        assert tested.json()["test"]["models"] == ["model-a", "model-b"]
        assert upstream_requests[0].headers["authorization"] == f"Bearer {api_key}"
        assert api_key not in tested.text

        rejected_rebind = await client.put(
            "/api/providers/cloud-main",
            json={"preset": "deepseek"},
            headers={"Origin": ORIGIN},
        )
        assert rejected_rebind.status_code == 409
        assert _error(rejected_rebind)["code"] == "provider_credential_rebind_required"
        assert api_key not in rejected_rebind.text
        assert registry.get_config("cloud-main").base_url == "https://api.openai.com/v1"
        assert {request.url.host for request in upstream_requests} == {"api.openai.com"}

        removed_credential = await client.delete(
            "/api/providers/cloud-main/credential",
            headers={"Origin": ORIGIN},
        )
        assert removed_credential.status_code == 200
        assert removed_credential.json()["credential"]["configured"] is False

        accepted_rebind = await client.put(
            "/api/providers/cloud-main",
            json={"preset": "deepseek"},
            headers={"Origin": ORIGIN},
        )
        assert accepted_rebind.status_code == 200
        assert accepted_rebind.json()["provider"]["base_url"] == "https://api.deepseek.com/v1"

        missing_credential = await client.post(
            "/api/providers/cloud-main/test",
            headers={"Origin": ORIGIN},
        )
        assert missing_credential.status_code == 409
        assert _error(missing_credential)["code"] == "provider_credential_missing"
        assert api_key not in missing_credential.text

        deleted = await client.delete(
            "/api/providers/cloud-main",
            headers={"Origin": ORIGIN},
        )
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True, "provider_id": "cloud-main"}
    finally:
        await client.aclose()
        await registry.close()


@pytest.mark.asyncio
async def test_provider_routes_reject_private_urls_and_never_persist_rejected_config(
    tmp_path,
    monkeypatch,
):
    registry = _registry(tmp_path, lambda _request: httpx.Response(500))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(registry, monkeypatch), raise_app_exceptions=False),
        base_url=ORIGIN,
    )
    try:
        for base_url in (
            "http://api.example.com/v1",
            "https://127.0.0.1/v1",
            "https://169.254.169.254/latest/meta-data",
            "https://user:secret@api.example.com/v1",
            "https://api.example.com/v1?api_key=secret",
            "https://api.example.com/v1#secret",
        ):
            response = await client.put(
                "/api/providers/rejected",
                json={"preset": "custom", "base_url": base_url},
                headers={"Origin": ORIGIN},
            )
            assert response.status_code == 400, (base_url, response.text)
            assert "secret" not in response.text
        providers = await client.get("/api/providers")
        assert [item["provider_id"] for item in providers.json()["providers"]] == ["ollama"]
        assert not registry.config_path.exists()
        assert not registry.secret_store.path.exists()
    finally:
        await client.aclose()
        await registry.close()


@pytest.mark.asyncio
async def test_provider_test_does_not_follow_redirect_or_expose_upstream_body(
    tmp_path,
    monkeypatch,
):
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"location": "http://169.254.169.254/latest/meta-data"},
            text="UPSTREAM-SECRET-BODY",
        )

    registry = _registry(tmp_path, redirect)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(registry, monkeypatch), raise_app_exceptions=False),
        base_url=ORIGIN,
    )
    api_key = "sk-redirect-private"
    try:
        created = await client.put(
            "/api/providers/cloud-main",
            json={"preset": "openai"},
            headers={"Origin": ORIGIN},
        )
        assert created.status_code == 200
        saved = await client.put(
            "/api/providers/cloud-main/credential",
            json={"api_key": api_key},
            headers={"Origin": ORIGIN},
        )
        assert saved.status_code == 200

        response = await client.post(
            "/api/providers/cloud-main/test",
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 502, response.text
        error = _error(response)
        assert error["code"] == "provider_http_error"
        assert error["details"] == {"upstream_status": 302}
        assert len(requests) == 1
        assert "UPSTREAM-SECRET-BODY" not in response.text
        assert api_key not in response.text
    finally:
        await client.aclose()
        await registry.close()


@pytest.mark.asyncio
async def test_credential_write_requires_same_origin_json_and_redacts_bad_body(
    tmp_path,
    monkeypatch,
):
    registry = _registry(tmp_path, lambda _request: httpx.Response(200, json={"data": []}))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(registry, monkeypatch), raise_app_exceptions=False),
        base_url=ORIGIN,
    )
    api_key = "sk-invalid-body-secret"
    try:
        created = await client.put(
            "/api/providers/cloud-main",
            json={"preset": "openai"},
            headers={"Origin": ORIGIN},
        )
        assert created.status_code == 200

        cross_origin = await client.put(
            "/api/providers/cloud-main/credential",
            json={"api_key": api_key},
            headers={"Origin": "https://evil.example"},
        )
        assert cross_origin.status_code == 403
        assert api_key not in cross_origin.text

        wrong_type = await client.put(
            "/api/providers/cloud-main/credential",
            content=json.dumps({"api_key": api_key}),
            headers={"Origin": ORIGIN, "Content-Type": "text/plain"},
        )
        assert wrong_type.status_code == 415
        assert api_key not in wrong_type.text

        invalid = await client.put(
            "/api/providers/cloud-main/credential",
            json={"api_key": api_key, "unexpected": api_key},
            headers={"Origin": ORIGIN},
        )
        assert invalid.status_code == 400
        assert _error(invalid)["code"] == "invalid_provider_request"
        assert api_key not in invalid.text
        assert not registry.secret_store.path.exists()
    finally:
        await client.aclose()
        await registry.close()
