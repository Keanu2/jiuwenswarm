# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the outbound A2A client.

The a2a SDK transports are mocked — we verify the client's circuit-breaker
admission, retry wrapping, transport fallback ordering, and auth injection
without hitting any real network.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.agents.harness.common.a2a_gateway.config import (
    A2AGatewayConfig,
    PeerAuthConfig,
    PeerConfig,
    RetryConfig,
)
from jiuwenswarm.agents.harness.common.a2a_gateway.outbound_client import (
    A2AOutboundClient,
    OutboundSendResult,
    _card_path_or_default,
)


def _make_peer(name: str = "p1", token: str = "tok", tunnel: str | None = None) -> PeerConfig:
    return PeerConfig(
        name=name,
        agent_card_url="http://peer.example/.well-known/agent-card.json",
        auth=PeerAuthConfig(type="bearer", token=token),
        tunnel_device_id=tunnel,
    )


def _install_a2a_stubs(monkeypatch: pytest.MonkeyPatch, *, send_side_effect=None) -> MagicMock:
    """Install fake a2a.client / a2a.types / a2a.utils into sys.modules.

    Returns the fake client object so a test can assert on send_message calls.
    """
    fake_client = MagicMock()
    send_mock = AsyncMock(side_effect=send_side_effect)
    # send_message is an async generator: it must yield StreamResponse.
    async def _send(_request, *, context=None):  # type: ignore[no-untyped-def]
        if send_side_effect is not None:
            # Let caller raise (retryable / non-retryable)
            result = send_mock(_request, context=context)
            try:
                await result
            except Exception:
                raise
        # Yield a minimal stream response with a message.text part.
        resp = types.SimpleNamespace()
        msg = types.SimpleNamespace(parts=[types.SimpleNamespace(text="hello")])
        resp.HasField = lambda f: f == "message"
        resp.message = msg
        resp.status_update = None
        resp.task = None
        resp.artifact_update = None
        yield resp
    fake_client.send_message = _send

    fake_factory = MagicMock()
    fake_factory.create_from_url = AsyncMock(return_value=fake_client)

    # Stub the _import_a2a_client / _import_a2a_types helpers.
    import jiuwenswarm.agents.harness.common.a2a_gateway.outbound_client as mod

    def _fake_import_client():
        Client = MagicMock
        ctx = MagicMock
        cfg = MagicMock
        ClientFactory = MagicMock
        A2ACardResolver = MagicMock
        ClientCallInterceptor = MagicMock
        return Client, ctx, cfg, ClientFactory, A2ACardResolver, ClientCallInterceptor

    def _fake_import_types():
        Message = MagicMock
        Part = MagicMock()
        # Part() returns an instance we set attributes on; simulate.
        class _Part:
            def __init__(self):
                self.text = ""
                self.raw = b""
                self.url = ""
                self.filename = ""
                self.media_type = ""

        class _Message:
            def __init__(self):
                self.message_id = ""
                self.role = None
                self.parts = []
                self.metadata = {}

            def CopyFrom(self, _other):  # noqa: D401
                pass

        class _Req:
            def __init__(self):
                self.message = _Message()
                self.configuration = types.SimpleNamespace(CopyFrom=lambda _o: None)

        Role = types.SimpleNamespace(ROLE_USER="user")
        SendMessageConfiguration = MagicMock
        SendMessageRequest = _Req
        return Message, _Part, Role, SendMessageConfiguration, SendMessageRequest

    monkeypatch.setattr(mod, "_import_a2a_client", _fake_import_client)
    monkeypatch.setattr(mod, "_import_a2a_types", _fake_import_types)
    # Patch create_from_url to return our fake client (bypass real factory).
    monkeypatch.setattr(mod, "_AgentCardToDict", lambda c: {}, raising=False)
    # The client builds a real ClientFactory(config); stub create_from_url.
    async def _create_from_url(self, url, relative_card_path=None):  # type: ignore[no-untyped-def]
        return fake_client
    # Monkeypatch the SDK's ClientFactory.create_from_url via the import helper
    # is insufficient; instead patch at the bound call site by replacing
    # ClientFactory in the module namespace after first send. Simpler: patch
    # A2AOutboundClient._do_send_message to use fake_factory.
    return fake_client


