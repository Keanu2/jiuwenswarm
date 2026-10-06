# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Supervisor / Manager / per-node agents for Designer runs."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    AssetRef,
    DesignerExecutionGraph,
    DesignerGraphNode,
)
from jiuwenswarm.server.runtime.designer.continuity import (
    continuity_prompt_clause as _continuity_prompt_clause,
    infer_continuity_lock as _infer_continuity_lock,
    merge_lock_with_previous,
)
from jiuwenswarm.server.runtime.designer.feedback import (
    save_feedback,
    suggestion_for_node,
)
from jiuwenswarm.server.runtime.designer.model_tools import (
    call_model_tool,
    list_configured_models,
)

logger = logging.getLogger(__name__)

# Role → tool set for one-pass node agents (no feedback loop).
_ROLE_TOOLS: dict[str, list[str]] = {
    "brief": ["call_model", "read_upstream"],
    "character": ["call_model", "read_upstream", "call_image_model"],
    "character_design": ["call_model", "read_upstream", "call_image_model"],
    "scene": ["call_model", "read_upstream", "call_image_model"],
    "storyboard": ["call_model", "read_upstream"],
    "frame": ["call_model", "read_upstream", "call_image_model"],
    "keyframe": ["call_model", "read_upstream", "call_image_model"],
    "clip": ["call_model", "read_upstream", "call_video_model"],
    "speech": ["call_model", "read_upstream", "call_speech_model"],
    "music": ["call_model", "read_upstream", "call_music_model"],
    "audio": ["call_model", "read_upstream", "call_speech_model", "call_music_model"],
    "compose": ["call_model", "read_upstream", "ffmpeg_compose", "mix_audio"],
}


def _role_key(node: DesignerGraphNode) -> str:
    cfg = node.get("config") or {}
    role = str(cfg.get("role") or node.get("type") or "").strip().lower()
    return role


def _tools_for_node(node: DesignerGraphNode) -> list[str]:
    cfg = node.get("config") or {}
    existing = cfg.get("tools")
    if isinstance(existing, list) and existing:
        return [str(t) for t in existing]
    role = _role_key(node)
    if role in _ROLE_TOOLS:
        return list(_ROLE_TOOLS[role])
    ntype = str(node.get("type") or "").lower()
    if ntype == "image":
        return ["call_model", "read_upstream", "call_image_model"]
    if ntype == "video":
        return ["call_model", "read_upstream", "call_video_model"]
    if "speech" in role or "tts" in role:
        return list(_ROLE_TOOLS["speech"])
    if "music" in role or "bgm" in role:
        return list(_ROLE_TOOLS["music"])
    if "compose" in role or "mix" in role or "film" in role:
        return list(_ROLE_TOOLS["compose"])
    return ["call_model", "read_upstream"]


def _spatial_continuity_patch(graph: DesignerExecutionGraph) -> list[str]:
    """Manager gate: stamp continuity locks so keyframe/clip prompts share blocking."""
    notes: list[str] = []
    meta = dict(graph.get("metadata") or {})
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    shot_locks: dict[int, dict[str, str]] = {}

    for shot in analysis.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        try:
            idx = int(shot.get("shot_index") or 0)
        except (TypeError, ValueError):
            idx = 0
        if idx < 1:
            continue
        action = str(shot.get("action") or shot.get("keyframe_prompt") or "")
        lock = merge_lock_with_previous(
            _infer_continuity_lock(action), shot_locks.get(idx - 1)
        )
        shot["continuity_lock"] = lock
        shot_locks[idx] = lock
        clause = _continuity_prompt_clause(lock)
        kf = str(shot.get("keyframe_prompt") or action)
        if clause and "CONTINUITY LOCK" not in kf:
            shot["keyframe_prompt"] = (kf[:500] + clause)[:700]
            notes.append(f"analysis shot {idx}: continuity lock stamped")

    if analysis.get("shots"):
        meta["script_analysis"] = analysis
        graph["metadata"] = meta

    for node in graph.get("nodes") or []:
        cfg = dict(node.get("config") or {})
        role = str(cfg.get("role") or "")
        if role not in {"frame", "clip", "keyframe", "storyboard"}:
            continue
        idx = int(cfg.get("shot_index") or 0) or 0
        action = str(cfg.get("shot_action") or "")
        lock = dict(cfg.get("continuity_lock") or {}) if isinstance(cfg.get("continuity_lock"), dict) else {}
        if idx and idx in shot_locks:
            lock = shot_locks[idx]
        elif action and not lock:
            lock = _infer_continuity_lock(action)
        if not lock:
            continue
        cfg["continuity_lock"] = lock
        clause = _continuity_prompt_clause(lock)
        gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
        prompt = str(gen.get("prompt") or "").strip()
        if clause and "CONTINUITY LOCK" not in prompt:
            if not prompt:
                camera = str(cfg.get("camera") or "medium / eye-level")
                prompt = f"Film shot {idx or '?'} only. Camera {camera}. Action: {action}."
            gen["prompt"] = (prompt + clause)[:1200]
            cfg["generate"] = gen
            notes.append(f"{node.get('id')}: continuity lock in generate.prompt")
        elif role == "storyboard":
            notes.append(f"{node.get('id')}: continuity locks available for planned shots")
        node["config"] = cfg

    # Refresh storyboard draft with continuity column when present.
    if shot_locks:
        try:
            from jiuwenswarm.server.runtime.designer.smart_graph import (
                _write_storyboard_markdown,
            )

            shots = list((analysis.get("shots") or []))
            characters = list((analysis.get("characters") or []))
            if shots:
                sb_md = _write_storyboard_markdown(shots, characters)
                for node in graph.get("nodes") or []:
                    cfg = dict(node.get("config") or {})
                    if str(cfg.get("role") or "") != "storyboard":
                        continue
                    if cfg.get("skip_llm"):
                        cfg["prewritten"] = sb_md
                    else:
                        cfg["draft_prewritten"] = sb_md
                    cfg["planned_shots"] = shots
                    node["config"] = cfg
        except Exception:  # noqa: BLE001
            logger.info("storyboard continuity refresh skipped", exc_info=True)

    meta = dict(graph.get("metadata") or {})
    meta["continuity_locks"] = {str(k): v for k, v in shot_locks.items()}
    graph["metadata"] = meta
    return notes


def _ensure_audio_nodes_for_intent(graph: DesignerExecutionGraph) -> list[str]:
    """If audio intent / analysis asks for speech or music, ensure nodes exist."""
    from jiuwenswarm.common.schema.designer_graph import NODE_TYPE_AUDIO

    notes: list[str] = []
    meta = dict(graph.get("metadata") or {})
    audio = dict(meta.get("audio_intent") or {})
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    analysis_audio = analysis.get("audio") if isinstance(analysis.get("audio"), dict) else {}
    want_speech = bool(audio.get("include_speech") or analysis_audio.get("include_speech"))
    # Explicit False on either intent or analysis wins (StrictGate / speech-only).
    explicit_no_music = (
        audio.get("include_music") is False or analysis_audio.get("include_music") is False
    )
    policy = str(audio.get("policy") or analysis_audio.get("policy") or "")
    want_music = (not explicit_no_music) and bool(
        audio.get("include_music")
        or analysis_audio.get("include_music")
        or policy in {"optional_music", "music", "speech_and_music"}
    )
    if policy in {"silent", "speech"} and explicit_no_music:
        want_music = False
    if policy == "silent":
        return notes

    nodes = list(graph.get("nodes") or [])
    edges = list(graph.get("edges") or [])
    existing = {str(n.get("id") or "") for n in nodes}
    brief_id = "n_brief" if "n_brief" in existing else None
    compose_id = "n_compose" if "n_compose" in existing else ("n_final" if "n_final" in existing else None)
    mode = str(meta.get("optimize_for") or "quality")

    def _add(node_id: str, label: str, role: str, skill_id: str, tool: str) -> None:
        nonlocal notes
        if node_id in existing:
            return
        nodes.append(
            {
                "id": node_id,
                "type": NODE_TYPE_AUDIO,
                "label": label,
                "config": {
                    "role": role,
                    "prompt": graph.get("description") or "",
                    "inputs": [brief_id] if brief_id else [],
                    "optimize_for": mode,
                    "agent_name": f"{label} Agent",
                    "kind": "agent",
                    "skill_id": skill_id,
                    "modality": "audio",
                    "delegate": "handler",
                    "force_handler": True,
                    "tools": [tool, "read_upstream", "call_model"],
                    "duration_sec": 4 if mode == "cost" else 6,
                },
                "layout": {"x": 1280.0, "y": 720.0 if role == "music" else 560.0, "width": 240, "height": 120},
            }
        )
        existing.add(node_id)
        if brief_id:
            edges.append({"id": f"e_{brief_id}_{node_id}", "source": brief_id, "target": node_id})
        if compose_id:
            edges.append({"id": f"e_{node_id}_{compose_id}", "source": node_id, "target": compose_id})
            for node in nodes:
                if node.get("id") != compose_id:
                    continue
                cfg = dict(node.get("config") or {})
                inputs = list(cfg.get("inputs") or [])
                if node_id not in inputs:
                    inputs.append(node_id)
                cfg["inputs"] = inputs
                node["config"] = cfg
        notes.append(f"added {node_id} for audio intent")

    if want_speech:
        _add("n_speech", "Speech / TTS", "speech", "speech_tts", "call_speech_model")
    if want_music:
        _add("n_music", "Music / BGM", "music", "audio_bed", "call_music_model")

    if notes:
        graph["nodes"] = nodes
        graph["edges"] = edges
        meta["audio_nodes"] = [
            nid for nid in ("n_speech", "n_music") if nid in existing
        ]
        graph["metadata"] = meta
    return notes


