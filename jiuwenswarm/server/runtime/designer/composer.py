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
    load_node_catalog,
    scenario_template,
)
from jiuwenswarm.server.runtime.designer.skills_loader import (
    attach_skills_metadata,
    detect_audio_intent,
    load_scenario_skill,
)
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
    ("multimodal", ("and also", "以及", "同时", "both", "pipeline", "全流程")),
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
    )
    if any(cue in text for cue in video_cues):
        scores["video"] = scores.get("video", 0) + 3
    best = max(scores, key=scores.get)
    if scores[best] <= 0:
        return "video"
    return best


def _append_audio_nodes(
    graph: DesignerExecutionGraph,
    *,
    prompt: str,
) -> DesignerExecutionGraph:
    """Optionally attach speech/music agents guided by audio intent + video skill."""
    audio = detect_audio_intent(prompt)
    meta = dict(graph.get("metadata") or {})
    meta["audio_intent"] = audio
    skill = load_scenario_skill("video")
    if skill:
        meta["scenario_skill_excerpt"] = skill[:6000]
    if audio.get("policy") == "silent":
        meta["audio_nodes"] = []
        graph["metadata"] = meta
        return graph

    nodes = list(graph.get("nodes") or [])
    edges = list(graph.get("edges") or [])
    existing = {str(n.get("id")) for n in nodes}
    # Prefer wiring into compose/final if present
    sink_id = "n_compose" if "n_compose" in existing else ("n_final" if "n_final" in existing else None)
    storyboard_id = "n_storyboard" if "n_storyboard" in existing else None
    added: list[str] = []

    brief_id = "n_brief" if "n_brief" in existing else None

    def _add_node(
        node_id: str,
        label: str,
        modality: str,
        inputs: list[str],
        y: float,
        *,
        role: str,
    ) -> None:
        if node_id in existing:
            return
        node_type = _MODALITY_TO_NODE_TYPE.get(modality, NODE_TYPE_AUDIO)
        skill_key = "speech_tts" if role == "speech" else "audio_bed"
        nodes.append(
            {
                "id": node_id,
                "type": node_type,
                "label": label,
                "config": {
                    "role": role,
                    "prompt": prompt,
                    "optimize_for": meta.get("optimize_for") or "quality",
                    "agent_id": f"agent_{node_id}",
                    "agent_name": f"{label} Agent",
                    "agent_role": "node_worker",
                    "catalog_id": node_id,
                    "skill_id": skill_key,
                    "tools": ["call_model", "read_upstream", f"call_{modality}_model"],
                    "kind": "agent",
                    "modality": modality,
                    "inputs": inputs,
                    # Local ffmpeg bed until remote music/TTS APIs are wired.
                    "delegate": "handler",
                    "force_handler": True,
                    "duration_sec": 4 if str(meta.get("optimize_for") or "") == "cost" else 6,
                },
                "layout": {"x": 980.0, "y": y, "width": 260.0, "height": 150.0},
            }
        )
        existing.add(node_id)
        added.append(node_id)
        for src in inputs:
            if src in existing:
                edges.append(
                    {
                        "id": f"e_{src}_{node_id}",
                        "source": src,
                        "target": node_id,
                    }
                )

    # Depend on Brief (not storyboard) so audio runs parallel with cast/scene/video.
    audio_srcs = [brief_id] if brief_id else ([storyboard_id] if storyboard_id else [])
    if audio.get("include_speech"):
        _add_node(
            "n_speech",
            "Speech / TTS",
            "audio",
            [s for s in audio_srcs if s],
            520.0,
            role="speech",
        )
    if audio.get("include_music") and audio.get("policy") != "silent":
        _add_node(
            "n_music",
            "Music / Bed",
            "audio",
            [s for s in audio_srcs if s],
            680.0,
            role="music",
        )

    if sink_id and added:
        for aid in added:
            edges.append({"id": f"e_{aid}_{sink_id}", "source": aid, "target": sink_id})
            # extend sink inputs
            for node in nodes:
                if node.get("id") != sink_id:
                    continue
                cfg = dict(node.get("config") or {})
                inputs = list(cfg.get("inputs") or [])
                if aid not in inputs:
                    inputs.append(aid)
                cfg["inputs"] = inputs
                node["config"] = cfg

    meta["audio_nodes"] = added
    graph["nodes"] = nodes
    graph["edges"] = edges
    graph["metadata"] = meta
    return normalize_execution_graph(graph)


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
    catalog = load_node_catalog()
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
            "catalog_schema": catalog.get("schema_version"),
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


def compose_execution_graph(
    *,
    project_id: str,
    prompt: str,
    title: str | None = None,
    optimize_for: OptimizeMode = "quality",
    scenario: str | None = None,
) -> DesignerExecutionGraph:
    """Build graph: video uses cast/shot-aware smart layout; others use catalog agents."""
    mode: OptimizeMode = "cost" if optimize_for == "cost" else "quality"
    detected = scenario or detect_scenario(prompt)
    if detected == "video":
        from jiuwenswarm.server.runtime.designer.script_analysis import (
            analyze_creative_brief_sync,
            heuristic_analysis,
            _llm_configured,
        )
        from jiuwenswarm.server.runtime.designer.smart_graph import (
            apply_runtime_delegate,
            build_smart_video_graph,
        )

        if _llm_configured():
            try:
                analysis = analyze_creative_brief_sync(
                    prompt, use_llm=True, timeout_sec=20.0
                )
            except Exception:  # noqa: BLE001
                analysis = heuristic_analysis(prompt)
        else:
            analysis = heuristic_analysis(prompt)
        graph = build_smart_video_graph(
            project_id=project_id,
            prompt=prompt,
            analysis=analysis,
            title=title,
            optimize_for=mode,
            ai_mode=_llm_configured(),
        )
        graph = apply_runtime_delegate(graph)
        meta = dict(graph.get("metadata") or {})
        meta["script_analysis"] = analysis
        meta["script_analysis_mode"] = str(analysis.get("source") or "heuristic")
        meta["pending_llm_analysis"] = bool(
            _llm_configured() and str(analysis.get("source") or "") != "llm"
        )
        meta["auto_accept_outputs"] = True
        graph["metadata"] = meta
        return attach_skills_metadata(graph, prompt)
    graph = _compose_from_catalog(
        project_id=project_id,
        prompt=prompt,
        title=title,
        optimize_for=mode,
        scenario=detected,
    )
    from jiuwenswarm.server.runtime.designer.smart_graph import apply_runtime_delegate

    return attach_skills_metadata(apply_runtime_delegate(graph), prompt)
