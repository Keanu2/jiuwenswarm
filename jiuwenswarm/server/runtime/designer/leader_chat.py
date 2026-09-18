# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Invisible Designer leader: chat-driven graph edits and output refine."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from jiuwenswarm.common.schema.designer_graph import (
    ACTIVITY_KIND_STAGE,
    ACTIVITY_KIND_THINKING,
    ACTIVITY_KIND_TOOL_CALL,
    NODE_TYPE_AUDIO,
    NODE_TYPE_IMAGE,
    NODE_TYPE_VIDEO,
    DesignerExecutionGraph,
    DesignerGraphNode,
    apply_graph_patch,
    node_pipeline,
    utc_now_ms,
)

logger = logging.getLogger(__name__)

ProgressFn = Callable[..., None]

_RUN_HINT = re.compile(
    r"(生成|重跑|重生成|运行|run\b|generate|rerun|regenerate)",
    re.I,
)
_REFINE_HINT = re.compile(
    r"(改|更|精修|refine|more |make |变成|换成|prompt|规格|brief|storyboard|分镜)",
    re.I,
)
_ADD_HINT = re.compile(
    r"(加|添加|新增|add |new |删|去掉|remove|delete|connect|接到|连到)",
    re.I,
)
_AUDIO_HINT = re.compile(r"(配乐|音乐|music|bgm|旁白|配音|speech|voice)", re.I)
_VIDEO_HINT = re.compile(r"(视频|镜头|clip|video)", re.I)

_LEADER_SYSTEM = """You are the invisible Designer Leader. Reply with a JSON object only.
Canvas node type and config.role must be one of: text, table, image, video, audio.
Character/Scene/Keyframe/Clip/Film are pipelines, never node kinds.
Do not rebuild the whole graph. Patch only what the user asked.

Schema:
{
  "intent": "edit_graph" | "refine_node" | "answer",
  "summary": "short user-facing Chinese or English summary",
  "thinking": "one-line peek of what you are doing",
  "patch": {
    "upsert_nodes": [],
    "upsert_edges": [],
    "remove_node_ids": [],
    "remove_edge_ids": []
  },
  "prompt_updates": [{"node_id": "", "prompt": ""}],
  "run_node_ids": []
}

Rules:
- edit_graph: change topology. Leave run_node_ids empty unless the user asked to generate/run.
- refine_node: update that node's config.prompt (and brief/storyboard text if asked). Put the target in run_node_ids so it regenerates.
- answer: no patch, just summary.
"""


_DONT_RUN = re.compile(
    r"(先别|不要跑|不要生成|别生成|不用跑|without running|don'?t run|do not run)",
    re.I,
)


def message_asks_to_run(message: str, *, run_new_nodes: bool = False) -> bool:
    text = str(message or "")
    if _DONT_RUN.search(text):
        return False
    if run_new_nodes:
        return True
    return bool(_RUN_HINT.search(text))


def _emit(progress: ProgressFn | None, kind: str, text: str, tool: str = "") -> None:
    if not callable(progress):
        return
    try:
        progress(kind, text, tool)
    except TypeError:
        progress(kind, text)


def _node_by_id(graph: DesignerExecutionGraph, node_id: str) -> DesignerGraphNode | None:
    target = str(node_id or "").strip()
    if not target:
        return None
    for node in graph.get("nodes") or []:
        if str(node.get("id") or "") == target:
            return node
    return None


def _match_node(graph: DesignerExecutionGraph, message: str) -> DesignerGraphNode | None:
    text = str(message or "").strip().lower()
    if not text:
        return None
    ranked: list[tuple[int, DesignerGraphNode]] = []
    for node in graph.get("nodes") or []:
        node_id = str(node.get("id") or "")
        label = str(node.get("label") or "")
        score = 0
        if node_id and node_id.lower() in text:
            score += 3
        if label and label.lower() in text:
            score += 2
        if score:
            ranked.append((score, node))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1] if ranked else None


def _next_label(graph: DesignerExecutionGraph, node_type: str) -> str:
    count = sum(1 for node in graph.get("nodes") or [] if str(node.get("type") or "") == node_type)
    title = node_type[:1].upper() + node_type[1:]
    return f"{title} {count + 1}"


def _next_node_id(graph: DesignerExecutionGraph, prefix: str) -> str:
    used = {str(node.get("id") or "") for node in graph.get("nodes") or []}
    if prefix not in used:
        return prefix
    index = 2
    while f"{prefix}_{index}" in used:
        index += 1
    return f"{prefix}_{index}"


def _place_right(graph: DesignerExecutionGraph) -> dict[str, float]:
    max_x = 40.0
    y = 240.0
    for node in graph.get("nodes") or []:
        layout = node.get("layout") or {}
        x = float(layout.get("x") or 0)
        if x >= max_x:
            max_x = x
            y = float(layout.get("y") or y)
    return {"x": max_x + 368, "y": y, "width": 280, "height": 160}


