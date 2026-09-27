# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Storyboard start/end state continuity tests."""

from __future__ import annotations


def test_ensure_shot_start_end_chains_same_setting() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.storyboard_shot_state import (
        ensure_shot_start_end_states,
        validate_storyboard_state_chain,
    )

    shots = [
        {
            "shot_index": 1,
            "setting_id": "set_a",
            "action": "Alex turns to the calendar",
            "speech_line": "Oh no!",
            "on_screen": ["char_alex"],
            "exiting_character_ids": [],
            "camera": "medium",
        },
        {
            "shot_index": 2,
            "setting_id": "set_a",
            "action": "Alex walks to the window",
            "speech_line": "",
            "on_screen": ["char_alex"],
            "camera": "tracking",
        },
    ]
    out = ensure_shot_start_end_states(shots)
    assert out[0]["start_state"]
    assert out[0]["end_state"]
    assert out[1]["start_state"]
    # Shot 2 start inherits shot 1 end pose when not authored.
    assert out[1]["start_state"].get("pose") or out[1]["start_state"].get("speech_done") is not None
    assert not validate_storyboard_state_chain(out)


def test_stamp_clears_continuity_clip_node() -> None:
    from jiuwenswarm.server.runtime.designer.pipeline.storyboard_shot_state import (
        stamp_shot_states_on_clip_cfg,
    )

    cfg = stamp_shot_states_on_clip_cfg(
        {"continuity_clip_node_id": "n_clip_1", "shot_index": 2},
        shot={
            "start_state": {"pose": "Already facing the window", "seats": {"char_alex": {"place": "desk"}}},
            "end_state": {"pose": "At the window", "speech_done": ""},
        },
    )
    assert "continuity_clip_node_id" not in cfg
    assert cfg.get("start_state")
    assert cfg.get("seat_anchors")
