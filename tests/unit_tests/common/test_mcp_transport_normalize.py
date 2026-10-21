# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests: MCP transport 'http' must normalize to 'streamable-http'.

openjiuwen core's MCP client registry does not recognize 'http' as a valid
client type. The entire save → store → dispatch → consume chain must
normalize 'http' → 'streamable-http' so that MCP registration succeeds.

See: https://gitcode.com/openJiuwen/jiuwenswarm/issues/5575
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common.mcp_config import build_mcp_server_config
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer


# --- build_mcp_server_config: http → streamable-http ---

def test_build_mcp_server_config_normalizes_http_transport() -> None:
    """build_mcp_server_config must map transport='http' to client_type='streamable-http'."""
    entry = {
        "name": "test-mcp",
        "transport": "http",
        "url": "https://example.com/mcp",
        "enabled": True,
    }
    cfg = build_mcp_server_config(entry)
    assert cfg is not None
    assert cfg.client_type == "streamable-http"
    assert cfg.server_name == "test-mcp"


def test_build_mcp_server_config_preserves_streamable_http() -> None:
    """streamable-http transport must pass through unchanged."""
    entry = {
        "name": "test-mcp",
        "transport": "streamable-http",
        "url": "https://example.com/mcp",
        "enabled": True,
    }
    cfg = build_mcp_server_config(entry)
    assert cfg is not None
    assert cfg.client_type == "streamable-http"


def test_build_mcp_server_config_preserves_sse() -> None:
    """sse transport must pass through unchanged."""
    entry = {
        "name": "test-mcp",
        "transport": "sse",
        "url": "https://example.com/sse",
        "enabled": True,
    }
    cfg = build_mcp_server_config(entry)
    assert cfg is not None
    assert cfg.client_type == "sse"


def test_build_mcp_server_config_preserves_stdio() -> None:
    """stdio transport must pass through unchanged."""
    entry = {
        "name": "test-mcp",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "mcp-server"],
        "enabled": True,
    }
    cfg = build_mcp_server_config(entry)
    assert cfg is not None
    assert cfg.client_type == "stdio"


# --- _normalize_mcp_payload: http → streamable-http ---

def test_normalize_mcp_payload_normalizes_http_transport() -> None:
    """_normalize_mcp_payload must persist transport='http' as 'streamable-http'."""
    payload = AgentWebSocketServer._normalize_mcp_payload({
        "name": "test-mcp",
        "transport": "http",
        "url": "https://example.com/mcp",
    })
    assert payload["transport"] == "streamable-http"
    assert payload["name"] == "test-mcp"
    assert payload["url"] == "https://example.com/mcp"


def test_normalize_mcp_payload_preserves_streamable_http() -> None:
    """streamable-http transport must pass through unchanged."""
    payload = AgentWebSocketServer._normalize_mcp_payload({
        "name": "test-mcp",
        "transport": "streamable-http",
        "url": "https://example.com/mcp",
    })
    assert payload["transport"] == "streamable-http"


def test_normalize_mcp_payload_preserves_sse() -> None:
    """sse transport must pass through unchanged."""
    payload = AgentWebSocketServer._normalize_mcp_payload({
        "name": "test-mcp",
        "transport": "sse",
        "url": "https://example.com/sse",
    })
    assert payload["transport"] == "sse"


def test_normalize_mcp_payload_preserves_stdio() -> None:
    """stdio transport must pass through unchanged."""
    payload = AgentWebSocketServer._normalize_mcp_payload({
        "name": "test-mcp",
        "transport": "stdio",
        "command": "npx",
    })
    assert payload["transport"] == "stdio"
    assert payload["command"] == "npx"


def test_normalize_mcp_payload_normalizes_http_case_insensitive() -> None:
    """HTTP (uppercase) must also normalize to streamable-http."""
    payload = AgentWebSocketServer._normalize_mcp_payload({
        "name": "test-mcp",
        "transport": "HTTP",
        "url": "https://example.com/mcp",
    })
    assert payload["transport"] == "streamable-http"