def _compose_or_sink_id(graph: DesignerExecutionGraph) -> str | None:
    ids = {str(node.get("id") or "") for node in graph.get("nodes") or []}
    for candidate in ("n_compose", "n_final"):
        if candidate in ids:
            return candidate
    for node in reversed(list(graph.get("nodes") or [])):
        if str(node.get("type") or "") == NODE_TYPE_VIDEO:
            return str(node.get("id") or "") or None
    return None


def heuristic_leader_plan(
    graph: DesignerExecutionGraph,
    message: str,
    *,
    selected_node_id: str = "",
    run_new_nodes: bool = False,
) -> dict[str, Any]:
    text = str(message or "").strip()
    selected = _node_by_id(graph, selected_node_id) or _match_node(graph, text)
    wants_run = message_asks_to_run(text, run_new_nodes=run_new_nodes)

    if selected is not None and (_REFINE_HINT.search(text) or not _ADD_HINT.search(text)):
        node = dict(selected)
        cfg = dict(node.get("config") or {})
        previous = str(cfg.get("prompt") or "").strip()
        cfg["prompt"] = f"{previous}\n{text}".strip() if previous else text
        node["config"] = cfg
        upsert = [node]
        mentioned_spec = bool(re.search(r"(brief|storyboard|分镜|剧本)", text, re.I))
        if mentioned_spec:
            for extra_id in ("n_brief", "n_storyboard"):
                extra = _node_by_id(graph, extra_id)
                if extra is None or extra.get("id") == node.get("id"):
                    continue
                extra_node = dict(extra)
                extra_cfg = dict(extra_node.get("config") or {})
                extra_prev = str(extra_cfg.get("prompt") or "").strip()
                extra_cfg["prompt"] = f"{extra_prev}\n{text}".strip() if extra_prev else text
                extra_node["config"] = extra_cfg
                upsert.append(extra_node)
        return {
            "intent": "refine_node",
            "summary": f"Updated {(node.get('label') or node.get('id'))} and will regenerate it.",
            "thinking": f"refining {node.get('label') or node.get('id')}",
            "patch": {"upsert_nodes": upsert},
            "run_node_ids": [str(node.get("id") or "")],
        }

    if _ADD_HINT.search(text):
        if _AUDIO_HINT.search(text):
            node_type = NODE_TYPE_AUDIO
            prefix = "n_audio"
        elif _VIDEO_HINT.search(text):
            node_type = NODE_TYPE_VIDEO
            prefix = "n_video"
        else:
            node_type = NODE_TYPE_IMAGE
            prefix = "n_image"
        node_id = _next_node_id(graph, prefix)
        sink = _compose_or_sink_id(graph)
        node: dict[str, Any] = {
            "id": node_id,
            "type": node_type,
            "label": _next_label(graph, node_type),
            "config": {"role": node_type, "prompt": text},
            "layout": _place_right(graph),
        }
        patch: dict[str, Any] = {"upsert_nodes": [node]}
        if sink:
            patch["upsert_edges"] = [
                {
                    "id": f"e_{node_id}_{sink}",
                    "source": node_id,
                    "target": sink,
                    "kind": "data",
                }
            ]
        return {
            "intent": "edit_graph",
            "summary": f"Added {node['label']}" + (" and will run it." if wants_run else " without running it."),
            "thinking": f"adding {node['label']}",
            "patch": patch,
            "run_node_ids": [node_id] if wants_run else [],
        }

    return {
        "intent": "answer",
        "summary": "Tell me which node to refine, or what to add/remove on the canvas.",
        "thinking": "waiting for a graph edit or refine request",
        "patch": {},
        "run_node_ids": [],
    }


def _merge_prompt_updates(graph: DesignerExecutionGraph, plan: dict[str, Any]) -> dict[str, Any]:
    patch = dict(plan.get("patch") or {})
    updates = plan.get("prompt_updates") or []
    if not isinstance(updates, list) or not updates:
        return patch
    upsert = list(patch.get("upsert_nodes") or [])
    by_id = {str(item.get("id") or ""): dict(item) for item in upsert if isinstance(item, dict)}
    for item in updates:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("node_id") or "").strip()
        prompt = str(item.get("prompt") or "").strip()
        if not node_id or not prompt:
            continue
        node = by_id.get(node_id) or (_node_by_id(graph, node_id) and dict(_node_by_id(graph, node_id) or {}))
        if not node:
            continue
        cfg = dict(node.get("config") or {})
        cfg["prompt"] = prompt
        node["config"] = cfg
        by_id[node_id] = node
    if by_id:
        patch["upsert_nodes"] = list(by_id.values())
    return patch


def apply_leader_plan(
    graph: DesignerExecutionGraph,
    plan: dict[str, Any],
) -> tuple[DesignerExecutionGraph, list[str], str]:
    intent = str(plan.get("intent") or "answer").strip() or "answer"
    summary = str(plan.get("summary") or "").strip()
    patch = _merge_prompt_updates(graph, plan)
    has_patch = any(patch.get(key) for key in ("upsert_nodes", "upsert_edges", "remove_node_ids", "remove_edge_ids"))
    next_graph = apply_graph_patch(graph, patch) if has_patch else graph
    raw_run_ids = plan.get("run_node_ids") or []
    run_ids = [str(item).strip() for item in raw_run_ids if str(item).strip()]
    if intent != "refine_node":
        # Topology edits only run when the plan explicitly listed ids.
        run_ids = run_ids
    known = {str(node.get("id") or "") for node in next_graph.get("nodes") or []}
    run_ids = [item for item in run_ids if item in known]
    if not summary:
        if intent == "refine_node":
            summary = "Updated the selected node."
        elif has_patch:
            summary = "Updated the workflow graph."
        else:
            summary = "No graph changes."
    return next_graph, run_ids, summary