def assign_audio_node_agents(graph: DesignerExecutionGraph) -> dict[str, Any]:
    """Supervisor: promote speech/music to fast LLM agents when backends exist.

    Without TTS/music backends, keeps ``force_handler`` (local bed/notes) so the
    pipeline stays fast — but speech/music nodes still exist when intent asks.
    """
    from jiuwenswarm.server.runtime.designer.capabilities import detect_audio_backends

    ensured = _ensure_audio_nodes_for_intent(graph)
    backends = detect_audio_backends()
    can_speech = bool(backends.get("can_speech"))
    can_music = bool(backends.get("can_music"))
    assigned: list[str] = []

    for node in graph.get("nodes") or []:
        cfg = dict(node.get("config") or {})
        role = str(cfg.get("role") or "").lower()
        nid = str(node.get("id") or "")
        if role in {"speech", "tts"} or nid == "n_speech":
            cfg["role"] = "speech"
            cfg["skill_id"] = cfg.get("skill_id") or "speech_tts"
            cfg["tools"] = ["call_model", "read_upstream", "call_speech_model"]
            if can_speech:
                cfg["force_handler"] = False
                cfg["delegate"] = "agent"
                cfg["supervisor_task"] = (
                    cfg.get("supervisor_task")
                    or "Write concise spoken lines from brief/storyboard, then call_speech_model "
                    "to synthesize TTS. Keep under 8s; sync to film beats. Skip if silent policy."
                )
                assigned.append(f"{nid}:speech_agent")
            else:
                cfg["force_handler"] = True
                cfg["delegate"] = "handler"
                cfg["supervisor_task"] = (
                    "Speech requested but no TTS backend — fast handler placeholder. "
                    "When TTS_API_KEY / models.speech is configured, supervisor will assign an agent."
                )
                assigned.append(f"{nid}:speech_handler_fallback")
            node["config"] = cfg
        elif role in {"music", "audio", "audio_bed"} or nid == "n_music":
            cfg["role"] = "music"
            cfg["skill_id"] = cfg.get("skill_id") or "audio_bed"
            cfg["tools"] = ["call_model", "read_upstream", "call_music_model"]
            if can_music:
                cfg["force_handler"] = False
                cfg["delegate"] = "agent"
                cfg["supervisor_task"] = (
                    cfg.get("supervisor_task")
                    or "Compose a short non-vocal BGM bed matching mood; call_music_model. "
                    "Keep headroom for speech; ≤8s unless compose needs longer."
                )
                assigned.append(f"{nid}:music_agent")
            else:
                cfg["force_handler"] = True
                cfg["delegate"] = "handler"
                cfg["supervisor_task"] = (
                    "Music requested but no music backend — fast local bed/notes. "
                    "When MUSIC_API_KEY / models.music is configured, supervisor will assign an agent."
                )
                assigned.append(f"{nid}:music_handler_fallback")
            node["config"] = cfg

    meta = dict(graph.get("metadata") or {})
    meta["supervisor_audio_assignment"] = {
        "can_speech": can_speech,
        "can_music": can_music,
        "assigned": assigned,
        "ensured_nodes": ensured,
        "backends": backends,
    }
    graph["metadata"] = meta
    return meta["supervisor_audio_assignment"]


def _pick_model(models: list[dict[str, Any]], *, optimize_for: str, prefer_image: bool = False) -> str:
    if not models:
        return ""
    ordered = list(models)
    if optimize_for == "cost":
        ordered = list(reversed(ordered))
    if prefer_image:
        for m in ordered:
            name = f"{m.get('id') or ''} {m.get('model_name') or ''}".lower()
            if any(k in name for k in ("image", "qwen-image", "flux", "sdxl", "wan")):
                return str(m.get("id") or "")
    for m in ordered:
        if m.get("is_default"):
            return str(m.get("id") or "")
    return str(ordered[0].get("id") or "")


def _clamp_score(value: Any, default: int = 5) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(0, min(10, score))


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Delegate to shared robust parser (fences + first object + trailing text)."""
    from jiuwenswarm.server.runtime.designer.script_analysis import (
        _extract_json_object as _shared_extract_json_object,
    )

    return _shared_extract_json_object(text)


class SupervisorAgent:
    """Assigns tasks / tools / models for every node agent (one-pass, no loop)."""

    def plan_fast(
        self,
        graph: DesignerExecutionGraph,
        *,
        optimize_for: str,
        prior_feedback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Deterministic plan — no LLM. Use prior_feedback only when caller opts in (rerun)."""
        models = list_configured_models()
        model_ids = [str(m.get("id")) for m in models]
        nodes = graph.get("nodes") or []
        mode = "cost" if optimize_for == "cost" else "quality"
        directives: dict[str, Any] = {}
        for node in nodes:
            nid = str(node.get("id") or "")
            if not nid:
                continue
            role = _role_key(node)
            label = str(node.get("label") or nid)
            tools = _tools_for_node(node)
            modality_agents = ((graph.get("metadata") or {}).get("modality_plan") or {}).get(
                "agents"
            ) or {}
            mod_entry = modality_agents.get(nid) if isinstance(modality_agents, dict) else None
            if isinstance(mod_entry, dict) and isinstance(mod_entry.get("tools"), list):
                tools = [str(t) for t in mod_entry["tools"] if str(t).strip()]
            prefer_image = any("image" in t for t in tools) or role in {
                "character",
                "character_design",
                "scene",
                "frame",
                "keyframe",
            }
            preferred = _pick_model(models, optimize_for=mode, prefer_image=prefer_image)
            if preferred and preferred not in model_ids and model_ids:
                preferred = model_ids[0]
            task = f"Execute {label} ({role or node.get('type')}) with tools {', '.join(tools)}"
            if prior_feedback:
                hint = suggestion_for_node(prior_feedback, nid)
                if hint:
                    task = f"{task}. Rerun constraint: {hint}"
            rating_mod = str(
                (mod_entry or {}).get("rating_modality")
                or ((graph.get("metadata") or {}).get("rating_modality"))
                or "text_only"
            )
            directives[nid] = {
                "optimize_for": mode,
                "preferred_model": preferred,
                "task": task,
                "tools": tools,
                "rating_modality": rating_mod,
            }
        rating_global = str(
            ((graph.get("metadata") or {}).get("modality_plan") or {}).get(
                "global_rating_modality"
            )
            or (graph.get("metadata") or {}).get("rating_modality")
            or "text_only"
        )
        notes = (
            f"One-pass fast plan; rating_modality={rating_global}. "
            "Tools/models assigned per node from manager capability decision when present."
        )
        if prior_feedback:
            final = prior_feedback.get("final") or {}
            plan_hint = str(final.get("improvement_plan") or final.get("summary") or "")
            if plan_hint:
                notes = f"{notes} Rerun guidance: {plan_hint[:800]}"
        plan = {
            "optimize_for_global": mode,
            "node_directives": directives,
            "notes": notes,
            "planner_model": "deterministic",
            "one_pass": True,
            "rating_modality": rating_global,
        }
        for node in nodes:
            nid = str(node.get("id") or "")
            cfg = dict(node.get("config") or {})
            d = directives.get(nid) or {}
            cfg["optimize_for"] = d.get("optimize_for", mode)
            cfg["preferred_model"] = d.get("preferred_model", "")
            cfg["supervisor_task"] = d.get("task", "")
            cfg["tools"] = d.get("tools") or _tools_for_node(node)
            cfg["rating_modality"] = d.get("rating_modality") or rating_global
            cfg["kind"] = "agent"
            if prior_feedback:
                hint = suggestion_for_node(prior_feedback, nid)
                if hint:
                    cfg["rerun_suggestion"] = hint
            node["config"] = cfg
        meta = dict(graph.get("metadata") or {})
        meta["supervisor_plan"] = plan
        meta["one_pass"] = True
        if rating_global:
            meta["rating_modality"] = rating_global
        graph["metadata"] = meta
        audio_assign = assign_audio_node_agents(graph)
        plan["audio_assignment"] = audio_assign
        meta = dict(graph.get("metadata") or {})
        meta["supervisor_plan"] = plan
        graph["metadata"] = meta
        return plan

    def adjust_clips_after_keyframes(
        self,
        graph: DesignerExecutionGraph,
        *,
        node_states: dict[str, Any] | None,
        agent_feedback: dict[str, dict[str, Any]] | None = None,
    ) -> list[str]:
        """Once after all keyframes complete: rewrite pending clip prompts from shot + frame text."""
        notes = _shot_distinctness_patch(graph)
        feedback = agent_feedback or {}
        states = node_states or {}
        for node in graph.get("nodes") or []:
            cfg = dict(node.get("config") or {})
            if str(cfg.get("role") or "") != "clip":
                continue
            if str((states.get(str(node.get("id") or "")) or {}).get("status") or "") in {
                "completed",
                "failed",
                "skipped",
            }:
                continue
            idx = int(cfg.get("shot_index") or 0) or 1
            frame_id = f"n_frame_{idx}"
            frame_msg = str((feedback.get(frame_id) or {}).get("message") or "")
            action = str(cfg.get("shot_action") or "").strip()
            camera = str(cfg.get("camera") or "medium / eye-level")
            gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
            lock = cfg.get("continuity_lock") if isinstance(cfg.get("continuity_lock"), dict) else {}
            clause = _continuity_prompt_clause(lock if isinstance(lock, dict) else None)
            gen["prompt"] = (
                f"Film shot {idx} only from its keyframe. Camera {camera}. "
                f"Action: {action or 'follow keyframe'}. "
                f"Keyframe note: {frame_msg[:180]}. "
                "Do not repeat other shots."
                f"{clause}"
            )
            cfg["generate"] = gen
            cfg["max_video_calls"] = 1
            if idx > 1:
                cfg["continuity_frame_node_id"] = cfg.get("continuity_frame_node_id") or f"n_frame_{idx - 1}"
            node["config"] = cfg
            notes.append(f"{node.get('id')}: post-keyframe clip brief updated")
        meta = dict(graph.get("metadata") or {})
        meta["clips_adjusted_after_keyframes"] = True
        meta["supervisor_keyframe_adjust"] = {"notes": notes[:40], "rating_modality": "text_only"}
        graph["metadata"] = meta
        return notes

    async def plan(
        self,
        graph: DesignerExecutionGraph,
        *,
        optimize_for: str,
        prior_feedback: dict[str, Any] | None,
        use_llm: bool = False,
    ) -> dict[str, Any]:
        # Default: fast deterministic plan. LLM plan only when explicitly requested (rare).
        if not use_llm:
            return self.plan_fast(
                graph, optimize_for=optimize_for, prior_feedback=prior_feedback
            )
        models = list_configured_models()
        model_ids = [str(m.get("id")) for m in models]
        nodes = graph.get("nodes") or []
        global_summary = ""
        if prior_feedback:
            final = prior_feedback.get("final") or {}
            global_summary = str(final.get("summary") or final.get("improvement_plan") or "")

        system = (
            "You are the Designer Supervisor Agent. "
            "One forward pass only (no loops). "
            "From the user prompt, ensure a detailed brief covering character consistency, "
            "scene consistency, motion consistency, and continuity. "
            "Assign each leaf node a concrete task + tools so agents produce real media "
            "(images/video/audio), not markdown stubs. "
            "Coordinate node agents in a ComfyUI-like pipeline. "
            "Follow the scenario skill and audio policy. "
            "For each node, choose optimize_for (cost|quality) and a preferred_model "
            "from the configured Settings model list. "
            "For speech/music nodes: if TTS/music backends exist, assign an agent task "
            "with call_speech_model / call_music_model; otherwise note handler fallback. "
            "On rerun, incorporate prior feedback suggestions. "
            "Respond with JSON only: "
            '{"optimize_for_global":"cost|quality",'
            '"brief_notes":"...",'
            '"node_directives":{"<node_id>":{"optimize_for":"...","preferred_model":"...","task":"..."}},'
            '"notes":"..."}'
        )
        prompt = json.dumps(
            {
                "user_prompt": graph.get("description"),
                "optimize_for_default": optimize_for,
                "available_models": model_ids,
                "scenario_skill": str((graph.get("metadata") or {}).get("scenario_skill_excerpt") or "")[
                    :2500
                ],
                "supervisor_skill": str(
                    (graph.get("metadata") or {}).get("supervisor_skill_excerpt")
                    or (graph.get("metadata") or {}).get("active_supervisor_skill")
                    or ""
                )[:2000],
                "audio_intent": (graph.get("metadata") or {}).get("audio_intent"),
                "nodes": [
                    {
                        "id": n.get("id"),
                        "label": n.get("label"),
                        "type": n.get("type"),
                        "agent": (n.get("config") or {}).get("agent_name"),
                        "prior_suggestion": suggestion_for_node(
                            prior_feedback, str(n.get("id") or "")
                        ),
                    }
                    for n in nodes
                ],
                "prior_global_summary": global_summary,
            },
            ensure_ascii=False,
        )
        result = await call_model_tool(
            prompt=prompt,
            system=system,
            optimize_for=optimize_for,
            max_tokens=1200,
        )
        parsed = _extract_json_object(str(result.get("text") or "")) or {}
        directives: dict[str, Any] = {}
        raw_dirs = parsed.get("node_directives") if isinstance(parsed, dict) else None
        if isinstance(raw_dirs, dict):
            directives = raw_dirs
        for node in nodes:
            nid = str(node.get("id") or "")
            if not nid:
                continue
            entry = directives.get(nid) if isinstance(directives.get(nid), dict) else {}
            mode = str(entry.get("optimize_for") or parsed.get("optimize_for_global") or optimize_for)
            mode = "cost" if mode == "cost" else "quality"
            preferred = str(entry.get("preferred_model") or "")
            if preferred and preferred not in model_ids and model_ids:
                preferred = model_ids[0]
            elif not preferred and model_ids:
                preferred = model_ids[0] if mode == "quality" else model_ids[-1]
            tools = _tools_for_node(node)
            directives[nid] = {
                "optimize_for": mode,
                "preferred_model": preferred,
                "task": str(entry.get("task") or f"Execute node {node.get('label') or nid}"),
                "tools": tools,
            }
        plan = {
            "optimize_for_global": (
                "cost"
                if str(parsed.get("optimize_for_global") or optimize_for) == "cost"
                else "quality"
            ),
            "node_directives": directives,
            "notes": str(parsed.get("notes") or result.get("text") or "")[:2000],
            "brief_notes": str(parsed.get("brief_notes") or "")[:2000],
            "planner_model": result.get("model"),
        }
        for node in nodes:
            nid = str(node.get("id") or "")
            cfg = dict(node.get("config") or {})
            d = directives.get(nid) or {}
            cfg["optimize_for"] = d.get("optimize_for", optimize_for)
            cfg["preferred_model"] = d.get("preferred_model", "")
            cfg["supervisor_task"] = d.get("task", "")
            cfg["tools"] = d.get("tools") or _tools_for_node(node)
            cfg["kind"] = "agent"
            node["config"] = cfg
        meta = dict(graph.get("metadata") or {})
        meta["supervisor_plan"] = plan
        if plan.get("brief_notes"):
            meta["supervisor_brief_notes"] = plan["brief_notes"]
        graph["metadata"] = meta
        audio_assign = assign_audio_node_agents(graph)
        plan["audio_assignment"] = audio_assign
        meta = dict(graph.get("metadata") or {})
        meta["supervisor_plan"] = plan
        graph["metadata"] = meta
        return plan


