# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.server.runtime.designer.experiments.clip_prompt_handoff import (
    handoff_clause_for_prompt,
    stamp_wan_prompt_handoff,
)
from jiuwenswarm.server.runtime.designer.experiments.continuity_card import (
    continuity_card_clause,
    continuity_card_from_prior,
    extract_already_done_beats,
)


def test_extract_already_done_forbids_restarting_run():
    beats = extract_already_done_beats("the man starts running toward the door")
    joined = " ".join(beats).lower()
    assert beats
    assert "onset" in joined or "do not" in joined
    assert "run" in joined


def test_continuity_card_clause_has_no_full_prior_prompt():
    card = continuity_card_from_prior(
        prior_action="woman begins walking away from the altar",
        prior_prompt="HUGE FULL WAN PROMPT " * 80,
        shot_index=1,
        node_id="n_clip_1",
    )
    clause = continuity_card_clause(card)
    assert "CONTINUITY CARD" in clause
    assert "ALREADY_DONE" in clause
    assert "HUGE FULL WAN PROMPT" not in clause
    assert "PRIOR CLIP CONTINUITY" not in clause


def test_stamp_handoff_stamps_card_not_full_wan_on_next():
    graph = {
        "nodes": [
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {"role": "clip", "shot_index": 1, "shot_action": "man starts running"},
            },
            {
                "id": "n_clip_2",
                "type": "video",
                "config": {"role": "clip", "shot_index": 2, "continuity_clip_node_id": "n_clip_1"},
            },
        ],
        "metadata": {},
    }
    big = "SHOT1_ONLY_IDENTITY_MARKER " + ("style lock dump " * 100)
    notes = stamp_wan_prompt_handoff(
        graph,
        shot_index=1,
        prompt=big,
        node_id="n_clip_1",
        shot_action="man starts running",
    )
    assert notes
    c1 = graph["nodes"][0]["config"]
    c2 = graph["nodes"][1]["config"]
    assert "SHOT1_ONLY_IDENTITY_MARKER" in c1.get("last_wan_prompt", "")
    assert "previous_clip_wan_prompt" not in c2
    assert isinstance(c2.get("previous_clip_continuity_card"), dict)
    assert any("onset" in str(x).lower() or "run" in str(x).lower() for x in (c2.get("already_done") or []))
    clause = handoff_clause_for_prompt(
        [
            {
                "shot_index": 1,
                "node_id": "n_clip_1",
                "shot_action": "man starts running",
                "wan_prompt": big,
                "continuity_card": c2["previous_clip_continuity_card"],
            }
        ]
    )
    assert "SHOT1_ONLY_IDENTITY_MARKER" not in clause
    assert "CONTINUITY CARD" in clause
