# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the A2A gateway runtime holders + init helper.

The outbound client + tunnel are per-process singletons; ``init_a2a_gateway``
builds them from config. The critical invariant: a process hosting agent tools
(the AgentServer) can bring up its own outbound half with a *distinct* tunnel
device id so the relay doesn't clobber another process's receive registration.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.a2a_gateway import runtime as gw_runtime


def _enabled_cfg(device_id: str = "jws-teammate-01", relay: str = "ws://r:8001") -> dict:
    return {
        "a2a_gateway": {
            "enabled": True,
            "tunnel": {
                "relay_url": relay,
                "device_id": device_id,
                "local_service_port": 19100,
                "dataset": "openclaw_devices",
            },
        },
    }


class TestInitA2AGateway:
    async def test_disabled_config_skips_init(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Reset holders to a clean state.
        monkeypatch.setattr(gw_runtime, "_outbound_client", None)
        monkeypatch.setattr(gw_runtime, "_tunnel", None)
        await gw_runtime.init_a2a_gateway({"a2a_gateway": {"enabled": False}}, tag="t")
        assert gw_runtime.get_outbound_client() is None
        assert gw_runtime.get_tunnel() is None

    async def test_enabled_registers_outbound_client(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gw_runtime, "_outbound_client", None)
        monkeypatch.setattr(gw_runtime, "_tunnel", None)
        # Stub the outbound client ctor so we don't need a live a2a SDK transport.
        import jiuwenswarm.agents.harness.common.a2a_gateway.outbound_client as oc_mod

        class _FakeOutbound:
            def __init__(self, _cfg):
                self._tunnel = None
            def set_tunnel(self, t):
                self._tunnel = t
        monkeypatch.setattr(oc_mod, "A2AOutboundClient", _FakeOutbound)

        await gw_runtime.init_a2a_gateway(_enabled_cfg(), tag="t")
        assert gw_runtime.get_outbound_client() is not None
        # Tunnel registered too (relay_url + device_id present).
        assert gw_runtime.get_tunnel() is not None

    async def test_device_id_override_applied_to_tunnel(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AgentServer must register a distinct id so it doesn't clobber the
        Gateway's receive registration on the relay."""
        monkeypatch.setattr(gw_runtime, "_outbound_client", None)
        monkeypatch.setattr(gw_runtime, "_tunnel", None)
        import jiuwenswarm.agents.harness.common.a2a_gateway.outbound_client as oc_mod

        class _FakeOutbound:
            def __init__(self, _cfg):
                self._tunnel = None
            def set_tunnel(self, t):
                self._tunnel = t
        monkeypatch.setattr(oc_mod, "A2AOutboundClient", _FakeOutbound)

        captured_opts: dict = {}

        class _FakeTunnel:
            def __init__(self, opts):
                captured_opts["opts"] = opts
            async def start(self):
                return None
        import jiuwenswarm.agents.harness.common.a2a_gateway.tunnel.session as ts_mod
        monkeypatch.setattr(ts_mod, "TunnelSession", _FakeTunnel)

        await gw_runtime.init_a2a_gateway(
            _enabled_cfg(device_id="jws-teammate-01"),
            device_id_override="jws-teammate-outbound",
            tag="AgentServer",
        )
        opts = captured_opts["opts"]
        assert opts.device_id == "jws-teammate-outbound"
        # Relay + local port still come from config.
        assert opts.relay_url == "ws://r:8001"
        assert opts.local_service_port == 19100

    async def test_no_tunnel_when_relay_missing(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(gw_runtime, "_outbound_client", None)
        monkeypatch.setattr(gw_runtime, "_tunnel", None)
        import jiuwenswarm.agents.harness.common.a2a_gateway.outbound_client as oc_mod

        class _FakeOutbound:
            def __init__(self, _cfg):
                self._tunnel = None
            def set_tunnel(self, t):
                self._tunnel = t
        monkeypatch.setattr(oc_mod, "A2AOutboundClient", _FakeOutbound)

        # Enabled but no relay_url/device_id → outbound only, no tunnel.
        cfg = {"a2a_gateway": {"enabled": True, "tunnel": {}}}
        await gw_runtime.init_a2a_gateway(cfg, tag="t")
        assert gw_runtime.get_outbound_client() is not None
        assert gw_runtime.get_tunnel() is None
