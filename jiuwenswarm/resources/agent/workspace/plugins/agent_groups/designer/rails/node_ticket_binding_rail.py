# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Ticket-binding rail for the designer AgentGroup's manifest workers.

The colleague's manifest sample (§3.6 ShotExecutionGuardRail) parses the
shot ticket from the task query up front so the TOOLS never depend on the
model relaying a ticket argument. This rail does the same for the node
ticket: every model call's input text is scanned for the
``node_ticket: design:{graph}:{run}:{node}`` line, and the parsed ticket is
written to the async-flow-scoped DESIGN_NODE_TICKET slot (installed-package
ContextVar — the one stable reference shared by this rail and the worker
tools regardless of how the extension loader materialized either module).

State is flow-scoped, so parallel workers in one process each carry their
own ticket — no bleed across a parallel wave.
"""

from __future__ import annotations

import tempfile
from typing import Any


class NodeTicketBindingRail:
    """Bind the task prompt's node ticket onto the worker tool context."""

    def __new__(cls, **params: Any) -> Any:
        from openjiuwen.core.single_agent.rail.base import AgentRail

        class _Rail(AgentRail):
            priority = 400

            async def before_model_call(self, ctx: Any) -> None:
                try:
                    inputs = getattr(ctx, "inputs", None)
                    if inputs is None:
                        return
                    ticket = self._scan_ticket(inputs)
                    if ticket is None:
                        return
                    from jiuwenswarm.server.runtime.design.context import (
                        DESIGN_NODE_TICKET,
                    )

                    DESIGN_NODE_TICKET.set(ticket)
                except Exception:  # noqa: BLE001 — never break the model call
                    pass

            @staticmethod
            def _scan_ticket(inputs: Any) -> dict[str, str] | None:
                """Scan EVERY human message for the ticket line.

                Runtime rails prepend their own user-shaped reminder blocks
                before the task message, so the ticket may sit in any user
                message — the first user message is NOT reliably the task.
                """
                messages = getattr(inputs, "messages", None) or []
                for message in messages:
                    text = ""
                    if isinstance(message, dict):
                        if str(message.get("role") or "") in ("user", "human"):
                            content = message.get("content")
                            text = content if isinstance(content, str) else ""
                    else:
                        role = str(getattr(message, "role", "") or "")
                        if role not in ("user", "human"):
                            continue
                        content = getattr(message, "content", None)
                        if isinstance(content, str):
                            text = content
                        elif isinstance(content, list):
                            text = "".join(
                                str(block.get("text") or "")
                                for block in content
                                if isinstance(block, dict)
                            )
                    if not text:
                        continue
                    ticket = _parse_ticket(text)
                    if ticket is not None:
                        return ticket
                return None

        def _parse_ticket(text: str) -> dict[str, str] | None:
            import re

            match = re.search(
                r"node_ticket:\s*design:(?P<graph_id>[^:\s]+):"
                r"(?P<run_id>[^:\s]+):(?P<node_id>[^:\s]+)",
                text,
            )
            if match is None:
                return None
            return {
                "graph_id": match.group("graph_id"),
                "run_id": match.group("run_id"),
                "node_id": match.group("node_id"),
            }

        return _Rail()
