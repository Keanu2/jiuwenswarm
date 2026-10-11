# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Director chat: canvas follow-up turns handled by the same Director that composed the graph.

Each step is a standalone function over an explicit graph snapshot so it can later be
registered as a team-leader tool: ``director_chat_context`` → ``plan_chat_turn`` →
``edit_graph`` → ``sync_documents``. ``run_director_chat`` chains them for one turn.
"""

from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from typing import Any, Callable

from jiuwenswarm.common.schema.designer_graph import (
    ACTIVITY_KIND_STAGE,
    ACTIVITY_KIND_THINKING,
    ACTIVITY_KIND_TOOL_CALL,
    PIPELINES,
    PIPELINE_TO_TYPE,
    DesignerExecutionGraph,
    DesignerGraphNode,
    DesignerGraphValidationError,
    apply_graph_patch,
    infer_pipeline_from_id,
    node_pipeline,
    node_clip_index,
    utc_now_ms,
)

from jiuwenswarm.server.runtime.designer.chat_document_sync import (
    ChatDocument,
    ChatDocumentConflict,
    graph_content_changed,
    prepare_document_update,
    validate_shot_topology,
)

from jiuwenswarm.server.runtime.designer.chat_document_plan import plan_document_edits
from jiuwenswarm.server.runtime.designer.edit_scope import (
    restore_document_attribute_scope,
    restore_graph_attribute_scope,
)

logger = logging.getLogger(__name__)

ProgressFn = Callable[..., None]

_RUN_HINT = re.compile(
    r"(生成|重跑|重生成|运行|run\b|generate|rerun|regenerate)",
    re.I,
)

_DIRECTOR_CHAT_SYSTEM = """You are the Designer Director. You authored this project's brief, storyboard and
execution graph; now you handle the user's follow-up requests on the canvas. Reply with a JSON object only.
director_context holds your earlier decisions (director skill, scenario skill, brief notes,
spatial, style, aspect, and language locks). Keep edits consistent with them unless the user asks to change them.
Canvas node type and config.role must be one of: text, table, image, video, audio.
Character/Scene/Clip/Film are pipelines, never node kinds.
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
  "thinking": "brief rationale: affected IDs, removed reference targets, and concrete details that must stay unchanged",
  "patch": {
    "description": "updated creative request, only when changed",
    "upsert_nodes": [{"id": "affected_node_id", "config": {"prompt": "complete updated generation prompt"}}],
    "upsert_edges": [],
    "remove_node_ids": [],
    "remove_edge_ids": []
  },
  "remove_shot_ids": [],
  "prompt_updates": [],
  "edit_documents": false,
  "run_node_ids": []
}

Rules:
- edit_graph: change topology. Leave run_node_ids empty unless the user asked to generate/run.
- refine_node: edit the existing node. Leave run_node_ids empty unless the user explicitly asks to generate/run.
- answer: return intent, summary and thinking only. Omit patch and prompt_updates entirely; edit_documents=false. A run-only request may list run_node_ids.
- Return ONE upsert_nodes entry per affected node, containing all its config changes. Leave prompt_updates empty. For existing nodes, config fields merge by ID. Include all required generation fields specified below even when some values stay unchanged; other unchanged fields may be omitted.
- Input and output use the SAME config paths. The effective generation prompt is config.prompt;
  update it there. The server also updates the execution prompt from that value. pipeline and
  review_fields_if_action_changes describe the input; do not copy them into the output node.
- Preserve existing node IDs and unrelated content. Renumber clip_index consecutively after inserting/deleting a clip; update each affected timeline, action, camera and prompt.
- Set edit_documents=true for content edits, including requests that only change prose. A separate document editor will receive the original documents and the applied graph changes. Do not return text replacements in this plan.
- Resolve the requested object before editing. Singular referents and background collections are distinct: e.g. changing one bicycle does not change other parked bicycles of the same color. Do not globally substitute color/material words in a sentence or record. In thinking, identify specific similar objects/details you will preserve.
- For deleting an ENTIRE CLIP, put an existing clip ID in remove_shot_ids. Use the
  supplied clips inventory to resolve the target. The server removes ALL clip nodes
  belonging to that original clip and their edges. Do not rely on remove_node_ids or your
  summary to express a whole-clip deletion. Use patch.remove_node_ids for explicit individual
  node deletions and requested exclusive scene cleanup; never delete shared dependencies.
  Leave remove_shot_ids empty when deleting only a scene/image/video node, keeping its clip.
