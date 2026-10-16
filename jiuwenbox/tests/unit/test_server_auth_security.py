# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Management API authentication and listener security boundaries."""

from unittest.mock import Mock

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from jiuwenbox.server import auth, launcher


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    for name in (
        "JIUWENBOX_API_TOKEN", "JIUWENBOX_LISTEN", "JIUWENBOX_UDS_PATH",
        "JIUWENBOX_SAVE_LOGS_DIR",
    ):
        monkeypatch.setenv(name, "")


@pytest.fixture
def server_run(monkeypatch):
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(launcher, "configure_logging", Mock())
    monkeypatch.setattr(launcher, "patch_uvicorn_logging", Mock())
    return run


def test_default_listener_is_loopback(server_run):
    assert launcher.main([]) == 0
    assert server_run.call_args.kwargs["host"] == "127.0.0.1"
    assert server_run.call_args.kwargs["port"] == 8321


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "example.invalid"])
@pytest.mark.parametrize("token", ["", "   "])
def test_public_listener_without_token_never_starts(server_run, monkeypatch, host, token):
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", token)
    uri = f"http://[{host}]:8321" if ":" in host else f"http://{host}:8321"
    assert launcher.main(["--listen", uri]) == 2
    server_run.assert_not_called()


@pytest.mark.parametrize("source", ["cli", "env"])
def test_public_listener_with_explicit_token_starts(server_run, monkeypatch, source, caplog):
    args = ["--listen", "http://0.0.0.0:8321"]
    if source == "cli":
        args += ["--api-token", "test-api-token"]
    else:
        monkeypatch.setenv("JIUWENBOX_API_TOKEN", "test-api-token")
    assert launcher.main(args) == 0
    assert server_run.call_args.kwargs["host"] == "0.0.0.0"
    assert auth.get_configured_token() == "test-api-token"
    assert "test-api-token" not in caplog.text


def test_public_env_listener_also_requires_token(server_run, monkeypatch):
    monkeypatch.setenv("JIUWENBOX_LISTEN", "http://0.0.0.0:8321")
    assert launcher.main([]) == 2
    server_run.assert_not_called()


def test_uds_does_not_require_tcp_token(server_run, monkeypatch):
    prepare = Mock()
    monkeypatch.setattr(launcher, "_prepare_uds", prepare)
    assert launcher.main(["--listen", "unix:///run/jiuwenbox/test.sock"]) == 0
    prepare.assert_called_once_with("/run/jiuwenbox/test.sock")
    assert server_run.call_args.kwargs["uds"] == "/run/jiuwenbox/test.sock"
    assert "host" not in server_run.call_args.kwargs


@pytest.mark.parametrize("uri", ["http://0.0.0.0:8321", "http://[::]:8321"])
@pytest.mark.asyncio
async def test_app_startup_rejects_public_config_before_sandbox_initialization(monkeypatch, uri):
    from jiuwenbox.server import app

    monkeypatch.setenv("JIUWENBOX_LISTEN", uri)
    reader = Mock(side_effect=AssertionError("Sandbox initialization must not run"))
    monkeypatch.setattr(app, "PolicyReader", reader)
    with pytest.raises(ValueError, match="JIUWENBOX_API_TOKEN"):
        async with app.lifespan(None):
            pytest.fail("Unsafe startup succeeded")
    reader.assert_not_called()


async def request_through_guard(server, client, path="/health", headers=None):
    reached = []

    async def handler(request):
        reached.append(request.url.path)
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/{path:path}", handler, methods=["GET", "POST"])])
    app.add_middleware(auth.BearerTokenAuthMiddleware)

    async def transport_app(scope, receive, send):
        scope["server"] = server
        scope["client"] = client
        await app(scope, receive, send)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=transport_app), base_url="http://127.0.0.1",
    ) as http:
        result = await http.post(path, headers=headers)
    return result, reached


@pytest.mark.parametrize("server,client", [
    (("127.0.0.1", 8321), ("127.0.0.1", 1234)),
    (("::1", 8321), ("::1", 1234)),
    (("::ffff:127.0.0.1", 8321), ("::ffff:127.0.0.1", 1234)),
    (None, None),
    (("/run/jiuwenbox/test.sock", None), None),
])
@pytest.mark.asyncio
async def test_token_free_local_transports_remain_usable(server, client):
    response, reached = await request_through_guard(server, client)
    assert response.status_code == 200
    assert reached == ["/health"]


@pytest.mark.parametrize("server,client", [
    (("192.0.2.10", 8321), ("198.51.100.5", 1234)),
    (("192.0.2.10", 8321), ("127.0.0.1", 1234)),
    (("127.0.0.1", 8321), ("198.51.100.5", 1234)),
    (("0.0.0.0", 8321), ("127.0.0.1", 1234)),
    (("::", 8321), ("::1", 1234)),
    (("::ffff:192.0.2.10", 8321), ("::1", 1234)),
    (("unknown.invalid", 8321), ("127.0.0.1", 1234)),
    (("127.0.0.1", 8321), None),
])
@pytest.mark.asyncio
async def test_remote_or_unknown_transport_without_token_cannot_reach_handler(server, client):
    # Neither a stale loopback setting nor spoofed forwarding headers grants access.
    response, reached = await request_through_guard(server, client, headers={
        "Host": "localhost", "X-Forwarded-For": "127.0.0.1",
        "X-Forwarded-Host": "localhost", "Authorization": "Bearer arbitrary",
    })
    assert response.status_code == 503
    assert response.json()["code"] == "API_TOKEN_REQUIRED"
    assert reached == []


@pytest.mark.parametrize("path", [
    "/health", "/mcp", "/docs", "/openapi.json", "/api/v1/sandboxes",
    "/api/v1/sandboxes/sb/exec", "/api/v1/sandboxes/sb/upload",
    "/api/v1/sandboxes/sb/download", "/api/v1/sandboxes/sb/search",
])
@pytest.mark.parametrize("header", [None, "Bearer wrong", "Basic test"])
@pytest.mark.asyncio
async def test_configured_token_protects_all_paths(monkeypatch, path, header):
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", "test-api-token")
    response, reached = await request_through_guard(
        ("127.0.0.1", 8321), ("127.0.0.1", 1234), path,
        {"Authorization": header} if header else {},
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert reached == []


@pytest.mark.asyncio
async def test_valid_token_allows_remote_request(monkeypatch):
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", "test-api-token")
    response, reached = await request_through_guard(
        ("192.0.2.10", 8321), ("198.51.100.5", 1234), "/api/v1/sandboxes",
        {"Authorization": "Bearer test-api-token"},
    )
    assert response.status_code == 200
    assert reached == ["/api/v1/sandboxes"]


@pytest.mark.parametrize("path", ["/health", "/mcp", "/api/v1/sandboxes", "/docs"])
@pytest.mark.parametrize("token", [None, "test-api-token"])
@pytest.mark.asyncio
async def test_actual_application_keeps_mounted_routes_behind_auth(monkeypatch, path, token):
    from jiuwenbox.server.app import create_app

    if token:
        monkeypatch.setenv("JIUWENBOX_API_TOKEN", token)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(), client=("198.51.100.5", 1234)),
        base_url="http://192.0.2.10",
    ) as http:
        response = await http.get(path)
    assert response.status_code == (401 if token else 503)
    if not token:
        assert response.json()["code"] == "API_TOKEN_REQUIRED"
