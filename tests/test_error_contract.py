"""统一 HTTP 错误契约与写接口请求边界回归测试。"""
from __future__ import annotations

import re

import httpx
import pytest

from core.active_turns import begin_maintenance, end_maintenance
from core.api_errors import SECURITY_HEADERS
from core.config import PORT
from server import app
from tests.data_guard import file_manifest


WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
EXPECTED_WRITE_ROUTES = frozenset({
    ("POST", "/api/backups/restores/{restore_id}/recover"),
    ("POST", "/api/backups/retention/apply"),
    ("POST", "/api/backups"),
    ("POST", "/api/backups/{backup_id}/dry-run"),
    ("POST", "/api/backups/{backup_id}/drill"),
    ("POST", "/api/backups/{backup_id}/restore"),
    ("PUT", "/api/characters/{char_id}"),
    ("DELETE", "/api/characters/{char_id}"),
    ("POST", "/api/chat/turns"),
    ("POST", "/api/chat/turns/regenerate"),
    ("POST", "/api/chat/turns/{turn_id}/cancel"),
    ("POST", "/api/chat"),
    ("POST", "/api/model/switch"),
    ("POST", "/api/memory-notes"),
    ("PATCH", "/api/memory-notes/{note_id}"),
    ("DELETE", "/api/memory-notes/{note_id}"),
    ("PATCH", "/api/session"),
    ("PATCH", "/api/session/reply-alternative"),
    ("POST", "/api/session/restore"),
    ("POST", "/api/session/summary/regenerate"),
    ("PATCH", "/api/session/summary"),
    ("POST", "/api/migrations/legacy/plan"),
    ("POST", "/api/migrations/legacy/{plan_id}/apply"),
    ("POST", "/api/migrations/{migration_id}/recover"),
    ("POST", "/api/projects"),
    ("DELETE", "/api/projects"),
    ("PUT", "/api/prompts/{name}"),
    ("POST", "/api/prompts/{name}/reset"),
    ("PUT", "/api/providers/{provider_id}"),
    ("DELETE", "/api/providers/{provider_id}"),
    ("PUT", "/api/providers/{provider_id}/credential"),
    ("DELETE", "/api/providers/{provider_id}/credential"),
    ("POST", "/api/providers/{provider_id}/test"),
    ("POST", "/api/recovery/quarantine"),
    ("POST", "/api/recovery/{recovery_id}/restore"),
    ("PUT", "/api/session/relationships"),
    ("DELETE", "/api/session/relationships"),
    ("PATCH", "/api/session/characters/{character_id}/silence"),
    ("PATCH", "/api/session/roleplay-policy"),
    ("POST", "/api/sessions"),
    ("POST", "/api/sessions/rename"),
    ("POST", "/api/sessions/delete"),
    ("POST", "/api/sessions/import"),
    ("POST", "/api/session/reset"),
    ("PUT", "/api/settings"),
    ("PUT", "/api/user"),
    ("DELETE", "/api/user"),
    ("PUT", "/api/worldbook/{entry_id}"),
    ("PATCH", "/api/session/worldbook/manual"),
    ("DELETE", "/api/worldbook/{entry_id}"),
})

RAW_OBJECT_ENDPOINTS = (
    ("PUT", "/api/characters/contract-character", "invalid_request_body"),
    ("POST", "/api/model/switch", "invalid_request_body"),
    ("POST", "/api/session/restore", "invalid_request_body"),
    (
        "POST",
        "/api/session/summary/regenerate",
        "summary_request_invalid",
    ),
    ("PATCH", "/api/session/summary", "summary_request_invalid"),
    ("PUT", "/api/prompts/system", "invalid_request_body"),
    ("PUT", "/api/settings", "invalid_request_body"),
    ("PUT", "/api/user", "invalid_request_body"),
)


def assert_error_contract(
    response: httpx.Response,
    *,
    status: int | None = None,
    code: str | None = None,
) -> dict:
    if status is not None:
        assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/json")
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value

    payload = response.json()
    assert set(payload) == {"error"}, payload
    error = payload["error"]
    assert set(error) == {"schema_version", "code", "message", "details"}, error
    assert error["schema_version"] == 1
    assert isinstance(error["code"], str) and error["code"]
    assert isinstance(error["message"], str) and error["message"]
    assert type(error["details"]) is dict
    if code is not None:
        assert error["code"] == code
    return error