- When deleting a clip, REMOVE each continuity claim that refers to it. Do not replace its number with the previous or next surviving number. A surviving clip does not inherit the deleted clip's events or props. Renumber references only if their original target survives.
- Keep description and brief/storyboard prompts consistent with the edited request. Do not create/remove brief/storyboard nodes unless explicitly requested; never edit output references or artifact paths.
- Update generation prompts of affected downstream nodes when changing a character, scene or explicit dependency.
- Generation uses the detailed config as well as prompt. Keep camera, cast_actions, blocking, scene_specs, start_state, end_state, pose_holds, spatial_lock, costume_lock, continuity_lock, relationship_lock and director_task consistent with the edit, preserving unrelated details. Nested records replace the entire field; include unchanged members.
- Camera descriptions may name the edited subject or prop. Update those details in config.camera while preserving framing, angle, focus target and camera motion unless the user asks to change them.
- Whenever clip_action changes, explicitly include every field in that node's review_fields_if_action_changes in upsert_nodes.config. Return each complete field with updated action/prop details, preserving unrelated members. These fields feed generation directly; do not leave hidden old action/prop details. Whenever a scene prompt or scene_specs changes, explicitly include both its complete scene_specs and updated config.prompt. For clips, return config.prompt when it is listed.
- Costume/identity settings merge by field like other config. Update costume_lock when the request changes clothing or accessories; otherwise omit it to keep the original value. An action or prop edit alone does not require restating unchanged clothing.
- positioning_lock, action_lock, occupancy.cast_actions, identity_refs mirrors and scene architecture prompts are derived by the server. Do not author these copies. Only return pose_holds for custom holds; Opening hold / Opening facing entries are rebuilt from start_state.
- Before finishing each node entry, check its review_fields_if_action_changes list against the config keys you returned. If you changed clip_action, every listed key MUST be present with its complete value, including spatial_lock. Review nested landmarks, poses and cameras for the requested object as well as the main action.
- Pure layout/label edits use edit_documents=false. Never generate media just to edit text.
"""


_ACTION_DEPENDENT_FIELDS = (
    "camera", "cast_actions", "blocking", "start_state", "end_state", "scene_specs",
    "spatial_lock", "continuity_lock", "director_task",
)

_CHAT_EDITABLE_CONFIG_FIELDS = {
    "prompt", "clip_index", "clip_action", "camera", "timeline", "shot_title",
    "character_id", "character_ids", "setting_id", "on_screen", "offscreen",
    "cast_actions", "scene_specs", "speech_line", "continuity_lock",
    "blocking", "start_state", "end_state", "pose_holds", "spatial_lock",
    "costume_lock", "relationship_lock", "director_task",
    "inputs", "character_node_ids", "scene_node_id", "identity_refs",
    "continuity_clip_node_id", "previous_clip_node_id",
}


_DONT_RUN = re.compile(
    r"(先别|(?:不要|别|不用|无需|暂不|不需要|先不)\s*(?:重新)?(?:生成|运行|重跑|跑)|without (?:running|generating)|don'?t (?:run|generate)|do not (?:run|generate))",
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


def _action_review_fields(config: dict[str, Any]) -> list[str]:
    fields = [key for key in _ACTION_DEPENDENT_FIELDS if config.get(key)]
    if config.get("prompt") or (config.get("generate") or {}).get("prompt"):
        fields.append("prompt")
    return fields


def _chat_node_context(node: DesignerGraphNode) -> dict[str, Any]:
    """Expose the execution prompt at the same config path accepted by chat edits."""
    config = node.get("config") or {}
    editable_config = {key: value for key, value in config.items() if key in _CHAT_EDITABLE_CONFIG_FIELDS}
    prompt = (config.get("generate") or {}).get("prompt") or config.get("prompt")
    if prompt:
        editable_config["prompt"] = prompt
    return {
        "id": node["id"],
        "type": node["type"],
        "label": node.get("label"),
        "pipeline": node_pipeline(node),
        "review_fields_if_action_changes": _action_review_fields(config),
        "config": editable_config,
    }


def _stamp_missing_node_type(node: dict[str, Any]) -> None:
    """Fill canvas type for a new chat node the model described only by id.

    Existing nodes already carry type from the saved graph. Inserted shots such
    as n_scene_4 / n_clip_4 often omit it, and normalize_node rejects that
    before the rest of the edit is saved.
    """
    if str(node.get("type") or "").strip():
        return
    config = node.get("config") if isinstance(node.get("config"), dict) else {}
    pipeline = str(config.get("pipeline") or "").strip()
    if pipeline not in PIPELINES:
        role = str(config.get("role") or "").strip()
        pipeline = role if role in PIPELINES else infer_pipeline_from_id(str(node.get("id") or ""))
    node_type = PIPELINE_TO_TYPE.get(pipeline, "")
    if not node_type:
        node_id = str(node.get("id") or "").strip() or "new node"
        raise DesignerGraphValidationError(
            f"node.type must be a non-empty string ({node_id})"
        )
    node["type"] = node_type
    if not str(config.get("pipeline") or "").strip():
        config["pipeline"] = pipeline
    node["config"] = config


def _merge_prompt_updates(graph: DesignerExecutionGraph, plan: dict[str, Any]) -> dict[str, Any]:
    patch = deepcopy(plan.get("patch") or {})
    existing = {node["id"]: node for node in graph.get("nodes", [])}
    upserts = list(patch.get("upsert_nodes") or [])
    for update in plan.get("prompt_updates") or []:
        node_id = update.get("node_id")
        if node_id not in existing and not any(n.get("id") == node_id for n in upserts):
            raise DesignerGraphValidationError(f"Unknown prompt update node: {node_id}")
        config = {key: update[key] for key in ("prompt", "clip_index", "clip_action", "camera", "timeline") if key in update}
        upserts.append({"id": node_id, "config": config})
    merged: dict[str, Any] = {}
    supplied: dict[str, set[str]] = {}
    for update in upserts:
        node_id = update["id"]
        supplied.setdefault(node_id, set()).update(update.get("config") or {})
        old = merged.get(node_id) or existing.get(node_id) or {}
        node = {**deepcopy(old), **update}
        config = {**deepcopy(old.get("config") or {}), **(update.get("config") or {})}
        if "generate" in (update.get("config") or {}):
            config["generate"] = {**(old.get("config", {}).get("generate") or {}), **config["generate"]}
        if "prompt" in (update.get("config") or {}):
            config["generate"] = {**(config.get("generate") or {}), "prompt": config["prompt"], "prompt_origin": "user"}
        if node.get("output_ref") != old.get("output_ref"):
            raise DesignerGraphValidationError("Chat plans cannot change artifact references")
        node["config"] = config
        _stamp_missing_node_type(node)
        merged[node_id] = node
    for node_id, node in merged.items():
        old_cfg = existing.get(node_id, {}).get("config") or {}
        cfg = node["config"]
        required = set()
        if cfg.get("clip_action") != old_cfg.get("clip_action"):
            required.update(_action_review_fields(old_cfg))
        if node_pipeline(node) == "scene" and any(
            cfg.get(key) != old_cfg.get(key) for key in ("prompt", "scene_specs")
        ):
            required.update(key for key in ("scene_specs", "prompt") if key in _action_review_fields(old_cfg))
        if missing := required - supplied[node_id]:
            raise DesignerGraphValidationError(f"Updated node {node_id} must include its generation details: {', '.join(sorted(missing))}")
    if merged:
        patch["upsert_nodes"] = list(merged.values())
    return patch


def _shot_removal_patch(graph: DesignerExecutionGraph, patch: dict[str, Any], shot_ids: list[str]) -> dict[str, Any]:
    """Expand explicit whole-clip targets using the original stable identities."""
    shots = {node["id"]: node_clip_index(node) for node in graph["nodes"]
             if node_pipeline(node) == "clip"}
    unknown = set(shot_ids) - shots.keys()
    if unknown:
        raise DesignerGraphValidationError(f"Clip deletion requires current clip IDs: {sorted(unknown)}")
    indices = {shots[node_id] for node_id in shot_ids}
    removed = {node_id for node_id, index in shots.items() if index in indices}
    return {**patch, "remove_node_ids": sorted(set(patch.get("remove_node_ids", [])) | removed)}


def apply_chat_plan(
    graph: DesignerExecutionGraph,
    plan: dict[str, Any],
) -> tuple[DesignerExecutionGraph, list[str], str]:
    intent = str(plan.get("intent") or "answer").strip() or "answer"
    summary = str(plan.get("summary") or "").strip()
    patch = _merge_prompt_updates(graph, plan)
    if plan.get("remove_shot_ids"):
        patch = _shot_removal_patch(graph, patch, plan["remove_shot_ids"])
    has_patch = bool(patch)
    next_graph = apply_graph_patch(graph, patch) if has_patch else graph
    raw_run_ids = plan.get("run_node_ids") or []
    run_ids = [str(item).strip() for item in raw_run_ids if str(item).strip()]
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
    if not isinstance(plan, dict) or plan.get("intent") not in {"edit_graph", "refine_node", "answer"}:
        raise DesignerGraphValidationError("Director returned an invalid editing plan; please retry")
    for key, kind in (("patch", dict), ("prompt_updates", list), ("run_node_ids", list), ("edit_documents", bool)):
        if key in plan and not isinstance(plan[key], kind):
            raise DesignerGraphValidationError(f"Director returned invalid {key}")
    shot_ids = plan.get("remove_shot_ids", [])
    if not isinstance(shot_ids, list) or any(not isinstance(item, str) or not item.strip() for item in shot_ids):
        raise DesignerGraphValidationError("remove_shot_ids must be an array of current clip IDs")
    intent = str(plan.get("intent") or "answer").strip()
    if intent not in {"edit_graph", "refine_node", "answer"}:
        intent = "answer"
    patch = {
        key: value for key, value in plan.get("patch", {}).items()
        if key not in {"upsert_nodes", "upsert_edges", "remove_node_ids", "remove_edge_ids"} or value != []
    }
    # Some models fill optional schema slots with empty values even for an answer.
    if intent == "answer" and patch.get("description") == "":
        patch.pop("description")
    run_ids = plan.get("run_node_ids") if isinstance(plan.get("run_node_ids"), list) else []
    prompt_updates = plan.get("prompt_updates") if isinstance(plan.get("prompt_updates"), list) else []
    return {
        "intent": intent,
        "summary": str(plan.get("summary") or "").strip(),
        "thinking": str(plan.get("thinking") or "").strip(),
        "patch": patch,
        "remove_shot_ids": shot_ids,
        "prompt_updates": prompt_updates,
        "edit_documents": plan.get("edit_documents", False),
        "run_node_ids": [str(item).strip() for item in run_ids if str(item).strip()],
    }


_DIRECTOR_CONTEXT_TEXT_LIMITS = (
    ("director_skill", ("director_skill_excerpt", "active_director_skill"), 2000),
    ("scenario_skill", ("scenario_skill_excerpt",), 2500),
    ("brief_notes", ("director_brief_notes",), 1500),
)


def director_chat_context(graph: DesignerExecutionGraph) -> dict[str, Any]:
    """Bootstrap-time Director decisions that later chat turns must stay consistent with."""
    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    context: dict[str, Any] = {}
    for name, keys, limit in _DIRECTOR_CONTEXT_TEXT_LIMITS:
        text = next((str(meta.get(key) or "").strip() for key in keys if str(meta.get(key) or "").strip()), "")
        if text:
            context[name] = text[:limit]
    for lock_name in ("spatial_lock", "style_lock", "aspect_lock"):
        lock = meta.get(lock_name)
        if not isinstance(lock, dict) or not lock:
            lock = analysis.get(lock_name)
        if isinstance(lock, dict) and lock:
            context[lock_name] = lock
    language_lock = str(meta.get("language_lock") or analysis.get("language_lock") or "").strip()
    if language_lock:
        context["language_lock"] = language_lock
    return context


async def plan_chat_turn(
    graph: DesignerExecutionGraph,
    message: str,
    *,
    documents: dict[str, ChatDocument],
    selected_node_id: str = "",
) -> dict[str, Any]:
    """Ask the Director model for a sanitized edit plan; does not mutate the graph."""
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
        "director_context": director_chat_context(graph),
        "user_canvas_edits": list(meta.get("user_canvas_edits") or [])[-20:],
        "description": graph.get("description", ""),
        "documents": [{"node_id": doc.node_id, "pipeline": doc.pipeline, "text": doc.text} for doc in documents.values()],
        "clips": [
            {"index": index, "node_ids": [node["id"] for node in graph["nodes"]
                                        if node_pipeline(node) == "clip" and node_clip_index(node) == index]}
            for index in sorted({node_clip_index(node) for node in graph["nodes"]
                                 if node_pipeline(node) == "clip"})
        ],
        "nodes": [_chat_node_context(node) for node in graph["nodes"]],
        "edges": [
            {"id": edge.get("id"), "source": edge.get("source"), "target": edge.get("target")}
            for edge in graph.get("edges") or []
        ],
        "user": message,
    }
    try:
        result = await call_model_tool(
            prompt=json.dumps(snapshot, ensure_ascii=False),
            system=_DIRECTOR_CHAT_SYSTEM,
            max_tokens=16384,
        )
        text = model_text_or_raise(result)
    except DesignerLlmError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.info("Director chat model call failed", exc_info=True)
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


def edit_graph(
    graph: DesignerExecutionGraph,
    plan: dict[str, Any],
    message: str,
    *,
    pending_documents: bool = False,
) -> tuple[DesignerExecutionGraph, list[str], str]:
    """Apply a sanitized plan to a copy of ``graph`` within the user's requested edit scope."""
    if plan.get("intent") == "answer" and any(plan.get(key) for key in ("patch", "remove_shot_ids", "prompt_updates", "edit_documents")):
        raise DesignerGraphValidationError("An answer cannot also modify the workflow")

    patch = plan.get("patch") or {}
    if pending_documents and (
        plan.get("edit_documents") or plan.get("prompt_updates") or plan.get("remove_shot_ids")
        or "description" in patch
        or any(patch.get(key) for key in ("remove_node_ids", "remove_edge_ids", "upsert_edges"))
        or any(set(node) - {"id", "label", "layout"} for node in patch.get("upsert_nodes", []))
    ):
        raise ChatDocumentConflict("大纲或分镜有待选择版本，请先保留原版或采用新版，再重试编辑。")
    next_graph, run_ids, summary = apply_chat_plan(deepcopy(graph), plan)
    return restore_graph_attribute_scope(graph, next_graph, message), run_ids, summary


