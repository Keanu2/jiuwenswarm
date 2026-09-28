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
_CONNECT_HINT = re.compile(r"(接到|连到|connect(?:\s+to)?)", re.I)
_VIDEO_HINT = re.compile(r"(视频|镜头|clip|video)", re.I)

_LEADER_SYSTEM = """You are the invisible Designer Leader. Reply with a JSON object only.
Canvas node type and config.role must be one of: text, table, image, video, audio.
Character/Scene/Keyframe/Clip/Film are pipelines, never node kinds.
Do not rebuild the whole graph. Patch only what the user asked.
Do not create audio nodes. Audio generation is not implemented; users add and upload audio on the canvas.
Do not wire a new node into clip/compose unless the user asked to connect it.
user_canvas_edits is the user's canvas log: add, remove, connect, disconnect, replace.
Treat that log as fact. Do not recreate a removed node, restore a disconnected edge,
or undo a replaced output. Do not connect an added node unless the user asked.
connect and disconnect name node_id and peer_id. replace names the node whose output the user changed.

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
) -> dict[str, Any]:
    from jiuwenswarm.server.runtime.designer.model_tools import (
        DesignerLlmError,
        LLM_API_ERROR,
        call_model_tool,
        model_text_or_raise,
    )
    from jiuwenswarm.server.runtime.designer.script_analysis import _extract_json_object

    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    snapshot = {
        "selected_node_id": selected_node_id,
        "user_canvas_edits": list(meta.get("user_canvas_edits") or [])[-20:],
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
        text = model_text_or_raise(result)
    except DesignerLlmError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.info("Leader chat model call failed", exc_info=True)
        raise DesignerLlmError(
            f"Chat model request failed while planning canvas edits: {exc}",
            code=LLM_API_ERROR,
        ) from exc
    parsed = _extract_json_object(text)
    plan = _sanitize_plan(parsed)
    if plan.get("intent") == "answer" and not str(plan.get("summary") or "").strip():
        raise DesignerLlmError(
            "Chat model did not return a usable canvas edit plan.",
            code=LLM_API_ERROR,
        )
    return plan


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
    thinking = str(plan.get("thinking") or "applying graph edits")
    _emit(progress, ACTIVITY_KIND_THINKING, thinking)
    if plan.get("intent") == "edit_graph" and not message_asks_to_run(
        text, run_new_nodes=run_new_nodes
    ):
        plan["run_node_ids"] = []
    if plan.get("intent") == "refine_node" and not plan.get("run_node_ids") and selected_node_id:
        plan["run_node_ids"] = [selected_node_id]
    if plan.get("intent") == "edit_graph" and not message_asks_to_run(
        text, run_new_nodes=run_new_nodes
    ):
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
