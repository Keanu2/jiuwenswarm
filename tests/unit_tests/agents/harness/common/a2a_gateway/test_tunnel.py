# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the relay tunnel protocol helpers and session message handling."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.agents.harness.common.a2a_gateway.tunnel.protocol import (
    MessageType,
    filter_headers,
    TunnelHttpRequest,
    TunnelHttpResponse,
)
from jiuwenswarm.agents.harness.common.a2a_gateway.tunnel.session import (
    TunnelSession,
    TunnelSessionOptions,
)


class TestFilterHeaders:
    def test_strips_hop_by_hop(self) -> None:
        out = filter_headers({"Host": "x", "Connection": "keep-alive",
                              "Transfer-Encoding": "chunked", "X-Trace": "t"})
        assert "host" not in out
        assert "connection" not in out
        assert "transfer-encoding" not in out
        assert out["x-trace"] == "t"

    def test_coerces_list_values(self) -> None:
        out = filter_headers({"X-Multi": ["a", "b"]})
        assert out["x-multi"] == "a"

    def test_handles_none(self) -> None:
        assert filter_headers(None) == {}
        assert filter_headers("not a dict") == {}


class TestMessageType:
    def test_constants(self) -> None:
        assert MessageType.REGISTER == "register"
        assert MessageType.FORWARD_REQUEST == "forward_request"
        assert MessageType.FORWARD_RESPONSE == "forward_response"
        assert MessageType.PING == "ping"
        assert MessageType.PONG == "pong"


class TestTunnelSessionMessageHandling:
    """Unit-test the message dispatch logic without a real websocket."""

    def _make_session(self) -> tuple[TunnelSession, MagicMock]:
        opts = TunnelSessionOptions(
            relay_url="ws://relay.test:1", device_id="dev-1",
            local_service_port=19100,
            heartbeat_interval_ms=999_999,  # disable heartbeat in tests
        )
        s = TunnelSession(opts)
        ws = MagicMock()
        ws.send = AsyncMock()
        ws.close = AsyncMock()
        s._ws = ws
        s._connected = True
        return s, ws

    def test_handle_forward_response_resolves_pending(self) -> None:
        s, ws = self._make_session()
        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            import asyncio as _aio
            timer = loop.call_later(10, lambda: None)
            from jiuwenswarm.agents.harness.common.a2a_gateway.tunnel.session import _Pending
            s._pending["mid-1"] = _Pending(future=fut, timer=timer)
            s._handle_message(json.dumps({
                "type": MessageType.FORWARD_RESPONSE,
                "message_id": "mid-1",
                "http_response": {"status": 200, "headers": {"x": "y"}, "body": "ok"},
            }))
            assert fut.done()
            result = fut.result()
            assert result.status == 200
            assert result.body == "ok"
        finally:
            loop.close()

    def test_handle_error_rejects_pending(self) -> None:
        s, ws = self._make_session()
        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            timer = loop.call_later(10, lambda: None)
            from jiuwenswarm.agents.harness.common.a2a_gateway.tunnel.session import _Pending
            s._pending["mid-2"] = _Pending(future=fut, timer=timer)
            s._handle_message(json.dumps({
                "type": MessageType.ERROR,
                "message_id": "mid-2",
                "error": "relay says no",
            }))
            assert fut.done()
            assert isinstance(fut.exception(), ConnectionError)
        finally:
            loop.close()

    def test_handle_registered_sets_event(self) -> None:
        s, ws = self._make_session()
        s._register_event = asyncio.Event()
        assert not s._register_event.is_set()
        s._handle_message(json.dumps({"type": MessageType.REGISTERED}))
        assert s._register_event.is_set()

    def test_handle_ping_sends_pong(self) -> None:
        s, ws = self._make_session()
        # _send is async; run via loop.
        loop = asyncio.new_event_loop()
        try:
            s._send = AsyncMock(return_value=True)
            s._handle_message(json.dumps({"type": MessageType.PING}))
            # The PING handler schedules an async _send(pong); pump the loop.
            # Use run_until_complete on pending tasks.
            pending = asyncio.all_tasks(loop)
            for t in pending:
                if not t.done():
                    loop.run_until_complete(t)
            s._send.assert_called_once_with({"type": MessageType.PONG})
        finally:
            loop.close()

    def test_handle_invalid_json_does_not_raise(self) -> None:
        s, _ = self._make_session()
        s._handle_message("not json at all")  # must not raise
        s._handle_message("123")

    def test_build_register_payload_with_extras(self) -> None:
        from jiuwenswarm.agents.harness.common.a2a_gateway.tunnel.protocol import (
            TunnelRegisterExtras,
        )
        opts = TunnelSessionOptions(
            relay_url="ws://r", device_id="d",
            register_extras=TunnelRegisterExtras(
                dataset="ds", service_id="svc", agent_card={"name": "agent"}))
        s = TunnelSession(opts)
        payload = s._build_register_payload()
        assert payload["type"] == MessageType.REGISTER
        assert payload["device_id"] == "d"
        assert payload["dataset"] == "ds"
        assert payload["service_id"] == "svc"
        assert payload["agent_card"] == {"name": "agent"}

    def test_build_register_payload_plain_after_rejection(self) -> None:
        from jiuwenswarm.agents.harness.common.a2a_gateway.tunnel.protocol import (
            TunnelRegisterExtras,
        )
        opts = TunnelSessionOptions(
            relay_url="ws://r", device_id="d",
            register_extras=TunnelRegisterExtras(dataset="ds", service_id="svc"))
        s = TunnelSession(opts)
        s._prefer_plain_register = True
        payload = s._build_register_payload()
        assert "dataset" not in payload
        assert "service_id" not in payload
        assert payload["device_id"] == "d"

    def test_reconnect_delay_caps_growth(self) -> None:
        opts = TunnelSessionOptions(
            relay_url="ws://r", device_id="d",
            reconnect_interval_ms=1_000, max_reconnect_interval_ms=10_000)
        s = TunnelSession(opts)
        assert s._reconnect_delay_ms(1) == 1_000
        assert s._reconnect_delay_ms(7) == 10_000  # capped
