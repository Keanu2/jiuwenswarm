# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.server.runtime.designer.executor import (
    locked_storyboard_shot_count,
    should_expand_shot_topology_after_nodes,
)
from jiuwenswarm.server.runtime.designer.smart_graph import prune_shot_nodes_beyond_analysis
from jiuwenswarm.common.schema.designer_graph import extra_media_ids_added_by_patch, shot_topology_ids_added_by_patch


def _graph(*, shots: int, frames: int) -> dict:
    nodes = [
        {
            "id": "n_storyboard",
            "type": "table",
            "config": {
                "pipeline": "storyboard",
                "planned_shots": [
                    {"shot_index": i, "action": f"beat {i}"} for i in range(1, shots + 1)
                ],
            },
        },
        {"id": "n_compose", "type": "video", "config": {"pipeline": "compose"}},
    ]
    edges = []
    for i in range(1, frames + 1):
        nodes.append(
            {
                "id": f"n_frame_{i}",
                "type": "image",
                "config": {"pipeline": "frame", "shot_index": i, "shot_action": f"Distinct beat for shot {i}"},
            }
        )
        nodes.append(
            {
                "id": f"n_clip_{i}",
                "type": "video",
                "config": {"pipeline": "clip", "shot_index": i},
            }
        )
        edges.append({"id": f"e_f{i}", "source": f"n_frame_{i}", "target": f"n_clip_{i}", "kind": "data"})
        edges.append({"id": f"e_c{i}", "source": f"n_clip_{i}", "target": "n_compose", "kind": "data"})
    return {
        "schema_version": "designer-execution-graph.v1",
        "graph_id": "g1",
        "project_id": "p1",
        "title": "t",
        "description": "",
        "nodes": nodes,
        "edges": edges,
        "metadata": {
            "script_analysis": {
                "target_shot_count": shots,
                "shots": [{"shot_index": i, "action": f"beat {i}"} for i in range(1, shots + 1)],
            }
        },
    }


def test_locked_count_uses_planned_shots_not_extra_frames():
    graph = _graph(shots=3, frames=7)
    assert locked_storyboard_shot_count(graph) == 3


def test_prune_drops_frames_and_clips_beyond_analysis():
    graph = _graph(shots=3, frames=7)
    pruned = prune_shot_nodes_beyond_analysis(graph)
    assert set(pruned) == {
        "n_frame_4",
        "n_frame_5",
        "n_frame_6",
        "n_frame_7",
        "n_clip_4",
        "n_clip_5",
        "n_clip_6",
        "n_clip_7",
    }
    ids = {str(n.get("id")) for n in graph["nodes"]}
    assert "n_frame_3" in ids and "n_clip_3" in ids
    assert "n_frame_4" not in ids and "n_clip_7" not in ids
    leftover = {(e["source"], e["target"]) for e in graph["edges"]}
    assert ("n_clip_7", "n_compose") not in leftover
    assert ("n_clip_3", "n_compose") in leftover


def test_prune_noop_when_analysis_matches_frames():
    graph = _graph(shots=3, frames=3)
    assert prune_shot_nodes_beyond_analysis(graph) == []
    assert len([n for n in graph["nodes"] if str(n["id"]).startswith("n_frame_")]) == 3


def test_patch_cannot_add_extra_clip_nodes_on_retry():
    graph = _graph(shots=2, frames=2)
    assert shot_topology_ids_added_by_patch(
        graph,
        {
            "upsert_nodes": [
                {"id": "n_clip_3", "type": "video", "config": {"pipeline": "clip"}},
                {"id": "n_frame_3", "type": "image", "config": {"pipeline": "frame"}},
            ]
        },
    ) == ["n_clip_3", "n_frame_3"]
    assert shot_topology_ids_added_by_patch(
        graph,
        {"upsert_nodes": [{"id": "n_clip_1", "type": "video", "config": {"pipeline": "clip"}}]},
    ) == []
    assert shot_topology_ids_added_by_patch(
        graph,
        {"upsert_nodes": [{"id": "n_note", "type": "text", "config": {"pipeline": "brief"}}]},
    ) == []


def test_patch_cannot_add_extra_image_or_video_canvas_nodes():
    graph = _graph(shots=1, frames=1)
    assert extra_media_ids_added_by_patch(
        graph,
        {
            "upsert_nodes": [
                {"id": "n_image_user", "type": "image", "config": {"role": "image"}},
                {"id": "n_video_user", "type": "video", "config": {"role": "video"}},
            ]
        },
    ) == ["n_image_user", "n_video_user"]
    assert extra_media_ids_added_by_patch(
        graph,
        {"upsert_nodes": [{"id": "n_note", "type": "text", "config": {"pipeline": "brief"}}]},
    ) == []


def test_failed_video_does_not_expand_shot_topology():
    assert should_expand_shot_topology_after_nodes(
        ["n_clip_1"],
        {"n_clip_1": {"status": "failed"}},
    ) is False
    assert should_expand_shot_topology_after_nodes(
        ["n_clip_1"],
        {"n_clip_1": {"status": "completed"}},
    ) is True
    assert should_expand_shot_topology_after_nodes([], {"n_clip_1": {"status": "failed"}}) is True


def test_heuristic_skeleton_does_not_lock_supervisor_shot_count():
    from jiuwenswarm.server.runtime.designer.orchestration import (
        _apply_llm_shot_list,
        _shot_expand_lock_count,
    )

    graph = {
        "metadata": {
            "freeze_shot_topology": False,
            "script_analysis": {"source": "heuristic_pending_llm", "target_shot_count": 1},
        }
    }
    analysis = {"source": "heuristic_pending_llm", "target_shot_count": 1}
    current = [{"shot_index": 1, "action": "placeholder"}]
    llm_shots = [{"shot_index": i, "action": f"beat {i}"} for i in range(1, 5)]
    lock = _shot_expand_lock_count(
        graph=graph, analysis=analysis, current_shots=current
    )
    assert lock == 0
    merged = _apply_llm_shot_list(current, llm_shots, lock_count=lock)
    assert len(merged) == 4


def test_frozen_llm_topology_still_clamps_extra_shots():
    from jiuwenswarm.server.runtime.designer.orchestration import (
        _apply_llm_shot_list,
        _shot_expand_lock_count,
    )

    graph = {"metadata": {"freeze_shot_topology": True}}
    analysis = {"source": "llm", "target_shot_count": 2}
    current = [{"shot_index": 1}, {"shot_index": 2}]
    llm_shots = [{"shot_index": i} for i in range(1, 6)]
    lock = _shot_expand_lock_count(
        graph=graph, analysis=analysis, current_shots=current
    )
    assert lock == 2
    merged = _apply_llm_shot_list(current, llm_shots, lock_count=lock)
    assert len(merged) == 2