def _sanitize_plan(plan: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return {"intent": "answer", "summary": "Could not understand that request.", "patch": {}, "run_node_ids": []}
    intent = str(plan.get("intent") or "answer").strip()
    if intent not in {"edit_graph", "refine_node", "answer"}:
        intent = "answer"
    patch = plan.get("patch") if isinstance(plan.get("patch"), dict) else {}
    run_ids = plan.get("run_node_ids") if isinstance(plan.get("run_node_ids"), list) else []
    prompt_updates = plan.get("prompt_updates") if isinstance(plan.get("prompt_updates"), list) else []
    return {
        "intent": intent,
        "summary": str(plan.get("summary") or "").strip(),
        "thinking": str(plan.get("thinking") or "").strip(),
        "patch": patch,
        "prompt_updates": prompt_updates,
        "run_node_ids": [str(item).strip() for item in run_ids if str(item).strip()],
    }


async def _llm_leader_plan(
    graph: DesignerExecutionGraph,
    message: str,
    *,
    selected_node_id: str = "",
) -> dict[str, Any] | None:
    from jiuwenswarm.server.runtime.designer.model_tools import call_model_tool, llm_available
    from jiuwenswarm.server.runtime.designer.script_analysis import _extract_json_object

    if not llm_available():
        return None
    snapshot = {
        "selected_node_id": selected_node_id,
        "nodes": [
            {
                "id": node.get("id"),
                "type": node.get("type"),
                "label": node.get("label"),
                "pipeline": node_pipeline(node),
                "prompt": str((node.get("config") or {}).get("prompt") or "")[:240],
            }
            for node in graph.get("nodes") or []
        ],
        "edges": [
            {"id": edge.get("id"), "source": edge.get("source"), "target": edge.get("target")}
            for edge in graph.get("edges") or []
        ],
        "user": message,
    }
    try:
        result = await call_model_tool(
            prompt=json.dumps(snapshot, ensure_ascii=False),
            system=_LEADER_SYSTEM,
            optimize_for="quality",
            max_tokens=16384,
        )
    except Exception:  # noqa: BLE001
        logger.info("Leader chat model call failed", exc_info=True)
        return None
    if not isinstance(result, dict) or result.get("fallback") or not result.get("ok"):
        return None
    parsed = _extract_json_object(str(result.get("text") or ""))
    return _sanitize_plan(parsed)


async def run_leader_chat(
    graph: DesignerExecutionGraph,
    message: str,
    *,
    selected_node_id: str = "",
    run_new_nodes: bool = False,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    text = str(message or "").strip()
    _emit(progress, ACTIVITY_KIND_THINKING, "reading the canvas and your request")
    plan = await _llm_leader_plan(graph, text, selected_node_id=selected_node_id)
    if plan is None:
        _emit(progress, ACTIVITY_KIND_STAGE, "planning graph edits")
        plan = heuristic_leader_plan(
            graph,
            text,
            selected_node_id=selected_node_id,
            run_new_nodes=run_new_nodes,
        )
    else:
        thinking = str(plan.get("thinking") or "applying graph edits")
        _emit(progress, ACTIVITY_KIND_THINKING, thinking)
        if plan.get("intent") == "edit_graph" and not message_asks_to_run(text, run_new_nodes=run_new_nodes):
            plan["run_node_ids"] = []
        if plan.get("intent") == "refine_node" and not plan.get("run_node_ids") and selected_node_id:
            plan["run_node_ids"] = [selected_node_id]
    if plan.get("intent") == "edit_graph" and not message_asks_to_run(text, run_new_nodes=run_new_nodes):
        plan["run_node_ids"] = []

    _emit(progress, ACTIVITY_KIND_TOOL_CALL, "designer_graph_patch", tool="designer_graph_patch")
    next_graph, run_ids, summary = apply_leader_plan(graph, plan)
    changed = next_graph is not graph and next_graph.get("updated_at") != graph.get("updated_at")
    if not changed:
        # apply_graph_patch always writes updated_at; compare node/edge identity.
        changed = (next_graph.get("nodes") != graph.get("nodes")) or (
            next_graph.get("edges") != graph.get("edges")
        )
    result = {
        "intent": plan.get("intent"),
        "summary": summary,
        "graph": next_graph,
        "run_node_ids": run_ids,
        "changed": changed or bool(plan.get("prompt_updates")),
        "updated_at": utc_now_ms(),
    }
    _emit(progress, ACTIVITY_KIND_STAGE, summary or "done", tool="")
    return result
