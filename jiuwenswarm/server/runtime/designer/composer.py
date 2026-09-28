# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compose Designer graphs: static video layout + catalog-driven other scenarios."""

from __future__ import annotations

import logging
from typing import Any, Literal

from jiuwenswarm.common.schema.designer_graph import (
    GRAPH_SOURCE_PROMPT,
    NODE_TYPE_AUDIO,
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
from jiuwenswarm.server.runtime.designer.catalog import (
    catalog_nodes_by_id,
    scenario_template,
)
from jiuwenswarm.server.runtime.designer.skills_loader import attach_skills_metadata
from jiuwenswarm.server.runtime.designer.static_graphs import build_static_video_graph

logger = logging.getLogger(__name__)

OptimizeMode = Literal["cost", "quality"]

_MODALITY_TO_NODE_TYPE = {
    "text": NODE_TYPE_TEXT,
    "table": NODE_TYPE_TABLE,
    "image": NODE_TYPE_IMAGE,
    "video": NODE_TYPE_VIDEO,
    "audio": NODE_TYPE_AUDIO,
    "mesh": NODE_TYPE_IMAGE,
}

_SCENARIO_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("3d", ("3d", "三维", "mesh", "glb", "blender", "texture", "pbr", "模型", "建模")),
    ("music", ("music", "song", "bgm", "soundtrack", "旋律", "音乐", "配乐", "作曲")),
    ("speech", ("podcast", "tts", "voiceover", "voice over", "配音", "旁白", "语音", "播客")),
    ("image", ("illustration", "poster", "logo", "封面", "插画", "海报", "still image")),
    ("video", ("video", "film", "movie", "cinematic", "trailer", "视频", "短片", "分镜", "动画")),
    # 「以及 / 同时」是普通连词，不能当成 multimodal。
    ("multimodal", ("and also", "both a video and", "pipeline", "全流程")),
]


def detect_scenario(prompt: str) -> str:
    text = prompt.lower()
    scores: dict[str, int] = {}
    for scenario, keywords in _SCENARIO_KEYWORDS:
        scores[scenario] = sum(1 for kw in keywords if kw in text)
    # Prefer video when cinematic/film cues appear alongside music/speech keywords.
    video_cues = (
        "video",
        "film",
        "movie",
        "cinematic",
        "trailer",
        "shot",
        "storyboard",
        "alley",
        "clip",
        "视频",
        "短片",
        "分镜",
        "动画",
        "油画",
        "原画",
        "画面",
        "致敬",
        "镜头",
        "关键帧",
    )
    if any(cue in text for cue in video_cues):
        scores["video"] = scores.get("video", 0) + 3
    best = max(scores, key=scores.get)
    if scores[best] <= 0:
        return "video"
    return best


def _layout_for_index(index: int, column: int) -> dict[str, float]:
    return {
        "x": 40.0 + column * 320.0,
        "y": 40.0 + (index % 6) * 180.0,
        "width": 280.0,
        "height": 160.0,
    }


def _compose_from_catalog(
    *,
    project_id: str,
    prompt: str,
    title: str | None,
    optimize_for: OptimizeMode,
    scenario: str,
) -> DesignerExecutionGraph:
    prompt_text = prompt.strip()
    by_id = catalog_nodes_by_id()
    template_ids = scenario_template(scenario)
    nodes: list[DesignerGraphNode] = []
    id_map: dict[str, str] = {}

    for idx, catalog_id in enumerate(template_ids):
        entry = by_id.get(catalog_id)
        if entry is None:
            logger.warning("catalog id missing: %s", catalog_id)
            continue
        modality = str(entry.get("modality") or "text")
        node_type = _MODALITY_TO_NODE_TYPE.get(modality, NODE_TYPE_TEXT)
        node_id = f"n_{catalog_id.replace('.', '_')}"
        id_map[catalog_id] = node_id
        agent_name = str(entry.get("label") or catalog_id) + " Agent"
        tools = ["call_model", "read_upstream"]
        # Scenario-specific tool flavor tags (actual call still goes through Settings models).
        if modality in {"image", "video", "audio", "mesh"}:
            tools.append(f"call_{modality}_model")
        nodes.append(
            {
                "id": node_id,
                "type": node_type,
                "label": str(entry.get("label") or catalog_id),
                "config": {
                    "prompt": prompt_text,
                    "optimize_for": optimize_for,
                    "agent_id": f"agent_{catalog_id.replace('.', '_')}",
                    "agent_name": agent_name,
                    "agent_role": "node_worker",
                    "catalog_id": catalog_id,
                    "tools": tools,
                    "kind": "agent",
                    "modality": modality,
                    "interactive_3d": modality == "mesh",
                    "inputs": [],
                },
                "layout": _layout_for_index(idx, min(idx // 2, 8)),
            }
        )

    edges: list[DesignerGraphEdge] = []
    edge_i = 0
    for node in nodes:
        catalog_id = str((node.get("config") or {}).get("catalog_id") or "")
        entry = by_id.get(catalog_id) or {}
        wired: list[str] = []
        for src_catalog in entry.get("typical_inputs") or []:
            if not isinstance(src_catalog, str) or src_catalog == "user_prompt":
                continue
            src_node = id_map.get(src_catalog)
            if not src_node:
                continue
            edge_i += 1
            edges.append(
                {
                    "id": f"e_{edge_i}_{src_node}_{node['id']}",
                    "source": src_node,
                    "target": node["id"],
                }
            )
            wired.append(src_node)
        cfg = dict(node.get("config") or {})
        cfg["inputs"] = wired
        node["config"] = cfg

    graph_id = new_graph_id()
    now = utc_now_ms()
    graph_title = title.strip() if isinstance(title, str) and title.strip() else prompt_text[:80]
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
            "bootstrap": "designer.graph.catalog_agents.v1",
            "scenario": scenario,
            "optimize_for": optimize_for,
        },
        "created_at": now,
        "updated_at": now,
    }
    return normalize_execution_graph(graph)


def compose_execution_graph(
    *,
    project_id: str,
    prompt: str,
    title: str | None = None,
    optimize_for: OptimizeMode = "quality",
    scenario: str | None = None,
) -> DesignerExecutionGraph:
    """Bootstrap canvas via Director/Leader smart video pipeline.

    Catalog templates are a node library, not the workflow dumped onto the canvas.
    """
    _ = scenario
    mode: OptimizeMode = "cost" if optimize_for == "cost" else "quality"
    from jiuwenswarm.server.runtime.designer.script_analysis import (
        analyze_creative_brief_sync,
    )
    from jiuwenswarm.server.runtime.designer.smart_graph import (
        apply_runtime_delegate,
        build_smart_video_graph,
    )

    # Blunt LLM call — credential/billing failures raise DesignerLlmError.
    analysis = analyze_creative_brief_sync(prompt, timeout_sec=20.0)
    graph = build_smart_video_graph(
        project_id=project_id,
        prompt=prompt,
        analysis=analysis,
        title=title,
        optimize_for=mode,
    )
    graph = apply_runtime_delegate(graph)
    meta = dict(graph.get("metadata") or {})
    meta["script_analysis"] = analysis
    meta["scenario"] = "video"
    graph["metadata"] = meta
    return attach_skills_metadata(graph, prompt)
