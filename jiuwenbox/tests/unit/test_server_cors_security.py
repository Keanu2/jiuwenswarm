# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise the real HTTP middleware and routes with a harmless sandbox backend."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from jiuwenbox.models.sandbox import ExecResult, SandboxRef
from jiuwenbox.server.cors import ENV_CORS_ORIGINS, get_cors_origins

TRUSTED = "https://console.example"
EVIL = "https://untrusted.example"
TOKEN = "test-cors-api-token"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    for name in (
        ENV_CORS_ORIGINS,
        "JIUWENBOX_API_TOKEN",
        "JIUWENBOX_LISTEN",
        "JIUWENBOX_MCP_ALLOWED_HOSTS",
    ):
        monkeypatch.setenv(name, "")


@pytest.fixture
def sandbox_backend(monkeypatch):
    from jiuwenbox.server.routes import sandbox

    ref = SandboxRef(id="cors-box", phase="ready")
    backend = SimpleNamespace(
        list_sandboxes=AsyncMock(return_value=[ref]),
        stop_sandbox=AsyncMock(return_value=ref),
        create_sandbox=AsyncMock(return_value=ref),
        exec_in_sandbox=AsyncMock(return_value=ExecResult(exit_code=0)),
        upload_file_to_sandbox=AsyncMock(),
        limits=SimpleNamespace(max_file_bytes=4096),
    )
    monkeypatch.setattr(sandbox, "_mgr", lambda: backend)
    return backend


def client(app, base_url="http://127.0.0.1:8321"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url=base_url,
    )


def make_app():
    from jiuwenbox.server.app import create_app

    return create_app()


@pytest.mark.parametrize(
    "value",
    [
        "*",
        "null",
        "https://*.example",
        "https://console.example/",
        "https://console.example/path",
        "https://console.example?",
        "https://console.example#",
        "https://user:pass@console.example",
        "file:///tmp/page.html",
        "https://example:0",
        "https://example:65536",
        "https://example:",
        "https://example:bad",
        "https://example,,https://other.example",
        "https://example,",
        "http://[bad]",
        "https://example\n.evil",
        "https://example\\evil",
        "https://example%2eevil",
    ],
)
def test_invalid_allowlist_fails_closed(monkeypatch, value):
    monkeypatch.setenv(ENV_CORS_ORIGINS, value)
    with pytest.raises(ValueError, match=ENV_CORS_ORIGINS):
        get_cors_origins()


def test_allowlist_is_explicit_and_deduplicated(monkeypatch):
    assert get_cors_origins() == []
    monkeypatch.setenv(ENV_CORS_ORIGINS, f" {TRUSTED}, http://localhost:3000, {TRUSTED} ")
    assert get_cors_origins() == [TRUSTED, "http://localhost:3000"]
    monkeypatch.setenv(ENV_CORS_ORIGINS, "http://[::1]:3000")
    assert get_cors_origins() == ["http://[::1]:3000"]


def test_invalid_allowlist_prevents_application_creation(monkeypatch):
    make_app()  # Import the module with valid settings first.
    monkeypatch.setenv(ENV_CORS_ORIGINS, "*")
    with pytest.raises(ValueError, match=ENV_CORS_ORIGINS):
        make_app()


def test_invalid_allowlist_prevents_listener_start(monkeypatch):
    from jiuwenbox.server import launcher
    import uvicorn

    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setenv(ENV_CORS_ORIGINS, "*")
    assert launcher.main([]) == 2
    run.assert_not_called()


