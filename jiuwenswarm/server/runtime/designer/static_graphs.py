# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Static Designer graphs (original video pipeline layout preserved)."""

from __future__ import annotations

from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    GRAPH_SOURCE_PROMPT,
    NODE_TYPE_IMAGE,
    NODE_TYPE_TABLE,
    NODE_TYPE_TEXT,
    NODE_TYPE_VIDEO,
    DesignerExecutionGraph,
    DesignerGraphEdge,
    DesignerGraphNode,
    SCHEMA_VERSION,
    new_graph_id,
    normalize_execution_graph,
    utc_now_ms,
)

# Original video scenario node ids / labels / layout (unchanged contract for UI).
VIDEO_STATIC_NODES: list[dict[str, Any]] = [
    {
        "id": "n_brief",
        "type": NODE_TYPE_TEXT,
        "label": "项目 brief",
        "agent_id": "agent_brief",
        "agent_name": "Brief Agent",
        "catalog_id": "n_brief",
        "tools": ["call_model", "read_upstream"],
        "layout": {"x": 40, "y": 200, "width": 280, "height": 160},
        "inputs": [],
    },
    {
        "id": "n_character",
        "type": NODE_TYPE_IMAGE,
        "label": "角色图",
        "agent_id": "agent_character",
        "agent_name": "Character Agent",
        "catalog_id": "n_character",
        "tools": ["call_model", "read_upstream"],
        "layout": {"x": 360, "y": 80, "width": 280, "height": 160},
        "inputs": ["n_brief"],
    },
    {
        "id": "n_storyboard",
        "type": NODE_TYPE_TABLE,
        "label": "分镜表",
        "agent_id": "agent_storyboard",
        "agent_name": "Storyboard Agent",
        "catalog_id": "n_storyboard",
        "tools": ["call_model", "read_upstream"],
        "layout": {"x": 360, "y": 320, "width": 280, "height": 160},
        "inputs": ["n_brief"],
    },
    {
        "id": "n_frame_1",
        "type": NODE_TYPE_IMAGE,
        "label": "视频片段1首帧",
        "agent_id": "agent_frame_1",
        "agent_name": "Frame-1 Agent",
        "catalog_id": "n_frame_1",
        "tools": ["call_model", "read_upstream"],
        "layout": {"x": 680, "y": 40, "width": 260, "height": 150},
        "inputs": ["n_character", "n_storyboard"],
    },
    {
        "id": "n_frame_2",
        "type": NODE_TYPE_IMAGE,
        "label": "视频片段2首帧",
        "agent_id": "agent_frame_2",
        "agent_name": "Frame-2 Agent",
        "catalog_id": "n_frame_2",
        "tools": ["call_model", "read_upstream"],
        "layout": {"x": 680, "y": 200, "width": 260, "height": 150},
        "inputs": ["n_character", "n_storyboard"],
    },
    {
        "id": "n_frame_3",
        "type": NODE_TYPE_IMAGE,
        "label": "视频片段3首帧",
        "agent_id": "agent_frame_3",
        "agent_name": "Frame-3 Agent",
        "catalog_id": "n_frame_3",
        "tools": ["call_model", "read_upstream"],
        "layout": {"x": 680, "y": 360, "width": 260, "height": 150},
        "inputs": ["n_character", "n_storyboard"],
    },
    {
        "id": "n_clip_1",
        "type": NODE_TYPE_VIDEO,
        "label": "视频片段1",
        "agent_id": "agent_clip_1",
        "agent_name": "Clip-1 Agent",
        "catalog_id": "n_clip_1",
        "tools": ["call_model", "read_upstream"],
        "layout": {"x": 980, "y": 40, "width": 260, "height": 150},
        "inputs": ["n_frame_1"],
    },
    {
        "id": "n_clip_2",
        "type": NODE_TYPE_VIDEO,
        "label": "视频片段2",
        "agent_id": "agent_clip_2",
        "agent_name": "Clip-2 Agent",
        "catalog_id": "n_clip_2",
        "tools": ["call_model", "read_upstream"],
        "layout": {"x": 980, "y": 200, "width": 260, "height": 150},
        "inputs": ["n_frame_2"],
    },
    {
        "id": "n_clip_3",
        "type": NODE_TYPE_VIDEO,
        "label": "视频片段3",
        "agent_id": "agent_clip_3",
        "agent_name": "Clip-3 Agent",
        "catalog_id": "n_clip_3",
        "tools": ["call_model", "read_upstream"],
        "layout": {"x": 980, "y": 360, "width": 260, "height": 150},
        "inputs": ["n_frame_3"],
    },
    {
        "id": "n_final",
        "type": NODE_TYPE_VIDEO,
        "label": "最终视频",
        "agent_id": "agent_final",
        "agent_name": "Final Assembly Agent",
        "catalog_id": "n_final",
        "tools": ["call_model", "read_upstream"],
        "layout": {"x": 1280, "y": 200, "width": 280, "height": 160},
        "inputs": ["n_clip_1", "n_clip_2", "n_clip_3"],
    },
]

