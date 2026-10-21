# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration dataclasses for the A2A Gateway.

Mirrors the OpenClaw ``openclaw.plugin.json`` config schema (agentCard /
server / security / routing / limits / timeouts / resilience / tunnel /
registry / peers) as plain Python dataclasses so they can be loaded from
JiuwenSwarm ``config.yaml`` (``a2a_gateway.*`` section) and ``A2A_*`` env
vars without pulling in a JSON-schema validator.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip() or default


@dataclass
class PeerAuthConfig:
    """Auth credentials for reaching a peer's inbound A2A server."""

    type: str = "bearer"  # "bearer" | "api_key"
    token: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PeerAuthConfig | None:
        if not isinstance(data, dict) or not data.get("token"):
            return None
        return cls(type=str(data.get("type") or "bearer").strip().lower() or "bearer",
                   token=str(data.get("token") or "").strip())

    def header(self) -> tuple[str, str]:
        """Return (header_name, header_value) for the configured auth type."""
        if self.type == "bearer":
            return ("authorization", f"Bearer {self.token}")
        return ("x-api-key", self.token)


@dataclass
class PeerConfig:
    """A single outbound A2A peer.

    ``agent_card_url`` points at the peer's Agent Card discovery endpoint
    (``/.well-known/agent-card.json``). ``tunnel_device_id`` is set when the
    peer is reachable only through the relay tunnel (HTTP-only).
    """

    name: str
    agent_card_url: str
    auth: PeerAuthConfig | None = None
    tunnel_device_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PeerConfig:
        return cls(
            name=str(data.get("name") or "").strip(),
            agent_card_url=str(data.get("agent_card_url") or data.get("agentCardUrl") or "").strip(),
            auth=PeerAuthConfig.from_dict(data.get("auth") if isinstance(data.get("auth"), dict) else None),
            tunnel_device_id=(str(data.get("tunnel_device_id") or data.get("tunnelDeviceId") or "").strip()
                              or None),
        )


@dataclass
class RetryConfig:
    """Exponential-backoff retry for outbound sends."""

    max_retries: int = 3
    initial_delay_ms: int = 500
    max_delay_ms: int = 10_000
    backoff_factor: float = 2.0
    jitter_ms: int = 250


@dataclass
class CircuitBreakerConfig:
    """Receptor-desensitization circuit breaker (bio-inspired).

    States: ``closed`` → ``desensitized`` → ``open`` → ``recovering`` → ``closed``.
    Mirrors OpenClaw ``src/types.ts`` ``CircuitBreakerConfig``.
    """

    enabled: bool = True
    failure_threshold: int = 5  # consecutive failures to open
    soft_threshold: float = 0.5  # failure rate over window to desensitize
    desensitized_capacity: int = 8  # max concurrent calls while desensitized
    recovery_rate_constant: float = 0.1  # per-second recovery toward closed
    open_timeout_ms: int = 30_000  # cool-down before recovering


@dataclass
class ResilienceConfig:
    """Resilience knobs (health check / retry / circuit breaker)."""

    retry: RetryConfig = field(default_factory=RetryConfig)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    health_check_interval_ms: int = 60_000
    health_check_timeout_ms: int = 5_000


@dataclass
class SecurityConfig:
    """Security limits for file handling (inbound enforced by a2a_connect.py,
    outbound enforced by A2ASendFileToolkit)."""

    inbound_auth: str = "bearer"  # "bearer" | "none"
    token: str = ""
    # Bearer token attached to outbound requests to A2X-registry-resolved peers
    # that carry no per-peer ``auth``. This is the shared secret peers expect;
    # per-peer ``auth`` (in ``a2a_gateway.peers[].auth``) always takes
    # precedence, since it is more specific.
    peer_token: str = ""
    allowed_mime_types: list[str] = field(default_factory=list)  # empty = allow all
    max_file_size_bytes: int = 50 * 1024 * 1024  # 50 MB
    file_uri_allowlist: list[str] = field(default_factory=list)  # empty = allow all


