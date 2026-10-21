# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process-level holders for the A2A Gateway outbound client and tunnel.

The outbound A2A client and tunnel session are long-lived singletons created
at gateway startup (``app_gateway.py``) and consumed per-request by the
agent tools (``A2ASendFileToolkit``) and the swarm build context. Holding
them in a module-level accessor avoids threading a parameter through the
entire swarm provider chain, mirroring how the inbound A2A channel is held
by ``channel_manager``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .outbound_client import A2AOutboundClient
    from .tunnel.session import TunnelSession

logger = logging.getLogger(__name__)

_outbound_client: A2AOutboundClient | None = None
_tunnel: TunnelSession | None = None


def set_outbound_client(client: A2AOutboundClient | None) -> None:
    global _outbound_client
    _outbound_client = client
    logger.info("[a2a.runtime] outbound client %s",
                "registered" if client else "cleared")


def get_outbound_client() -> A2AOutboundClient | None:
    """Return the process-wide outbound A2A client, or ``None`` if not started.

    Tools call this lazily; when ``None`` they report that A2A outbound is
    disabled rather than raising.
    """
    return _outbound_client


def set_tunnel(tunnel: TunnelSession | None) -> None:
    global _tunnel
    _tunnel = tunnel
    if _tunnel is not None and _outbound_client is not None:
        _outbound_client.set_tunnel(_tunnel)
    logger.info("[a2a.runtime] tunnel %s", "registered" if tunnel else "cleared")


def get_tunnel() -> TunnelSession | None:
    return _tunnel


async def init_a2a_gateway(
    full_cfg: dict | None,
    *,
    device_id_override: str | None = None,
    tag: str = "App",
) -> None:
    """Initialize the A2A outbound client + optional relay tunnel in this process.

    Mirrors the block historically wired in ``app_gateway.py``, factored out so
    every process that hosts agent tools (notably the **AgentServer**, whose
    conversational agent runs ``a2a_send_message``) can bring up its own
    outbound half. The Gateway still owns the 19100 inbound A2A server, but
    the outbound client + tunnel must live in the same process as the tool
    that calls them — the module-level holders are per-process, not shared.

    ``device_id_override`` lets a second process register a *distinct* device
    id so the relay doesn't clobber the first process's receive registration
    (two sessions on the same device_id → relay keeps only the newest). The
    AgentServer, which only *sends* (outbound), passes e.g.
    ``"jws-teammate-outbound"``; the Gateway keeps its configured id.

    Safe to call when A2A is disabled (``a2a_gateway_cfg.enabled`` is False):
    logs and returns. Best-effort: logs and returns on failure rather than
    aborting the host process.
    """
    import asyncio  # noqa: PLC0415

    from .config import A2AGatewayConfig  # noqa: PLC0415
    from .outbound_client import A2AOutboundClient  # noqa: PLC0415
    from .tunnel.protocol import TunnelRegisterExtras  # noqa: PLC0415
    from .tunnel.session import TunnelSession, TunnelSessionOptions  # noqa: PLC0415

    a2a_gateway_cfg = A2AGatewayConfig.from_config(
        full_cfg.get("a2a_gateway") if isinstance(full_cfg, dict) else None,
    )
    if not a2a_gateway_cfg.enabled:
        logger.info("[%s] A2A gateway disabled (enabled=False), skipping outbound init", tag)
        return

    try:
        outbound_client = A2AOutboundClient(a2a_gateway_cfg)
        set_outbound_client(outbound_client)

        # Optional relay tunnel for NAT traversal.
        if a2a_gateway_cfg.tunnel.relay_url and a2a_gateway_cfg.tunnel.device_id:
            device_id = (device_id_override or a2a_gateway_cfg.tunnel.device_id).strip()
            extras = TunnelRegisterExtras(
                dataset=a2a_gateway_cfg.tunnel.dataset,
                service_id=a2a_gateway_cfg.tunnel.service_id,
            )
            tunnel_opts = TunnelSessionOptions(
                relay_url=a2a_gateway_cfg.tunnel.relay_url,
                device_id=device_id,
                local_service_port=a2a_gateway_cfg.tunnel.local_service_port,
                heartbeat_interval_ms=a2a_gateway_cfg.tunnel.heartbeat_interval_ms,
                heartbeat_timeout_ms=a2a_gateway_cfg.tunnel.heartbeat_timeout_ms,
                reconnect_interval_ms=a2a_gateway_cfg.tunnel.reconnect_delay_ms,
                max_reconnect_interval_ms=a2a_gateway_cfg.tunnel.max_reconnect_delay_ms,
                max_reconnect_attempts=a2a_gateway_cfg.tunnel.max_reconnect_attempts,
                register_extras=extras,
            )
            tunnel = TunnelSession(tunnel_opts)

            def _on_tunnel_task_done(task: asyncio.Task) -> None:
                try:
                    task.result()
                except asyncio.CancelledError:
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.error("[%s] A2A tunnel failed: %s", tag, exc)

            tunnel_task = asyncio.create_task(tunnel.start(), name="a2a-tunnel")
            tunnel_task.add_done_callback(_on_tunnel_task_done)
            set_tunnel(tunnel)
            logger.info(
                "[%s] A2A tunnel started: device=%s relay=%s local=:%d",
                tag,
                device_id,
                a2a_gateway_cfg.tunnel.relay_url,
                a2a_gateway_cfg.tunnel.local_service_port,
            )
        else:
            logger.info("[%s] A2A gateway outbound enabled (no tunnel relay configured)", tag)
    except Exception as exc:  # noqa: BLE001
        logger.error("[%s] A2A gateway outbound failed to start: %s", tag, exc)
        set_outbound_client(None)

