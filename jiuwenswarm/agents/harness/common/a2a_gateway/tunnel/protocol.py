# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Relay tunnel wire protocol — message types and helpers.

Mirrors OpenClaw ``src/tunnel/protocol.ts``. The relay is a simple
JSON-over-WebSocket store-and-forward: a device registers, then sends
``forward_request`` targeting another device's id and awaits the matching
``forward_response``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Hop-by-hop headers the relay strips / clients shouldn't propagate.
_HOP_BY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host",
})


class MessageType:
    """Tunnel relay message type labels (kept as plain str constants)."""

    REGISTER = "register"
    REGISTERED = "registered"
    FORWARD_REQUEST = "forward_request"
    FORWARD_RESPONSE = "forward_response"
    ERROR = "error"
    PING = "ping"
    PONG = "pong"


@dataclass
class TunnelHttpRequest:
    method: str = "GET"
    path: str = "/"
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""


@dataclass
class TunnelHttpResponse:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""


@dataclass
class TunnelRegisterExtras:
    """Enriched REGISTER payload (mirrors OpenClaw ``TunnelRegisterExtras``)."""

    dataset: str = ""
    service_id: str = ""
    agent_card: dict[str, Any] | None = None


def filter_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    """Strip hop-by-hop headers and coerce values to ``str``.

    Mirrors OpenClaw ``filterHeaders``: the relay should not carry
    connection-level headers, and clients expect flat string maps.
    """
    if not isinstance(headers, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in headers.items():
        key = str(k).lower()
        if key in _HOP_BY_HOP_HEADERS:
            continue
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ""
        out[key] = str(v) if v is not None else ""
    return out