@dataclass
class TunnelConfig:
    """NAT-traversal relay tunnel configuration.

    Mirrors OpenClaw ``src/tunnel/session.ts`` TunnelSession options.
    """

    relay_url: str = ""  # ws:// or wss:// relay endpoint
    device_id: str = ""  # local device identity (Ed25519 / uuid)
    heartbeat_interval_ms: int = 30_000
    heartbeat_timeout_ms: int = 90_000
    reconnect_delay_ms: int = 1_000
    max_reconnect_delay_ms: int = 30_000
    max_reconnect_attempts: int = 0  # 0 = unlimited
    local_service_port: int = 19100  # inbound A2A port to forward to
    dataset: str = ""
    service_id: str = ""

    @classmethod
    def from_env(cls) -> TunnelConfig:
        return cls(
            relay_url=_env_str("A2A_TUNNEL_RELAY_URL", ""),
            device_id=_env_str("A2A_TUNNEL_DEVICE_ID", ""),
            heartbeat_interval_ms=_env_int("A2A_TUNNEL_HEARTBEAT_MS", 30_000),
            heartbeat_timeout_ms=_env_int("A2A_TUNNEL_HEARTBEAT_TIMEOUT_MS", 90_000),
            reconnect_delay_ms=_env_int("A2A_TUNNEL_RECONNECT_DELAY_MS", 1_000),
            max_reconnect_delay_ms=_env_int("A2A_TUNNEL_MAX_RECONNECT_DELAY_MS", 30_000),
            max_reconnect_attempts=_env_int("A2A_TUNNEL_MAX_RECONNECT_ATTEMPTS", 0),
            local_service_port=_env_int("A2A_SERVER_PORT", 19100),
            dataset=_env_str("A2A_TUNNEL_DATASET", ""),
            service_id=_env_str("A2A_TUNNEL_SERVICE_ID", ""),
        )


@dataclass
class A2AGatewayConfig:
    """Top-level A2A Gateway configuration."""

    enabled: bool = False
    peers: list[PeerConfig] = field(default_factory=list)
    default_agent_id: str = "main"  # routing.defaultAgentId
    resilience: ResilienceConfig = field(default_factory=ResilienceConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    tunnel: TunnelConfig = field(default_factory=TunnelConfig)
    agent_response_timeout_ms: int = 300_000

    def peer_by_name(self, name: str) -> PeerConfig | None:
        for p in self.peers:
            if p.name == name:
                return p
        return None

    def default_peer_auth(self) -> PeerAuthConfig | None:
        """Return the bearer auth to attach to registry-resolved peers.

        Only built when ``security.peer_token`` is set (empty token means
        "no auth", which preserves the previous no-header behavior for
        peers that do not require one). Per-peer ``auth`` is resolved
        separately by callers and always wins over this default.
        """
        token = (self.security.peer_token or "").strip()
        if not token:
            return None
        return PeerAuthConfig(type="bearer", token=token)

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> A2AGatewayConfig:
        """Build from a JiuwenSwarm config dict (``a2a_gateway`` section).

        Falls back to env vars when the config section is absent, so the
        gateway can be driven purely from ``A2A_*`` env vars (matching the
        inbound ``A2A_SERVER_*`` convention in ``app_gateway.py``).
        """
        cfg: dict[str, Any] = config or {}
        enabled = bool(cfg.get("enabled", _env_bool("A2A_GATEWAY_ENABLED", False)))

        peers_raw = cfg.get("peers") or []
        if not isinstance(peers_raw, list):
            peers_raw = []
        peers = [PeerConfig.from_dict(p) for p in peers_raw if isinstance(p, dict)]
        peers = [p for p in peers if p.name and p.agent_card_url]

        tunnel_cfg = cfg.get("tunnel") if isinstance(cfg.get("tunnel"), dict) else {}
        # Tunnel has its own env fallback; merge config-dict over env-derived base.
        tunnel = TunnelConfig.from_env()
        if tunnel_cfg:
            for k in ("relay_url", "device_id", "dataset", "service_id"):
                v = tunnel_cfg.get(k)
                if isinstance(v, str) and v.strip():
                    setattr(tunnel, k, v.strip())
            for k in ("heartbeat_interval_ms", "heartbeat_timeout_ms",
                      "reconnect_delay_ms", "max_reconnect_delay_ms",
                      "max_reconnect_attempts", "local_service_port"):
                v = tunnel_cfg.get(k)
                if isinstance(v, int):
                    setattr(tunnel, k, v)

        sec_raw = cfg.get("security") if isinstance(cfg.get("security"), dict) else {}
        security = SecurityConfig(
            inbound_auth=str(sec_raw.get("inbound_auth") or _env_str("A2A_SERVER_AUTH", "bearer")).strip().lower(),
            token=str(sec_raw.get("token") or os.getenv("A2A_SERVER_TOKEN") or "").strip(),
            peer_token=str(sec_raw.get("peer_token") or os.getenv("A2A_PEER_TOKEN") or "").strip(),
            allowed_mime_types=[str(m) for m in (sec_raw.get("allowed_mime_types") or [])] or [],
            max_file_size_bytes=int(sec_raw.get("max_file_size_bytes") or 50 * 1024 * 1024),
            file_uri_allowlist=[str(u) for u in (sec_raw.get("file_uri_allowlist") or [])] or [],
        )

        routing = cfg.get("routing") if isinstance(cfg.get("routing"), dict) else {}
        return cls(
            enabled=enabled,
            peers=peers,
            default_agent_id=str(routing.get("default_agent_id") or "main").strip() or "main",
            tunnel=tunnel,
            security=security,
            agent_response_timeout_ms=int(cfg.get("agent_response_timeout_ms") or 300_000),
        )
