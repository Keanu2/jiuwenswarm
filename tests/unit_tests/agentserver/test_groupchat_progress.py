# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the pure progress tracker: event mapping, caps, snapshots.

``TurnProgressTracker`` is deliberately IO-free — these tests pin the mapping
from agent stream events to progress steps and the retention caps that keep a
long turn bounded, without a bridge, server, or socket.
"""

from __future__ import annotations

from jiuwenswarm.agents.harness.team.groupchat.progress import (
    MAX_DETAIL_CHARS,
    MAX_REASONING_CHARS,
    MAX_STEPS,
    PHASE_BUSY,
    PHASE_DONE,
    PHASE_ERROR,
    PHASE_TIMEOUT,
    PROGRESS_EVENT_TYPE,
    TurnProgressTracker,
    detail_text,
)


def _tracker() -> TurnProgressTracker:
    clock = iter(i * 1.0 for i in range(1, 10_000))
    return TurnProgressTracker(
        group_id=3,
        request_id="groupchat-3-9",
        trigger_seq=9,
        sender_id="u_agent",
        clock=lambda: next(clock),
    )


# ── event → step mapping ─────────────────────────────────────────────────────


def test_note_event_maps_each_chat_event_once() -> None:
    tracker = _tracker()
    assert tracker.note_event({"event_type": "chat.tool_call", "tool_call": {"name": "web_search", "arguments": {"query": "q"}}})["step"]["phase"] == "tool_call"
    assert tracker.note_event({"event_type": "chat.tool_result", "tool_name": "web_search", "status": "completed", "summary": "ok"})["step"]["phase"] == "tool_result"
    assert tracker.note_event({"event_type": "chat.reasoning", "content": "hm"})["step"]["phase"] == "reasoning"
    assert tracker.note_event({"event_type": "chat.delta", "content": "a"})["step"]["phase"] == "answering"
    # The second delta, usage and unknown events map to nothing.
    assert tracker.note_event({"event_type": "chat.delta", "content": "b"}) is None
    assert tracker.note_event({"event_type": "chat.usage_metadata"}) is None
    assert tracker.note_event({"event_type": "chat.symphony_status"}) is None
    assert tracker.note_event({}) is None


def test_tool_call_step_reads_name_and_arguments() -> None:
    tracker = _tracker()
    step = tracker.note_event(
        {"event_type": "chat.tool_call", "tool_call": {"tool_name": "read_file", "arguments": {"path": "a.py"}}}
    )["step"]
    assert step["tool"] == "read_file"
    assert "a.py" in step["detail"]


def test_tool_result_status_falls_back_to_error_flags() -> None:
    tracker = _tracker()
    ok = tracker.note_event({"event_type": "chat.tool_result", "tool_name": "t", "summary": "fine"})["step"]
    assert ok["status"] == "completed"
    flagged = tracker.note_event({"event_type": "chat.tool_result", "tool_name": "t", "is_error": True})["step"]
    assert flagged["status"] == "error"
    success_false = tracker.note_event({"event_type": "chat.tool_result", "tool_name": "t", "success": False})["step"]
    assert success_false["status"] == "error"


def test_first_delta_only_marks_answering() -> None:
    tracker = _tracker()
    assert tracker.note_event({"event_type": "chat.delta", "content": "x"}) is not None
    assert tracker.note_event({"event_type": "chat.delta", "content": "y"}) is None
    # A new tracker (next turn) answers again.
    assert _tracker().note_event({"event_type": "chat.delta", "content": "x"}) is not None


# ── caps ─────────────────────────────────────────────────────────────────────


def test_steps_are_capped_to_the_last_max_steps() -> None:
    tracker = _tracker()
    for _ in range(MAX_STEPS + 20):
        tracker.note_event({"event_type": "chat.tool_call", "tool_call": {"name": "t"}})
    assert len(tracker.steps) == MAX_STEPS
    snapshot = tracker.snapshot()
    assert len(snapshot["steps"]) == MAX_STEPS


def test_detail_is_compacted_and_clipped() -> None:
    long_text = " ".join(["word"] * 100)
    assert len(detail_text(long_text)) == MAX_DETAIL_CHARS
    # Newlines and runs of spaces collapse to single spaces before clipping.
    assert detail_text("a\n\n  b\tc") == "a b c"
    assert detail_text({"query": "x y"}) == '{"query": "x y"}'
    assert detail_text(None) == ""


def test_reasoning_accumulates_and_keeps_only_the_tail() -> None:
    tracker = _tracker()
    tracker.note_event({"event_type": "chat.reasoning", "content": "abc"})
    assert tracker.reasoning == "abc"
    chunk = "x" * 1000
    for _ in range(MAX_REASONING_CHARS // 1000 + 3):
        tracker.note_event({"event_type": "chat.reasoning", "content": chunk})
    assert len(tracker.reasoning) == MAX_REASONING_CHARS
    # All chunks are identical, so the kept tail is simply the cap in x's —
    # proving the *earlier* text ("abc") fell off, not that the cap is 16000.
    assert tracker.reasoning == "x" * MAX_REASONING_CHARS
    # Blank reasoning chunks produce no step at all.
    assert tracker.note_event({"event_type": "chat.reasoning", "content": ""}) is None


# ── lifecycle ────────────────────────────────────────────────────────────────


def test_header_fields_travel_on_every_payload() -> None:
    tracker = _tracker()
    payload = tracker.start()
    assert payload["event_type"] == PROGRESS_EVENT_TYPE
    assert payload["group_id"] == 3
    assert payload["request_id"] == "groupchat-3-9"
    assert payload["trigger_seq"] == 9
    assert payload["sender_id"] == "u_agent"
    assert payload["state"] == "running"
    assert payload["step"]["phase"] == "started"


def test_busy_step_records_the_dropped_seq() -> None:
    tracker = _tracker()
    step = tracker.note_busy(trigger_seq=12)["step"]
    assert step["phase"] == PHASE_BUSY
    assert step["detail"] == "seq=12"


def test_finish_sets_terminal_state() -> None:
    for phase in (PHASE_DONE, PHASE_TIMEOUT, PHASE_ERROR):
        tracker = _tracker()
        payload = tracker.finish(phase)
        assert payload["state"] == phase
        assert payload["step"]["phase"] == phase
    # Unknown phases degrade to done rather than inventing a state.
    assert _tracker().finish("weird")["state"] == PHASE_DONE


def test_snapshot_carries_steps_and_reasoning() -> None:
    tracker = _tracker()
    tracker.start()
    tracker.note_event({"event_type": "chat.tool_call", "tool_call": {"name": "t"}})
    tracker.note_event({"event_type": "chat.reasoning", "content": "half"})
    snapshot = tracker.snapshot()
    assert snapshot["state"] == "running"
    assert [step["phase"] for step in snapshot["steps"]] == ["started", "tool_call"]
    assert snapshot["reasoning"] == "half"
    # The snapshot is a copy: later steps must not leak into it.
    tracker.note_event({"event_type": "chat.tool_call", "tool_call": {"name": "t2"}})
    assert len(snapshot["steps"]) == 2


def test_steps_number_sequentially_with_timestamps() -> None:
    tracker = _tracker()
    first = tracker.start()["step"]
    second = tracker.note_event({"event_type": "chat.tool_call", "tool_call": {"name": "t"}})["step"]
    assert second["seq"] == first["seq"] + 1
    assert second["ts"] >= first["ts"]
