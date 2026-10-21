# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Turn progress tracking for @-mentions in group chat.

While the bridge runs an agent turn, the room sees nothing until the final
reply lands — up to ``turn_timeout`` seconds later. This module turns the
turn's stream events into ``team.group.progress`` push events so the projects
panel can show a live "what is the agent doing" card.

Scope the UI can rely on: the events are transient and instance-local. They
travel over the AgentServer's server-push channel to the browsers connected
to *this* gateway and are never written to the relay's group log, so other
members' instances (and the persisted transcript) are untouched.

``TurnProgressTracker`` is pure state plus payload builders — no IO — so its
event mapping and retention caps are unit-testable without a server. The one
impure piece is :func:`emit_progress`, the push the bridge makes per event.
"""

from __future__ import annotations

import json
import time
from typing import Any, Mapping

PROGRESS_EVENT_TYPE = "team.group.progress"

# Retention caps. The card is a live indicator, not a transcript; the frontend
# mirrors these numbers (applyProgressEvent) so both sides stay bounded no
# matter how long the turn runs.
MAX_STEPS = 100
MAX_DETAIL_CHARS = 200
MAX_REASONING_CHARS = 16 * 1024

PHASE_STARTED = "started"
PHASE_TOOL_CALL = "tool_call"
PHASE_TOOL_RESULT = "tool_result"
PHASE_REASONING = "reasoning"
PHASE_ANSWERING = "answering"
PHASE_BUSY = "busy"
PHASE_DONE = "done"
PHASE_TIMEOUT = "timeout"
PHASE_ERROR = "error"

TERMINAL_PHASES = (PHASE_DONE, PHASE_TIMEOUT, PHASE_ERROR)


def detail_text(value: Any, limit: int = MAX_DETAIL_CHARS) -> str:
    """Render one step's detail line, capped.

    Tool arguments and result summaries arrive as strings or as JSON objects;
    either way what the card needs is one short line, so the value is
    compacted (whitespace collapsed, then clipped).
    """
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    return " ".join(text.split())[:limit]


class TurnProgressTracker:
    """One agent turn's progress: steps, reasoning text, and push payloads.

    The bridge feeds it agent stream payloads (:meth:`note_event`) and emits
    whatever it returns; the frontend rebuilds the same state from those
    events (or from :meth:`snapshot` after a page refresh).
    """

    def __init__(
        self,
        *,
        group_id: int,
        request_id: str,
        trigger_seq: Any,
        sender_id: str,
        clock: Any = time.time,
    ) -> None:
        self.group_id = int(group_id)
        self.request_id = request_id
        self.trigger_seq = trigger_seq
        self.sender_id = sender_id
        self.state = "running"
        self.started_at = float(clock())
        self.steps: list[dict[str, Any]] = []
        self.reasoning = ""
        self._clock = clock
        self._step_seq = 0
        self._answering_noted = False

    # -- payload assembly ---------------------------------------------------

    def _header(self) -> dict[str, Any]:
        return {
            "event_type": PROGRESS_EVENT_TYPE,
            "group_id": self.group_id,
            "request_id": self.request_id,
            "trigger_seq": self.trigger_seq,
            "sender_id": self.sender_id,
            "state": self.state,
            "started_at": self.started_at,
        }

    def _next_step(self, phase: str, **fields: Any) -> dict[str, Any]:
        self._step_seq += 1
        step: dict[str, Any] = {
            "seq": self._step_seq,
            "phase": phase,
            "ts": round(float(self._clock()), 3),
        }
        step.update({key: value for key, value in fields.items() if value not in (None, "")})
        return step

    def _record(self, step: dict[str, Any]) -> dict[str, Any]:
        """Keep the step (bounded) and wrap it in an incremental push payload."""
        self.steps.append(step)
        if len(self.steps) > MAX_STEPS:
            del self.steps[: len(self.steps) - MAX_STEPS]
        return {**self._header(), "step": step}

    # -- lifecycle steps ----------------------------------------------------

    def start(self) -> dict[str, Any]:
        """The turn is beginning: posted before the agent is invoked."""
        return self._record(self._next_step(PHASE_STARTED))

    def note_busy(self, *, trigger_seq: Any) -> dict[str, Any]:
        """A mention arrived while this turn runs and was dropped.

        The bridge ignores overlapping mentions by design; without a step the
        room would see the message vanish with no explanation.
        """
        return self._record(self._next_step(PHASE_BUSY, detail=f"seq={trigger_seq}"))

    def finish(self, phase: str) -> dict[str, Any]:
        """The turn ended: ``done`` (answer or notice posted), ``timeout`` or ``error``."""
        if phase not in TERMINAL_PHASES:
            phase = PHASE_DONE
        self.state = phase
        return {**self._header(), "step": self._next_step(phase)}

    def snapshot(self) -> dict[str, Any]:
        """Full state for ``team.group.progress.get`` (panel refresh mid-turn)."""
        return {
            **self._header(),
            "steps": [dict(step) for step in self.steps],
            "reasoning": self.reasoning,
        }

    # -- stream events ------------------------------------------------------

    def note_event(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        """Fold one agent stream payload into progress state.

        Returns the push payload to emit, or ``None`` when the event maps to
        no progress step — which is most of them (deltas after the first,
        usage, todo, keepalive…). Kept as one method so the mapping lives in
        exactly one place.
        """
        event_type = str(payload.get("event_type") or "")
        if event_type == "chat.tool_call":
            return self._tool_call(payload)
        if event_type == "chat.tool_result":
            return self._tool_result(payload)
        if event_type == "chat.reasoning":
            return self._reasoning(payload)
        if event_type == "chat.delta" and not self._answering_noted:
            # Once per turn is enough: the card needs "the answer is being
            # written", not a per-token feed.
            self._answering_noted = True
            return self._record(self._next_step(PHASE_ANSWERING))
        return None

    def _tool_call(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        info = payload.get("tool_call")
        info = info if isinstance(info, Mapping) else {}
        tool = str(info.get("name") or info.get("tool_name") or "").strip()
        return self._record(
            self._next_step(PHASE_TOOL_CALL, tool=tool, detail=detail_text(info.get("arguments")))
        )

    def _tool_result(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        tool = str(payload.get("tool_name") or payload.get("tool") or "").strip()
        status = str(payload.get("status") or "").strip()
        if not status:
            if payload.get("is_error") or payload.get("success") is False:
                status = "error"
            else:
                status = "completed"
        summary = payload.get("summary")
        if summary in (None, ""):
            summary = payload.get("result")
        return self._record(
            self._next_step(PHASE_TOOL_RESULT, tool=tool, status=status, detail=detail_text(summary))
        )

    def _reasoning(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        content = payload.get("content")
        if not isinstance(content, str) or not content:
            return None
        # Reasoning accumulates outside ``steps``: chunks are high-volume and
        # the card shows them as one growing text block, not as N rows.
        merged = self.reasoning + content
        self.reasoning = merged[-MAX_REASONING_CHARS:]
        return {**self._header(), "step": self._next_step(PHASE_REASONING, content=content)}


async def emit_progress(payload: Mapping[str, Any]) -> None:
    """Push one progress event through the AgentServer's server-push channel.

    Same wire shape as the hub's ``team.group.*`` pushes (see
    ``subscription.set_group_frame_callback``): no session_id, because the
    projects panel is global and the web channel broadcasts this event type to
    every connection for the frontend to filter by group_id.
    """
    from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport

    step = payload.get("step") if isinstance(payload, Mapping) else None
    suffix = step.get("seq") if isinstance(step, Mapping) else None
    if suffix is None:
        suffix = payload.get("state") if isinstance(payload, Mapping) else "x"
    await WebSocketGatewayPushTransport().send_push(
        {
            "request_id": f"group-progress-{payload.get('group_id')}-{suffix}",
            "channel_id": "web",
            "payload": dict(payload),
        }
    )


__all__ = [
    "MAX_DETAIL_CHARS",
    "MAX_REASONING_CHARS",
    "MAX_STEPS",
    "PHASE_ANSWERING",
    "PHASE_BUSY",
    "PHASE_DONE",
    "PHASE_ERROR",
    "PHASE_REASONING",
    "PHASE_STARTED",
    "PHASE_TIMEOUT",
    "PHASE_TOOL_CALL",
    "PHASE_TOOL_RESULT",
    "PROGRESS_EVENT_TYPE",
    "TERMINAL_PHASES",
    "TurnProgressTracker",
    "detail_text",
    "emit_progress",
]