class NodeAgent:
    """One agent per graph node; tools: call_model + read_upstream."""

    async def run(
        self,
        node: DesignerGraphNode,
        *,
        graph: DesignerExecutionGraph,
        run_id: str,
        upstream_outputs: dict[str, AssetRef | None] | None,
        prior_feedback: dict[str, Any] | None,
    ) -> dict[str, Any]:
        cfg = dict(node.get("config") or {})
        node_id = str(node.get("id") or "")
        optimize_for = str(cfg.get("optimize_for") or "quality")
        preferred = str(cfg.get("preferred_model") or "") or None
        suggestion = suggestion_for_node(prior_feedback, node_id)
        upstream = upstream_outputs or {}
        upstream_summary = {
            k: (v or {}) for k, v in upstream.items() if isinstance(k, str)
        }

        system = (
            f"You are {cfg.get('agent_name') or 'a Designer node agent'} "
            f"(id={cfg.get('agent_id') or node_id}). "
            "You may use tools conceptually: call_model (any Settings model) and read_upstream. "
            "Produce the artifact for your node. "
            "End with JSON: "
            '{"self_score":0-10,"notes":"...","suggestion_for_next":"...","artifact_summary":"..."}'
        )
        prompt = json.dumps(
            {
                "node_id": node_id,
                "label": node.get("label"),
                "type": node.get("type"),
                "user_prompt": cfg.get("prompt") or graph.get("description"),
                "supervisor_task": cfg.get("supervisor_task"),
                "optimize_for": optimize_for,
                "preferred_model": preferred,
                "available_tools": cfg.get("tools") or ["call_model", "read_upstream"],
                "upstream": upstream_summary,
                "rerun_suggestion": suggestion,
            },
            ensure_ascii=False,
        )
        tool = await call_model_tool(
            prompt=prompt,
            system=system,
            optimize_for=optimize_for,
            preferred_model=preferred,
            max_tokens=1000,
        )
        parsed = _extract_json_object(str(tool.get("text") or "")) or {}
        self_score = _clamp_score(parsed.get("self_score"), default=6)
        notes = str(parsed.get("notes") or "")
        suggestion_next = str(parsed.get("suggestion_for_next") or "")
        artifact = str(parsed.get("artifact_summary") or tool.get("text") or "")[:4000]

        output_ref: AssetRef = {
            "kind": str(node.get("type") or "text"),
            "uri": f"designer://agent/{run_id}/{node_id}",
            "label": str(node.get("label") or node_id),
            "mime_type": "application/json",
        }
        return {
            "output_ref": output_ref,
            "message": f"{cfg.get('agent_name') or node_id} via {tool.get('model')}",
            "feedback": {
                "agent_id": cfg.get("agent_id"),
                "agent_name": cfg.get("agent_name"),
                "self_score": self_score,
                "notes": notes,
                "suggestion_for_next": suggestion_next,
                "model_used": tool.get("model"),
                "artifact_summary": artifact,
            },
            "payload": {
                "tool_result": tool,
                "artifact_summary": artifact,
            },
        }


def _heuristic_node_score(
    node_id: str,
    *,
    agent_feedback: dict[str, dict[str, Any]],
    node_states: dict[str, Any] | None,
) -> tuple[int, str]:
    fb = agent_feedback.get(node_id) or {}
    if "self_score" in fb:
        return _clamp_score(fb.get("self_score"), 6), str(fb.get("notes") or "")
    state = (node_states or {}).get(node_id) or {}
    status = str(state.get("status") or "")
    if status == "completed" and state.get("output_ref"):
        return 7, "Completed with output"
    if status == "completed":
        return 6, "Completed"
    if status == "failed":
        return 2, str(state.get("error") or "failed")
    return 5, status or "unknown"


def _shot_distinctness_patch(graph: DesignerExecutionGraph) -> list[str]:
    """Ensure each clip/frame has unique shot_action + generate prompt (text-only)."""
    notes: list[str] = []
    camera_cycle = (
        "wide / establishing",
        "medium / eye-level",
        "close-up / eye-level",
        "medium / slow pan",
    )
    for node in graph.get("nodes") or []:
        cfg = dict(node.get("config") or {})
        role = str(cfg.get("role") or "")
        if role not in {"frame", "clip", "keyframe"}:
            continue
        idx = int(cfg.get("shot_index") or 0) or 1
        action = str(cfg.get("shot_action") or "").strip()
        camera = str(cfg.get("camera") or "").strip() or camera_cycle[(idx - 1) % len(camera_cycle)]
        cfg["camera"] = camera
        if not action:
            action = f"Distinct beat for shot {idx}"
            cfg["shot_action"] = action
            notes.append(f"{node.get('id')}: filled missing shot_action")
        gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
        prompt = str(gen.get("prompt") or "").strip()
        marker = f"shot {idx}"
        cast_names = [str(x) for x in (cfg.get("cast_names") or []) if str(x).strip()]
        cast_who = ", ".join(cast_names)
        lock = cfg.get("continuity_lock") if isinstance(cfg.get("continuity_lock"), dict) else None
        if not lock and action:
            lock = _infer_continuity_lock(action)
            cfg["continuity_lock"] = lock
        clause = _continuity_prompt_clause(lock if isinstance(lock, dict) else None)
        if not prompt or marker not in prompt.lower():
            gen["prompt"] = (
                f"Film {marker} only. Camera {camera}. Action: {action}. "
                + (f"Focus cast on screen: {cast_who}. " if cast_who else "")
                + "Must differ from sibling shots."
                + clause
            )
            cfg["generate"] = gen
            notes.append(f"{node.get('id')}: refreshed generate.prompt")
        elif clause and "CONTINUITY LOCK" not in prompt:
            gen["prompt"] = (prompt + clause)[:1200]
            cfg["generate"] = gen
            notes.append(f"{node.get('id')}: appended continuity lock")
        if role == "clip":
            cfg["max_video_calls"] = 1
            if idx > 1 and not cfg.get("continuity_frame_node_id"):
                cfg["continuity_frame_node_id"] = f"n_frame_{idx - 1}"
                notes.append(f"{node.get('id')}: linked continuity from prior keyframe")
        if role in {"frame", "keyframe"}:
            cfg["max_image_calls"] = 1
        node["config"] = cfg
    return notes


