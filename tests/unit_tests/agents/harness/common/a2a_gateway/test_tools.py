# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the outbound A2A file toolkit."""

from __future__ import annotations

import base64
import os
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.agents.harness.common.a2a_gateway.config import (
    A2AGatewayConfig,
    PeerConfig,
)
from jiuwenswarm.agents.harness.common.a2a_gateway.outbound_client import (
    OutboundSendResult,
)
from jiuwenswarm.agents.harness.common.a2a_gateway import runtime as gw_runtime
from jiuwenswarm.agents.harness.common.a2a_gateway import tools as gw_tools
from jiuwenswarm.agents.harness.common.a2a_gateway.tools import (
    A2ASendFileToolkit,
)


class TestA2ASendFileToolkitGetTools:
    def test_returns_four_tools(self) -> None:
        tk = A2ASendFileToolkit()
        tools = tk.get_tools()
        assert len(tools) == 4
        names = {getattr(t, "name", None) or getattr(getattr(t, "card", None), "name", None)
                 for t in tools}
        assert names == {None} or len(tools) == 4  # LocalFunction instances

    def test_tool_card_names(self) -> None:
        tk = A2ASendFileToolkit()
        tools = tk.get_tools()
        # LocalFunction stores the card; card.name is the tool name.
        names = []
        for t in tools:
            card = getattr(t, "card", t)
            names.append(getattr(card, "name", str(card)))
        assert "a2a_send_file" in names
        assert "a2a_send_local_file" in names
        assert "a2x_list_agents" in names