def test_write_route_inventory_is_explicit_and_complete():
    actual = {
        (method, route.path)
        for route in app.routes
        for method in (getattr(route, "methods", None) or set())
        if method in WRITE_METHODS
    }
    assert actual == EXPECTED_WRITE_ROUTES


@pytest.mark.asyncio
async def test_every_write_route_uses_the_same_maintenance_error_contract(app_client):
    token = begin_maintenance("error_contract_test")
    try:
        for method, template in sorted(EXPECTED_WRITE_ROUTES):
            path = re.sub(r"{[^}]+}", "contract-id", template)
            response = await app_client.request(method, path, json={})
            error = assert_error_contract(
                response,
                status=503,
                code="turn_maintenance",
            )
            assert error["details"] == {"operation": "error_contract_test"}
    finally:
        end_maintenance(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method,path,object_code",
    RAW_OBJECT_ENDPOINTS,
    ids=[path for _method, path, _code in RAW_OBJECT_ENDPOINTS],
)
async def test_raw_json_endpoints_reject_malformed_and_non_object_without_writes(
    app_client,
    isolated_paths,
    method,
    path,
    object_code,
):
    before = file_manifest(isolated_paths["root"])
    malformed = await app_client.request(
        method,
        path,
        content=b'{"broken":',
        headers={"content-type": "application/json"},
    )
    assert_error_contract(malformed, status=400, code="invalid_json_body")
    assert file_manifest(isolated_paths["root"]) == before

    non_object = await app_client.request(method, path, json=[])
    assert_error_contract(non_object, status=400, code=object_code)
    assert file_manifest(isolated_paths["root"]) == before


@pytest.mark.asyncio
async def test_validation_error_is_redacted_and_has_only_safe_issue_fields(app_client):
    secret = "TOP_SECRET_VALIDATION_INPUT"
    response = await app_client.post(
        "/api/projects",
        json={"name": {"secret": secret}},
    )

    error = assert_error_contract(
        response,
        status=422,
        code="request_validation_failed",
    )
    assert set(error["details"]) == {"issues"}
    assert error["details"]["issues"]
    for issue in error["details"]["issues"]:
        assert set(issue) <= {"type", "loc", "msg"}
        assert not ({"input", "ctx", "url"} & set(issue))
    assert secret not in response.text


@pytest.mark.asyncio
async def test_string_http_exception_and_starlette_404_are_normalized(app_client):
    invalid = await app_client.post("/api/projects", json={"name": "   "})
    error = assert_error_contract(invalid, status=400, code="http_400")
    assert error["details"] == {}

    missing = await app_client.post("/api/does-not-exist", json={})
    error = assert_error_contract(missing, status=404, code="http_404")
    assert error["details"] == {}


@pytest.mark.asyncio
async def test_unexpected_exception_is_safe_and_keeps_security_headers(monkeypatch):
    from routes import settings as settings_routes

    def fail_write(*_args, **_kwargs):
        raise OSError(r"C:\private\TOP_SECRET\settings.json")

    monkeypatch.setattr(settings_routes, "atomic_write", fail_write)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=f"http://127.0.0.1:{PORT}",
    ) as client:
        response = await client.put(
            "/api/settings",
            json={"data": {"temperature": 0.5}},
        )

    error = assert_error_contract(response, status=500, code="internal_error")
    assert error["details"] == {}
    assert "TOP_SECRET" not in response.text
    assert "OSError" not in response.text
    assert "private" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    (
        {"temperature": "0.5"},
        {"temperature": True},
        {"temperature": 2.01},
        {"top_p": -0.01},
        {"top_k": 1001},
        {"num_predict": 0},
        {"think": 1},
        {"unknown_setting": "forbidden"},
    ),
)
async def test_settings_reject_invalid_types_ranges_and_unknown_fields_without_writes(
    app_client,
    isolated_paths,
    data,
):
    before = file_manifest(isolated_paths["root"])
    response = await app_client.put("/api/settings", json={"data": data})
    assert_error_contract(
        response,
        status=422,
        code="request_validation_failed",
    )
    assert file_manifest(isolated_paths["root"]) == before
@pytest.mark.asyncio
async def test_untrusted_host_is_rejected_before_provider_origin(app_client):
    response = await app_client.put(
        "/api/providers/rebinding",
        json={"preset": "openai"},
        headers={"Host": "evil.example", "Origin": "http://evil.example"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "host_not_allowed"
    assert response.headers["content-security-policy"]
