# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exact-input capture for the Designer trajectory bundle."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.designer.handlers.clip import generate_clip_video
from jiuwenswarm.server.runtime.designer.media_generation import DesignerVideoRequest
from jiuwenswarm.server.runtime.designer.trajectory import (
    TRAJECTORY_SCHEMA,
    TrajectoryRecorder,
    current_trajectory_span,
)


def test_nested_calls_keep_complete_prompts_and_inputs() -> None:
    recorder = TrajectoryRecorder("graph-exact", "run-exact")
    prompt = "keep this prompt verbatim\nincluding the second line"
    tool_input = {
        "prompt": prompt,
        "nested": {"values": [1, 2, {"name": "unchanged"}]},
    }

    with recorder.span(
        agent_id="director",
        action="plan",
        phase="orchestration",
        role="director",
    ):
        with current_trajectory_span(
            action="agent_call",
            phase="inference",
            detail={
                "prompt": prompt,
                "system_prompt": "exact system prompt",
                "input": {
                    "messages": [
                        {"role": "system", "content": "exact system prompt"},
                        {"role": "user", "content": prompt},
                    ]
                },
            },
        ):
            pass
        with current_trajectory_span(
            action="tool_call",
            phase="tool",
            tool="example_tool",
            detail={"input": tool_input},
        ):
            pass

    tool_input["nested"]["values"].append("mutated later")
    bundle = recorder.to_dict()
    assert bundle["schema_version"] == TRAJECTORY_SCHEMA == "designer-trajectory.v2"

    agent_call = next(event for event in bundle["events"] if event["action"] == "agent_call")
    assert agent_call["detail"]["prompt"] == prompt
    assert agent_call["detail"]["system_prompt"] == "exact system prompt"
    assert agent_call["detail"]["input"]["messages"][1]["content"] == prompt

    tool_call = next(event for event in bundle["events"] if event["action"] == "tool_call")
    assert tool_call["detail"]["input"] == {
        "prompt": prompt,
        "nested": {"values": [1, 2, {"name": "unchanged"}]},
    }
    assert bundle["agents"]["director"]["agent_calls"] == 1
    assert bundle["agents"]["director"]["tool_calls"] == 1


@pytest.mark.asyncio
async def test_video_backend_records_effective_tool_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "generated.mp4"
    output.write_bytes(b"video")
    received: list[DesignerVideoRequest] = []

    async def fake_generate(request: DesignerVideoRequest, *, save_dir: str | None = None) -> dict[str, str]:
        received.append(request)
        return {"video_path": str(output)}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.media_generation.generate_video",
        fake_generate,
    )

    recorder = TrajectoryRecorder("graph-video", "run-video")
    with recorder.span(
        agent_id="n_clip_1",
        action="execute",
        phase="node",
        role="clip",
    ):
        result = await generate_clip_video(
            "exact video prompt",
            reference_images=["character.png", "scene.png"],
            duration=7,
            audio=True,
            force_reference_mode=True,
        )

    assert result["video_path"] == str(output)
    event = next(
        item
        for item in recorder.events
        if item["action"] == "tool_call" and item["tool"] == "video_generation"
    )
    [request] = received
    recorded = event["detail"]["input"]
    assert recorded["prompt"] == request.prompt == "exact video prompt"
    assert recorded["reference_images"] == list(request.reference_images) == ["character.png", "scene.png"]
    assert recorded["duration"] == request.duration == 7
    assert recorded["audio"] is request.audio is True
    assert recorded["force_reference_mode"] is request.reference_mode is True
    assert recorded["size"] == request.size
    assert recorded["resolution"] == request.resolution