@pytest.mark.parametrize("origin", [EVIL, "null", "", "http://localhost:3000"])
@pytest.mark.asyncio
async def test_default_rejects_cross_origin_reads_and_simple_writes(sandbox_backend, origin):
    async with client(make_app()) as http:
        for method, path in [
            ("GET", "/api/v1/sandboxes"),
            ("POST", "/api/v1/sandboxes/cors-box/stop"),
        ]:
            response = await http.request(method, path, headers={"Origin": origin})
            assert response.status_code == 403
            assert response.json()["code"] == "ORIGIN_NOT_ALLOWED"
            assert "access-control-allow-origin" not in response.headers
    sandbox_backend.list_sandboxes.assert_not_awaited()
    sandbox_backend.stop_sandbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_untrusted_multipart_never_uploads_even_with_valid_token(
    monkeypatch, sandbox_backend
):
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", TOKEN)
    async with client(make_app()) as http:
        response = await http.post(
            "/api/v1/sandboxes/cors-box/upload?sandbox_path=/tmp/probe.txt",
            headers={"Origin": EVIL, "Authorization": f"Bearer {TOKEN}"},
            files={"file": ("probe.txt", b"probe", "text/plain")},
        )
    assert response.status_code == 403
    sandbox_backend.upload_file_to_sandbox.assert_not_awaited()


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/v1/sandboxes", {}),
        ("/api/v1/sandboxes/cors-box/exec", {"command": ["mock-only"]}),
    ],
)
@pytest.mark.asyncio
async def test_untrusted_json_never_creates_or_executes(sandbox_backend, path, body):
    async with client(make_app()) as http:
        response = await http.post(path, headers={"Origin": EVIL}, json=body)
    assert response.status_code == 403
    sandbox_backend.create_sandbox.assert_not_awaited()
    sandbox_backend.exec_in_sandbox.assert_not_awaited()


@pytest.mark.parametrize("path", ["/health", "/docs", "/openapi.json", "/mcp"])
@pytest.mark.asyncio
async def test_origin_guard_also_covers_non_sandbox_and_mounted_routes(monkeypatch, path):
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", TOKEN)
    async with client(make_app()) as http:
        response = await http.get(
            path, headers={"Origin": EVIL, "Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 403
    assert response.json()["code"] == "ORIGIN_NOT_ALLOWED"


@pytest.mark.parametrize(
    "origin",
    [
        "https://console.example.evil",
        "http://console.example",
        "https://console.example:8443",
        "https://sub.console.example",
        "https://console.example/",
        "null",
    ],
)
@pytest.mark.asyncio
async def test_allowlist_uses_exact_origin_not_prefix_or_hostname(
    monkeypatch, sandbox_backend, origin
):
    monkeypatch.setenv(ENV_CORS_ORIGINS, TRUSTED)
    async with client(make_app()) as http:
        response = await http.post(
            "/api/v1/sandboxes/cors-box/stop",
            headers={"Origin": origin},
        )
    assert response.status_code == 403
    sandbox_backend.stop_sandbox.assert_not_awaited()


@pytest.mark.parametrize(
    "headers",
    [
        [("Origin", TRUSTED), ("Origin", EVIL)],
        [("Origin", EVIL), ("Origin", TRUSTED)],
        {"Origin": f"{TRUSTED}, {EVIL}"},
    ],
)
@pytest.mark.asyncio
async def test_multiple_origins_are_rejected(monkeypatch, sandbox_backend, headers):
    monkeypatch.setenv(ENV_CORS_ORIGINS, TRUSTED)
    async with client(make_app()) as http:
        response = await http.get("/api/v1/sandboxes", headers=headers)
    assert response.status_code == 403
    sandbox_backend.list_sandboxes.assert_not_awaited()


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "http://127.0.0.1:8321",
        "http://localhost:8321",
        "http://[::1]:8321",
    ],
)
@pytest.mark.asyncio
async def test_local_native_and_same_origin_clients_remain_usable(sandbox_backend, origin):
    base_url = origin or "http://127.0.0.1:8321"
    async with client(make_app(), base_url) as http:
        response = await http.post(
            "/api/v1/sandboxes/cors-box/stop",
            headers={"Origin": origin} if origin else {},
        )
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
    sandbox_backend.stop_sandbox.assert_awaited_once()