class TestA2AOutboundClient:
    async def test_send_message_circuit_open_returns_503(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = A2AGatewayConfig.from_config({
            "enabled": True,
            "peers": [{"name": "p1", "agent_card_url": "http://p/card.json"}],
            "resilience": {"circuit_breaker": {"enabled": True, "failure_threshold": 1,
                                               "soft_threshold": 2.0}},
        })
        client = A2AOutboundClient(cfg)
        # Force the breaker open.
        await client._breaker.acquire("p1")
        await client._breaker.release("p1", success=False)
        # second failure trips threshold=1
        # Actually need 2 failures for threshold; just trip via direct state.
        client._breaker._state("p1").state = __import__(
            "jiuwenswarm.agents.harness.common.a2a_gateway.circuit_breaker", fromlist=["CircuitState"]
        ).CircuitState.OPEN
        result = await client.send_message(cfg.peer_by_name("p1"), text="hi")
        assert result.ok is False
        assert result.status_code == 503

    async def test_ordered_bindings_filters_grpc_for_tunnel_peer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = A2AGatewayConfig.from_config({
            "enabled": True,
            "peers": [{"name": "p1", "agent_card_url": "http://p/card.json",
                       "tunnel_device_id": "dev-x"}],
        })
        client = A2AOutboundClient(cfg)
        client.set_tunnel(MagicMock())  # tunnel present
        bindings = client._ordered_bindings(cfg.peer_by_name("p1"))
        assert "grpc" not in [b.lower() for b in bindings]
        # JSONRPC preferred first.
        assert bindings[0].lower() in ("jsonrpc", "json-rpc")

    async def test_httpx_client_injects_auth_headers(self) -> None:
        peer = _make_peer()
        cfg = A2AGatewayConfig.from_config({
            "enabled": True,
            "peers": [{"name": "p1", "agent_card_url": peer.agent_card_url,
                       "auth": {"type": "bearer", "token": "abc"}}],
        })
        client = A2AOutboundClient(cfg)
        httpx_client = client._httpx_client_for(cfg.peer_by_name("p1"))
        assert httpx_client.headers.get("authorization") == "Bearer abc"
        await client.close()

    async def test_close_releases_httpx_clients(self) -> None:
        cfg = A2AGatewayConfig.from_config({
            "enabled": True, "peers": [{"name": "p1", "agent_card_url": "http://p/c"}],
        })
        client = A2AOutboundClient(cfg)
        _ = client._httpx_client_for(cfg.peer_by_name("p1"))
        await client.close()
        assert client._httpx_clients == {}

    async def test_send_message_success_aggregates_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = A2AGatewayConfig.from_config({
            "enabled": True,
            "peers": [{"name": "p1", "agent_card_url": "http://p/card.json"}],
            "resilience": {"retry": {"max_retries": 0}},
        })
        client = A2AOutboundClient(cfg)

        # Replace _do_send_message with a stub that returns success.
        async def _stub_send(peer, **kwargs):
            return OutboundSendResult(ok=True, status_code=200, text="hi from peer")
        monkeypatch.setattr(client, "_do_send_message", _stub_send)

        result = await client.send_message(cfg.peer_by_name("p1"), text="ping")
        assert result.ok is True
        assert result.text == "hi from peer"
        await client.close()

    async def test_send_message_retryable_failure_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = A2AGatewayConfig.from_config({
            "enabled": True,
            "peers": [{"name": "p1", "agent_card_url": "http://p/card.json"}],
            "resilience": {"retry": {"max_retries": 2, "initial_delay_ms": 1,
                                     "max_delay_ms": 1, "jitter_ms": 0}},
        })
        client = A2AOutboundClient(cfg)
        calls = {"n": 0}

        async def _stub_send(peer, **kwargs):
            calls["n"] += 1
            if calls["n"] < 2:
                raise TimeoutError("transient")
            return OutboundSendResult(ok=True, status_code=200, text="ok")
        monkeypatch.setattr(client, "_do_send_message", _stub_send)

        result = await client.send_message(cfg.peer_by_name("p1"), text="ping")
        assert result.ok is True
        assert calls["n"] == 2
        await client.close()

    async def test_send_message_non_retryable_failure_no_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = A2AGatewayConfig.from_config({
            "enabled": True,
            "peers": [{"name": "p1", "agent_card_url": "http://p/card.json"}],
            "resilience": {"retry": {"max_retries": 3, "initial_delay_ms": 1,
                                     "max_delay_ms": 1, "jitter_ms": 0}},
        })
        client = A2AOutboundClient(cfg)
        calls = {"n": 0}

        async def _stub_send(peer, **kwargs):
            calls["n"] += 1
            # PermissionError is not retryable (no timeout/connect in name).
            raise PermissionError("403 forbidden")
        monkeypatch.setattr(client, "_do_send_message", _stub_send)

        result = await client.send_message(cfg.peer_by_name("p1"), text="ping")
        assert result.ok is False
        assert calls["n"] == 1
        await client.close()


class TestTunnelHttpxTransport:
    """The relay transport: httpx request → TunnelHttpRequest → forward → Response.

    A tunnel peer's base_url is its *local* address (e.g. http://127.0.0.1:18800),
    unreachable from this host. The transport rewrites each httpx request into a
    TunnelHttpRequest and forwards it through the relay to the peer's device id.
    These tests mock TunnelSession.forward and exercise the transport via a real
    httpx.AsyncClient so we verify the request→tunnel→response mapping end-to-end
    of the transport layer (no live websocket).
    """

    def _fake_tunnel(self, *, response=None, raises=None, captured=None) -> MagicMock:
        """Return a TunnelSession-like mock whose .forward records the request."""
        from jiuwenswarm.agents.harness.common.a2a_gateway.tunnel.protocol import (
            TunnelHttpResponse,
        )

        tunnel = MagicMock()

        async def _forward(target_device, request):  # noqa: ARG001
            if captured is not None:
                captured["target_device"] = target_device
                captured["request"] = request
            if raises is not None:
                raise raises
            if response is not None:
                return response
            return TunnelHttpResponse(status=200, headers={"x-echo": "1"}, body="pong")

        tunnel.forward = _forward
        return tunnel

    async def test_tunnel_transport_routes_request_through_relay(self) -> None:
        import httpx  # noqa: PLC0415
        from jiuwenswarm.agents.harness.common.a2a_gateway.outbound_client import (
            _TunnelHttpxTransport,
        )

        captured: dict = {}
        tunnel = self._fake_tunnel(captured=captured)
        transport = _TunnelHttpxTransport(tunnel, target_device="dev-phone1")
        client = httpx.AsyncClient(transport=transport)

        resp = await client.post(
            "http://127.0.0.1:18800/a2a/jsonrpc",
            content=b'{"jsonrpc":"2.0","method":"sendMessage","id":1}',
            headers={"content-type": "application/json", "x-custom": "keep"},
        )
        assert resp.status_code == 200
        assert resp.text == "pong"
        assert resp.headers.get("x-echo") == "1"

        # The request was forwarded to the right device id with the path + body intact.
        assert captured["target_device"] == "dev-phone1"
        req = captured["request"]
        assert req.method == "POST"
        assert req.path == "/a2a/jsonrpc"
        assert "jsonrpc" in req.body and "sendMessage" in req.body
        # Headers passed through (minus hop-by-hop).
        assert req.headers.get("x-custom") == "keep"

        await client.aclose()

    async def test_tunnel_transport_forward_failure_returns_502(self) -> None:
        import httpx  # noqa: PLC0415
        from jiuwenswarm.agents.harness.common.a2a_gateway.outbound_client import (
            _TunnelHttpxTransport,
        )

        tunnel = self._fake_tunnel(raises=ConnectionError("relay gone"))
        transport = _TunnelHttpxTransport(tunnel, target_device="dev-x")
        client = httpx.AsyncClient(transport=transport)

        resp = await client.get("http://127.0.0.1:18800/.well-known/agent-card.json")
        assert resp.status_code == 502
        assert "tunnel forward failed" in resp.text
        await client.aclose()

    async def test_httpx_client_for_uses_tunnel_transport_when_peer_has_device_id(
        self,
    ) -> None:
        # A peer with tunnel_device_id + a registered tunnel gets a transport-backed
        # client; a plain peer gets a vanilla httpx client (no custom transport).
        from jiuwenswarm.agents.harness.common.a2a_gateway.outbound_client import (
            _TunnelHttpxTransport,
        )

        # Tunnel peer.
        cfg_t = A2AGatewayConfig.from_config({
            "enabled": True,
            "peers": [{"name": "tp", "agent_card_url": "http://127.0.0.1:18800/c",
                       "tunnel_device_id": "dev-tp"}],
        })
        client_t = A2AOutboundClient(cfg_t)
        client_t.set_tunnel(MagicMock())
        hx_t = client_t._httpx_client_for(cfg_t.peer_by_name("tp"))
        # httpx wraps our transport; ._transport is the one we passed.
        assert hx_t._transport.__class__.__name__ == "_TunnelHttpxTransport"
        await client_t.close()

        # Plain peer — no tunnel device id → no custom transport.
        cfg_p = A2AGatewayConfig.from_config({
            "enabled": True,
            "peers": [{"name": "pp", "agent_card_url": "http://peer/c"}],
        })
        client_p = A2AOutboundClient(cfg_p)
        hx_p = client_p._httpx_client_for(cfg_p.peer_by_name("pp"))
        assert hx_p._transport.__class__.__name__ != "_TunnelHttpxTransport"
        await client_p.close()


class TestCardPathOrDefault:
    """The card-path heuristic: JSON-RPC endpoints must NOT be passed as card paths."""

    def test_none_and_empty_return_none(self) -> None:
        assert _card_path_or_default(None) is None
        assert _card_path_or_default("") is None
        assert _card_path_or_default("/") is None

    def test_jsonrpc_endpoint_path_returns_none(self) -> None:
        # The registry's agent_card_url is often the JSON-RPC endpoint, not the
        # card URL. Passing /a2a/jsonrpc makes the resolver 404 on the rpc path.
        assert _card_path_or_default("/a2a/jsonrpc") is None
        assert _card_path_or_default("/a2a/rest") is None
        assert _card_path_or_default("/rpc") is None

    def test_well_known_card_path_passes_through(self) -> None:
        assert _card_path_or_default("/.well-known/agent-card.json") == "/.well-known/agent-card.json"
        assert _card_path_or_default("/.well-known/agent-card") == "/.well-known/agent-card"

    def test_explicit_agent_card_path_passes_through(self) -> None:
        assert _card_path_or_default("/agent-card.json") == "/agent-card.json"
        assert _card_path_or_default("/api/agent-card") == "/api/agent-card"

    def test_case_insensitive(self) -> None:
        assert _card_path_or_default("/AGENT-CARD.json") == "/AGENT-CARD.json"
        assert _card_path_or_default("/.WELL-KNOWN/agent-card.json") == "/.WELL-KNOWN/agent-card.json"


class TestBuildSendRequestBlocking:
    """Regression: ``_build_send_message`` referenced ``blocking`` without it
    being a parameter — a NameError surfaced at call time (status 500,
    "name 'blocking' is not defined"). The param is now threaded through and
    maps to ``return_immediately`` (blocking=True → wait for final response)."""

    def _types(self):
        from jiuwenswarm.agents.harness.common.a2a_gateway.outbound_client import (
            _import_a2a_types,
        )
        return _import_a2a_types()

    def test_blocking_true_sets_return_immediately_false(self) -> None:
        from jiuwenswarm.agents.harness.common.a2a_gateway.outbound_client import (
            _build_send_request,
        )
        Message, Part, Role, SendMessageConfiguration, SendMessageRequest = self._types()
        req = _build_send_request(
            Message, Part, Role, SendMessageConfiguration, SendMessageRequest,
            text="hi", parts=None, agent_id="", blocking=True,
        )
        assert req.configuration.return_immediately is False

    def test_blocking_false_sets_return_immediately_true(self) -> None:
        from jiuwenswarm.agents.harness.common.a2a_gateway.outbound_client import (
            _build_send_request,
        )
        Message, Part, Role, SendMessageConfiguration, SendMessageRequest = self._types()
        req = _build_send_request(
            Message, Part, Role, SendMessageConfiguration, SendMessageRequest,
            text="hi", parts=None, agent_id="", blocking=False,
        )
        assert req.configuration.return_immediately is True

    def test_blocking_defaults_to_true(self) -> None:
        from jiuwenswarm.agents.harness.common.a2a_gateway.outbound_client import (
            _build_send_request,
        )
        Message, Part, Role, SendMessageConfiguration, SendMessageRequest = self._types()
        # Omit blocking entirely → default True → return_immediately False.
        req = _build_send_request(
            Message, Part, Role, SendMessageConfiguration, SendMessageRequest,
            text="hi", parts=None, agent_id="",
        )
        assert req.configuration.return_immediately is False
        # Text part carried through.
        assert any(p.text == "hi" for p in req.message.parts)
