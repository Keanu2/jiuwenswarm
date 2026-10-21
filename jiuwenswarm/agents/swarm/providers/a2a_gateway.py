# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Swarm provider for the outbound A2A file tools.

Declares the ``swarm.a2a_send`` tool element, gated by the
``a2a_gateway.enabled`` config switch (and the presence of a started
outbound client). The toolkit returns the ``a2a_send_file`` and
``a2a_send_local_file`` tools for runner registration.

Mirrors ``runtime_tools.py`` ``build_send_file_tools``: the provider is a
thin adapter that builds the toolkit and returns ``get_tools()``; it never
touches ``Runner.resource_mgr`` directly (imperative wiring is the
customizer's job, not a provider's).
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.agent_teams.harness.manifest import (
    ConstructionInput,
    context_field,
    ElementKind,
    harness_element,
    param_field,
)

from jiuwenswarm.agents.harness.common.a2a_gateway.tools import A2ASendFileToolkit
from jiuwenswarm.agents.swarm.context import SwarmBuildContext

logger = logging.getLogger(__name__)

# Provider name constant; namespaced under the shared "swarm." prefix.
A2A_SEND = "swarm.a2a_send"


class A2ASendInput(ConstructionInput):
    """Construction inputs for the outbound A2A file toolkit."""

    a2a_gateway_config: dict[str, Any] = param_field(
        default_factory=dict,
        description="The a2a_gateway config section (enabled / peers / ...).",
    )
    request_id: str | None = context_field(
        attr="request_id",
        description="Originating request id (required; skipped when absent).",
    )
    channel_id: str | None = context_field(
        attr="channel_id",
        description="Raw channel id (required; skipped when absent).",
    )


@harness_element(
    kind=ElementKind.TOOL,
    name=A2A_SEND,
    description="Outbound A2A file tools (a2a_send_file / a2a_send_local_file), "
    "gated by a2a_gateway.enabled and the presence of a started outbound client.",
    input_model=A2ASendInput,
)
def build_a2a_send_tools(params: dict[str, Any], ctx: SwarmBuildContext) -> list[Any]:
    """Build the outbound A2A file tools from the config source.

    Args:
        params: Provider params (unused beyond the gate).
        ctx: The per-member build context.

    Returns:
        The a2a_send_* tool instances, or an empty list when the capability
        is skipped (disabled / no outbound client) or construction fails.
    """
    inp = A2ASendInput.resolve(params, ctx)
    gw_cfg = inp.a2a_gateway_config if isinstance(inp.a2a_gateway_config, dict) else {}
    if not gw_cfg.get("enabled", False):
        logger.info("[swarm.a2a_send] skipped: a2a_gateway.enabled is false")
        return []

    if not inp.request_id or not inp.channel_id:
        logger.info("[swarm.a2a_send] skipped: missing request_id or channel_id")
        return []

    # Late import to avoid a circular dependency (runtime -> tools -> runtime).
    from jiuwenswarm.agents.harness.common.a2a_gateway.runtime import get_outbound_client

    client = get_outbound_client()
    if client is None:
        logger.info("[swarm.a2a_send] skipped: outbound client not started")
        return []

    try:
        toolkit = A2ASendFileToolkit()
        tools = list(toolkit.get_tools())
        logger.info(
            "[swarm.a2a_send] built %d a2a file tools for channel=%s",
            len(tools), inp.channel_id,
        )
        return tools
    except Exception as exc:
        logger.warning("[swarm.a2a_send] tool construction failed: %s", exc)
        return []


__all__ = ["A2A_SEND", "build_a2a_send_tools"]