def _cast_focus_alignment_patch(graph: DesignerExecutionGraph) -> list[str]:
    """Manager gate: re-score shot focus from action text so wrong cast is not reused."""
    from jiuwenswarm.server.runtime.designer.script_analysis import (
        _focus_character_ids,
        _match_terms_for_character,
    )

    notes: list[str] = []
    meta = dict(graph.get("metadata") or {})
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    characters = list(analysis.get("characters") or [])
    if not characters:
        return notes
    for ch in characters:
        if isinstance(ch, dict) and not ch.get("match_terms"):
            ch["match_terms"] = _match_terms_for_character(
                str(ch.get("name") or ""), str(ch.get("description") or "")
            )
    id_to_name = {
        str(c.get("id")): str(c.get("name") or c.get("id"))
        for c in characters
        if isinstance(c, dict) and c.get("id")
    }
    # Refresh analysis shots if present. Do not expand a tight focus into a
    # full-cast keyword match (floods I2V reference images → DashScope fails).
    for shot in analysis.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        blob = f"{shot.get('action') or ''} {shot.get('keyframe_prompt') or ''}"
        focus = _focus_character_ids(blob, characters)
        old = [str(x) for x in (shot.get("character_ids") or []) if str(x)]
        if focus and focus != old:
            old_set, new_set = set(old), set(focus)
            if old and old_set.issubset(new_set) and len(new_set) > len(old_set):
                continue
            notes.append(
                f"analysis shot {shot.get('shot_index')}: "
                f"{shot.get('character_ids')} -> {focus}"
            )
            shot["character_ids"] = focus
    meta["script_analysis"] = analysis

    # Refresh prewritten storyboard so the UI table matches corrected focus cast.
    try:
        from jiuwenswarm.server.runtime.designer.smart_graph import _write_storyboard_markdown

        planned = list(analysis.get("shots") or [])
        if planned:
            sb_md = _write_storyboard_markdown(planned, characters)
            for node in graph.get("nodes") or []:
                cfg = dict(node.get("config") or {})
                if str(cfg.get("role") or "") != "storyboard":
                    continue
                cfg["prewritten"] = sb_md
                cfg["planned_shots"] = planned
                node["config"] = cfg
                notes.append(f"{node.get('id')}: storyboard refreshed from cast-focus fixes")
    except Exception:  # noqa: BLE001
        pass

    for node in graph.get("nodes") or []:
        cfg = dict(node.get("config") or {})
        role = str(cfg.get("role") or "")
        if role not in {"frame", "clip", "keyframe"}:
            continue
        blob = f"{cfg.get('shot_action') or ''} {(cfg.get('generate') or {}).get('prompt') or ''}"
        focus = _focus_character_ids(blob, characters)
        if not focus:
            continue
        old = [str(x) for x in (cfg.get("character_ids") or [])]
        if focus != old:
            old_set, new_set = set(old), set(focus)
            # Keep tighter original when keyword match only expands the cast.
            if old and old_set.issubset(new_set) and len(new_set) > len(old_set):
                continue
            cfg["character_ids"] = focus
            cfg["cast_names"] = [id_to_name.get(cid, cid) for cid in focus]
            notes.append(f"{node.get('id')}: cast focus {old} -> {focus}")
            # Prefer SOLO identity sheets (one character_id) over combined compose aids.
            solo_nodes: list[str] = []
            for cid in focus:
                for other in graph.get("nodes") or []:
                    if not isinstance(other, dict):
                        continue
                    oc = other.get("config") if isinstance(other.get("config"), dict) else {}
                    if str(oc.get("role") or "") != "character_design":
                        continue
                    if oc.get("combined_cast"):
                        continue
                    oids = [str(x) for x in (oc.get("character_ids") or []) if str(x)]
                    if not oids and oc.get("character_id"):
                        oids = [str(oc.get("character_id"))]
                    if oids == [cid]:
                        solo_nodes.append(str(other.get("id")))
                        break
            if solo_nodes and len(solo_nodes) == len(focus):
                cfg["character_node_ids"] = list(dict.fromkeys(solo_nodes))
            else:
                char_nodes = []
                for other in graph.get("nodes") or []:
                    if not isinstance(other, dict):
                        continue
                    oc = other.get("config") if isinstance(other.get("config"), dict) else {}
                    if str(oc.get("role") or "") != "character_design":
                        continue
                    if oc.get("combined_cast"):
                        continue
                    oids = [str(x) for x in (oc.get("character_ids") or []) if str(x)]
                    if not oids and oc.get("character_id"):
                        oids = [str(oc.get("character_id"))]
                    if set(oids) & set(focus):
                        char_nodes.append(str(other.get("id")))
                if char_nodes:
                    cfg["character_node_ids"] = list(dict.fromkeys(char_nodes))
            costume_parts = [
                f"{id_to_name.get(cid, cid)}: "
                + next(
                    (
                        str((o.get("config") or {}).get("costume_lock") or "")
                        for o in (graph.get("nodes") or [])
                        if str(o.get("id")) in (cfg.get("character_node_ids") or [])
                        and str(((o.get("config") or {}).get("character_id") or "")) == cid
                    ),
                    str(
                        next(
                            (
                                c.get("description") or ""
                                for c in characters
                                if str(c.get("id")) == cid
                            ),
                            "",
                        )
                    )[:120],
                )
                for cid in focus
            ]
            costume_lock = "; ".join(p for p in costume_parts if p).strip("; ")[:480]
            identity_refs = {
                "character_ids": list(focus),
                "character_node_ids": list(cfg.get("character_node_ids") or []),
                "cast_names": list(cfg.get("cast_names") or []),
                "costume_lock": costume_lock,
                "scene_node_id": "n_scene",
                "prior_keyframe_node_id": cfg.get("prior_keyframe_node_id"),
                "keyframe_strategy": cfg.get("keyframe_strategy") or "compose_from_solo_refs",
            }
            cfg["identity_refs"] = identity_refs
            cfg["costume_lock"] = costume_lock
            cfg["supervisor_task"] = (
                f"Use identity_refs sheets {identity_refs['character_node_ids']} "
                f"({', '.join(cfg.get('cast_names') or [])}). "
                f"Costume lock: {costume_lock}. Do not redesign wardrobe."
            )
            node["config"] = cfg
    graph["metadata"] = meta
    return notes


def _identity_consistency_patch(graph: DesignerExecutionGraph) -> list[str]:
    """Manager gate: every frame/clip must point at solo identity sheets + costume lock."""
    notes: list[str] = []
    solo_by_cid: dict[str, str] = {}
    costume_by_cid: dict[str, str] = {}
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        if str(cfg.get("role") or "") != "character_design":
            continue
        if cfg.get("combined_cast"):
            continue
        oids = [str(x) for x in (cfg.get("character_ids") or []) if str(x)]
        if not oids and cfg.get("character_id"):
            oids = [str(cfg.get("character_id"))]
        if len(oids) == 1:
            solo_by_cid[oids[0]] = str(node.get("id"))
            costume_by_cid[oids[0]] = str(cfg.get("costume_lock") or cfg.get("prompt") or "")[:200]

    meta = dict(graph.get("metadata") or {})
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    characters = list(analysis.get("characters") or [])
    id_to_name = {
        str(c.get("id")): str(c.get("name") or c.get("id"))
        for c in characters
        if isinstance(c, dict) and c.get("id")
    }

    prev_frame: str | None = None
    prev_camera = ""
    from jiuwenswarm.server.runtime.designer.smart_graph import _cameras_compatible

    frame_nodes = sorted(
        [
            n
            for n in (graph.get("nodes") or [])
            if str((n.get("config") or {}).get("role") or "") in {"frame", "keyframe"}
        ],
        key=lambda n: int((n.get("config") or {}).get("shot_index") or 0),
    )

    for node in graph.get("nodes") or []:
        cfg = dict(node.get("config") or {})
        role = str(cfg.get("role") or "")
        if role not in {"frame", "clip", "keyframe"}:
            continue
        cids = [str(x) for x in (cfg.get("character_ids") or []) if str(x)]
        if not cids:
            continue
        solo_nodes = [solo_by_cid[cid] for cid in cids if cid in solo_by_cid]
        if solo_nodes and list(cfg.get("character_node_ids") or []) != solo_nodes:
            cfg["character_node_ids"] = solo_nodes
            notes.append(f"{node.get('id')}: identity_refs -> solo sheets {solo_nodes}")
        names = [id_to_name.get(cid, cid) for cid in cids]
        costume_lock = str(cfg.get("costume_lock") or "").strip()
        if not costume_lock:
            costume_lock = "; ".join(
                f"{id_to_name.get(cid, cid)}: {costume_by_cid.get(cid, '')[:120]}".strip(": ")
                for cid in cids
            )[:480]
            cfg["costume_lock"] = costume_lock
            notes.append(f"{node.get('id')}: costume_lock stamped")

        camera = str(cfg.get("camera") or "")
        prior = None
        strategy = "compose_from_solo_refs"
        if role in {"frame", "keyframe"}:
            idx = int(cfg.get("shot_index") or 0)
            if prev_frame and _cameras_compatible(prev_camera, camera):
                prior = prev_frame
                strategy = "edit_prior_keyframe"
                cfg["prior_keyframe_node_id"] = prior
                inputs = list(cfg.get("inputs") or [])
                if prior not in inputs:
                    inputs.append(prior)
                cfg["inputs"] = inputs
                notes.append(f"{node.get('id')}: sequential edit from {prior}")
            cfg["keyframe_strategy"] = strategy
            prev_frame = str(node.get("id") or "")
            prev_camera = camera

        identity_refs = {
            "character_ids": cids,
            "character_node_ids": list(cfg.get("character_node_ids") or solo_nodes),
            "cast_names": names or list(cfg.get("cast_names") or []),
            "costume_lock": costume_lock,
            "scene_node_id": str(
                (cfg.get("identity_refs") or {}).get("scene_node_id")
                if isinstance(cfg.get("identity_refs"), dict)
                else "n_scene"
            )
            or "n_scene",
            "prior_keyframe_node_id": cfg.get("prior_keyframe_node_id") or prior,
            "keyframe_strategy": cfg.get("keyframe_strategy") or strategy,
        }
        cfg["identity_refs"] = identity_refs
        cfg["cast_names"] = identity_refs["cast_names"]
        if not cfg.get("supervisor_task"):
            cfg["supervisor_task"] = (
                f"Use character sheets {identity_refs['character_node_ids']} as reference_images. "
                f"Costume lock: {costume_lock}. Strategy={identity_refs['keyframe_strategy']}."
            )
        # Strengthen generate prompt with identity lock if missing.
        gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
        prompt = str(gen.get("prompt") or "")
        if costume_lock and "Costume lock" not in prompt and "IDENTITY" not in prompt:
            gen["prompt"] = (prompt + f" IDENTITY sheets={identity_refs['character_node_ids']}. Costume lock: {costume_lock}.")[:1200]
            cfg["generate"] = gen
            notes.append(f"{node.get('id')}: identity clause in generate.prompt")
        node["config"] = cfg

    # Unused frame_nodes var was for ordering - we iterate all nodes; fine.
    _ = frame_nodes
    plan = dict(meta.get("consistency_plan") or {})
    plan.update(
        {
            "character_identity": "solo_sheets_first",
            "multi_shot_compose": "compose_from_solo_refs",
            "sequential_keyframe": "edit_prior_when_camera_compatible",
            "costume_lock": True,
            "validated_by_manager": True,
        }
    )
    meta["consistency_plan"] = plan
    graph["metadata"] = meta
    return notes