@pytest.mark.asyncio
async def test_dns_name_and_forwarded_headers_cannot_claim_local_same_origin(sandbox_backend):
    async with client(make_app(), "http://rebind.example:8321") as http:
        response = await http.post(
            "/api/v1/sandboxes/cors-box/stop",
            headers={
                "Origin": "http://rebind.example:8321",
                "X-Forwarded-Host": "localhost:8321",
                "X-Forwarded-For": "127.0.0.1",
            },
        )
    assert response.status_code == 403
    sandbox_backend.stop_sandbox.assert_not_awaited()


@pytest.mark.parametrize("path", ["/api/v1/sandboxes", "/mcp", "/health", "/docs"])
@pytest.mark.parametrize("origin", [TRUSTED, "http://127.0.0.1:8321"])
@pytest.mark.asyncio
async def test_allowed_preflight_needs_no_token_and_never_calls_backend(
    monkeypatch,
    sandbox_backend,
    path,
    origin,
):
    monkeypatch.setenv(ENV_CORS_ORIGINS, origin)
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", TOKEN)
    async with client(make_app()) as http:
        response = await http.options(
            path,
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type, mcp-session-id, "
                "mcp-protocol-version, last-event-id",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert "origin" in response.headers["vary"].lower()
    assert "access-control-allow-credentials" not in response.headers
    sandbox_backend.list_sandboxes.assert_not_awaited()
    sandbox_backend.create_sandbox.assert_not_awaited()


@pytest.mark.parametrize(
    "origin,method,requested_headers,status",
    [
        (EVIL, "POST", "content-type", 403),
        (TRUSTED, "TRACE", "content-type", 400),
        (TRUSTED, "POST", "x-unapproved-header", 400),
    ],
)
@pytest.mark.asyncio
async def test_preflight_rejects_unapproved_origin_method_or_header(
    monkeypatch,
    origin,
    method,
    requested_headers,
    status,
):
    monkeypatch.setenv(ENV_CORS_ORIGINS, TRUSTED)
    async with client(make_app()) as http:
        response = await http.options(
            "/api/v1/sandboxes",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": requested_headers,
            },
        )
    assert response.status_code == status


@pytest.mark.parametrize("origin", [None, TRUSTED, "http://127.0.0.1:8321"])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", f"Bearer {TOKEN}"])
@pytest.mark.asyncio
async def test_actual_requests_still_require_token(
    monkeypatch, sandbox_backend, origin, authorization
):
    monkeypatch.setenv(ENV_CORS_ORIGINS, TRUSTED)
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", TOKEN)
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    if authorization:
        headers["Authorization"] = authorization
    async with client(make_app()) as http:
        response = await http.post("/api/v1/sandboxes/cors-box/stop", headers=headers)
    valid = authorization == f"Bearer {TOKEN}"
    assert response.status_code == (200 if valid else 401)
    assert sandbox_backend.stop_sandbox.await_count == int(valid)
    if origin == TRUSTED:
        assert response.headers["access-control-allow-origin"] == TRUSTED
        assert "access-control-allow-credentials" not in response.headers
    if not valid:
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("token", [None, TOKEN])
@pytest.mark.asyncio
async def test_allowed_browser_can_create_exec_and_upload(monkeypatch, sandbox_backend, token):
    monkeypatch.setenv(ENV_CORS_ORIGINS, TRUSTED)
    if token:
        monkeypatch.setenv("JIUWENBOX_API_TOKEN", token)
    headers = {"Origin": TRUSTED}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with client(make_app()) as http:
        created = await http.post("/api/v1/sandboxes", headers=headers, json={})
        executed = await http.post(
            "/api/v1/sandboxes/cors-box/exec",
            headers=headers,
            json={"command": ["mock-only"]},
        )
        uploaded = await http.post(
            "/api/v1/sandboxes/cors-box/upload?sandbox_path=/tmp/probe.txt",
            headers=headers,
            files={"file": ("probe.txt", b"probe", "text/plain")},
        )
    assert created.status_code == 201
    assert executed.status_code == 200
    assert uploaded.status_code == 204
    sandbox_backend.create_sandbox.assert_awaited_once()
    sandbox_backend.exec_in_sandbox.assert_awaited_once()
    sandbox_backend.upload_file_to_sandbox.assert_awaited_once_with(
        "cors-box", "/tmp/probe.txt", b"probe"
    )


