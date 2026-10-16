"""Web authentication, immutable ownership, RPC and delivery boundaries."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.security.web_identity import (
    PRINCIPAL_ATTR, WebAccessDenied, WebIdentityPolicy, WebPrincipal, authorize_rpc,
    can_access, claim_resource, identity_scope, owner_of,
    web_client_headers,
)
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
from jiuwenswarm.gateway.routing.keys import AgentRef, RoutingKey

ALICE = WebPrincipal("alice")
BOB = WebPrincipal("bob")
ALICE_TOKEN = "a" * 40
BOB_TOKEN = "b" * 40


class Socket:
    def __init__(self, principal=None, headers=None, incoming=None):
        self.remote_address = ("127.0.0.1", 10001)
        self.local_address = ("127.0.0.1", 19000)
        self.request_headers = headers or {}
        self.frames = []
        self.incoming = list(incoming or [])
        self.closed = False
        if principal:
            setattr(self, PRINCIPAL_ATTR, principal)

    async def send(self, frame):
        self.frames.append(json.loads(frame) if isinstance(frame, str) else frame)

    async def recv(self):
        return self.incoming.pop(0)

    async def close(self, **kwargs):
        self.closed = True
        self.close_args = kwargs

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.incoming:
            raise StopAsyncIteration
        return await self.recv()


@pytest.fixture(autouse=True)
def isolated_root(monkeypatch, tmp_path):
    from jiuwenswarm.common import utils
    from jiuwenswarm.server.runtime.session import project_store, session_metadata
    for name in ("JIUWENSWARM_WEB_USERS", "JIUWENSWARM_WEB_TOKEN", "JIUWENSWARM_WEB_USER_ID",
                 "JIUWENSWARM_WEB_ADMINS", "JIUWENSWARM_WEB_CLIENT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(utils, "get_agent_root_dir", lambda: tmp_path)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: tmp_path / "sessions")
    monkeypatch.setattr(project_store, "_projects_file", lambda: tmp_path / "projects.json")
    session_metadata._METADATA_CACHE.clear()
    project_store.invalidate_cache()
    yield
    session_metadata._METADATA_CACHE.clear()
    project_store.invalidate_cache()


def configure_users(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_WEB_USERS", json.dumps({"alice": ALICE_TOKEN, "bob": BOB_TOKEN}))
    return WebIdentityPolicy()


@pytest.mark.asyncio
async def test_verified_identity_ignores_all_client_user_fields(monkeypatch):
    policy = configure_users(monkeypatch)
    ws = Socket(headers={"Authorization": "Bearer " + ALICE_TOKEN, "X-User-Id": "bob"})
    assert await policy.authenticate(ws) == ALICE
    assert not ws.frames  # Header-authenticated clients retain connection.ack as their first event.
    assert WebChannel._resolve_connection_user_id({"user_id": "bob"}, ws) == "alice"
    anonymous = Socket(headers={"X-User-Id": "alice"})
    assert WebChannel._resolve_connection_user_id({"user_id": "bob"}, anonymous) is None


@pytest.mark.asyncio
async def test_browser_first_frame_authentication(monkeypatch):
    policy = configure_users(monkeypatch)
    ws = Socket(incoming=[json.dumps({"type": "auth", "token": BOB_TOKEN, "user_id": "alice"})])
    assert await policy.authenticate(ws) == BOB
    assert [f["event"] for f in ws.frames] == ["auth.required", "auth.ok"]
    assert ALICE_TOKEN not in repr(ws.frames) and BOB_TOKEN not in repr(ws.frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [
    {"type": "req", "id": "x", "method": "session.list"},
    {"type": "auth", "token": "wrong"}, {"type": "auth", "token": ""},
])
async def test_unauthenticated_connection_never_registers_or_runs_hooks(monkeypatch, frame):
    configure_users(monkeypatch)
    channel = WebChannel(WebChannelConfig(), RobotMessageRouter())
    hook = AsyncMock()
    channel.on_connect(hook)
    ws = Socket(incoming=[json.dumps(frame)])
    await channel._connection_handler(ws, "/ws?user_id=alice&session_id=victim")
    assert ws.closed and ws.close_args["code"] == 4401
    hook.assert_not_called()
    assert not channel.clients
    assert not any(f.get("event") == "connection.ack" for f in ws.frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["remote", "local", "origin", "unknown"])
async def test_tokenless_local_mode_checks_actual_peer_and_browser_origin(change):
    ws = Socket()
    if change == "remote":
        ws.remote_address = ("10.0.0.1", 123)
    elif change == "local":
        ws.local_address = ("0.0.0.0", 19000)
    elif change == "origin":
        ws.request_headers = {"Origin": "https://attacker.example"}
    else:
        ws.local_address = None
    with pytest.raises(WebAccessDenied):
        await WebIdentityPolicy().authenticate(ws)


@pytest.mark.asyncio
async def test_legacy_local_client_has_fixed_workspace_identity():
    ws = Socket(headers={"X-User-Id": "victim", "Origin": "http://localhost:8080"})
    assert await WebIdentityPolicy().authenticate(ws) == WebPrincipal("local", True)
    assert WebChannel._resolve_connection_user_id({"user_id": "victim"}, ws) == "local"


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.1", "public.example"])
def test_network_listener_cannot_be_enabled_without_credentials(host):
    with pytest.raises(WebAccessDenied):
        WebIdentityPolicy().validate_listener(host)


@pytest.mark.parametrize("setting", ["{}", "[]", "not-json", '{"alice":"short"}'])
def test_invalid_auth_configuration_fails_closed(monkeypatch, setting):
    monkeypatch.setenv("JIUWENSWARM_WEB_USERS", setting)
    with pytest.raises(ValueError):
        WebIdentityPolicy()


def test_ownership_is_durable_exclusive_and_ignores_legacy_user_metadata(tmp_path):
    claim_resource("sessions", "sess-owned", ALICE)
    assert owner_of("sessions", "sess-owned") == "alice"
    claim_resource("sessions", "sess-owned", ALICE)
    with pytest.raises(WebAccessDenied):
        claim_resource("sessions", "sess-owned", BOB)
    assert can_access(ALICE, "sessions", "sess-owned")
    assert not can_access(BOB, "sessions", "sess-owned")
    assert not can_access(ALICE, "sessions", "../escape")
    legacy = tmp_path / "sessions" / "legacy"
    legacy.mkdir(parents=True)
    (legacy / "metadata.json").write_text('{"user_id":"alice"}')
    assert not can_access(ALICE, "sessions", "legacy")


@pytest.mark.parametrize("method", ["history.get", "chat.send", "chat.interrupt", "session.delete",
                                       "session.rename", "session.switch", "command.goal"])
def test_session_methods_require_verified_ownership(method):
    claim_resource("sessions", "alice-session", ALICE)
    authorize_rpc(ALICE, method, {"session_id": "alice-session"})
    with pytest.raises(WebAccessDenied):
        authorize_rpc(BOB, method, {"session_id": "alice-session"})
    with pytest.raises(WebAccessDenied):
        authorize_rpc(ALICE, method, {})


def test_nested_resources_and_workspace_configuration_cannot_bypass_checks():
    claim_resource("sessions", "alice-session", ALICE)
    for params in ({"session_id": "alice-session", "target_session_id": "other"},
                   {"session_ids": ["alice-session", "other"]},
                   {"requester": {"session_id": "other"}}):
        with pytest.raises(WebAccessDenied):
            authorize_rpc(ALICE, "session.rename", params)
    for method in ("config.get", "config.set", "models.list", "cron.job.list", "channel.configure"):
        with pytest.raises(WebAccessDenied):
            authorize_rpc(ALICE, method, {"session_id": "alice-session"})
    authorize_rpc(WebPrincipal("local", True), "config.get", {})


@pytest.mark.asyncio
async def test_dispatch_denies_before_attachments_callbacks_or_route_registration():
    channel = WebChannel(WebChannelConfig(), RobotMessageRouter())
    ws = Socket(ALICE)
    response = AsyncMock()
    channel.send_response = response
    callback = AsyncMock()
    channel.on_message(callback)
    channel._process_files = AsyncMock()
    await channel._handle_message_dict(ws, {"type": "req", "id": "x", "method": "chat.send",
                                           "params": {"session_id": "victim", "user_id": "bob"}}, {})
    assert response.call_args.kwargs["code"] == "FORBIDDEN"
    callback.assert_not_called()
    channel._process_files.assert_not_called()
    assert not channel.clients


@pytest.mark.asyncio
async def test_created_session_claimed_from_trusted_response_and_stream_delivery_is_scoped():
    channel = WebChannel(WebChannelConfig(), RobotMessageRouter())
    alice, bob = Socket(ALICE), Socket(BOB)
    for ws, user in ((alice, "alice"), (bob, "bob")):
        await channel.register_ws(ws, RoutingKey(user, "web", "default", AgentRef("agent", "default"), "placeholder"))
    try:
        channel._pending_creates.add((id(alice), "create"))
        await channel.send_response(alice, "create", ok=True, payload={"session_id": "new-session"})
        assert can_access(ALICE, "sessions", "new-session")
        await channel.broadcast_event("chat.delta", {"session_id": "new-session", "content": "private"})
        await channel.broadcast_event("chat.final", {"cron": {"secret": "private"}})
        await channel.broadcast_event("config.changed", {"api_key": "private"})
        await asyncio.sleep(0.01)
        assert any(f.get("event") == "chat.delta" for f in alice.frames)
        assert not bob.frames
        assert not any(f.get("event") in {"chat.final", "config.changed"} for f in alice.frames)
    finally:
        await channel.unregister_ws(alice)
        await channel.unregister_ws(bob)


def test_session_lists_filter_before_pagination_and_project_aggregates(tmp_path):
    from jiuwenswarm.server.runtime.session import project_store, session_metadata
    for user, name, stamp in [(ALICE, "a-old", 1), (BOB, "b-new", 100), (ALICE, "a-new", 10)]:
        directory = tmp_path / "sessions" / name
        directory.mkdir(parents=True)
        (directory / "metadata.json").write_text(json.dumps({
            "session_id": name, "channel_id": "web", "user_id": "spoofed",
            "created_at": stamp, "last_message_at": stamp, "work_mode": "work",
        }))
        claim_resource("sessions", name, user)
    with identity_scope(ALICE):
        rows, total = session_metadata.get_all_sessions_metadata(limit=1)
        assert total == 2 and rows[0]["session_id"] == "a-new"
        assert {r["session_id"] for r in session_metadata.collect_all_sessions_metadata()} == {"a-old", "a-new"}
        project = project_store.create_project("Alice", "", "work")
        assert owner_of("projects", project.project_id) == "alice"
    with identity_scope(BOB):
        assert not project_store.list_projects()
        assert project_store.get_project_by_id(project.project_id) is None
        assert project_store.get_project_by_name_and_mode("Alice", "work") is None
        other, restored = project_store.create_or_restore_project("Alice", "", "work")
        assert not restored and other.project_id != project.project_id
    assert len(project_store.list_projects()) == 2


@pytest.mark.asyncio
async def test_git_authorizes_before_watch_lookup_or_mutation():
    from jiuwenswarm.gateway.channel_manager.web.git_ws_handler import GitDiffWebSocketHandler
    channel = SimpleNamespace(send_response=AsyncMock())
    registry = Mock()
    handler = GitDiffWebSocketHandler(channel, registry)
    claim_resource("projects", "alice-project", ALICE)
    await handler._handle_message(Socket(BOB), json.dumps({
        "type": "req", "id": "git", "method": "project.git.diff_watch",
        "params": {"project_id": "alice-project"},
    }))
    assert channel.send_response.call_args.kwargs["code"] == "FORBIDDEN"
    assert not registry.mock_calls


@pytest.mark.asyncio
async def test_real_websocket_auth_create_reconnect_and_cross_user_denial(monkeypatch):
    from websockets.legacy.client import connect
    from websockets.legacy.server import serve

    configure_users(monkeypatch)
    channel = WebChannel(WebChannelConfig(), RobotMessageRouter())

    async def created(ws, req_id, params, session_id, user_id=None):
        assert user_id == "alice" and params["user_id"] == "alice"
        await channel.send_response(ws, req_id, ok=True, payload={"session_id": "network-session"})

    async def history(ws, req_id, params, session_id):
        await channel.send_response(ws, req_id, ok=True, payload={"session_id": session_id, "content": "private"})

    channel.register_method("session.create", created)
    channel.register_method("history.get", history)
    channel.on_message(lambda message: False)

    async def read_response(ws):
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), 3))
            if frame.get("type") == "res":
                return frame

    async with serve(channel._connection_handler, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws?user_id=bob"
        async with connect(url, extra_headers={"Authorization": "Bearer " + ALICE_TOKEN}) as ws:
            await ws.send(json.dumps({"type": "req", "id": "create", "method": "session.create",
                                      "params": {"user_id": "bob"}}))
            assert (await read_response(ws))["ok"]
        async with connect(url) as ws:
            assert json.loads(await ws.recv())["event"] == "auth.required"
            await ws.send(json.dumps({"type": "auth", "token": ALICE_TOKEN}))
            await ws.send(json.dumps({"type": "req", "id": "read", "method": "history.get",
                                      "params": {"session_id": "network-session"}}))
            assert (await read_response(ws))["payload"]["content"] == "private"
        async with connect(url, extra_headers={"Authorization": "Bearer " + BOB_TOKEN,
                                               "X-User-Id": "alice"}) as ws:
            await ws.send(json.dumps({"type": "req", "id": "read", "method": "history.get",
                                      "params": {"session_id": "network-session", "user_id": "alice"}}))
            response = await read_response(ws)
            assert response["code"] == "FORBIDDEN" and "private" not in json.dumps(response)


@pytest.mark.asyncio
async def test_restricted_users_cannot_use_adjacent_gateway_to_bypass_web(monkeypatch):
    from jiuwenswarm.gateway.app_gateway import GatewayServer, GatewayServerConfig
    configure_users(monkeypatch)
    ws = Socket(headers={"Authorization": "Bearer " + ALICE_TOKEN})
    server = GatewayServer(GatewayServerConfig(), RobotMessageRouter())
    monkeypatch.setattr(server, "_resolve_route", lambda path: (SimpleNamespace(channel_id="tui"), "/tui"))
    await server._connection_handler(ws, "/tui")
    assert ws.closed and not server._clients


@pytest.mark.asyncio
async def test_proxy_forwarded_connection_does_not_get_local_auth_exception():
    ws = Socket(headers={"X-Jiuwen-Remote-Proxy": "1"})
    with pytest.raises(WebAccessDenied):
        await WebIdentityPolicy().authenticate(ws)


def test_http_identity_uses_the_same_user_and_never_trusts_user_header(monkeypatch):
    policy = configure_users(monkeypatch)
    peer, local = ("10.0.0.2", 1000), ("10.0.0.1", 8080)
    assert policy.authenticate_http({"Authorization": "Bearer " + ALICE_TOKEN, "X-User-Id": "bob"},
                                    peer, local) == ALICE
    with pytest.raises(WebAccessDenied):
        policy.authenticate_http({"X-User-Id": "alice"}, peer, local)


def test_workspace_admin_is_explicit_server_configuration(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_WEB_ADMINS", "alice")
    policy = configure_users(monkeypatch)
    assert policy.verify(ALICE_TOKEN) == WebPrincipal("alice", True)
    assert not policy.verify(BOB_TOKEN).workspace_owner
    monkeypatch.setenv("JIUWENSWARM_WEB_ADMINS", "unknown")
    with pytest.raises(ValueError):
        WebIdentityPolicy()


def test_new_project_cannot_alias_other_users_history_or_workspace(tmp_path):
    for path in (str(tmp_path / "sessions" / "victim"), str(tmp_path.parent), "../victim"):
        with pytest.raises(WebAccessDenied):
            authorize_rpc(ALICE, "project.create", {"name": "mine", "project_dir": path})
    params = {"name": "mine"}
    authorize_rpc(ALICE, "project.create", params)
    assert str(tmp_path / "web-workspaces") in params["project_dir"]
    authorize_rpc(WebPrincipal("local", True), "project.create", {"project_dir": str(tmp_path)})


@pytest.mark.asyncio
async def test_create_idempotency_keys_and_metadata_are_user_bound():
    channel = WebChannel(WebChannelConfig(), RobotMessageRouter())
    messages = []
    channel.on_message(lambda msg: messages.append(msg) or True)
    sockets = [Socket(ALICE), Socket(BOB)]
    for ws in sockets:
        await channel._handle_message_dict(ws, {
            "type": "req", "id": "create", "method": "session.create",
            "params": {"create_token": "same-client-key", "user_id": "victim"},
        }, {"user_id": ["victim"]})
    assert messages[0].params["create_token"] != messages[1].params["create_token"]
    assert messages[0].user_id == messages[0].params["user_id"] == "alice"
    assert messages[0].metadata["query"]["user_id"] == ["alice"]


@pytest.mark.asyncio
async def test_creation_response_cannot_reassign_an_existing_users_session():
    channel = WebChannel(WebChannelConfig(), RobotMessageRouter())
    alice = Socket(ALICE)
    await channel.register_ws(alice, RoutingKey("alice", "web", "default", AgentRef("agent", "default"), "placeholder"))
    claim_resource("sessions", "bobs-session", BOB)
    try:
        channel._pending_creates.add((id(alice), "create"))
        await channel.send_response(alice, "create", ok=True,
                                    payload={"session_id": "bobs-session", "private": "bob-data"})
        await asyncio.sleep(0)
        assert owner_of("sessions", "bobs-session") == "bob"
        assert alice.frames[0]["code"] == "FORBIDDEN"
        assert "bob-data" not in repr(alice.frames)
    finally:
        await channel.unregister_ws(alice)


def test_native_clients_never_send_deployment_secret_to_arbitrary_targets(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_WEB_TOKEN", ALICE_TOKEN)
    monkeypatch.setenv("WEB_PORT", "19000")
    monkeypatch.setenv("GATEWAY_PORT", "19001")
    assert web_client_headers("ws://127.0.0.1:19001/tui")["Authorization"] == "Bearer " + ALICE_TOKEN
    assert not web_client_headers("wss://evil.example:19001/tui")
    assert not web_client_headers("ws://127.0.0.1:9999/tui")
    monkeypatch.delenv("JIUWENSWARM_WEB_TOKEN")
    configure_users(monkeypatch)
    assert not web_client_headers("ws://127.0.0.1:19001/tui")  # Never select another user's credential.
    monkeypatch.setenv("JIUWENSWARM_WEB_CLIENT_TOKEN", BOB_TOKEN)
    assert web_client_headers("wss://explicit.example/tui")["Authorization"] == "Bearer " + BOB_TOKEN


@pytest.mark.asyncio
async def test_python_cli_retains_ack_protocol_with_authenticated_gateway(monkeypatch):
    from websockets.legacy.server import serve
    from jiuwenswarm.cli.gateway_client import GatewayClient
    monkeypatch.setenv("JIUWENSWARM_WEB_TOKEN", ALICE_TOKEN)
    monkeypatch.setenv("JIUWENSWARM_WEB_CLIENT_TOKEN", ALICE_TOKEN)
    policy = WebIdentityPolicy()

    async def handler(ws, path):
        await policy.authenticate(ws)
        await ws.send(json.dumps({"type": "event", "event": "connection.ack", "payload": {}}))
        await ws.wait_closed()

    async with serve(handler, "127.0.0.1", 0) as server:
        client = GatewayClient(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/tui")
        try:
            await client.connect()
            assert client.is_open
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_acp_client_adds_credentials_without_changing_reader_protocol(monkeypatch):
    from websockets.legacy import client as ws_client
    from jiuwenswarm.gateway.channel_manager.protocol.acp.acp_connect import AcpChannel, AcpChannelConfig
    monkeypatch.setenv("JIUWENSWARM_WEB_CLIENT_TOKEN", ALICE_TOKEN)
    ws = Socket()
    connect = AsyncMock(return_value=ws)
    monkeypatch.setattr(ws_client, "connect", connect)
    channel = AcpChannel(AcpChannelConfig(enabled=True), RobotMessageRouter())
    channel._gateway_url = "ws://127.0.0.1:19001/acp"
    reader = AsyncMock()
    monkeypatch.setattr(channel, "_gateway_reader_loop", reader)
    await channel._ensure_gateway_connection()
    assert connect.call_args.kwargs["extra_headers"] == {"Authorization": "Bearer " + ALICE_TOKEN}
    await channel._gateway_reader_task
    reader.assert_awaited_once()


@pytest.mark.asyncio
async def test_internal_channel_control_inherits_single_user_local_credentials(monkeypatch):
    import sys
    from jiuwenswarm.agents.harness.common.tools.channel_config_tools import _request_gateway_control_websocket
    monkeypatch.setenv("JIUWENSWARM_WEB_TOKEN", ALICE_TOKEN)
    monkeypatch.setenv("GATEWAY_HOST", "127.0.0.1")
    monkeypatch.setenv("GATEWAY_PORT", "19001")
    monkeypatch.delenv("JIUWENSWARM_GATEWAY_CONTROL_URL", raising=False)
    frames = [json.dumps({"type": "event", "event": "connection.ack", "payload": {}}),
              json.dumps({"type": "res", "id": "control", "ok": True, "payload": {"ok": True}})]
    ws = Socket(incoming=frames)

    class Connection:
        async def __aenter__(self):
            return ws

        async def __aexit__(self, *args):
            return None

    connect = Mock(return_value=Connection())
    monkeypatch.setitem(sys.modules, "websockets", SimpleNamespace(connect=connect))
    result = await _request_gateway_control_websocket({"type": "req", "id": "control",
                                                      "method": "wechat.login_status", "params": {}})
    assert result == {"ok": True}
    assert connect.call_args.kwargs["extra_headers"] == {"Authorization": "Bearer " + ALICE_TOKEN}