class ManagerAgent:
    """Rates supervisor + all nodes; decides modality from models/tools at run start."""

    def decide_capabilities(self, graph: DesignerExecutionGraph) -> dict[str, Any]:
        """Inspect chat/vision backends and assign per-agent tools + rating modality."""
        from jiuwenswarm.server.runtime.designer.capabilities import decide_modality_plan

        plan = decide_modality_plan(graph)
        meta = dict(graph.get("metadata") or {})
        meta["manager_capability_decision"] = {
            "global_rating_modality": plan.get("global_rating_modality"),
            "can_vision": plan.get("can_vision"),
            "can_video": plan.get("can_video"),
            "reason": plan.get("reason"),
            "vision_backend": plan.get("vision_backend"),
        }
        graph["metadata"] = meta
        return plan

    def validate_plan_fast(self, graph: DesignerExecutionGraph) -> dict[str, Any]:
        """One-time start gate: modality + shot distinctness + cast + spatial continuity."""
        meta = dict(graph.get("metadata") or {})
        modality = meta.get("modality_plan")
        if not isinstance(modality, dict) or not modality.get("global_rating_modality"):
            modality = self.decide_capabilities(graph)
            meta = dict(graph.get("metadata") or {})
        cast_notes = _cast_focus_alignment_patch(graph)
        notes = _shot_distinctness_patch(graph)
        continuity_notes = _spatial_continuity_patch(graph)
        identity_notes = _identity_consistency_patch(graph)
        from jiuwenswarm.server.runtime.designer.smart_graph import (
            ensure_combined_cast_reach_compose,
            prune_non_contributing_nodes,
        )
        from jiuwenswarm.server.runtime.designer.model_tools import llm_available

        # Quality path: prune orphans; legacy combined-cast wire only if any remain.
        pruned = prune_non_contributing_nodes(graph)
        cast_wire_notes = ensure_combined_cast_reach_compose(graph)
        agent_notes = self._enforce_leaf_agents(graph, use_agents=llm_available())
        # Re-apply audio agent policy after modality (backends may clear force_handler).
        audio_assign = assign_audio_node_agents(graph)
        rating_mod = str(
            (modality or {}).get("global_rating_modality")
            or meta.get("rating_modality")
            or "text_only"
        )
        ack = {
            "ok": True,
            "patched": notes
            + cast_notes
            + continuity_notes
            + identity_notes
            + cast_wire_notes
            + [f"pruned:{x}" for x in pruned]
            + agent_notes,
            "cast_focus_fixes": cast_notes,
            "continuity_fixes": continuity_notes,
            "identity_fixes": identity_notes,
            "cast_wire_fixes": cast_wire_notes,
            "pruned_nodes": pruned,
            "agent_enforcement": agent_notes,
            "audio_assignment": audio_assign,
            "rating_modality": rating_mod,
            "can_vision": bool((modality or {}).get("can_vision")),
            "can_video": bool((modality or {}).get("can_video")),
            "can_speech": bool((modality or {}).get("can_speech")),
            "can_music": bool((modality or {}).get("can_music")),
            "notes": (
                f"Manager start validation: rating_modality={rating_mod}. "
                f"Cast-focus fixes={len(cast_notes)}. "
                f"Continuity locks={len(continuity_notes)}. "
                f"Identity locks={len(identity_notes)}. "
                f"Pruned={len(pruned)}. Agents={len(agent_notes)}. "
                f"{str((modality or {}).get('reason') or '')[:400]}"
            ),
            "source": "heuristic",
        }
        meta = dict(graph.get("metadata") or {})
        meta["manager_plan_ack"] = ack
        graph["metadata"] = meta
        return ack

    def _enforce_leaf_agents(
        self, graph: DesignerExecutionGraph, *, use_agents: bool
    ) -> list[str]:
        """Ensure every contributing leaf is an agent with tools (handlers only if forced/no LLM)."""
        notes: list[str] = []
        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            cfg = dict(node.get("config") or {})
            role = str(cfg.get("role") or "")
            nid = str(node.get("id") or "")
            if not nid:
                continue
            cfg["kind"] = "agent"
            tools = list(cfg.get("tools") or [])
            if not tools:
                if role in {"clip", "compose"}:
                    tools = ["call_video_model", "read_upstream", "call_model"]
                elif role in {"frame", "character", "character_design", "scene"}:
                    tools = ["call_image_model", "read_upstream", "call_model"]
                elif role in {"speech"}:
                    tools = ["call_speech_model", "read_upstream", "call_model"]
                elif role in {"music"}:
                    tools = ["call_music_model", "read_upstream", "call_model"]
                else:
                    tools = ["call_model", "write_artifact", "read_upstream"]
                cfg["tools"] = tools
                notes.append(f"tools:{nid}")
            if cfg.get("force_handler"):
                cfg["delegate"] = "handler"
            elif use_agents:
                cfg["delegate"] = "agent"
                cfg["skip_llm"] = False
            else:
                cfg["delegate"] = "handler"
            node["config"] = cfg
        return notes

    async def review_storyboard_once(
        self,
        graph: DesignerExecutionGraph,
        *,
        node_states: dict[str, Any] | None,
        use_llm: bool = False,
    ) -> dict[str, Any]:
        """After storyboard completes: fidelity + enhancement + continuity (one shot, no loop)."""
        meta = dict(graph.get("metadata") or {})
        if meta.get("storyboard_reviewed"):
            return dict(meta.get("manager_storyboard_ack") or {"ok": True, "skipped": True})
        analysis = dict(meta.get("script_analysis") or {}) if isinstance(meta.get("script_analysis"), dict) else {}
        shots = list(analysis.get("shots") or [])
        user_prompt = str(graph.get("description") or "")
        ack: dict[str, Any] = {
            "ok": True,
            "source": "heuristic",
            "notes": "Storyboard continuity + duration pass.",
            "patched": [],
        }
        # Heuristic continuity: mark leave/stand forbids on later shots when earlier action implies it.
        leave_markers = ("leave", "leaves", "stood", "stands up", "gets up", "rising")
        left_chars: list[str] = []
        for shot in shots:
            action = str(shot.get("action") or shot.get("keyframe_prompt") or "").lower()
            if any(m in action for m in leave_markers):
                left_chars.extend([str(x) for x in (shot.get("character_ids") or [])])
        left_chars = list(dict.fromkeys(left_chars))
        patched: list[str] = []
        for shot in shots:
            idx = int(shot.get("shot_index") or 0)
            timeline = str(shot.get("timeline") or "").strip()
            if not timeline:
                shot["timeline"] = f"{(idx - 1) * 5:.1f}-{idx * 5:.1f}s"
                patched.append(f"duration:shot{idx}")
            if left_chars and idx > 1:
                lock = dict(shot.get("continuity_lock") or {}) if isinstance(shot.get("continuity_lock"), dict) else {}
                lock.setdefault(
                    "forbid",
                    "do not reseat or re-show a character who already stood and left earlier",
                )
                lock.setdefault("time", "forward-only continuity with prior beats")
                shot["continuity_lock"] = lock
                patched.append(f"continuity:shot{idx}")
            # Mild enhancement when action is too short.
            action = str(shot.get("action") or "").strip()
            if len(action) < 40 and user_prompt:
                shot["action"] = (
                    f"{action} — enrich atmosphere (crowd reaction, lighting, depth) "
                    f"while staying faithful to: {user_prompt[:160]}"
                )[:500]
                patched.append(f"enhance:shot{idx}")

        if use_llm:
            try:
                system = (
                    "You are the Designer Manager. Review the storyboard once for fidelity to the "
                    "user prompt and approved brief. Fix missing characters/views, enhance sparse "
                    "shots (crowd, atmosphere) without inventing new plot, set shot durations, and "
                    "enforce time-coherent continuity. Respond JSON only: "
                    '{"ok":true,"shot_fixes":[{"shot_index":1,"action":"...","camera":"...",'
                    '"timeline":"0-5s","continuity_lock":{"forbid":"..."},"character_ids":["char_1"]}],'
                    '"notes":"..."}'
                )
                result = await call_model_tool(
                    prompt=json.dumps(
                        {
                            "user_prompt": user_prompt,
                            "shots": shots,
                            "brief_hint": meta.get("supervisor_brief_notes") or "",
                        },
                        ensure_ascii=False,
                    ),
                    system=system,
                    optimize_for="quality",
                    max_tokens=1400,
                )
                parsed = _extract_json_object(str(result.get("text") or "")) or {}
                for fix in parsed.get("shot_fixes") or []:
                    if not isinstance(fix, dict):
                        continue
                    try:
                        idx = int(fix.get("shot_index") or 0)
                    except (TypeError, ValueError):
                        continue
                    for shot in shots:
                        if int(shot.get("shot_index") or 0) != idx:
                            continue
                        for key in ("action", "camera", "timeline", "keyframe_prompt"):
                            if fix.get(key):
                                shot[key] = str(fix[key])[:600]
                        if isinstance(fix.get("continuity_lock"), dict):
                            shot["continuity_lock"] = {
                                str(k): str(v) for k, v in fix["continuity_lock"].items()
                            }
                        if isinstance(fix.get("character_ids"), list):
                            shot["character_ids"] = [str(x) for x in fix["character_ids"] if str(x)]
                        patched.append(f"llm:shot{idx}")
                ack["source"] = "llm"
                ack["notes"] = str(parsed.get("notes") or ack["notes"])[:1000]
            except Exception:  # noqa: BLE001
                logger.info("Manager storyboard LLM review failed; keeping heuristic", exc_info=True)

        if shots:
            analysis["shots"] = shots
            meta["script_analysis"] = analysis
            # Patch storyboard + downstream frame/clip configs once.
            try:
                from jiuwenswarm.server.runtime.designer.smart_graph import (
                    _write_storyboard_markdown,
                )

                characters = list(analysis.get("characters") or [])
                sb_md = _write_storyboard_markdown(shots, characters)
                for node in graph.get("nodes") or []:
                    cfg = dict(node.get("config") or {})
                    role = str(cfg.get("role") or "")
                    if role == "storyboard":
                        if cfg.get("skip_llm"):
                            cfg["prewritten"] = sb_md
                        else:
                            cfg["draft_prewritten"] = sb_md
                        cfg["planned_shots"] = shots
                        node["config"] = cfg
                    elif role in {"frame", "clip", "keyframe"}:
                        idx = int(cfg.get("shot_index") or 0)
                        for shot in shots:
                            if int(shot.get("shot_index") or 0) != idx:
                                continue
                            if shot.get("action"):
                                cfg["shot_action"] = str(shot["action"])[:500]
                            if shot.get("camera"):
                                cfg["camera"] = str(shot["camera"])[:120]
                            if isinstance(shot.get("continuity_lock"), dict):
                                cfg["continuity_lock"] = shot["continuity_lock"]
                            gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
                            if shot.get("action") and gen.get("prompt"):
                                # Append continuity clause if missing.
                                lock = shot.get("continuity_lock") or {}
                                forbid = str(lock.get("forbid") or "")
                                prompt = str(gen.get("prompt") or "")
                                if forbid and forbid not in prompt:
                                    gen["prompt"] = (prompt + f" CONTINUITY LOCK: {forbid}")[:1200]
                                    cfg["generate"] = gen
                            node["config"] = cfg
            except Exception:  # noqa: BLE001
                logger.info("Storyboard patch into nodes failed", exc_info=True)

        ack["patched"] = patched[:40]
        meta["storyboard_reviewed"] = True
        meta["manager_storyboard_ack"] = ack
        graph["metadata"] = meta
        return ack

    async def validate_plan(
        self, graph: DesignerExecutionGraph, *, use_llm: bool = False
    ) -> dict[str, Any]:
        """Validate supervisor brief/shots/graph once. LLM when available; else heuristic."""
        ack = self.validate_plan_fast(graph)
        if not use_llm:
            return ack
        models = list_configured_models()
        if not models:
            return ack
        analysis = (graph.get("metadata") or {}).get("script_analysis") or {}
        system = (
            "You are the Designer Manager Agent. Validate the supervisor cast/shot plan "
            "and brief/storytelling once (no loops). Check: "
            "(1) each shot's character_ids match that beat's focus subjects, "
            "(2) later beats do not reuse the wrong earlier cast, "
            "(3) enough shots cover every major character, "
            "(4) brief/storyboard are comprehensive enough for keyframe and clip prompting, "
            "(5) SPATIAL CONTINUITY: motion direction and geography must stay consistent, "
            "(6) IDENTITY CONSISTENCY: every frame/clip must reference canonical SOLO character "
            "sheets (identity_refs.character_node_ids), not reinvent costumes — e.g. a preacher "
            "cannot be a suit in shot 1 and a white robe in shot 2 unless the brief says so. "
            "Multi-person shots compose from those solo sheets; sequential shots with compatible "
            "cameras should edit the prior keyframe when possible. "
            "Respond JSON only: "
            '{"ok":true|false,"issues":["..."],'
            '"shot_fixes":[{"shot_index":1,"character_ids":["char_1"],'
            '"action":"...","continuity_lock":{"motion":"...","facing":"...","forbid":"..."},'
            '"costume_lock":"...","camera":"..."}],"notes":"..."}'
        )
        prompt = json.dumps(
            {
                "user_prompt": graph.get("description"),
                "script_analysis": analysis,
                "continuity_locks": (graph.get("metadata") or {}).get("continuity_locks"),
                "nodes": [
                    {
                        "id": n.get("id"),
                        "label": n.get("label"),
                        "role": (n.get("config") or {}).get("role"),
                        "character_ids": (n.get("config") or {}).get("character_ids"),
                        "cast_names": (n.get("config") or {}).get("cast_names"),
                        "shot_action": (n.get("config") or {}).get("shot_action"),
                        "camera": (n.get("config") or {}).get("camera"),
                        "continuity_lock": (n.get("config") or {}).get("continuity_lock"),
                        "generate_prompt": ((n.get("config") or {}).get("generate") or {}).get(
                            "prompt"
                        ),
                    }
                    for n in (graph.get("nodes") or [])
                ],
                "heuristic_ack": ack,
            },
            ensure_ascii=False,
        )
        try:
            result = await call_model_tool(
                prompt=prompt,
                system=system,
                optimize_for="quality",
                max_tokens=1400,
            )
            parsed = _extract_json_object(str(result.get("text") or "")) or {}
        except Exception:  # noqa: BLE001
            logger.info("Manager LLM validate_plan failed; keeping heuristic ack", exc_info=True)
            return ack

        # Apply one-shot shot_fixes into analysis + frame/clip configs (no re-loop).
        fixes = parsed.get("shot_fixes") if isinstance(parsed.get("shot_fixes"), list) else []
        applied: list[str] = []
        analysis = dict(analysis) if isinstance(analysis, dict) else {}
        shots = list(analysis.get("shots") or [])
        id_to_name = {
            str(c.get("id")): str(c.get("name") or c.get("id"))
            for c in (analysis.get("characters") or [])
            if isinstance(c, dict) and c.get("id")
        }
        for fix in fixes:
            if not isinstance(fix, dict):
                continue
            try:
                idx = int(fix.get("shot_index") or 0)
            except (TypeError, ValueError):
                continue
            if idx < 1:
                continue
            cids = [str(x) for x in (fix.get("character_ids") or []) if str(x)]
            action = str(fix.get("action") or "").strip()
            lock = fix.get("continuity_lock") if isinstance(fix.get("continuity_lock"), dict) else None
            camera = str(fix.get("camera") or "").strip()
            costume = str(fix.get("costume_lock") or "").strip()
            for shot in shots:
                if int(shot.get("shot_index") or 0) != idx:
                    continue
                if cids:
                    shot["character_ids"] = cids
                if action:
                    shot["action"] = action[:500]
                    shot["keyframe_prompt"] = action[:600]
                if lock:
                    shot["continuity_lock"] = {str(k): str(v) for k, v in lock.items()}
                    clause = _continuity_prompt_clause(shot["continuity_lock"])
                    kf = str(shot.get("keyframe_prompt") or "")
                    if clause and "CONTINUITY LOCK" not in kf:
                        shot["keyframe_prompt"] = (kf + clause)[:700]
                if camera:
                    shot["camera"] = camera[:120]
                if costume:
                    shot["costume_lock"] = costume[:480]
                applied.append(f"shot {idx} llm-fix")
            for node in graph.get("nodes") or []:
                cfg = dict(node.get("config") or {})
                if str(cfg.get("role") or "") not in {"frame", "clip", "keyframe"}:
                    continue
                if int(cfg.get("shot_index") or 0) != idx:
                    continue
                if cids:
                    cfg["character_ids"] = cids
                    cfg["cast_names"] = [id_to_name.get(cid, cid) for cid in cids]
                if action:
                    cfg["shot_action"] = action[:500]
                if camera:
                    cfg["camera"] = camera[:120]
                if costume:
                    cfg["costume_lock"] = costume[:480]
                if lock:
                    cfg["continuity_lock"] = {str(k): str(v) for k, v in lock.items()}
                    gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
                    prompt = str(gen.get("prompt") or cfg.get("shot_action") or "")
                    clause = _continuity_prompt_clause(cfg["continuity_lock"])
                    if clause and "CONTINUITY LOCK" not in prompt:
                        gen["prompt"] = (prompt + clause)[:1200]
                        cfg["generate"] = gen
                node["config"] = cfg
        if shots:
            analysis["shots"] = shots
            meta = dict(graph.get("metadata") or {})
            meta["script_analysis"] = analysis
            graph["metadata"] = meta
            try:
                from jiuwenswarm.server.runtime.designer.smart_graph import (
                    _write_storyboard_markdown,
                )

                characters = list(analysis.get("characters") or [])
                sb_md = _write_storyboard_markdown(shots, characters)
                for node in graph.get("nodes") or []:
                    cfg = dict(node.get("config") or {})
                    if str(cfg.get("role") or "") != "storyboard":
                        continue
                    if cfg.get("skip_llm"):
                        cfg["prewritten"] = sb_md
                    else:
                        cfg["draft_prewritten"] = sb_md
                    cfg["planned_shots"] = shots
                    node["config"] = cfg
            except Exception:  # noqa: BLE001
                pass

        # Re-stamp continuity + identity after LLM fixes.
        continuity_notes = _spatial_continuity_patch(graph)
        identity_notes = _identity_consistency_patch(graph)
        from jiuwenswarm.server.runtime.designer.smart_graph import (
            ensure_combined_cast_reach_compose,
            prune_non_contributing_nodes,
        )

        pruned = prune_non_contributing_nodes(graph)
        cast_wire_notes = ensure_combined_cast_reach_compose(graph)

        ack = {
            **ack,
            "ok": bool(parsed.get("ok", True)),
            "llm_issues": list(parsed.get("issues") or [])[:20],
            "llm_notes": str(parsed.get("notes") or "")[:1000],
            "patched": list(ack.get("patched") or [])
            + applied
            + continuity_notes
            + identity_notes
            + cast_wire_notes
            + [f"pruned:{x}" for x in pruned],
            "continuity_fixes": list(ack.get("continuity_fixes") or []) + continuity_notes,
            "identity_fixes": list(ack.get("identity_fixes") or []) + identity_notes,
            "cast_wire_fixes": list(ack.get("cast_wire_fixes") or []) + cast_wire_notes,
            "pruned_nodes": pruned,
            "source": "llm",
        }
        meta = dict(graph.get("metadata") or {})
        meta["manager_plan_ack"] = ack
        graph["metadata"] = meta
        return ack

    def ack_keyframe_adjustment(
        self, graph: DesignerExecutionGraph, supervisor_notes: list[str]
    ) -> dict[str, Any]:
        ack = {
            "ok": True,
            "supervisor_notes": supervisor_notes[:20],
            "notes": "Manager accepted post-keyframe clip adjustments (once).",
            "rating_modality": str(
                (graph.get("metadata") or {}).get("rating_modality") or "text_only"
            ),
        }
        meta = dict(graph.get("metadata") or {})
        meta["manager_keyframe_ack"] = ack
        graph["metadata"] = meta
        return ack

    def review_fast(
        self,
        graph: DesignerExecutionGraph,
        *,
        agent_feedback: dict[str, dict[str, Any]],
        supervisor_plan: dict[str, Any] | None,
        supervisor_report: dict[str, Any] | None,
        node_states: dict[str, Any] | None,
        optimize_for: str,
        vision_notes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        scores: dict[str, int] = {}
        suggestions: dict[str, str] = {}
        rating_mod = str(
            (graph.get("metadata") or {}).get("rating_modality")
            or ((graph.get("metadata") or {}).get("modality_plan") or {}).get(
                "global_rating_modality"
            )
            or "text_only"
        )
        vision_used = bool(vision_notes)
        for node in graph.get("nodes") or []:
            nid = str(node.get("id") or "")
            if not nid:
                continue
            score, note = _heuristic_node_score(
                nid, agent_feedback=agent_feedback, node_states=node_states
            )
            # Blend supervisor node rating when present
            sup_score = ((supervisor_report or {}).get("scores") or {}).get(nid)
            if sup_score is not None:
                score = _clamp_score(round((score + _clamp_score(sup_score)) / 2), score)
            vnote = (vision_notes or {}).get(nid) or ""
            if vnote:
                low = vnote.lower()
                if any(
                    w in low for w in ("mismatch", "wrong", "unrelated", "blank", "empty")
                ):
                    score = _clamp_score(score - 2, score)
                    note = (note + " | vision: " + vnote[:200]).strip(" |")
                elif any(w in low for w in ("match", "consistent", "clear", "good")):
                    score = _clamp_score(score + 1, score)
            scores[nid] = score
            if score < 6:
                suggestions[nid] = note or "Improve artifact quality on next Run again"
        vals = [v for k, v in scores.items() if k != "overall"]
        overall = int(round(sum(vals) / max(1, len(vals)))) if vals else 6
        # Rate the supervisor plan itself
        plan_notes = str((supervisor_plan or {}).get("notes") or "")
        supervisor_score = 8 if (supervisor_plan or {}).get("node_directives") else 5
        scores["supervisor"] = supervisor_score
        scores["overall"] = overall
        suggestions["global"] = (
            "Stored for Run again only — not applied in this pass. "
            f"Supervisor: {plan_notes[:400]}"
        )
        return {
            "scores": scores,
            "summary": (
                f"Manager review ({rating_mod}"
                f"{', vision used' if vision_used else ''}). "
                f"Overall {overall}/10. Supervisor {supervisor_score}/10. Optimize={optimize_for}."
            )[:3000],
            "pipeline_notes": (
                f"One-pass ratings → runs/ + trajectory. rating_modality={rating_mod}. "
                "Text-only only when models/tools lack image/video understanding."
            ),
            "suggestions": suggestions,
            "manager_model": "heuristic+vision" if vision_used else "heuristic",
            "rates_supervisor": True,
            "rating_modality": rating_mod,
            "vision_used": vision_used,
            "vision_notes": vision_notes or {},
        }

    async def review(
        self,
        graph: DesignerExecutionGraph,
        *,
        agent_feedback: dict[str, dict[str, Any]],
        supervisor_plan: dict[str, Any] | None,
        prior_feedback: dict[str, Any] | None,
        optimize_for: str,
        use_llm: bool = False,
        supervisor_report: dict[str, Any] | None = None,
        node_states: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        vision_notes: dict[str, str] = {}
        modality = (graph.get("metadata") or {}).get("modality_plan") or {}
        if bool(modality.get("can_vision")):
            from jiuwenswarm.server.runtime.designer.capabilities import (
                collect_rateable_image_paths,
                inspect_image_for_rating,
            )

            for nid, path in collect_rateable_image_paths(graph, node_states, limit=2):
                q = (
                    "Rate briefly for a short film pipeline: is this image usable and "
                    "consistent with a cinematic still? Reply in 2 short sentences; "
                    "say match/mismatch/clear/blank if relevant."
                )
                ans = await inspect_image_for_rating(path, q)
                if ans:
                    vision_notes[nid] = ans
        if not use_llm:
            return self.review_fast(
                graph,
                agent_feedback=agent_feedback,
                supervisor_plan=supervisor_plan,
                supervisor_report=supervisor_report,
                node_states=node_states,
                optimize_for=optimize_for,
                vision_notes=vision_notes or None,
            )
        _ = prior_feedback
        system = (
            "You are the Designer Manager Agent. Review the whole pipeline. "
            "Score every node, the supervisor, and overall job 0-10. "
            "Respond JSON only: "
            '{"scores":{"<node_id>":0-10,"supervisor":0-10,"overall":0-10},'
            '"summary":"...","pipeline_notes":"...","suggestions":{"<node_id>":"...","global":"..."}}'
        )
        prompt = json.dumps(
            {
                "user_prompt": graph.get("description"),
                "optimize_for": optimize_for,
                "supervisor_plan": supervisor_plan or {},
                "supervisor_report": supervisor_report or {},
                "agent_feedback": agent_feedback,
                "prior_feedback_final": (prior_feedback or {}).get("final"),
                "vision_notes": vision_notes,
                "rating_modality": (graph.get("metadata") or {}).get("rating_modality"),
            },
            ensure_ascii=False,
        )
        result = await call_model_tool(
            prompt=prompt,
            system=system,
            optimize_for="quality",
            max_tokens=1200,
        )
        parsed = _extract_json_object(str(result.get("text") or "")) or {}
        scores_raw = parsed.get("scores") if isinstance(parsed.get("scores"), dict) else {}
        scores: dict[str, int] = {}
        for node in graph.get("nodes") or []:
            nid = str(node.get("id") or "")
            scores[nid] = _clamp_score(scores_raw.get(nid), default=6)
        scores["supervisor"] = _clamp_score(scores_raw.get("supervisor"), default=6)
        scores["overall"] = _clamp_score(scores_raw.get("overall"), default=6)
        rating_mod = str(
            (graph.get("metadata") or {}).get("rating_modality") or "text_only"
        )
        return {
            "scores": scores,
            "summary": str(parsed.get("summary") or result.get("text") or "")[:3000],
            "pipeline_notes": str(parsed.get("pipeline_notes") or "")[:2000],
            "suggestions": parsed.get("suggestions")
            if isinstance(parsed.get("suggestions"), dict)
            else {},
            "manager_model": result.get("model"),
            "rates_supervisor": True,
            "rating_modality": rating_mod,
            "vision_used": bool(vision_notes),
            "vision_notes": vision_notes,
        }

    async def dual_rate_final(
        self,
        graph: DesignerExecutionGraph,
        *,
        agent_feedback: dict[str, dict[str, Any]],
        supervisor_final: dict[str, Any],
        manager_review: dict[str, Any],
        node_states: dict[str, Any] | None,
        use_llm: bool = False,
    ) -> dict[str, Any]:
        """Assign two independent rating agents; aggregate for Run-again feedback only."""
        base_payload = {
            "user_prompt": graph.get("description"),
            "node_ids": [str(n.get("id")) for n in (graph.get("nodes") or []) if n.get("id")],
            "agent_feedback_keys": list((agent_feedback or {}).keys())[:40],
            "supervisor_summary": (supervisor_final or {}).get("summary"),
            "manager_summary": (manager_review or {}).get("summary"),
            "compose_status": ((node_states or {}).get("n_compose") or {}).get("status"),
        }
        system = (
            "You are an independent Rater Agent for a Designer film pipeline. "
            "Strict Hollywood bar: score overall 0-10 (floats ok). "
            "Rate fidelity to user prompt, identity continuity, motion honesty "
            "(real video not stills), graph design, and per-node prompt/tool quality. "
            "Respond JSON only: "
            '{"overall":0-10,"node_scores":{"<id>":0-10},'
            '"feedback_nodes":{"<id>":"..."},'
            '"feedback_supervisor":"...","feedback_manager":"...","graph_design":"..."}'
        )
        raters: list[dict[str, Any]] = []
        if use_llm:
            for label in ("rater_a", "rater_b"):
                try:
                    result = await call_model_tool(
                        prompt=json.dumps({**base_payload, "rater_id": label}, ensure_ascii=False),
                        system=system,
                        optimize_for="quality",
                        max_tokens=900,
                    )
                    parsed = _extract_json_object(str(result.get("text") or "")) or {}
                    raters.append(
                        {
                            "id": label,
                            "overall": float(parsed.get("overall") or 0),
                            "node_scores": parsed.get("node_scores")
                            if isinstance(parsed.get("node_scores"), dict)
                            else {},
                            "feedback_nodes": parsed.get("feedback_nodes")
                            if isinstance(parsed.get("feedback_nodes"), dict)
                            else {},
                            "feedback_supervisor": str(parsed.get("feedback_supervisor") or "")[:800],
                            "feedback_manager": str(parsed.get("feedback_manager") or "")[:800],
                            "graph_design": str(parsed.get("graph_design") or "")[:800],
                            "model": result.get("model"),
                        }
                    )
                except Exception:  # noqa: BLE001
                    logger.info("Dual rater %s failed", label, exc_info=True)
        if not raters:
            # Heuristic dual notes when LLM unavailable.
            overall = float((manager_review or {}).get("scores", {}).get("overall") or 5)
            raters = [
                {
                    "id": "rater_a",
                    "overall": overall,
                    "node_scores": {},
                    "feedback_nodes": {},
                    "feedback_supervisor": "Prefer real I2V clips and prune non-contributing nodes.",
                    "feedback_manager": "Keep brief/storyboard fidelity gates strict.",
                    "graph_design": "Brief→storyboard→solo cast+scenes→keyframes→clips→compose.",
                    "model": "heuristic",
                },
                {
                    "id": "rater_b",
                    "overall": max(0.0, overall - 0.5),
                    "node_scores": {},
                    "feedback_nodes": {},
                    "feedback_supervisor": "Strengthen continuity locks across shots.",
                    "feedback_manager": "Aggregate rater feedback only on Run again.",
                    "graph_design": "Ensure speech/music always feed compose when present.",
                    "model": "heuristic",
                },
            ]
        overalls = [float(r.get("overall") or 0) for r in raters]
        agg_overall = sum(overalls) / max(1, len(overalls))
        # Merge node feedback from both raters.
        feedback_nodes: dict[str, str] = {}
        for r in raters:
            for nid, text in (r.get("feedback_nodes") or {}).items():
                prev = feedback_nodes.get(str(nid), "")
                chunk = str(text or "").strip()
                if not chunk:
                    continue
                feedback_nodes[str(nid)] = (prev + " | " + chunk).strip(" |")[:1200]
        aggregated = {
            "raters": raters,
            "aggregated_overall": round(agg_overall, 2),
            "feedback_nodes": feedback_nodes,
            "feedback_supervisor": " || ".join(
                str(r.get("feedback_supervisor") or "") for r in raters
            )[:1600],
            "feedback_manager": " || ".join(
                str(r.get("feedback_manager") or "") for r in raters
            )[:1600],
            "graph_design": " || ".join(str(r.get("graph_design") or "") for r in raters)[:1600],
            "apply_on": "run_again_only",
        }
        meta = dict(graph.get("metadata") or {})
        meta["dual_rater_aggregate"] = aggregated
        graph["metadata"] = meta
        # Fold into manager review suggestions for persistence.
        suggestions = dict(manager_review.get("suggestions") or {})
        suggestions.update(feedback_nodes)
        if aggregated["feedback_supervisor"]:
            suggestions["supervisor"] = aggregated["feedback_supervisor"]
        if aggregated["graph_design"]:
            suggestions["graph_design"] = aggregated["graph_design"]
        manager_review = dict(manager_review)
        manager_review["suggestions"] = suggestions
        manager_review["dual_raters"] = aggregated
        manager_review["aggregated_overall"] = aggregated["aggregated_overall"]
        return manager_review


class SupervisorReviewer:
    """Writes per-node report + ratings after one-pass execution (no re-run loop)."""

    def finalize_fast(
        self,
        graph: DesignerExecutionGraph,
        *,
        agent_feedback: dict[str, dict[str, Any]],
        node_states: dict[str, Any] | None,
        optimize_for: str,
        vision_notes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        scores: dict[str, int] = {}
        suggestions: dict[str, str] = {}
        reports: dict[str, str] = {}
        self_scores: list[int] = []
        rating_mod = str(
            (graph.get("metadata") or {}).get("rating_modality")
            or ((graph.get("metadata") or {}).get("modality_plan") or {}).get(
                "global_rating_modality"
            )
            or "text_only"
        )
        vision_used = bool(vision_notes)
        for node in graph.get("nodes") or []:
            nid = str(node.get("id") or "")
            if not nid:
                continue
            score, note = _heuristic_node_score(
                nid, agent_feedback=agent_feedback, node_states=node_states
            )
            fb = agent_feedback.get(nid) or {}
            vnote = (vision_notes or {}).get(nid) or ""
            if vnote:
                low = vnote.lower()
                if any(
                    w in low for w in ("mismatch", "wrong", "unrelated", "blank", "empty")
                ):
                    score = _clamp_score(score - 2, score)
                elif any(w in low for w in ("match", "consistent", "clear", "good")):
                    score = _clamp_score(score + 1, score)
                note = (note + " | vision: " + vnote[:180]).strip(" |")
            reports[nid] = str(
                fb.get("artifact_summary") or fb.get("notes") or note
            )[:1500]
            scores[nid] = score
            self_scores.append(score)
            if score < 6:
                suggestions[nid] = note or "Retry with clearer task constraints"
        overall = int(round(sum(self_scores) / max(1, len(self_scores)))) if self_scores else 6
        scores["overall"] = overall
        suggestions["global"] = (
            "One-pass complete. Use Run again to apply these ratings as constraints."
        )
        return {
            "scores": scores,
            "node_reports": reports,
            "summary": (
                f"Supervisor finalize ({rating_mod}"
                f"{', vision used' if vision_used else ''}). "
                f"Overall {overall}/10. Optimize={optimize_for}."
            )[:3000],
            "suggestions": suggestions,
            "aggregated_score": overall,
            "improvement_plan": suggestions["global"],
            "supervisor_model": "heuristic+vision" if vision_used else "heuristic",
            "optimize_for": optimize_for,
            "rating_modality": rating_mod,
            "vision_used": vision_used,
            "vision_notes": vision_notes or {},
        }

    async def finalize(
        self,
        graph: DesignerExecutionGraph,
        *,
        agent_feedback: dict[str, dict[str, Any]],
        manager_review: dict[str, Any],
        optimize_for: str,
        use_llm: bool = False,
        node_states: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        vision_notes: dict[str, str] = {}
        modality = (graph.get("metadata") or {}).get("modality_plan") or {}
        if bool(modality.get("can_vision")):
            from jiuwenswarm.server.runtime.designer.capabilities import (
                collect_rateable_image_paths,
                inspect_image_for_rating,
            )

            for nid, path in collect_rateable_image_paths(graph, node_states, limit=3):
                q = (
                    "Does this image match a usable cinematic reference/keyframe for a short film? "
                    "Two short sentences; include match/mismatch/clear/blank if relevant."
                )
                ans = await inspect_image_for_rating(path, q)
                if ans:
                    vision_notes[nid] = ans
        if not use_llm:
            # Fast path does not need manager_review; manager runs after supervisor report.
            return self.finalize_fast(
                graph,
                agent_feedback=agent_feedback,
                node_states=node_states,
                optimize_for=optimize_for,
                vision_notes=vision_notes or None,
            )
        _ = manager_review
        system = (
            "You are the Designer Supervisor closing the run. "
            "Use vision_notes when present; otherwise rate from text status/messages only. "
            "Do not claim to have seen media without vision_notes. "
            "Rate every node agent and overall 0-10, summarize, and give per-node + global improvements. "
            "JSON only: "
            '{"scores":{"<node_id>":0-10,"overall":0-10},'
            '"summary":"...","suggestions":{"<node_id>":"...","global":"..."},'
            '"aggregated_score":0-10,"improvement_plan":"..."}'
        )
        prompt = json.dumps(
            {
                "agent_feedback": agent_feedback,
                "manager_review": manager_review,
                "optimize_for": optimize_for,
                "user_prompt": graph.get("description"),
                "vision_notes": vision_notes,
                "rating_modality": (graph.get("metadata") or {}).get("rating_modality"),
            },
            ensure_ascii=False,
        )
        result = await call_model_tool(
            prompt=prompt,
            system=system,
            optimize_for="quality",
            max_tokens=1200,
        )
        parsed = _extract_json_object(str(result.get("text") or "")) or {}
        scores_raw = parsed.get("scores") if isinstance(parsed.get("scores"), dict) else {}
        scores: dict[str, int] = {}
        self_scores: list[int] = []
        for node in graph.get("nodes") or []:
            nid = str(node.get("id") or "")
            scores[nid] = _clamp_score(scores_raw.get(nid), default=6)
            self_scores.append(_clamp_score((agent_feedback.get(nid) or {}).get("self_score"), 6))
        scores["overall"] = _clamp_score(scores_raw.get("overall"), default=6)
        manager_overall = _clamp_score((manager_review.get("scores") or {}).get("overall"), 6)
        avg_self = sum(self_scores) / max(1, len(self_scores))
        aggregated = _clamp_score(
            parsed.get("aggregated_score"),
            default=int(round((scores["overall"] + manager_overall + avg_self) / 3)),
        )
        suggestions = (
            parsed.get("suggestions")
            if isinstance(parsed.get("suggestions"), dict)
            else {}
        )
        return {
            "scores": scores,
            "summary": str(parsed.get("summary") or "")[:3000],
            "suggestions": suggestions,
            "aggregated_score": aggregated,
            "improvement_plan": str(parsed.get("improvement_plan") or suggestions.get("global") or "")[
                :3000
            ],
            "supervisor_model": result.get("model"),
            "rating_modality": str(
                (graph.get("metadata") or {}).get("rating_modality") or "text_only"
            ),
            "vision_used": bool(vision_notes),
            "vision_notes": vision_notes,
        }


async def write_run_feedback(
    *,
    graph: DesignerExecutionGraph,
    run_id: str,
    agent_feedback: dict[str, dict[str, Any]],
    supervisor_plan: dict[str, Any] | None,
    manager_review: dict[str, Any],
    supervisor_final: dict[str, Any],
    optimize_for: str,
) -> str:
    """Persist report under package runs/ (primary) and mirror to agent feedback store."""
    from jiuwenswarm.server.runtime.designer.paths import run_bundle_path

    graph_id = str(graph.get("graph_id") or "")
    payload = {
        "schema_version": "designer-feedback.v1",
        "optimize_for": optimize_for,
        "one_pass": True,
        "agents": agent_feedback,
        "supervisor_plan": supervisor_plan or {},
        "supervisor": {
            "scores": supervisor_final.get("scores") or {},
            "node_reports": supervisor_final.get("node_reports") or {},
            "summary": supervisor_final.get("summary") or "",
            "suggestions": supervisor_final.get("suggestions") or {},
            "rating_modality": supervisor_final.get("rating_modality") or "text_only",
            "vision_used": bool(supervisor_final.get("vision_used")),
        },
        "manager": {
            "scores": manager_review.get("scores") or {},
            "summary": manager_review.get("summary") or "",
            "pipeline_notes": manager_review.get("pipeline_notes") or "",
            "suggestions": manager_review.get("suggestions") or {},
            "rates_supervisor": bool(manager_review.get("rates_supervisor")),
            "rating_modality": manager_review.get("rating_modality") or "text_only",
            "vision_used": bool(manager_review.get("vision_used")),
            "dual_raters": manager_review.get("dual_raters") or {},
            "aggregated_overall": manager_review.get("aggregated_overall"),
        },
        "final": {
            "aggregated_score": supervisor_final.get("aggregated_score"),
            "dual_rater_overall": manager_review.get("aggregated_overall"),
            "summary": supervisor_final.get("summary"),
            "improvement_plan": supervisor_final.get("improvement_plan"),
            "apply_on": "run_again_only",
        },
    }
    # Primary report path lives next to trajectory under the package runs/ folder.
    report_path = run_bundle_path(graph_id, f"{run_id}.report")
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    path = save_feedback(graph_id, run_id, payload)
    meta = dict(graph.get("metadata") or {})
    meta["last_feedback_path"] = str(report_path)
    meta["last_feedback_run_id"] = run_id
    meta["last_aggregated_score"] = supervisor_final.get("aggregated_score")
    meta["last_report_path"] = str(report_path)
    graph["metadata"] = meta
    return str(report_path)