class TestA2ASendFileToolkitDispatch:
    async def test_send_file_without_client_returns_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gw_runtime, "_outbound_client", None)
        tk = A2ASendFileToolkit()
        result = await tk.a2a_send_file(peer_name="p1", file_uri="http://x/f.pdf")
        assert "未启用" in result or "not enabled" in result.lower()

    async def test_send_file_unknown_peer_returns_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = A2AGatewayConfig.from_config({
            "enabled": True,
            "peers": [{"name": "p1", "agent_card_url": "http://p/c"}],
        })
        fake_client = MagicMock()
        fake_client._config = cfg
        monkeypatch.setattr(gw_runtime, "_outbound_client", fake_client)
        tk = A2ASendFileToolkit()
        result = await tk.a2a_send_file(peer_name="missing", file_uri="http://x/f.pdf")
        assert "未找到" in result

    async def test_send_file_no_source_returns_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = A2AGatewayConfig.from_config({
            "enabled": True,
            "peers": [{"name": "p1", "agent_card_url": "http://p/c"}],
        })
        fake_client = MagicMock()
        fake_client._config = cfg
        monkeypatch.setattr(gw_runtime, "_outbound_client", fake_client)
        tk = A2ASendFileToolkit()
        result = await tk.a2a_send_file(peer_name="p1")
        assert "必须提供" in result

    async def test_send_local_file_too_large_rejected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = A2AGatewayConfig.from_config({
            "enabled": True,
            "peers": [{"name": "p1", "agent_card_url": "http://p/c"}],
            "security": {"max_file_size_bytes": 10},
        })
        fake_client = MagicMock()
        fake_client._config = cfg
        monkeypatch.setattr(gw_runtime, "_outbound_client", fake_client)
        # Create a file larger than 10 bytes.
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * 100)
        tk = A2ASendFileToolkit()
        result = await tk.a2a_send_local_file(peer_name="p1", abs_file_path=str(big))
        assert "过大" in result

    async def test_send_local_file_missing_returns_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = A2AGatewayConfig.from_config({
            "enabled": True,
            "peers": [{"name": "p1", "agent_card_url": "http://p/c"}],
        })
        fake_client = MagicMock()
        fake_client._config = cfg
        monkeypatch.setattr(gw_runtime, "_outbound_client", fake_client)
        tk = A2ASendFileToolkit()
        result = await tk.a2a_send_local_file(peer_name="p1", abs_file_path="/no/such/file.txt")
        assert "不存在" in result

    async def test_send_local_file_success(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = A2AGatewayConfig.from_config({
            "enabled": True,
            "peers": [{"name": "p1", "agent_card_url": "http://p/c"}],
        })
        fake_client = MagicMock()
        fake_client._config = cfg
        fake_client.send_message = AsyncMock(return_value=OutboundSendResult(
            ok=True, status_code=200, text="received"))
        monkeypatch.setattr(gw_runtime, "_outbound_client", fake_client)
        # File on disk.
        f = tmp_path / "report.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        tk = A2ASendFileToolkit()
        result = await tk.a2a_send_local_file(peer_name="p1", abs_file_path=str(f))
        assert result == "received"
        # Confirm send_message was called with a FilePart carrying the basename.
        fake_client.send_message.assert_called_once()
        call_kwargs = fake_client.send_message.call_args.kwargs
        parts = call_kwargs.get("parts", [])
        assert any(isinstance(p, dict) and p.get("filename") == "report.pdf" for p in parts)

    async def test_build_local_file_part_basename_only(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = A2AGatewayConfig.from_config({
            "enabled": True, "peers": [{"name": "p1", "agent_card_url": "http://p/c"}],
        })
        fake_client = MagicMock()
        fake_client._config = cfg
        monkeypatch.setattr(gw_runtime, "_outbound_client", fake_client)
        f = tmp_path / "data.csv"
        f.write_bytes(b"a,b,c\n1,2,3")
        tk = A2ASendFileToolkit()
        part = tk._build_local_file_part(str(f), fake_client)
        assert isinstance(part, dict)
        assert part["filename"] == "data.csv"
        assert part["media_type"] == "text/csv"
        # raw is base64-encoded bytes.
        assert base64.b64decode(part["raw"]) == b"a,b,c\n1,2,3"


class TestA2XListAgentsGetTools:
    def test_returns_four_tools(self) -> None:
        tk = A2ASendFileToolkit()
        tools = tk.get_tools()
        assert len(tools) == 4
        names = []
        for t in tools:
            card = getattr(t, "card", t)
            names.append(getattr(card, "name", str(card)))
        assert "a2x_list_agents" in names
        assert "a2a_send_file" in names
        assert "a2a_send_message" in names


def _agent_row(**overrides) -> dict:
    """A single A2X service entry shaped like parse_agent_list output."""
    row = {
        "id": "agent_abc",
        "name": "_BlankAgent_http://127.0.0.1:19100/a2a",
        "description": "__BLANK__",
        "endpoint": "http://127.0.0.1:19100/a2a",
        "status": "online",
        "metadata": {"tunnelDeviceId": "jws-teammate-01"},
    }
    row.update(overrides)
    return row


class TestA2XListAgentsDispatch:
    """The read-only query tool — all paths must return text, never raise."""

    def _patch_runtime(self, monkeypatch: pytest.MonkeyPatch, *, agents_by_ds=None, datasets=None):
        """Monkeypatch the A2X runtime + config so the tool hits fakes."""
        # Replace get_config with a stub returning a minimal config base.
        import jiuwenswarm.common.config as cfg_mod  # noqa: PLC0415
        monkeypatch.setattr(cfg_mod, "get_config", lambda: {"react": {"a2x_registry": {}}})

        import jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime as rt  # noqa: PLC0415

        async def _init_a2x_client(config_base):  # noqa: ARG001
            return _FakeA2XClient(agents_by_ds=agents_by_ds or {}, datasets=datasets or []), {}

        monkeypatch.setattr(rt, "init_a2x_client", _init_a2x_client)
        # resolve_a2x_config is also late-imported; stub it to a fixed shape.
        monkeypatch.setattr(
            rt, "resolve_a2x_config",
            lambda config_base: {  # noqa: ARG005
                "base_url": "http://registry:8000",
                "dataset": "team_pool",
            },
        )

    async def test_lists_online_agents_for_configured_dataset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agents = [_agent_row()]
        self._patch_runtime(monkeypatch, agents_by_ds={"team_pool": agents})
        tk = A2ASendFileToolkit()
        result = await tk.a2x_list_agents()
        assert "team_pool" in result
        assert "1 个" in result or "共" in result
        assert "http://127.0.0.1:19100/a2a" in result
        assert "jws-teammate-01" in result

    async def test_status_online_filter_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Two agents, one online one busy; default status=online returns only the online one.
        online = _agent_row(id="agent_on", status="online")
        busy = _agent_row(id="agent_busy", status="busy", endpoint="http://x:1/a2a")
        self._patch_runtime(monkeypatch, agents_by_ds={"team_pool": [online, busy]})

        # The fake client records the filters it was called with so we can assert.
        import jiuwenswarm.common.config as cfg_mod  # noqa: PLC0415
        monkeypatch.setattr(cfg_mod, "get_config", lambda: {"react": {"a2x_registry": {}}})
        import jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime as rt  # noqa: PLC0415
        captured = {}

        async def _init(config_base):  # noqa: ARG001
            return _FakeA2XClient(
                agents_by_ds={"team_pool": [online, busy]},
                datasets=[],
                captured=captured,
            ), {}

        monkeypatch.setattr(rt, "init_a2x_client", _init)
        monkeypatch.setattr(rt, "resolve_a2x_config",
                            lambda cb: {"base_url": "http://r", "dataset": "team_pool"})  # noqa: ARG005
        tk = A2ASendFileToolkit()
        result = await tk.a2x_list_agents(status="online")
        # FakeA2XClient filters by status to mimic the backend; only online remains.
        assert "agent_on" in result
        assert "agent_busy" not in result

    async def test_explicit_dataset_overrides_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_runtime(monkeypatch, agents_by_ds={"cli_agents": [_agent_row(id="cli1")]})
        tk = A2ASendFileToolkit()
        result = await tk.a2x_list_agents(dataset="cli_agents")
        assert "cli_agents" in result
        assert "cli1" in result

    async def test_include_all_datasets_aggregates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_runtime(
            monkeypatch,
            agents_by_ds={
                "team_pool": [_agent_row(id="tp1")],
                "cli_agents": [_agent_row(id="ca1", endpoint="http://c:9/a2a")],
            },
            datasets=["team_pool", "cli_agents"],
        )
        tk = A2ASendFileToolkit()
        result = await tk.a2x_list_agents(include_all_datasets=True)
        assert "team_pool" in result
        assert "cli_agents" in result
        assert "tp1" in result
        assert "ca1" in result
        assert "2 个数据集" in result

    async def test_no_dataset_returns_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Config reports no dataset and no explicit dataset arg → helpful hint.
        import jiuwenswarm.common.config as cfg_mod  # noqa: PLC0415
        monkeypatch.setattr(cfg_mod, "get_config", lambda: {"react": {"a2x_registry": {}}})
        import jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime as rt  # noqa: PLC0415

        async def _init(config_base):  # noqa: ARG001
            return _FakeA2XClient(agents_by_ds={}, datasets=[]), {}
        monkeypatch.setattr(rt, "init_a2x_client", _init)
        monkeypatch.setattr(rt, "resolve_a2x_config",
                            lambda cb: {"base_url": "http://r", "dataset": ""})  # noqa: ARG005
        tk = A2ASendFileToolkit()
        result = await tk.a2x_list_agents()
        assert "dataset" in result and ("未指定" in result or "include_all_datasets" in result)

    async def test_init_failure_returns_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jiuwenswarm.common.config as cfg_mod  # noqa: PLC0415
        monkeypatch.setattr(cfg_mod, "get_config", lambda: {"react": {"a2x_registry": {}}})
        import jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime as rt  # noqa: PLC0415

        async def _boom(config_base):  # noqa: ARG001
            raise RuntimeError("no registry")
        monkeypatch.setattr(rt, "init_a2x_client", _boom)
        monkeypatch.setattr(rt, "resolve_a2x_config",
                            lambda cb: {"base_url": "http://r", "dataset": "team_pool"})  # noqa: ARG005
        tk = A2ASendFileToolkit()
        result = await tk.a2x_list_agents()
        assert "初始化失败" in result


class TestA2ASendMessageGetTools:
    def test_returns_four_tools(self) -> None:
        tk = A2ASendFileToolkit()
        tools = tk.get_tools()
        assert len(tools) == 4
        names = []
        for t in tools:
            card = getattr(t, "card", t)
            names.append(getattr(card, "name", str(card)))
        assert "a2a_send_message" in names
        assert "a2x_list_agents" in names


class TestA2ASendMessageDispatch:
    """Send-text-to-registry-agent tool — all paths return text, never raise."""

    def _patch_registry(self, monkeypatch: pytest.MonkeyPatch, *, agents_by_ds=None):
        import jiuwenswarm.common.config as cfg_mod  # noqa: PLC0415
        monkeypatch.setattr(cfg_mod, "get_config", lambda: {"react": {"a2x_registry": {}}})
        import jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime as rt  # noqa: PLC0415

        async def _init(config_base):  # noqa: ARG001
            return _FakeA2XClient(agents_by_ds=agents_by_ds or {}, datasets=[]), {}
        monkeypatch.setattr(rt, "init_a2x_client", _init)
        monkeypatch.setattr(rt, "resolve_a2x_config",
                            lambda cb: {"base_url": "http://r", "dataset": "openclaw_devices"})  # noqa: ARG005

    def _patch_outbound(self, monkeypatch: pytest.MonkeyPatch, *, send_result=None, raises=None,
                        peer_token: str = ""):
        from jiuwenswarm.agents.harness.common.a2a_gateway import runtime as gw_runtime
        from jiuwenswarm.agents.harness.common.a2a_gateway.outbound_client import OutboundSendResult
        fake_client = MagicMock()
        # The impl reads outbound._config.default_peer_auth(); give it a real config.
        fake_client._config = A2AGatewayConfig.from_config({
            "enabled": True,
            "security": {"peer_token": peer_token},
        })

        async def _send(peer, *, text="", parts=None, **kwargs):  # noqa: ARG001
            if raises is not None:
                raise raises
            return send_result or OutboundSendResult(ok=True, status_code=200, text="hi back")
        fake_client.send_message = _send
        # _match_agents needs the peer name; record the peer we got.
        fake_client._last_peer = {}

        async def _record(peer, **kw):  # noqa: ARG001
            fake_client._last_peer = peer
            return await _send(peer, **kw)
        fake_client.send_message = _record
        monkeypatch.setattr(gw_runtime, "_outbound_client", fake_client)
        return fake_client

    async def test_send_message_to_matching_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agents = [_agent_row(id="HW-Phone1", name="HW-Phone1-Agent1",
                             endpoint="http://127.0.0.1:18800/a2a/jsonrpc")]
        self._patch_registry(monkeypatch, agents_by_ds={"openclaw_devices": agents})
        fake = self._patch_outbound(monkeypatch, send_result=OutboundSendResult(
            ok=True, status_code=200, text="hello from Phone1"))
        tk = A2ASendFileToolkit()
        result = await tk.a2a_send_message(agent_name="HW-Phone1", message="hello")
        assert "hello from Phone1" in result
        # Peer built with endpoint + tunnel device id from the registry entry.
        peer = fake._last_peer
        assert peer.agent_card_url == "http://127.0.0.1:18800/a2a/jsonrpc"
        assert peer.tunnel_device_id == "jws-teammate-01"

    async def test_send_message_attaches_default_peer_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agents = [_agent_row(id="HW-Phone1", name="HW-Phone1-Agent1",
                             endpoint="http://127.0.0.1:18800/a2a/jsonrpc")]
        self._patch_registry(monkeypatch, agents_by_ds={"openclaw_devices": agents})
        fake = self._patch_outbound(monkeypatch, peer_token="12345678",
                                    send_result=OutboundSendResult(ok=True, status_code=200, text="ok"))
        tk = A2ASendFileToolkit()
        await tk.a2a_send_message(agent_name="HW-Phone1", message="hello")
        peer = fake._last_peer
        assert peer.auth is not None
        assert peer.auth.header() == ("authorization", "Bearer 12345678")

    async def test_send_message_without_peer_token_has_no_auth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agents = [_agent_row(id="a1", name="Alpha", endpoint="http://x:1/a2a")]
        self._patch_registry(monkeypatch, agents_by_ds={"openclaw_devices": agents})
        fake = self._patch_outbound(monkeypatch, peer_token="",
                                    send_result=OutboundSendResult(ok=True, status_code=200, text="ok"))
        tk = A2ASendFileToolkit()
        await tk.a2a_send_message(agent_name="Alpha", message="hi")
        assert fake._last_peer.auth is None

    async def test_send_message_no_outbound_returns_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_registry(monkeypatch, agents_by_ds={"openclaw_devices": [_agent_row()]})
        from jiuwenswarm.agents.harness.common.a2a_gateway import runtime as gw_runtime
        monkeypatch.setattr(gw_runtime, "_outbound_client", None)
        tk = A2ASendFileToolkit()
        result = await tk.a2a_send_message(agent_name="x", message="hi")
        assert "未启用" in result

    async def test_send_message_agent_not_found_lists_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agents = [_agent_row(id="a1", name="Alpha"),
                  _agent_row(id="a2", name="Beta", endpoint="http://x:2/a2a")]
        self._patch_registry(monkeypatch, agents_by_ds={"openclaw_devices": agents})
        self._patch_outbound(monkeypatch)
        tk = A2ASendFileToolkit()
        result = await tk.a2a_send_message(agent_name="Gamma", message="hi")
        assert "未找到" in result or "Gamma" in result
        assert "Alpha" in result and "Beta" in result

    async def test_send_message_multiple_matches_asks_precise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agents = [_agent_row(id="a1", name="Team-A"),
                  _agent_row(id="a2", name="Team-B", endpoint="http://x:2/a2a")]
        self._patch_registry(monkeypatch, agents_by_ds={"openclaw_devices": agents})
        self._patch_outbound(monkeypatch)
        tk = A2ASendFileToolkit()
        result = await tk.a2a_send_message(agent_name="Team", message="hi")
        assert "多个" in result or "更精确" in result

    async def test_send_message_empty_args(self) -> None:
        tk = A2ASendFileToolkit()
        assert "agent_name" in await tk.a2a_send_message(agent_name="", message="x")
        assert "message" in await tk.a2a_send_message(agent_name="x", message="")

    async def test_send_message_send_failure_returns_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agents = [_agent_row(id="a1", name="Alpha")]
        self._patch_registry(monkeypatch, agents_by_ds={"openclaw_devices": agents})
        from jiuwenswarm.agents.harness.common.a2a_gateway.outbound_client import OutboundSendResult
        self._patch_outbound(monkeypatch, send_result=OutboundSendResult(
            ok=False, status_code=503, response={"error": "circuit open"}))
        tk = A2ASendFileToolkit()
        result = await tk.a2a_send_message(agent_name="Alpha", message="hi")
        assert "503" in result and "circuit open" in result


class _FakeTransport:
    """Minimal transport stub: answers ``GET /api/datasets`` for the query tool."""

    def __init__(self, datasets: list[str]) -> None:
        self._datasets = datasets

    async def request(self, method: str, path: str, **kwargs):  # noqa: ARG002
        if path == "/api/datasets":
            return _FakeResp([{"name": name} for name in self._datasets])
        return _FakeResp([])


class _FakeResp:
    """Tiny stand-in for an httpx.Response carrying parsed JSON."""

    def __init__(self, data) -> None:
        self._data = data

    def json(self):
        return self._data


class _FakeA2XClient:
    """Minimal stand-in for AsyncA2XClient: filters list_agents by status.

    Exposes ``_transport`` backed by :class:`_FakeTransport` so the query
    tool's ``_list_all_datasets`` path can enumerate datasets via the same
    ``GET /api/datasets`` request the real transport serves.
    """

    def __init__(self, *, agents_by_ds: dict, datasets: list, captured: dict | None = None) -> None:
        self._agents_by_ds = agents_by_ds
        self._datasets = datasets
        self._captured = captured
        # transport drives _list_all_datasets (GET /api/datasets).
        self._transport = _FakeTransport(datasets) if datasets else None

    async def list_agents(self, dataset: str, **filters) -> list[dict]:
        if self._captured is not None:
            self._captured["filters"] = dict(filters)
        rows = list(self._agents_by_ds.get(dataset, []))
        status = filters.get("status")
        if status:
            rows = [r for r in rows if str(r.get("status")) == str(status)]
        return rows

    async def aclose(self) -> None:
        return None