async def sync_documents(
    graph: DesignerExecutionGraph,
    next_graph: DesignerExecutionGraph,
    documents: dict[str, ChatDocument],
    message: str,
    *,
    edit_documents: bool,
    progress: ProgressFn | None = None,
) -> tuple[DesignerExecutionGraph, dict[str, str], bool]:
    """Bring brief/storyboard texts in line with ``next_graph``; returns (graph, texts, changed)."""
    text_edits = []
    if edit_documents or graph_content_changed(graph, next_graph):
        validate_shot_topology(next_graph)
        remaining_ids = {node["id"] for node in next_graph["nodes"]}
        remaining_documents = {
            key: doc for key, doc in documents.items() if key in remaining_ids and doc.text
        }
        if remaining_documents:
            _emit(progress, ACTIVITY_KIND_STAGE, "Director · synchronizing the complete brief and storyboard")
            next_graph, text_edits = await plan_document_edits(graph, next_graph, remaining_documents, message)
    next_graph, texts, changed = prepare_document_update(graph, next_graph, documents, text_edits)
    next_graph = restore_graph_attribute_scope(graph, next_graph, message)
    texts = restore_document_attribute_scope(
        graph,
        {key: doc.text for key, doc in documents.items()},
        texts,
        message,
    )
    return next_graph, texts, changed


