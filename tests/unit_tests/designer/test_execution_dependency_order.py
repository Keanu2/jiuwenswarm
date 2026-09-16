# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.common.schema.designer_graph import (
    NODE_STATUS_PENDING,
    break_cycles_for_schedule,
    execution_predecessors,
    filter_ready_by_dependency_order,
    raw_execution_predecessors,
)
from jiuwenswarm.server.runtime.designer.executor import _is_ready


def _graph(nodes, edges):
    return {
        "schema_version": "designer-execution-graph.v1",
        "graph_id": "g1",
        "project_id": "p1",
        "title": "t",
        "description": "",
        "nodes": nodes,
        "edges": edges,
        "metadata": {},
    }


def test_config_inputs_count_as_schedule_deps_even_without_edge():
    graph = _graph(
        [
            {"id": "a", "type": "image", "config": {"role": "frame", "shot_index": 1}},
            {
                "id": "b",
                "type": "image",
                "config": {
                    "role": "frame",
                    "shot_index": 2,
                    "inputs": ["a"],
                    "scene_prompt_handoff_from": "a",
                },
            },
        ],
        [],  # no edges — soft config dep only
    )
    preds = execution_predecessors(graph)
    assert "a" in preds["b"]
    run = {
        "node_states": {
            "a": {"status": NODE_STATUS_PENDING},
            "b": {"status": NODE_STATUS_PENDING},
        }
    }
    groups = {"a": frozenset({"a"}), "b": frozenset({"b"})}
    assert _is_ready("a", run, preds, groups)
    assert not _is_ready("b", run, preds, groups)


def test_cycle_breaks_by_shot_priority_so_dependent_waits():
    graph = _graph(
        [
            {"id": "n_clip_1", "type": "video", "config": {"role": "clip", "shot_index": 1}},
            {"id": "n_clip_2", "type": "video", "config": {"role": "clip", "shot_index": 2}},
        ],
        [
            {"id": "e12", "source": "n_clip_1", "target": "n_clip_2", "kind": "data"},
            {"id": "e21", "source": "n_clip_2", "target": "n_clip_1", "kind": "data"},  # cycle
        ],
    )
    raw = raw_execution_predecessors(graph)
    assert "n_clip_1" in raw["n_clip_2"] and "n_clip_2" in raw["n_clip_1"]
    dag = break_cycles_for_schedule(raw, graph)
    # Keep forward 1→2; drop back-edge 2→1
    assert "n_clip_1" in dag["n_clip_2"]
    assert "n_clip_2" not in dag["n_clip_1"]
    run = {
        "node_states": {
            "n_clip_1": {"status": NODE_STATUS_PENDING},
            "n_clip_2": {"status": NODE_STATUS_PENDING},
        }
    }
    groups = {k: frozenset({k}) for k in ("n_clip_1", "n_clip_2")}
    assert _is_ready("n_clip_1", run, dag, groups)
    assert not _is_ready("n_clip_2", run, dag, groups)


def test_same_level_ready_set_never_starts_dependent_with_peer_pred():
    graph = _graph(
        [
            {"id": "n_clip_1", "type": "video", "config": {"role": "clip", "shot_index": 1}},
            {"id": "n_clip_2", "type": "video", "config": {"role": "clip", "shot_index": 2}},
            {"id": "n_char_1", "type": "image", "config": {"role": "character_design"}},
        ],
        [
            {"id": "e12", "source": "n_clip_1", "target": "n_clip_2", "kind": "data"},
        ],
    )
    preds = execution_predecessors(graph)
    # Simulate a buggy ready set that includes both ends of an edge.
    selected = filter_ready_by_dependency_order(
        ["n_clip_1", "n_clip_2", "n_char_1"],
        preds=preds,
        in_flight=set(),
        graph=graph,
    )
    assert "n_clip_1" in selected
    assert "n_char_1" in selected
    assert "n_clip_2" not in selected


def test_soft_clip_dep_unlocks_when_prior_prompt_artifact_ready():
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_STATUS_PENDING,
        NODE_STATUS_RUNNING,
        artifact_dependency_satisfied,
        is_soft_artifact_dependency,
    )
    from jiuwenswarm.server.runtime.designer.executor import _is_ready

    graph = _graph(
        [
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {
                    "role": "clip",
                    "shot_index": 1,
                    "handoff_artifact_ready": True,
                    "last_wan_prompt": "shot1 wan prompt already used for tools",
                    "continuity_card": {
                        "already_done": ["shot 1: onset already happened — do not restart the run"],
                        "prior_action_summary": "man starts running",
                    },
                },
            },
            {
                "id": "n_clip_2",
                "type": "video",
                "config": {
                    "role": "clip",
                    "shot_index": 2,
                    "continuity_clip_node_id": "n_clip_1",
                    "previous_clip_continuity_card": {
                        "already_done": ["shot 1: onset already happened — do not restart the run"],
                        "prior_action_summary": "man starts running",
                    },
                    "previous_clip_handoff_ready": True,
                },
            },
        ],
        [{"id": "e12", "source": "n_clip_1", "target": "n_clip_2", "kind": "data"}],
    )
    assert is_soft_artifact_dependency(graph, "n_clip_1", "n_clip_2")
    assert artifact_dependency_satisfied(graph, "n_clip_2", "n_clip_1")
    preds = execution_predecessors(graph)
    groups = {k: frozenset({k}) for k in ("n_clip_1", "n_clip_2")}
    run = {
        "node_states": {
            "n_clip_1": {"status": NODE_STATUS_RUNNING},
            "n_clip_2": {"status": NODE_STATUS_PENDING},
        }
    }
    assert _is_ready("n_clip_2", run, preds, groups, graph)
    selected = filter_ready_by_dependency_order(
        ["n_clip_2"],
        preds=preds,
        in_flight={"n_clip_1"},
        graph=graph,
    )
    assert selected == ["n_clip_2"]


def test_hard_keyframe_dep_still_blocks_until_complete():
    from jiuwenswarm.common.schema.designer_graph import NODE_STATUS_PENDING, NODE_STATUS_RUNNING
    from jiuwenswarm.server.runtime.designer.executor import _is_ready

    graph = _graph(
        [
            {"id": "n_frame_1", "type": "image", "config": {"role": "frame", "shot_index": 1}},
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {"role": "clip", "shot_index": 1, "inputs": ["n_frame_1"]},
            },
        ],
        [{"id": "e", "source": "n_frame_1", "target": "n_clip_1", "kind": "data"}],
    )
    preds = execution_predecessors(graph)
    groups = {k: frozenset({k}) for k in ("n_frame_1", "n_clip_1")}
    run = {
        "node_states": {
            "n_frame_1": {"status": NODE_STATUS_RUNNING},
            "n_clip_1": {"status": NODE_STATUS_PENDING},
        }
    }
    assert not _is_ready("n_clip_1", run, preds, groups, graph)