@pytest.mark.asyncio
async def test_allowed_preflight_does_not_disable_remote_transport_boundary(
    monkeypatch, sandbox_backend
):
    monkeypatch.setenv(ENV_CORS_ORIGINS, TRUSTED)
    async with client(make_app(), "http://192.0.2.10:8321") as http:
        preflight = await http.options(
            "/api/v1/sandboxes",
            headers={
                "Origin": TRUSTED,
                "Access-Control-Request-Method": "POST",
            },
        )
        actual = await http.post("/api/v1/sandboxes", headers={"Origin": TRUSTED}, json={})
    assert preflight.status_code == 200
    assert actual.status_code == 503
    assert actual.json()["code"] == "API_TOKEN_REQUIRED"
    sandbox_backend.create_sandbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_ordinary_options_is_not_an_authentication_exemption(monkeypatch):
    monkeypatch.setenv(ENV_CORS_ORIGINS, TRUSTED)
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", TOKEN)
    async with client(make_app()) as http:
        response = await http.options("/api/v1/sandboxes", headers={"Origin": TRUSTED})
    assert response.status_code == 401


def test_mcp_keeps_host_protection_and_exact_browser_origins(monkeypatch):
    from jiuwenbox.server.routes.mcp import _build_transport_security

    monkeypatch.setenv(ENV_CORS_ORIGINS, TRUSTED)
    monkeypatch.setenv("JIUWENBOX_MCP_ALLOWED_HOSTS", "box.example:8321")
    settings = _build_transport_security()
    assert settings.enable_dns_rebinding_protection
    assert "box.example:8321" in settings.allowed_hosts
    assert TRUSTED in settings.allowed_origins
    assert "http://localhost:8321" in settings.allowed_origins
    assert EVIL not in settings.allowed_origins
    assert not any("*" in origin for origin in settings.allowed_origins)


@pytest.mark.parametrize("origin", [None, TRUSTED, "http://127.0.0.1:8321"])
@pytest.mark.asyncio
async def test_mcp_native_and_browser_initialization_with_token(monkeypatch, origin):
    from mcp.server.fastmcp import FastMCP
    from jiuwenbox.server.routes import mcp

    monkeypatch.setenv(ENV_CORS_ORIGINS, TRUSTED)
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", TOKEN)
    fresh_mcp = FastMCP(
        "cors-test", transport_security=mcp._build_transport_security(), json_response=True
    )
    monkeypatch.setattr(mcp, "mcp_server", fresh_mcp)
    app = make_app()
    headers = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"}
    if origin:
        headers["Origin"] = origin
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "cors-regression", "version": "1.0"},
        },
    }
    async with fresh_mcp.session_manager.run(), client(app) as http:
        response = await http.post("/mcp", headers=headers, json=initialize)
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == "cors-test"
        session_id = response.headers["mcp-session-id"]
        if origin == TRUSTED:
            assert response.headers["access-control-allow-origin"] == TRUSTED
            assert "mcp-session-id" in response.headers["access-control-expose-headers"].lower()
        initialized = await http.post(
            "/mcp",
            headers={**headers, "Mcp-Session-Id": session_id, "MCP-Protocol-Version": "2025-03-26"},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert initialized.status_code == 202
        missing_auth = await http.post("/mcp", headers={"Origin": TRUSTED}, json=initialize)
        assert missing_auth.status_code == 401
        invalid_host = await http.post(
            "/mcp",
            headers={**headers, "Origin": TRUSTED, "Host": "untrusted-host.example"},
            json=initialize,
        )
        assert invalid_host.status_code == 421