async def run_director_chat(
    graph: DesignerExecutionGraph,
    message: str,
    *,
    documents: dict[str, ChatDocument],
    selected_node_id: str = "",
    run_new_nodes: bool = False,
    progress: ProgressFn | None = None,
    pending_documents: bool = False,
) -> dict[str, Any]:
    text = str(message or "").strip()
    _emit(progress, ACTIVITY_KIND_THINKING, "Director · reading the canvas and current documents")
    plan = await plan_chat_turn(graph, text, selected_node_id=selected_node_id, documents=documents)
    _emit(progress, ACTIVITY_KIND_THINKING, plan.get("thinking") or "Director · preparing workflow edits")
    if not message_asks_to_run(text, run_new_nodes=run_new_nodes):
        plan["run_node_ids"] = []
    next_graph, run_ids, summary = edit_graph(graph, plan, text, pending_documents=pending_documents)
    next_graph, texts, changed = await sync_documents(
        graph,
        next_graph,
        documents,
        text,
        edit_documents=bool(plan.get("edit_documents")),
        progress=progress,
    )
    if changed:
        _emit(progress, ACTIVITY_KIND_TOOL_CALL, "Director · validated workflow edits; preparing to save", tool="designer_graph_patch")
    return {
        "intent": plan["intent"],
        "summary": summary,
        "graph": next_graph,
        "texts": texts,
        "run_node_ids": run_ids,
        "changed": changed,
        "updated_at": utc_now_ms(),
    }
