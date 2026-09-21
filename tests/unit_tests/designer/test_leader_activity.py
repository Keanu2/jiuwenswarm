# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from jiuwenswarm.common.schema.designer_graph import (
    ACTIVITY_KIND_STAGE,
    ACTIVITY_KIND_TOOL_CALL,
    LEADER_NODE_ID,
    NODE_STATUS_RUNNING,
    apply_node_activity,
    is_leader_node_id,
    normalize_node_state,
)
from jiuwenswarm.server.runtime.designer.activity import (
    apply_run_activity,
    graph_node_states,
    should_publish,
)
from jiuwenswarm.server.runtime.designer.leader_chat import (
    apply_leader_plan,
    heuristic_leader_plan,
    message_asks_to_run,
)


def test_normalize_node_state_keeps_activity() -> None:
    state = normalize_node_state(
        {
            "status": NODE_STATUS_RUNNING,
            "activity": {"kind": ACTIVITY_KIND_TOOL_CALL, "text": "calling designer_graph_patch", "tool": "designer_graph_patch", "at": 1},
            "activity_tail": ["calling designer_graph_patch", "building"],
        }
    )
    assert state["activity"]["tool"] == "designer_graph_patch"
    assert len(state["activity_tail"]) == 2


def test_apply_node_activity_caps_tail() -> None:
    state = {"status": NODE_STATUS_RUNNING}
    for index in range(12):
        state = apply_node_activity(state, kind=ACTIVITY_KIND_STAGE, text=f"step {index}")
    assert len(state["activity_tail"]) == 8
    assert state["activity_tail"][-1].endswith("11")


def test_graph_node_states_skips_leader() -> None:
    run = {
        "node_states": {
            "n_brief": {"status": "completed"},
            LEADER_NODE_ID: {"status": NODE_STATUS_RUNNING},
        }
    }
    states = graph_node_states(run)
    assert "n_brief" in states
    assert LEADER_NODE_ID not in states
    assert is_leader_node_id(LEADER_NODE_ID)


def test_activity_publish_throttles() -> None:
    assert should_publish("run_a", "n_1", now=1000, force=True) is True
    assert should_publish("run_a", "n_1", now=1100) is False
    assert should_publish("run_a", "n_1", now=1400) is True


def test_apply_run_activity_creates_leader_state() -> None:
    run = {"run_id": "run_1", "node_states": {}}
    apply_run_activity(run, LEADER_NODE_ID, kind=ACTIVITY_KIND_STAGE, text="reading brief")
    assert run["node_states"][LEADER_NODE_ID]["status"] == NODE_STATUS_RUNNING
    assert run["node_states"][LEADER_NODE_ID]["activity"]["text"]


def _sample_graph() -> dict:
    return {
        "schema_version": "designer-execution-graph.v1",
        "graph_id": "graph_test",
        "project_id": "p1",
        "title": "Demo",
        "nodes": [
            {
                "id": "n_brief",
                "type": "text",
                "label": "Text 1",
                "config": {"role": "text", "pipeline": "brief", "prompt": "alley at night"},
                "layout": {"x": 40, "y": 240, "width": 280, "height": 160},
            },
            {
                "id": "n_character",
                "type": "image",
                "label": "Image 1",
                "config": {"role": "image", "pipeline": "character_design", "prompt": "officer"},
                "layout": {"x": 400, "y": 40, "width": 280, "height": 160},
            },
            {
                "id": "n_compose",
                "type": "video",
                "label": "Video 2",
                "config": {"role": "video", "pipeline": "compose"},
                "layout": {"x": 1200, "y": 240, "width": 280, "height": 160},
            },
        ],
        "edges": [
            {
                "id": "e_brief_character",
                "source": "n_brief",
                "target": "n_character",
                "kind": "data",
            }
        ],
        "created_at": 1,
        "updated_at": 1,
    }


def test_heuristic_refine_selected_node_reruns() -> None:
    graph = _sample_graph()
    plan = heuristic_leader_plan(
        graph,
        "把角色改得更赛博",
        selected_node_id="n_character",
    )
    assert plan["intent"] == "refine_node"
    assert plan["run_node_ids"] == ["n_character"]
    next_graph, run_ids, summary = apply_leader_plan(graph, plan)
    assert run_ids == ["n_character"]
    char = next(node for node in next_graph["nodes"] if node["id"] == "n_character")
    assert "赛博" in str(char["config"]["prompt"])
    assert "Updated" in summary or "Image" in summary


def test_heuristic_add_node_does_not_run_by_default() -> None:
    graph = _sample_graph()
    plan = heuristic_leader_plan(graph, "加一个配乐节点接到成片，先别生成")
    assert plan["intent"] == "edit_graph"
    assert plan["run_node_ids"] == []
    next_graph, run_ids, _summary = apply_leader_plan(graph, plan)
    assert run_ids == []
    types = {node["type"] for node in next_graph["nodes"]}
    assert "audio" in types
    audio_id = next(node["id"] for node in next_graph["nodes"] if node["type"] == "audio")
    assert any(
        edge.get("source") == audio_id and edge.get("target") == "n_compose"
        for edge in next_graph.get("edges") or []
    )


def test_heuristic_add_image_does_not_wire_into_video_sink() -> None:
    graph = _sample_graph()
    plan = heuristic_leader_plan(graph, "加一个图")
    assert plan["intent"] == "edit_graph"
    next_graph, _run_ids, _summary = apply_leader_plan(graph, plan)
    image_ids = [
        node["id"]
        for node in next_graph["nodes"]
        if node["type"] == "image" and node["id"] != "n_character"
    ]
    assert image_ids
    added = image_ids[0]
    assert not any(
        edge.get("source") == added and edge.get("target") == "n_compose"
        for edge in next_graph.get("edges") or []
    )


def test_heuristic_add_node_runs_when_asked() -> None:
    graph = _sample_graph()
    plan = heuristic_leader_plan(graph, "加一个配乐节点并生成")
    assert plan["run_node_ids"]
    assert message_asks_to_run("加一个配乐节点并生成") is True
