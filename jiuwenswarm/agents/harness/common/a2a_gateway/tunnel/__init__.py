# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Relay tunnel subsystem for the A2A Gateway.

Exposes :class:`TunnelSession` and :func:`create_tunnel_fetch`. The latter
wraps a tunnel session so an outbound A2A client can reach a peer behind NAT
by routing its HTTP requests through the relay (gRPC is not tunnelable — the
outbound client filters it out for tunnel peers).
"""

from __future__ import annotations

from .protocol import (
    MessageType,
    TunnelHttpRequest,
    TunnelHttpResponse,
    TunnelRegisterExtras,
    filter_headers,
)
from .session import (
    DEFAULT_HEARTBEAT_INTERVAL_MS,
    DEFAULT_HEARTBEAT_TIMEOUT_MS,
    DEFAULT_MAX_RECONNECT_ATTEMPTS,
    DEFAULT_MAX_RECONNECT_INTERVAL_MS,
    DEFAULT_RECONNECT_INTERVAL_MS,
    DEFAULT_REQUEST_TIMEOUT_MS,
    TunnelSession,
    TunnelSessionOptions,
    create_tunnel_session,
)

__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_MS",
    "DEFAULT_HEARTBEAT_TIMEOUT_MS",
    "DEFAULT_MAX_RECONNECT_ATTEMPTS",
    "DEFAULT_MAX_RECONNECT_INTERVAL_MS",
    "DEFAULT_RECONNECT_INTERVAL_MS",
    "DEFAULT_REQUEST_TIMEOUT_MS",
    "MessageType",
    "TunnelHttpRequest",
    "TunnelHttpResponse",
    "TunnelRegisterExtras",
    "TunnelSession",
    "TunnelSessionOptions",
    "create_tunnel_session",
    "filter_headers",
]


def create_tunnel_fetch(tunnel: TunnelSession, target_device: str):
    """Return an async callable ``fetch(request: TunnelHttpRequest) -> TunnelHttpResponse``.

    The outbound client uses this to reach tunnel peers: instead of opening a
    direct TCP connection, it forwards the HTTP request through the relay to
    the peer's device id. The peer's local inbound A2A server handles it.
    """

    async def _fetch(request: TunnelHttpRequest) -> TunnelHttpResponse:
        return await tunnel.forward(target_device, request)

    return _fetch