VIDEO_STATIC_EDGES: list[dict[str, str]] = [
    {"id": "e_brief_character", "source": "n_brief", "target": "n_character"},
    {"id": "e_brief_storyboard", "source": "n_brief", "target": "n_storyboard"},
    {"id": "e_character_frame_1", "source": "n_character", "target": "n_frame_1"},
    {"id": "e_character_frame_2", "source": "n_character", "target": "n_frame_2"},
    {"id": "e_character_frame_3", "source": "n_character", "target": "n_frame_3"},
    {"id": "e_storyboard_frame_1", "source": "n_storyboard", "target": "n_frame_1"},
    {"id": "e_storyboard_frame_2", "source": "n_storyboard", "target": "n_frame_2"},
    {"id": "e_storyboard_frame_3", "source": "n_storyboard", "target": "n_frame_3"},
    {"id": "e_frame_1_clip_1", "source": "n_frame_1", "target": "n_clip_1"},
    {"id": "e_frame_2_clip_2", "source": "n_frame_2", "target": "n_clip_2"},
    {"id": "e_frame_3_clip_3", "source": "n_frame_3", "target": "n_clip_3"},
    {"id": "e_clip_1_final", "source": "n_clip_1", "target": "n_final"},
    {"id": "e_clip_2_final", "source": "n_clip_2", "target": "n_final"},
    {"id": "e_clip_3_final", "source": "n_clip_3", "target": "n_final"},
]


def _agent_config(
    spec: dict[str, Any],
    *,
    prompt: str,
    optimize_for: str,
) -> dict[str, Any]:
    return {
        "prompt": prompt,
        "inputs": list(spec.get("inputs") or []),
        "optimize_for": optimize_for,
        "agent_id": spec["agent_id"],
        "agent_name": spec["agent_name"],
        "agent_role": "node_worker",
        "catalog_id": spec["catalog_id"],
        "tools": list(spec.get("tools") or ["call_model", "read_upstream"]),
        "kind": "agent",
    }


def build_static_video_graph(
    *,
    project_id: str,
    prompt: str,
    title: str | None = None,
    optimize_for: str = "quality",
) -> DesignerExecutionGraph:
    """Original video DAG with agent metadata on every node."""
    prompt_text = prompt.strip()
    graph_id = new_graph_id()
    now = utc_now_ms()
    graph_title = (
        title.strip() if isinstance(title, str) and title.strip() else prompt_text[:80]
    )
    mode = "cost" if str(optimize_for).strip().lower() == "cost" else "quality"
    nodes: list[DesignerGraphNode] = []
    for spec in VIDEO_STATIC_NODES:
        nodes.append(
            {
                "id": spec["id"],
                "type": spec["type"],
                "label": spec["label"],
                "config": _agent_config(spec, prompt=prompt_text, optimize_for=mode),
                "layout": dict(spec["layout"]),
            }
        )
    edges: list[DesignerGraphEdge] = [
        {"id": e["id"], "source": e["source"], "target": e["target"]} for e in VIDEO_STATIC_EDGES
    ]
    graph: DesignerExecutionGraph = {
        "schema_version": SCHEMA_VERSION,
        "graph_id": graph_id,
        "project_id": project_id,
        "title": graph_title or "Designer Project",
        "description": prompt_text,
        "source": GRAPH_SOURCE_PROMPT,
        "nodes": nodes,
        "edges": edges,
        "metadata": {
            "bootstrap": "designer.graph.static_video.v1",
            "scenario": "video",
            "optimize_for": mode,
            "agentic": True,
            "orchestration": {
                "supervisor_id": "supervisor",
                "manager_id": "manager",
            },
        },
        "created_at": now,
        "updated_at": now,
    }
    return normalize_execution_graph(graph)
