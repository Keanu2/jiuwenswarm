# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Outbound A2A client — the piece JiuwenSwarm previously lacked.

Ports OpenClaw ``src/client.ts`` ``A2AClient`` to Python, built on the Google
A2A Python SDK (``a2a-sdk`` 1.0.0) instead of ``@a2a-js/sdk``. The SDK's
:class:`a2a.client.ClientFactory` already handles Agent-Card resolution and
transport selection; this module layers on:

* per-peer circuit breaker (receptor desensitization),
* exponential-backoff retry on retryable transport errors,
* adaptive transport ordering by past performance (pathway selection),
* HTTP-only filtering for tunnel-relay peers (gRPC cannot traverse the relay),
* bearer / api-key auth injection via ``ClientCallInterceptor``.

Reused as-is (not ported): inbound A2A server (``a2a_connect.py``) and the
A2X registry runtime (``a2x_registry_runtime.py``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from .circuit_breaker import PeerCircuitBreaker
from .config import A2AGatewayConfig, PeerConfig, RetryConfig
from .retry import with_retry
from .tunnel.protocol import TunnelHttpRequest

logger = logging.getLogger(__name__)

# Lazy imports: a2a-sdk is an optional extra. Importing at module top-level
# would break environments that haven't installed ``jiuwenswarm[a2a]``.
def _import_a2a_client():
    from a2a.client import (  # noqa: PLC0415
        Client,
        ClientCallContext,
        ClientConfig,
        ClientFactory,
    )
    from a2a.client.card_resolver import A2ACardResolver  # noqa: PLC0415
    from a2a.client.interceptors import ClientCallInterceptor  # noqa: PLC0415
    return Client, ClientCallContext, ClientConfig, ClientFactory, A2ACardResolver, ClientCallInterceptor


def _import_a2a_types():
    from a2a.types import (  # noqa: PLC0415
        Message,
        Part,
        Role,
        SendMessageConfiguration,
        SendMessageRequest,
    )
    return Message, Part, Role, SendMessageConfiguration, SendMessageRequest


@dataclass
class OutboundSendResult:
    """Result of an outbound A2A send, mirroring OpenClaw ``OutboundSendResult``."""

    ok: bool
    status_code: int
    text: str = ""
    response: dict[str, Any] = field(default_factory=dict)


@dataclass
class _TransportStats:
    """Per-transport success/latency stats for adaptive ordering."""

    success: int = 0
    failure: int = 0
    total_latency_ms: float = 0.0

    @property
    def count(self) -> int:
        return self.success + self.failure

    @property
    def success_rate(self) -> float:
        return self.success / self.count if self.count else 0.0


def _card_path_or_default(path: str | None) -> str | None:
    """Return the agent-card path to pass to ``A2ACardResolver``/``create_from_url``.

    A peer's ``agent_card_url`` from the registry is often the JSON-RPC endpoint
    (e.g. ``http://127.0.0.1:18800/a2a/jsonrpc``), not the card URL. Passing
    ``/a2a/jsonrpc`` as ``relative_card_path`` makes the resolver fetch the card
    from there → 404. When the path isn't recognisably a card endpoint, return
    ``None`` so the resolver uses the default ``/.well-known/agent-card.json``.
    Only pass the path through when it actually looks like a card location.
    """
    if not path:
        return None
    lowered = path.lower()
    if "agent-card" in lowered or ".well-known" in lowered:
        return path
    return None


class _TunnelHttpxTransport(httpx.AsyncBaseTransport):
    """httpx transport that routes every request through the relay tunnel.

    The A2A SDK issues plain HTTP requests to a peer's ``base_url`` (Agent-Card
    discovery at ``/.well-known/agent-card.json`` + JSON-RPC / REST calls). For
    a peer behind NAT that base_url points at the peer's *local* address
    (e.g. ``http://127.0.0.1:18800``) which this machine cannot reach directly.
    This transport rewrites each such request into a
    :class:`~jiuwenswarm.agents.harness.common.a2a_gateway.tunnel.protocol.TunnelHttpRequest`
    and forwards it through the relay to the peer's ``target_device`` id; the
    peer's local inbound A2A server handles it. gRPC is filtered out upstream
    (``_ordered_bindings``) since the relay only carries HTTP.
    """

    def __init__(self, tunnel: Any, target_device: str) -> None:
        self._tunnel = tunnel
        self._target_device = target_device
        # Build the fetch closure lazily so the import stays local.
        from jiuwenswarm.agents.harness.common.a2a_gateway.tunnel import (  # noqa: PLC0415
            create_tunnel_fetch,
        )
        self._fetch = create_tunnel_fetch(tunnel, target_device)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Collapse the request body to a string (tunnel protocol carries str).
        body_bytes = b""
        if request.content is not None:
            body_bytes = request.content if isinstance(request.content, bytes) else bytes(request.content)
        else:
            try:
                body_bytes = await request.aread()
            except Exception:
                body_bytes = b""
        body_text = body_bytes.decode("utf-8", errors="replace") if body_bytes else ""

        tunnel_request = TunnelHttpRequest(
            method=request.method,
            path=str(request.url.path) or "/",
            headers={k: v for k, v in request.headers.items()},
            body=body_text,
        )
        try:
            tunnel_response = await self._fetch(tunnel_request)
        except Exception as exc:
            logger.warning(
                "[a2a.outbound.tunnel] forward failed target_device=%s path=%s: %s",
                self._target_device, tunnel_request.path, exc,
            )
            return httpx.Response(
                status_code=502,
                headers={"content-type": "application/json"},
                content=f'{{"error":"tunnel forward failed: {exc}"}}'.encode("utf-8"),
                request=request,
            )

        # Observe the peer's response through the tunnel BEFORE the SDK turns
        # an error status into an exception (which discards the body). This is
        # what tells us whether the peer's A2A bridge genuinely returned the
        # 5xx, or the peer's inbound-forward fallback fabricated it.
        resp_status = int(tunnel_response.status or 200)
        resp_body = tunnel_response.body or ""
        if resp_status >= 400:
            body_snippet = resp_body[:500] if resp_body else "<empty>"
            logger.info(
                "[a2a.outbound.tunnel] peer forwarded HTTP %s target_device=%s "
                "method=%s path=%s body_head=%r",
                resp_status, self._target_device, request.method,
                tunnel_request.path, body_snippet,
            )

        resp_headers = dict(tunnel_response.headers or {})
        resp_bytes = resp_body.encode("utf-8")
        return httpx.Response(
            status_code=resp_status,
            headers=resp_headers,
            content=resp_bytes,
            request=request,
        )

    async def aclose(self) -> None:
        # The tunnel session is process-scoped and shared across peers; the
        # transport only borrows it, so it must not close the underlying ws.
        return None


class A2AOutboundClient:
    """Outbound A2A client with circuit breaker, retry, and transport fallback.

    Hold one instance per process (created at gateway startup, injected into
    the swarm build context). It is safe to call :meth:`send_message`
    concurrently from multiple agent tools.
    """

    def __init__(self, config: A2AGatewayConfig) -> None:
        self._config = config
        self._breaker = PeerCircuitBreaker(config.resilience.circuit_breaker)
        self._stats: dict[str, dict[str, _TransportStats]] = {}
        # One httpx client per peer (auth headers differ); lazily built.
        self._httpx_clients: dict[str, httpx.AsyncClient] = {}
        self._tunnel: Any = None  # TunnelSession, injected via set_tunnel
        self._closed = False

    def set_tunnel(self, tunnel: Any | None) -> None:
        """Attach / replace the relay tunnel session (called after tunnel start)."""
        self._tunnel = tunnel

    def _httpx_client_for(self, peer: PeerConfig) -> httpx.AsyncClient:
        client = self._httpx_clients.get(peer.name)
        if client is None:
            headers: dict[str, str] = {}
            if peer.auth is not None:
                h, v = peer.auth.header()
                headers[h] = v
            # Tunnel peers are reached via the relay, not a direct TCP dial:
            # route every HTTP request (card discovery + JSON-RPC/REST) through
            # the tunnel to the peer's device id. The transport owns the
            # connection; httpx just serializes requests over it.
            if peer.tunnel_device_id and self._tunnel is not None:
                transport = _TunnelHttpxTransport(self._tunnel, peer.tunnel_device_id)
                client = httpx.AsyncClient(headers=headers, transport=transport,
                                            timeout=httpx.Timeout(30.0))
            else:
                client = httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30.0))
            self._httpx_clients[peer.name] = client
        return client

    def _peer_stats(self, peer_name: str) -> dict[str, _TransportStats]:
        s = self._stats.get(peer_name)
        if s is None:
            s = {}
            self._stats[peer_name] = s
        return s

    def _ordered_bindings(self, peer: PeerConfig) -> list[str]:
        """Return ordered transport-binding labels for *peer*.

        Priority: JSONRPC > HTTP_JSON > GRPC, refined by adaptive stats.
        Tunnel peers drop GRPC entirely (the relay only carries HTTP).
        """
        from a2a.utils.constants import TransportProtocol  # noqa: PLC0415

        base = [TransportProtocol.JSONRPC, TransportProtocol.HTTP_JSON, TransportProtocol.GRPC]
        if peer.tunnel_device_id and self._tunnel is not None:
            base = [b for b in base if b != TransportProtocol.GRPC]

        stats = self._peer_stats(peer.name)
        has_stats = any(stats.get(b) and stats[b].count > 0 for b in base)
        if not has_stats:
            return base
        # Adaptive: sort by success rate desc, then by preference order for ties.
        return sorted(base, key=lambda b: -(stats[b].success_rate if b in stats and stats[b].count else 0.0))

    def _record_transport(self, peer_name: str, binding: str, ok: bool, latency_ms: float) -> None:
        stats = self._peer_stats(peer_name)
        s = stats.get(binding)
        if s is None:
            s = _TransportStats()
            stats[binding] = s
        if ok:
            s.success += 1
        else:
            s.failure += 1
        s.total_latency_ms += latency_ms

    async def discover_agent_card(self, peer: PeerConfig) -> dict[str, Any]:
        """Fetch a peer's Agent Card as a JSON dict (for health probes / display)."""
        Client, ClientCallContext, ClientConfig, ClientFactory, A2ACardResolver, _ = _import_a2a_client()
        httpx_client = self._httpx_client_for(peer)
        parsed = urlparse(peer.agent_card_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        resolver = A2ACardResolver(httpx_client, base_url)
        card = await resolver.get_agent_card(relative_card_path=_card_path_or_default(parsed.path))
        return _agent_card_to_dict(card)

    async def send_message(
        self,
        peer: PeerConfig,
        *,
        text: str = "",
        parts: list[dict[str, Any]] | None = None,
        agent_id: str = "",
        blocking: bool = True,
        timeout_ms: int | None = None,
    ) -> OutboundSendResult:
        """Send a message to *peer* and return the aggregated text response.

        The call is guarded by the circuit breaker and wrapped in retry.
        Transport fallback is delegated to the SDK's ``ClientFactory`` which
        picks the best interface; we additionally try an alternate binding
        order on retryable failure.
        """
        retry_cfg: RetryConfig = self._config.resilience.retry

        # Circuit breaker admission.
        if not await self._breaker.acquire(peer.name):
            return OutboundSendResult(
                ok=False, status_code=503,
                response={"error": f"Circuit open: peer {peer.name!r} unavailable"},
            )

        async def _do_send() -> OutboundSendResult:
            return await self._do_send_message(peer, text=text, parts=parts,
                                               agent_id=agent_id, blocking=blocking,
                                               timeout_ms=timeout_ms)

        try:
            result = await with_retry(_do_send, retry_cfg, peer_name=peer.name,
                                      log=True)
            await self._breaker.release(peer.name, success=result.ok)
            return result
        except Exception as exc:  # noqa: BLE001
            await self._breaker.release(peer.name, success=False)
            logger.warning("[a2a.outbound] peer=%s all attempts failed: %s", peer.name, exc)
            return OutboundSendResult(ok=False, status_code=500,
                                      response={"error": str(exc)})
        finally:
            # _do_send_message already recorded per-transport stats.
            pass

    async def _do_send_message(
        self,
        peer: PeerConfig,
        *,
        text: str,
        parts: list[dict[str, Any]] | None,
        agent_id: str,
        blocking: bool,
        timeout_ms: int | None,
    ) -> OutboundSendResult:
        Client, ClientCallContext, ClientConfig, ClientFactory, _, _Auth = _import_a2a_client()
        Message, Part, Role, SendMessageConfiguration, SendMessageRequest = _import_a2a_types()
        from a2a.utils.constants import TransportProtocol  # noqa: PLC0415
        import time as _time  # noqa: PLC0415

        bindings = self._ordered_bindings(peer)
        last_error: str = ""

        for binding in bindings:
            started = _time.monotonic()
            try:
                httpx_client = self._httpx_client_for(peer)
                client_config = ClientConfig(
                    streaming=False,
                    polling=not blocking,
                    httpx_client=httpx_client,
                    supported_protocol_bindings=[binding],
                )
                factory = ClientFactory(client_config)

                # Resolve card from the peer's base URL, then create a client
                # pinned to the chosen transport binding.
                parsed = urlparse(peer.agent_card_url)
                base_url = f"{parsed.scheme}://{parsed.netloc}"
                client = await factory.create_from_url(base_url, relative_card_path=_card_path_or_default(parsed.path))

                request = _build_send_request(
                    Message, Part, Role, SendMessageConfiguration, SendMessageRequest,
                    text=text, parts=parts, agent_id=agent_id, blocking=blocking,
                )
                ctx = ClientCallContext(timeout=(timeout_ms or self._config.agent_response_timeout_ms) / 1000.0)

                collected_text: list[str] = []
                async for event in client.send_message(request, context=ctx):
                    txt = _extract_text(event)
                    if txt:
                        collected_text.append(txt)

                latency_ms = (_time.monotonic() - started) * 1000
                self._record_transport(peer.name, binding, ok=True, latency_ms=latency_ms)
                return OutboundSendResult(
                    ok=True, status_code=200,
                    text="\n".join(t for t in collected_text if t),
                    response={"transport": binding, "text": collected_text},
                )
            except Exception as exc:  # noqa: BLE001
                latency_ms = (_time.monotonic() - started) * 1000
                self._record_transport(peer.name, binding, ok=False, latency_ms=latency_ms)
                from .retry import _is_retryable_exception  # noqa: PLC0415
                last_error = f"{binding}: {exc}"
                logger.info("[a2a.outbound] peer=%s binding=%s failed: %s", peer.name, binding, exc)
                if not _is_retryable_exception(exc):
                    # Non-retryable (auth / protocol): stop immediately.
                    return OutboundSendResult(ok=False, status_code=500,
                                              response={"error": last_error})
                # else: try next binding.

        return OutboundSendResult(
            ok=False, status_code=500,
            response={"error": f"All transports failed for peer {peer.name!r}: {last_error}"},
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for client in self._httpx_clients.values():
            await client.aclose()
        self._httpx_clients.clear()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _build_send_request(Message, Part, Role, SendMessageConfiguration,
                         SendMessageRequest, *, text, parts, agent_id, blocking=True):
    """Construct a SendMessageRequest with one or more Parts.

    ``parts`` (if provided) is a list of dicts with keys matching Part fields
    (``text``, ``raw`` bytes, ``url``, ``filename``, ``media_type``). When
    ``parts`` is empty, a single text Part is built from ``text``.

    ``blocking`` (default True) maps to ``return_immediately=False`` so the
    server waits for the final response; non-blocking returns a queued task.
    """
    import uuid as _uuid  # noqa: PLC0415

    proto_parts: list[Part] = []
    if parts:
        for p in parts:
            part = Part()
            if "text" in p and p["text"]:
                part.text = str(p["text"])
            elif "raw" in p and p["raw"]:
                part.raw = p["raw"] if isinstance(p["raw"], (bytes, bytearray)) else bytes(p["raw"])
                if p.get("filename"):
                    part.filename = str(p["filename"])
                if p.get("media_type"):
                    part.media_type = str(p["media_type"])
            elif "url" in p and p["url"]:
                part.url = str(p["url"])
                if p.get("filename"):
                    part.filename = str(p["filename"])
                if p.get("media_type"):
                    part.media_type = str(p["media_type"])
            proto_parts.append(part)
    else:
        part = Part()
        part.text = text or ""
        proto_parts.append(part)

    msg = Message()
    msg.message_id = str(_uuid.uuid4())
    msg.role = Role.ROLE_USER
    msg.parts.extend(proto_parts)
    if agent_id:
        # Non-standard routing hint (mirrors OpenClaw ``message.agentId``).
        # Stored in metadata so servers that don't recognize it ignore it.
        msg.metadata["agentId"] = agent_id

    req = SendMessageRequest()
    req.message.CopyFrom(msg)
    # ``return_immediately=False`` = blocking (wait for the final response);
    # ``True`` = non-blocking (server returns a queued task, client polls).
    cfg = SendMessageConfiguration()
    cfg.return_immediately = not blocking
    req.configuration.CopyFrom(cfg)
    return req


def _extract_text(stream_response) -> str:
    """Pull text out of a StreamResponse (message.parts[].text)."""
    try:
        if stream_response.HasField("message"):
            chunks: list[str] = []
            for part in stream_response.message.parts:
                if part.text:
                    chunks.append(part.text)
            return "\n".join(chunks)
        if stream_response.HasField("status_update"):
            # TaskStatusUpdateEvent may carry a final message with parts.
            su = stream_response.status_update
            if su.HasField("message") if hasattr(su, "HasField") else False:
                for part in su.message.parts:
                    if part.text:
                        return part.text
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _agent_card_to_dict(card) -> dict[str, Any]:
    """Best-effort flatten of an AgentCard protobuf to a JSON dict."""
    try:
        from google.protobuf.json_format import MessageToDict  # noqa: PLC0415
        return MessageToDict(card, preserving_proto_field_name=True)
    except Exception:  # noqa: BLE001
        return {"name": getattr(card, "name", ""), "url": getattr(card, "url", "")}
