# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JiuwenSwarm A2A Gateway — outbound client, NAT tunnel, and file tools.

This module ports the OpenClaw ``a2a-gateway`` plugin's outbound capabilities
into JiuwenSwarm. The inbound A2A server (``a2a_connect.py``) and the A2X
registry runtime already exist and are reused; this package adds the three
pieces JiuwenSwarm previously lacked:

* :class:`~jiuwenswarm.agents.harness.common.a2a_gateway.outbound_client.A2AOutboundClient`
  — outbound A2A client built on ``a2a-sdk`` (Google A2A Python SDK), with
  circuit-breaker, retry, and transport fallback.
* :class:`~jiuwenswarm.agents.harness.common.a2a_gateway.tunnel.session.TunnelSession`
  — WebSocket relay client for NAT traversal (the only truly novel component).
* :class:`~jiuwenswarm.agents.harness.common.a2a_gateway.tools.A2ASendFileToolkit`
  — ``a2a_send_file`` / ``a2a_send_local_file`` agent tools.

Configuration mirrors the OpenClaw plugin's ``openclaw.plugin.json`` schema,
expressed as JiuwenSwarm ``config.yaml`` sections and ``A2A_*`` env vars.
"""

from __future__ import annotations

from .config import (
    A2AGatewayConfig,
    CircuitBreakerConfig,
    PeerAuthConfig,
    PeerConfig,
    ResilienceConfig,
    RetryConfig,
    SecurityConfig,
    TunnelConfig,
)

__all__ = [
    "A2AGatewayConfig",
    "CircuitBreakerConfig",
    "PeerAuthConfig",
    "PeerConfig",
    "ResilienceConfig",
    "RetryConfig",
    "SecurityConfig",
    "TunnelConfig",
]
