# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the A2A Gateway config dataclasses."""

from __future__ import annotations

import os

import pytest

from jiuwenswarm.agents.harness.common.a2a_gateway.config import (
    A2AGatewayConfig,
    PeerAuthConfig,
    PeerConfig,
)


class TestPeerAuthConfig:
    def test_bearer_header(self) -> None:
        auth = PeerAuthConfig(type="bearer", token="abc")
        assert auth.header() == ("authorization", "Bearer abc")

    def test_api_key_header(self) -> None:
        auth = PeerAuthConfig(type="api_key", token="xyz")
        assert auth.header() == ("x-api-key", "xyz")

    def test_from_dict_no_token_returns_none(self) -> None:
        assert PeerAuthConfig.from_dict({"type": "bearer"}) is None
        assert PeerAuthConfig.from_dict(None) is None


class TestPeerConfig:
    def test_from_dict_camel_case_fallback(self) -> None:
        peer = PeerConfig.from_dict({
            "name": "remote",
            "agentCardUrl": "http://x/card.json",
            "tunnelDeviceId": "dev1",
            "auth": {"type": "bearer", "token": "t"},
        })
        assert peer.name == "remote"
        assert peer.agent_card_url == "http://x/card.json"
        assert peer.tunnel_device_id == "dev1"
        assert peer.auth is not None and peer.auth.token == "t"

    def test_from_dict_snake_case(self) -> None:
        peer = PeerConfig.from_dict({
            "name": "r",
            "agent_card_url": "http://y/card.json",
        })
        assert peer.agent_card_url == "http://y/card.json"
        assert peer.tunnel_device_id is None
        assert peer.auth is None


class TestA2AGatewayConfig:
    def test_from_config_disabled_by_default(self) -> None:
        cfg = A2AGatewayConfig.from_config(None)
        assert cfg.enabled is False
        assert cfg.peers == []
        assert cfg.default_agent_id == "main"

    def test_from_config_with_peers(self) -> None:
        cfg = A2AGatewayConfig.from_config({
            "enabled": True,
            "peers": [
                {"name": "a", "agent_card_url": "http://a/card.json"},
                {"name": "b", "agentCardUrl": "http://b/card.json",
                 "auth": {"token": "k"}},
            ],
        })
        assert cfg.enabled is True
        assert len(cfg.peers) == 2
        assert cfg.peers[0].name == "a"
        assert cfg.peers[1].auth is not None

    def test_from_config_filters_invalid_peers(self) -> None:
        cfg = A2AGatewayConfig.from_config({
            "enabled": True,
            "peers": [
                {"name": "", "agent_card_url": "http://x"},   # no name
                {"name": "y", "agent_card_url": ""},           # no url
                {"name": "z", "agent_card_url": "http://z"},   # valid
            ],
        })
        assert len(cfg.peers) == 1
        assert cfg.peers[0].name == "z"

    def test_peer_by_name(self) -> None:
        cfg = A2AGatewayConfig.from_config({
            "enabled": True,
            "peers": [{"name": "p1", "agent_card_url": "http://p1"}],
        })
        assert cfg.peer_by_name("p1") is not None
        assert cfg.peer_by_name("missing") is None

    def test_from_config_env_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A2A_GATEWAY_ENABLED", "true")
        monkeypatch.setenv("A2A_TUNNEL_RELAY_URL", "ws://relay:1")
        monkeypatch.setenv("A2A_TUNNEL_DEVICE_ID", "dev-env")
        cfg = A2AGatewayConfig.from_config({})
        assert cfg.enabled is True
        assert cfg.tunnel.relay_url == "ws://relay:1"
        assert cfg.tunnel.device_id == "dev-env"

    def test_from_config_config_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A2A_TUNNEL_RELAY_URL", "ws://env-relay")
        cfg = A2AGatewayConfig.from_config({
            "tunnel": {"relay_url": "ws://cfg-relay"},
        })
        assert cfg.tunnel.relay_url == "ws://cfg-relay"

    def test_security_peer_token_from_config(self) -> None:
        cfg = A2AGatewayConfig.from_config({
            "security": {"peer_token": "12345678"},
        })
        assert cfg.security.peer_token == "12345678"
        auth = cfg.default_peer_auth()
        assert auth is not None
        assert auth.type == "bearer"
        assert auth.header() == ("authorization", "Bearer 12345678")

    def test_security_peer_token_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A2A_PEER_TOKEN", "env-secret")
        cfg = A2AGatewayConfig.from_config({})
        assert cfg.security.peer_token == "env-secret"
        assert cfg.default_peer_auth() is not None

    def test_default_peer_auth_none_when_token_blank(self) -> None:
        cfg = A2AGatewayConfig.from_config({"security": {"peer_token": "   "}})
        assert cfg.default_peer_auth() is None

    def test_config_camel_case_peer_token(self) -> None:
        cfg = A2AGatewayConfig.from_config({
            "security": {"peerToken": "camel"},
        })
        assert cfg.security.peer_token == ""  # not recognized; use snake_case peer_token
