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
    node_pipeline,
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


def _heuristic_placeholder_source(analysis: dict[str, Any] | None) -> bool:
    src = str((analysis or {}).get("source") or "")
    return src in {"", "heuristic", "heuristic_pending_llm"}


def _shot_expand_lock_count(
    *,
    graph: DesignerExecutionGraph,
    analysis: dict[str, Any],
    current_shots: list[dict[str, Any]],
) -> int:
    """How many shots the LLM is allowed to keep.

    ``0`` means Supervisor owns N (heuristic 1-shot skeleton is not a lock).
    A positive count truncates a longer LLM list — only after the canvas was
    truly frozen by a prior director design, or the user edited topology.
    """
    meta = dict(graph.get("metadata") or {})
    if meta.get("user_topology_edit"):
        try:
            target = int(analysis.get("target_shot_count") or 0)
        except (TypeError, ValueError):
            target = 0
        return max(1, len(current_shots) or target or 1)
    if _heuristic_placeholder_source(analysis):
        return 0
    # A style that forbids a single take must not be frozen below its floor.
    try:
        from jiuwenswarm.server.runtime.designer.video_styles import (
            resolve_video_style,
            video_style_min_shots,
        )

        floor = video_style_min_shots(resolve_video_style(graph))
        if floor >= 2 and len(current_shots) < floor:
            return 0
    except Exception:  # noqa: BLE001
        pass
    if meta.get("freeze_shot_topology"):
        try:
            target = int(analysis.get("target_shot_count") or 0)
        except (TypeError, ValueError):
            target = 0
        return max(1, len(current_shots) or target or 1)
    return 0


def _apply_llm_shot_list(
    current_shots: list[dict[str, Any]],
    cleaned: list[dict[str, Any]],
    *,
    lock_count: int,
) -> list[dict[str, Any]]:
    if not cleaned:
        return current_shots
    if lock_count >= 1 and len(cleaned) > lock_count:
        cleaned = cleaned[:lock_count]
        for i, shot in enumerate(cleaned, start=1):
            shot["shot_index"] = i
    return cleaned

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
    "music": ["call_model", "read_upstream", "call_music_model"],
    "audio": ["call_model", "read_upstream", "call_music_model"],
    "compose": ["call_model", "read_upstream", "ffmpeg_compose", "mix_audio"],
}


def _role_key(node: DesignerGraphNode) -> str:
    pipeline = str(node_pipeline(node) or "").strip().lower()
    if pipeline:
        return pipeline
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
        inputs = [str(x) for x in (cfg.get("inputs") or []) if str(x)]
        if any(
            item.startswith("n_clip") or item.startswith("n_video") or item in {"n_compose", "n_final"}
            for item in inputs
        ):
            return list(_ROLE_TOOLS["compose"])
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
        role = _role_key(node)
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
                    if _role_key(node) != "storyboard":
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


def _spatial_geography_lock_patch(graph: DesignerExecutionGraph) -> list[str]:
    """Stamp / refresh spatial_lock on scene/frame/clip so architecture stays faithful."""
    notes: list[str] = []
    meta = dict(graph.get("metadata") or {})
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    lock = meta.get("spatial_lock") if isinstance(meta.get("spatial_lock"), dict) else {}
    if not lock and isinstance(analysis.get("spatial_lock"), dict):
        lock = dict(analysis.get("spatial_lock") or {})
    if not lock:
        scenes = list(analysis.get("scenes") or [])
        scene0 = scenes[0] if scenes and isinstance(scenes[0], dict) else {}
        lock = {
            "setting": str(scene0.get("name") or "Primary setting"),
            "architecture": str(scene0.get("description") or "one coherent interior"),
            "static_rule": (
                "STATIC OBJECTS LOCKED across shots: landmarks, terrain, buildings, props, and "
                "light direction must match the master scene plate — only camera may change."
            ),
            "crowd_rule": (
                "Empty environment plates; keyframes keep the SAME extras layout "
                "across shots; never clone a featured person into two places at once."
            ),
        }
    # Ensure required keys
    lock.setdefault(
        "static_rule",
        "Keep landmarks, layout, and lighting identical to the master plate.",
    )
    lock.setdefault(
        "crowd_rule",
        "Do not invent a new extras layout per shot.",
    )
    meta["spatial_lock"] = lock
    if isinstance(analysis, dict):
        analysis = dict(analysis)
        analysis["spatial_lock"] = lock
        meta["script_analysis"] = analysis
    graph["metadata"] = meta

    lock_clause = (
        " SPATIAL LOCK: "
        + "; ".join(f"{k}={v}" for k, v in lock.items() if str(v).strip())
    )[:500]

    ids = {
        str(item.get("id") or "")
        for item in (graph.get("nodes") or [])
        if isinstance(item, dict)
    }
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = dict(node.get("config") or {})
        role = _role_key(node)
        nid = str(node.get("id") or "")
        if role not in {"scene", "frame", "clip", "keyframe", "brief", "storyboard"}:
            continue
        cfg["spatial_lock"] = lock
        if (
            role == "scene"
            and nid != "n_scene"
            and "n_scene" in ids
            and not cfg.get("master_scene_node_id")
        ):
            cfg["master_scene_node_id"] = "n_scene"
            cfg.setdefault("scene_strategy", "edit_master_view")
            notes.append(f"{nid}: master_scene_node_id=n_scene")
        if role in {"frame", "clip", "keyframe", "scene"}:
            gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
            prompt = str(gen.get("prompt") or cfg.get("prompt") or "")
            if lock_clause.strip() and "SPATIAL LOCK" not in prompt:
                if gen.get("prompt") is not None or role in {"frame", "clip", "keyframe"}:
                    gen["prompt"] = (prompt + lock_clause)[:1400]
                    cfg["generate"] = gen
                else:
                    cfg["prompt"] = (prompt + lock_clause)[:1400]
                notes.append(f"{nid}: spatial_lock stamped")
        node["config"] = cfg
    return notes


def _manager_prune_and_cohere(graph: DesignerExecutionGraph) -> list[str]:
    """Prune unused nodes, rewire spatial edges, keep every kept node useful for final film."""
    from jiuwenswarm.server.runtime.designer.smart_graph import (
        ensure_combined_cast_reach_compose,
        prune_non_contributing_nodes,
        prune_shot_nodes_beyond_analysis,
    )

    notes: list[str] = []
    meta0 = dict(graph.get("metadata") or {})
    skip_scene_plate = bool(meta0.get("skip_scene_plate"))
    # Drop unused combined cast sheets that never feed a frame/clip.
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
    outs: dict[str, set[str]] = {}
    for e in edges:
        s, t = str(e.get("source") or ""), str(e.get("target") or "")
        if s and t:
            outs.setdefault(s, set()).add(t)
    drop: list[str] = []
    for n in nodes:
        cfg = n.get("config") if isinstance(n.get("config"), dict) else {}
        nid = str(n.get("id") or "")
        if not nid:
            continue
        if cfg.get("combined_cast") and not any(
            str(t).startswith("n_frame") or str(t).startswith("n_clip") or t == "n_compose"
            for t in (outs.get(nid) or [])
        ):
            drop.append(nid)
    if drop:
        drop_set = set(drop)
        graph["nodes"] = [n for n in nodes if str(n.get("id")) not in drop_set]
        graph["edges"] = [
            e
            for e in edges
            if str(e.get("source") or "") not in drop_set
            and str(e.get("target") or "") not in drop_set
        ]
        notes.extend([f"drop_unused_combined:{x}" for x in drop])

    # Coherence: master plate → shot views; frames/clips include master + shot scene.
    # Skipped when compose-first / no empty plates.
    ids = {
        str(n.get("id"))
        for n in (graph.get("nodes") or [])
        if isinstance(n, dict) and n.get("id")
    }
    edge_pairs = {
        (str(e.get("source") or ""), str(e.get("target") or ""))
        for e in (graph.get("edges") or [])
        if isinstance(e, dict)
    }
    master_id = "n_scene" if "n_scene" in ids and not skip_scene_plate else ""
    if master_id:
        for n in list(graph.get("nodes") or []):
            if not isinstance(n, dict):
                continue
            cfg = dict(n.get("config") or {})
            nid = str(n.get("id") or "")
            role = _role_key(n)
            if role == "scene" and nid != master_id:
                cfg.setdefault("master_scene_node_id", master_id)
                cfg.setdefault("scene_strategy", "edit_master_view")
                inputs = [str(x) for x in (cfg.get("inputs") or []) if str(x)]
                if master_id not in inputs:
                    inputs.append(master_id)
                    cfg["inputs"] = inputs
                    notes.append(f"cohere_inputs:{nid}+{master_id}")
                if (master_id, nid) not in edge_pairs:
                    graph.setdefault("edges", []).append(
                        {
                            "id": f"e_mgr_{master_id}_{nid}",
                            "source": master_id,
                            "target": nid,
                            "kind": "data",
                            "label": "spatial_ref",
                        }
                    )
                    edge_pairs.add((master_id, nid))
                    notes.append(f"cohere_edge:{master_id}->{nid}")
                n["config"] = cfg
            elif role in {"frame", "keyframe", "clip"}:
                shot_idx = int(cfg.get("shot_index") or 0)
                shot_scene = f"n_scene_{shot_idx}" if shot_idx >= 1 else ""
                inputs = [str(x) for x in (cfg.get("inputs") or []) if str(x)]
                changed = False
                for need in (master_id, shot_scene):
                    if need and need in ids and need not in inputs:
                        inputs.append(need)
                        changed = True
                    if need and need in ids and (need, nid) not in edge_pairs:
                        graph.setdefault("edges", []).append(
                            {
                                "id": f"e_mgr_{need}_{nid}",
                                "source": need,
                                "target": nid,
                                "kind": "data",
                                "label": "spatial_ref",
                            }
                        )
                        edge_pairs.add((need, nid))
                        notes.append(f"cohere_edge:{need}->{nid}")
                if changed:
                    cfg["inputs"] = inputs
                    notes.append(f"cohere_inputs:{nid}")
                n["config"] = cfg

    extra_shots = prune_shot_nodes_beyond_analysis(graph)
    notes.extend([f"pruned_extra_shot:{x}" for x in extra_shots])
    pruned = prune_non_contributing_nodes(graph)
    notes.extend([f"pruned:{x}" for x in pruned])
    notes.extend(ensure_combined_cast_reach_compose(graph))
    # Final structural prune after rewires (orphans must not remain).
    pruned2 = prune_non_contributing_nodes(graph)
    notes.extend([f"pruned:{x}" for x in pruned2])
    notes.extend(_manager_reedit_artifacts_after_prune(graph, pruned=list(pruned) + list(pruned2)))
    ids = {str(n.get("id")) for n in (graph.get("nodes") or []) if isinstance(n, dict)}
    if any(
        str((n.get("config") or {}).get("scene_strategy") or "") == "edit_master_view"
        for n in (graph.get("nodes") or [])
        if isinstance(n, dict)
    ) and "n_scene" not in ids:
        notes.append("warn:edit_master_view_without_n_scene")
    return notes


def _manager_reedit_artifacts_after_prune(
    graph: DesignerExecutionGraph,
    *,
    pruned: list[str] | None = None,
) -> list[str]:
    """After graph prune, re-edit Brief / Storyboard / locks to match surviving nodes."""
    notes: list[str] = []
    meta = dict(graph.get("metadata") or {})
    analysis = dict(meta.get("script_analysis") or {}) if isinstance(meta.get("script_analysis"), dict) else {}
    kept_shot_idxs: list[int] = []
    for n in graph.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        cfg = n.get("config") if isinstance(n.get("config"), dict) else {}
        role = _role_key(n)
        nid = str(n.get("id") or "")
        if role in {"frame", "keyframe", "clip"} or nid.startswith("n_frame_") or nid.startswith("n_clip_"):
            idx = int(cfg.get("shot_index") or 0)
            if idx >= 1 and idx not in kept_shot_idxs:
                kept_shot_idxs.append(idx)
    kept_shot_idxs.sort()
    shots = [s for s in (analysis.get("shots") or []) if isinstance(s, dict)]
    if kept_shot_idxs and shots:
        kept = {
            int(s.get("shot_index") or 0)
            for s in shots
            if int(s.get("shot_index") or 0) in set(kept_shot_idxs)
        }
        if kept and kept != {int(s.get("shot_index") or 0) for s in shots}:
            analysis["shots"] = [
                s for s in shots if int(s.get("shot_index") or 0) in kept
            ]
            notes.append(f"reedit_shots_keep:{sorted(kept)}")
        # Rebuild already_done chains from surviving chronological shots.
        already: list[str] = []
        revised: list[dict[str, Any]] = []
        for s in analysis.get("shots") or []:
            if not isinstance(s, dict):
                continue
            shot = dict(s)
            shot["already_done"] = list(already)
            action = str(shot.get("action") or shot.get("keyframe_prompt") or "").strip()
            exits = [
                str(x)
                for x in (shot.get("exiting_character_ids") or shot.get("exiting") or [])
                if str(x)
            ]
            idx = int(shot.get("shot_index") or 0)
            if action:
                already.append(f"shot{idx}: {action[:120]}")
            for cid in exits:
                already.append(f"{cid} exited by shot{idx} — do not show leaving again")
            # Refresh occupancy from storyboard visible / offscreen / doing.
            visible = [
                str(x)
                for x in (
                    shot.get("on_screen")
                    or shot.get("visible_cast_ids")
                    or shot.get("character_ids")
                    or []
                )
                if str(x)
            ]
            offscreen = [
                str(x)
                for x in (shot.get("offscreen") or shot.get("off_screen_cast_ids") or [])
                if str(x) and str(x) not in visible
            ]
            ensemble = [
                str(x)
                for x in (
                    shot.get("ensemble_cast_ids")
                    or shot.get("compose_cast_ids")
                    or visible
                    or []
                )
                if str(x)
            ]
            exited = set(exits)
            occ = dict(shot.get("occupancy") or {}) if isinstance(shot.get("occupancy"), dict) else {}
            occ["must_appear"] = [c for c in visible if c not in exited]
            occ["offscreen"] = list(offscreen)
            occ["featured"] = [
                str(x)
                for x in (shot.get("featured_cast_ids") or visible or ensemble)
                if str(x)
            ]
            if isinstance(shot.get("cast_actions"), dict):
                occ["cast_actions"] = shot["cast_actions"]
            occ.setdefault(
                "rule",
                "Draw must_appear only; keep offscreen out of frame; do not restage already_done.",
            )
            shot["occupancy"] = occ
            revised.append(shot)
        analysis["shots"] = revised
        meta["script_analysis"] = analysis
        notes.append("reedit_already_done_occupancy")

    # Sync storyboard markdown + approved_storyboard from surviving shots.
    chars = [c for c in (analysis.get("characters") or []) if isinstance(c, dict)]
    surviving = [s for s in (analysis.get("shots") or []) if isinstance(s, dict)]
    if surviving:
        lines = ["# Storyboard Scenario", ""]
        for s in surviving:
            idx = int(s.get("shot_index") or 0)
            lines.append(
                f"## Shot {idx} ({s.get('setting_id') or 'set_1'}) — "
                f"{s.get('title') or s.get('camera') or 'beat'}"
            )
            lines.append(f"- Timeline: {s.get('timeline') or f'{(idx-1)*5}-{idx*5}s'}")
            lines.append(f"- Action: {s.get('action') or s.get('keyframe_prompt') or ''}")
            lines.append(f"- Camera: {s.get('camera') or ''}")
            if s.get("speech_line"):
                lines.append(f"- Speech: {s.get('speech_line')}")
            done = s.get("already_done") or []
            if done:
                lines.append(f"- Already done: {'; '.join(str(x) for x in done[:8])}")
            occ = s.get("occupancy") if isinstance(s.get("occupancy"), dict) else {}
            if occ:
                lines.append(
                    f"- Occupancy must_appear={occ.get('must_appear')}; featured={occ.get('featured')}"
                )
            lines.append("")
        sb_md = "\n".join(lines).strip() + "\n"
        meta["approved_storyboard"] = sb_md
        for n in graph.get("nodes") or []:
            if not isinstance(n, dict):
                continue
            if str(n.get("id") or "") != "n_storyboard" and _role_key(n) != "storyboard":
                continue
            cfg = dict(n.get("config") or {})
            if cfg.get("prewritten") is not None:
                cfg["prewritten"] = sb_md
            if cfg.get("draft_prewritten") is not None:
                cfg["draft_prewritten"] = sb_md
            n["config"] = cfg
            notes.append("reedit_storyboard_node")
        notes.append("reedit_approved_storyboard")

    # Brief: keep detail, stamp counts for surviving topology.
    brief = str(meta.get("approved_brief") or "").strip()
    cast_n = len(chars)
    scene_n = len([s for s in (analysis.get("scenes") or []) if isinstance(s, dict)])
    shot_n = len(surviving)
    stamp = (
        f"\n\n## Manager prune sync\n"
        f"- Cast count: {cast_n}\n"
        f"- Scene count: {scene_n}\n"
        f"- Surviving shots: {shot_n} (indices {kept_shot_idxs})\n"
        f"- Pruned nodes: {', '.join(pruned or []) or 'none'}\n"
        f"- Audio routing: {meta.get('audio_routing') or {}}\n"
    )
    if brief:
        # Replace prior sync block if present.
        if "## Manager prune sync" in brief:
            brief = brief.split("## Manager prune sync")[0].rstrip()
        meta["approved_brief"] = (brief + stamp).strip() + "\n"
        notes.append("reedit_approved_brief")
        for n in graph.get("nodes") or []:
            if not isinstance(n, dict):
                continue
            if str(n.get("id") or "") != "n_brief" and _role_key(n) != "brief":
                continue
            cfg = dict(n.get("config") or {})
            if cfg.get("prewritten") is not None:
                cfg["prewritten"] = meta["approved_brief"]
            if cfg.get("draft_prewritten") is not None:
                cfg["draft_prewritten"] = meta["approved_brief"]
            n["config"] = cfg

    # Stamp occupancy / already_done onto surviving frame+clip nodes.
    by_idx = {
        int(s.get("shot_index") or 0): s
        for s in (analysis.get("shots") or [])
        if isinstance(s, dict) and int(s.get("shot_index") or 0) >= 1
    }
    for n in graph.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        cfg = dict(n.get("config") or {})
        role = _role_key(n)
        if role not in {"frame", "keyframe", "clip"}:
            continue
        idx = int(cfg.get("shot_index") or 0)
        shot = by_idx.get(idx)
        if not shot:
            continue
        if isinstance(shot.get("occupancy"), dict):
            cfg["occupancy"] = shot["occupancy"]
        if shot.get("already_done") is not None:
            cfg["already_done"] = list(shot.get("already_done") or [])
        # Append already_done clause into generate.prompt if missing.
        gen = dict(cfg.get("generate") or {})
        prompt = str(gen.get("prompt") or "")
        done = [str(x) for x in (cfg.get("already_done") or []) if str(x)]
        if done and "ALREADY_DONE" not in prompt:
            prompt = (
                prompt.rstrip()
                + "\nALREADY_DONE (do not restage): "
                + "; ".join(done[:12])
            )
            gen["prompt"] = prompt
            cfg["generate"] = gen
            notes.append(f"reedit_prompt_already_done:{n.get('id')}")
        n["config"] = cfg

    graph["metadata"] = meta
    if pruned:
        notes.append(f"prune_count:{len(pruned)}")
    return notes


def _ensure_audio_nodes_for_intent(graph: DesignerExecutionGraph) -> list[str]:
    """If audio intent / analysis asks for speech or music, ensure nodes exist.

    Dialogue always stays in clip-native audio; Designer has no TTS node.
    """
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
    # Audible bed when speech is already requested — never scan prompt for topic nouns.
    if not explicit_no_music and policy != "silent" and want_speech:
        want_music = True
        policy = "speech_and_music"
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
                    "delegate": "agent",
                    "tools": [tool, "read_upstream", "call_model"],
                    "duration_sec": 18 if mode != "cost" else 6,
                    "max_audio_sec": 24 if mode != "cost" else 8,
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

    if want_music:
        _add("n_music", "Music / BGM", "music", "audio_bed", "call_music_model")

    if notes:
        graph["nodes"] = nodes
        graph["edges"] = edges
        meta["audio_nodes"] = ["n_music"] if "n_music" in existing else []
        graph["metadata"] = meta
    return notes


def assign_audio_node_agents(graph: DesignerExecutionGraph) -> dict[str, Any]:
    """Supervisor: promote speech/music to fast LLM agents when backends exist.

    Dialogue always lives in clip leaves (``clip_embedded``), and any legacy
    Speech node is dropped. The Music node always survives: it is the
    single film-wide BGM mixed after concat, and without a music API its handler
    writes a silent placeholder.
    """
    from jiuwenswarm.server.runtime.designer.capabilities import detect_audio_backends
    from jiuwenswarm.server.runtime.designer.smart_graph import prune_non_contributing_nodes

    backends = detect_audio_backends()
    can_speech = False
    can_music = bool(backends.get("can_music"))
    meta = dict(graph.get("metadata") or {})
    routing = dict(meta.get("audio_routing") or {})
    # Dialogue folds into the clips when there is no TTS backend. Music never
    # folds in: one BGM track is mixed after concat, and the Music node emits a
    # silent placeholder until a music API is wired.
    clip_embedded = True
    if clip_embedded:
        drop = {
            str(n.get("id") or "")
            for n in (graph.get("nodes") or [])
            if isinstance(n, dict)
            and (
                str(n.get("id") or "") == "n_speech"
                or _role_key(n).lower() in {"speech", "tts"}
            )
        }
        if drop:
            graph["nodes"] = [
                n
                for n in (graph.get("nodes") or [])
                if str(n.get("id") or "") not in drop
            ]
            graph["edges"] = [
                e
                for e in (graph.get("edges") or [])
                if str(e.get("source") or "") not in drop
                and str(e.get("target") or "") not in drop
            ]
            for n in graph.get("nodes") or []:
                if not isinstance(n, dict):
                    continue
                cfg = dict(n.get("config") or {})
                if str(cfg.get("role") or "") != "clip":
                    continue
                from jiuwenswarm.server.runtime.designer.audio_locks import (
                    stamp_audio_fields_on_clip_config,
                )

                analysis = (
                    meta.get("script_analysis")
                    if isinstance(meta.get("script_analysis"), dict)
                    else {}
                )
                idx = int(cfg.get("shot_index") or 0)
                shot_row = next(
                    (
                        s
                        for s in (analysis.get("shots") or [])
                        if isinstance(s, dict) and int(s.get("shot_index") or 0) == idx
                    ),
                    {},
                )
                cfg = stamp_audio_fields_on_clip_config(
                    cfg,
                    shot=shot_row if isinstance(shot_row, dict) else {},
                    analysis=analysis,
                    meta=meta,
                    clip_embedded=True,
                )
                n["config"] = cfg
            prune_non_contributing_nodes(graph)
        routing["clip_embedded"] = True
        routing["can_speech"] = can_speech
        routing["can_music"] = can_music
        routing["can_video_audio"] = bool(backends.get("can_video_audio", True))
        routing["video_audio_model"] = str(backends.get("video_audio_model") or "")
        meta["audio_routing"] = routing
        meta["prefer_clip_native_audio"] = True
        meta["prefer_wan3_clip_audio"] = True  # legacy alias
        if isinstance(meta.get("script_analysis"), dict):
            from jiuwenswarm.server.runtime.designer.audio_locks import ensure_audio_locks_on_analysis

            meta["script_analysis"] = ensure_audio_locks_on_analysis(
                meta["script_analysis"],
                str(graph.get("description") or meta.get("user_prompt") or ""),
            )
            meta["language_lock"] = str(
                meta["script_analysis"].get("language_lock")
                or meta.get("language_lock")
                or "en"
            )
            if isinstance(meta["script_analysis"].get("bgm_lock"), dict):
                meta["bgm_lock"] = meta["script_analysis"]["bgm_lock"]
        graph["metadata"] = meta

    ensured = _ensure_audio_nodes_for_intent(graph)
    assigned: list[str] = ["clip_embedded"] if clip_embedded else []

    for node in graph.get("nodes") or []:
        cfg = dict(node.get("config") or {})
        role = _role_key(node).lower()
        nid = str(node.get("id") or "")
        if role in {"music", "audio", "audio_bed"} or nid == "n_music":
            cfg["role"] = "music"
            cfg["skill_id"] = cfg.get("skill_id") or "audio_bed"
            cfg["tools"] = ["call_music_model", "read_upstream", "call_model"]
            if can_music:
                cfg["force_handler"] = False
                cfg["delegate"] = "agent"
                cfg["supervisor_task"] = (
                    cfg.get("supervisor_task")
                    or "Compose ONE non-vocal BGM bed from the Brief / bgm_lock for the "
                    "full concatenated film. call_music_model. Never generate per-clip scores. "
                    "Keep headroom so clip dialogue stays intelligible."
                )
                assigned.append(f"{nid}:music_agent")
            else:
                cfg["force_handler"] = True
                cfg["delegate"] = "handler"
                cfg["supervisor_task"] = (
                    "No music API yet. Output a silent/empty placeholder file only. "
                    "Do not invent a score. When MUSIC_API_KEY / models.music is "
                    "configured, this node will call_music_model instead."
                )
                assigned.append(f"{nid}:music_placeholder")
            node["config"] = cfg

    meta = dict(graph.get("metadata") or {})
    meta["supervisor_audio_assignment"] = {
        "can_speech": can_speech,
        "can_music": can_music,
        "can_video_audio": bool(backends.get("can_video_audio", True)),
        "assigned": assigned,
        "ensured_nodes": ensured,
        "backends": backends,
        "clip_embedded": bool(clip_embedded),
        "prefer_clip_native_audio": bool(clip_embedded),
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


def validate_plan_occupancy(analysis: dict[str, Any]) -> dict[str, Any]:
    """Code validators after Manager patch (no LLM): setting_id, on_screen, scene_locks, ids."""
    errors: list[str] = []
    warnings: list[str] = []
    characters = [c for c in (analysis.get("characters") or []) if isinstance(c, dict)]
    shots = [s for s in (analysis.get("shots") or []) if isinstance(s, dict)]
    scenes = [s for s in (analysis.get("scenes") or []) if isinstance(s, dict)]
    scene_locks = (
        analysis.get("scene_locks")
        if isinstance(analysis.get("scene_locks"), dict)
        else {}
    )
    valid_ids = {str(c.get("id") or "") for c in characters if str(c.get("id") or "")}
    if not characters:
        errors.append("no_characters")
    if not shots:
        errors.append("no_shots")
    for i, shot in enumerate(shots, start=1):
        idx = int(shot.get("shot_index") or i)
        sid = str(shot.get("setting_id") or shot.get("scene_id") or "").strip()
        if not sid:
            errors.append(f"shot{idx}_missing_setting_id")
        on_screen = [
            str(x)
            for x in (
                shot.get("on_screen")
                or shot.get("visible_cast_ids")
                or shot.get("featured_cast_ids")
                or []
            )
            if str(x)
        ]
        if not on_screen:
            errors.append(f"shot{idx}_empty_on_screen")
        for cid in on_screen:
            if cid not in valid_ids:
                errors.append(f"shot{idx}_unknown_on_screen:{cid}")
        if sid and sid not in scene_locks and scenes:
            # Prefer explicit locks; warn if missing (builder may synthesize).
            warnings.append(f"shot{idx}_missing_scene_lock:{sid}")
        # Phase 3 light check: multi-shot same setting should diversify views when present.
    by_setting: dict[str, list[str]] = {}
    for shot in shots:
        sid = str(shot.get("setting_id") or "").strip()
        vk = str(shot.get("view_key") or "").strip()
        if sid and vk:
            by_setting.setdefault(sid, []).append(vk)
    for sid, views in by_setting.items():
        if len(views) >= 2 and len(set(views)) < 2:
            warnings.append(f"setting_{sid}_views_not_diverse")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
    }


class SupervisorAgent:
    """Assigns tasks / tools / models for every node agent (one-pass, no loop)."""

    def onboard_user_added_nodes(self, graph: DesignerExecutionGraph) -> dict[str, Any]:
        """When the user adds canvas nodes: promote to LLM agents (if available),
        decide tools, and let Manager lock-check media prompts.

        Never auto-wires into clip/compose — the user's successor ``+`` / drawn
        edge is the only topology. Never deletes user_added orphans.
        """
        from jiuwenswarm.server.runtime.designer.model_tools import llm_available
        from jiuwenswarm.server.runtime.designer.smart_graph import (
            find_non_contributing_node_ids,
        )

        use_agents = bool(llm_available())
        notes: list[str] = []
        onboarded: list[str] = []

        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            cfg = dict(node.get("config") or {})
            nid = str(node.get("id") or "")
            if not nid or not cfg.get("user_added"):
                continue
            onboarded.append(nid)
            role = _role_key(node)
            tools = _tools_for_node(node)
            cfg["kind"] = "agent"
            cfg["tools"] = tools
            cfg["user_added"] = True
            if use_agents:
                cfg.pop("force_handler", None)
                cfg["delegate"] = "agent"
                cfg["skip_llm"] = False
                if cfg.get("prewritten") and not cfg.get("draft_prewritten"):
                    cfg["draft_prewritten"] = cfg.pop("prewritten")
                else:
                    cfg.pop("prewritten", None)
                if not str(cfg.get("supervisor_task") or "").strip():
                    cfg["supervisor_task"] = (
                        f"User-added {role or node.get('type') or 'node'} agent. "
                        f"Use tools {', '.join(tools)}. "
                        "Produce only this node's output. Do not add extra image or "
                        "video nodes, and do not rewire into clip/compose."
                    )[:800]
                notes.append(f"agent:{nid}")
            else:
                cfg["delegate"] = "handler"
                notes.append(f"handler_no_llm:{nid}")
            node["config"] = cfg

        # Manager lock-gates every user-added media leaf.
        manager = ManagerAgent()
        lock_notes: list[str] = []
        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
            if not cfg.get("user_added"):
                continue
            if _role_key(node) not in {
                "frame",
                "keyframe",
                "clip",
                "character",
                "character_design",
                "scene",
                "image",
                "video",
            }:
                continue
            gate = manager.review_leaf_media_prompt(graph, node)
            if gate.get("patched"):
                lock_notes.append(f"locks:{node.get('id')}")
            notes.extend(
                [f"mgr:{x}" for x in (gate.get("notes") or []) if isinstance(x, str)][:4]
            )

        orphans = [
            nid
            for nid in find_non_contributing_node_ids(graph)
            if any(
                str(n.get("id") or "") == nid
                and isinstance(n.get("config"), dict)
                and n["config"].get("user_added")
                for n in (graph.get("nodes") or [])
                if isinstance(n, dict)
            )
        ]
        meta = dict(graph.get("metadata") or {})
        if orphans:
            meta["non_contributing_user_nodes"] = orphans
            meta["contribution_warning"] = (
                "User-added nodes do not feed the final clip/compose: "
                + ", ".join(orphans)
                + ". Connect them into the pipeline if you want them in the film."
            )
        elif onboarded:
            meta.pop("non_contributing_user_nodes", None)
            meta.pop("contribution_warning", None)
        result = {
            "ok": True,
            "use_agents": use_agents,
            "onboarded": onboarded,
            "orphans": orphans,
            "notes": notes[:80],
            "lock_notes": lock_notes[:40],
        }
        meta["supervisor_user_node_onboard"] = result
        graph["metadata"] = meta
        return result

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
            if _role_key(node) != "clip":
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
            # Preserve storyboard/analysis beat — never wipe to "follow keyframe".
            beat = action or "match storyboard beat for this shot"
            gen["prompt"] = (
                f"Film shot {idx} only from its keyframe. Camera {camera}. "
                f"Action: {beat}. "
                + (f"Keyframe note: {frame_msg[:180]}. " if frame_msg else "")
                + "Do not repeat other shots."
                f"{clause}"
            )
            cfg["generate"] = gen
            if action:
                cfg["shot_action"] = action[:500]
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
            "scene consistency (spatial lock: landmarks/layout/light must not drift), "
            "motion consistency, and continuity. "
            "Assign each leaf node a concrete task + tools so agents produce real media "
            "(images/video/audio), not markdown stubs. "
            "Scene master plate must be authored first; later scene views must EDIT that plate. "
            "Coordinate node agents in a ComfyUI-like pipeline. "
            "Follow the scenario skill and audio policy. "
            "For each node, choose optimize_for (cost|quality) and a preferred_model "
            "from the configured Settings model list. "
            "Dialogue is clip-native; never create or assign a TTS/Speech node. "
            "For the Music node, use call_music_model when available, otherwise keep "
            "the silent handler placeholder. "
            "On rerun, incorporate prior feedback suggestions. "
            "Respond with JSON only: "
            '{"optimize_for_global":"cost|quality",'
            '"brief_notes":"...",'
            '"spatial_lock":{"setting":"...","landmarks":"...","light":"...","static_rule":"..."},'
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
            max_tokens=32768,
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
            "spatial_lock": parsed.get("spatial_lock")
            if isinstance(parsed.get("spatial_lock"), dict)
            else {},
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
        if plan.get("spatial_lock"):
            meta["spatial_lock"] = {
                str(k): str(v)[:400] for k, v in plan["spatial_lock"].items() if str(v).strip()
            }
            analysis = dict(meta.get("script_analysis") or {})
            analysis["spatial_lock"] = meta["spatial_lock"]
            meta["script_analysis"] = analysis
            _spatial_geography_lock_patch(graph)
        graph["metadata"] = meta
        audio_assign = assign_audio_node_agents(graph)
        plan["audio_assignment"] = audio_assign
        meta = dict(graph.get("metadata") or {})
        meta["supervisor_plan"] = plan
        graph["metadata"] = meta
        return plan

    async def author_plan_one_pass(
        self,
        prompt: str,
        *,
        optimize_for: str = "quality",
        reference_images: list[str] | None = None,
        timeout_sec: float = 120.0,
    ) -> dict[str, Any]:
        """Single Supervisor LLM call: brief + storyboard + cast + occupancy + scene_locks."""
        from jiuwenswarm.server.runtime.designer.script_analysis import (
            _normalize_llm_analysis,
            heuristic_analysis,
        )

        base = heuristic_analysis(prompt)
        system = (
            "You are the Designer Supervisor. ONE JSON plan for Enter (schema plan.v1). "
            "Stay faithful to the user prompt — do not invent plot or people. "
            "Extract EVERY named human into characters[]. "
            "Shots MUST have setting_id (new place/meet/leave/exterior → new setting_id). "
            "Per shot REQUIRED: on_screen (visible only), offscreen, cast_actions, "
            "featured_cast_ids, setting_id, action, camera, timeline, keyframe_prompt, view_key. "
            "NEVER put later-meet cast into earlier on_screen. "
            "Include scene_locks[setting_id]={place,lighting,objects,crowd,coherence_rule,views}. "
            "Include brief_markdown and storyboard_markdown for UI. "
            "Max 8 shots. Output ONLY one JSON object. Schema: "
            '{"schema":"plan.v1","characters":[{"id":"char_1","name":"...","description":"..."}],'
            '"scenes":[{"id":"set_1","name":"...","description":"..."}],'
            '"shots":[{"shot_index":1,"timeline":"0-5s","camera":"...","action":"...",'
            '"on_screen":["char_1"],"offscreen":[],"cast_actions":{"char_1":"..."},'
            '"featured_cast_ids":["char_1"],"setting_id":"set_1","view_key":"front",'
            '"keyframe_prompt":"..."}],'
            '"scene_locks":{"set_1":{"place":"...","lighting":"...","objects":[],'
            '"crowd":"...","coherence_rule":"...","views":{"front":"..."}}},'
            '"audio":{"include_speech":false,"include_music":false},'
            '"brief_markdown":"...","storyboard_markdown":"...","notes":"..."}'
        )
        payload: dict[str, Any] = {
            "user_prompt": prompt[:3000],
            "rule": (
                "Occupancy is authoritative. Each shot's on_screen lists ONLY people "
                "visible in that beat; later-meet cast stay offscreen until their beat."
            ),
        }
        if reference_images:
            payload["reference_image_count"] = len(reference_images)

        async def _call(*, reinforce: bool = False) -> dict[str, Any] | None:
            sys_msg = system
            body = dict(payload)
            if reinforce:
                sys_msg = (
                    "Output ONLY one JSON object starting with '{'. "
                    "Required keys: characters, shots, scene_locks, brief_markdown, "
                    "storyboard_markdown. Every shot needs non-empty on_screen + setting_id."
                )
                body["retry"] = True
            result = await call_model_tool(
                prompt=json.dumps(body, ensure_ascii=False),
                system=sys_msg,
                optimize_for=optimize_for,
                max_tokens=65536,
            )
            parsed = _extract_json_object(str(result.get("text") or "")) or {}
            if not isinstance(parsed, dict) or not parsed:
                return None
            # Prefer shared normalizer when possible; preserve extra plan fields.
            norm = _normalize_llm_analysis(parsed, base)
            if not isinstance(norm, dict):
                return None
            for key in (
                "scene_locks",
                "brief_markdown",
                "storyboard_markdown",
                "notes",
                "audio",
                "scenes",
            ):
                if key in parsed and parsed[key] is not None:
                    norm[key] = parsed[key]
            if isinstance(parsed.get("scenes"), list) and parsed["scenes"]:
                norm["scenes"] = parsed["scenes"]
            norm["source"] = "llm"
            norm["schema"] = "plan.v1"
            return norm

        try:
            plan = await _call(reinforce=False)
            if plan is None:
                plan = await _call(reinforce=True)
        except Exception:  # noqa: BLE001
            logger.info("author_plan_one_pass LLM failed", exc_info=True)
            plan = None
        if not isinstance(plan, dict):
            base["source"] = "heuristic"
            base["schema"] = "plan.v1"
            base["llm_pending"] = False
            return base
        return plan

    async def author_creative_brief(
        self, graph: DesignerExecutionGraph, *, use_llm: bool = False
    ) -> dict[str, Any]:
        """LLM-author a detailed brief onto n_brief; heuristic fallback only if LLM fails."""
        from jiuwenswarm.server.runtime.designer.smart_graph import _write_brief_markdown

        meta = dict(graph.get("metadata") or {})
        analysis = (
            dict(meta.get("script_analysis") or {})
            if isinstance(meta.get("script_analysis"), dict)
            else {}
        )
        characters = list(analysis.get("characters") or [])
        scenes = list(analysis.get("scenes") or [])
        audio = (
            dict(analysis.get("audio") or meta.get("audio_intent") or {})
            if isinstance(analysis.get("audio") or meta.get("audio_intent"), dict)
            else {}
        )
        user_prompt = str(graph.get("description") or "")
        brief_md = ""
        source = "heuristic"
        notes = "Heuristic brief from script analysis."
        if use_llm:
            try:
                system = (
                    "You are the Designer Supervisor. Author a detailed creative brief "
                    "for a short film. Cover: character identity locks (face/hair/body/costume), "
                    "scene geography (spatial lock), motion consistency, time-coherent continuity "
                    "(e.g. after a man stands and leaves he must not reappear seated), "
                    "shot-view coverage for every named beat, audio policy. "
                    "Stay faithful to the user prompt — do not invent plot. "
                    "Respond with markdown brief only (no JSON wrapper)."
                )
                result = await call_model_tool(
                    prompt=json.dumps(
                        {
                            "user_prompt": user_prompt,
                            "characters": characters,
                            "scenes": scenes,
                            "shots": analysis.get("shots"),
                            "audio": audio,
                            "spatial_lock": meta.get("spatial_lock"),
                            "supervisor_brief_notes": meta.get("supervisor_brief_notes")
                            or ((meta.get("supervisor_plan") or {}).get("brief_notes")),
                        },
                        ensure_ascii=False,
                    ),
                    system=system,
                    optimize_for="quality",
                    max_tokens=32768,
                )
                text = str(result.get("text") or "").strip()
                if text and len(text) > 80 and not text.startswith("[local-tool-fallback]"):
                    brief_md = text if text.lstrip().startswith("#") else f"# Brief\n\n{text}"
                    source = "llm"
                    notes = "Supervisor LLM authored creative brief."
            except Exception:  # noqa: BLE001
                logger.info("Supervisor author_creative_brief LLM failed", exc_info=True)
        if not brief_md:
            brief_md = _write_brief_markdown(user_prompt, characters, scenes, audio)
            source = "heuristic"
            notes = "Heuristic brief (LLM unavailable or failed)."

        stamped = False
        for node in graph.get("nodes") or []:
            cfg = dict(node.get("config") or {})
            if _role_key(node) != "brief" and str(node.get("id") or "") != "n_brief":
                continue
            if cfg.get("skip_llm"):
                cfg["prewritten"] = brief_md
            else:
                cfg["draft_prewritten"] = brief_md
                cfg["prewritten"] = brief_md
            cfg["kind"] = "agent"
            node["config"] = cfg
            stamped = True
            break
        meta["approved_brief"] = brief_md
        meta["supervisor_brief_ack"] = {
            "ok": True,
            "source": source,
            "notes": notes,
            "stamped": stamped,
            "chars": len(brief_md),
        }
        graph["metadata"] = meta
        return dict(meta["supervisor_brief_ack"])

    async def author_storyboard(
        self, graph: DesignerExecutionGraph, *, use_llm: bool = False
    ) -> dict[str, Any]:
        """From approved brief + script_analysis, author storyboard markdown + planned_shots."""
        from jiuwenswarm.server.runtime.designer.smart_graph import _write_storyboard_markdown

        meta = dict(graph.get("metadata") or {})
        analysis = (
            dict(meta.get("script_analysis") or {})
            if isinstance(meta.get("script_analysis"), dict)
            else {}
        )
        characters = list(analysis.get("characters") or [])
        shots = list(analysis.get("shots") or [])
        user_prompt = str(graph.get("description") or "")
        approved_brief = str(meta.get("approved_brief") or "")[:4000]
        sb_md = ""
        source = "heuristic"
        notes = "Heuristic storyboard from planned shots."
        if use_llm:
            try:
                system = (
                    "You are the Designer Supervisor (Director). Author a hierarchical "
                    "storyboard: Scene (setting_id) → Keyframes/shots. FIRST list every "
                    "named human as characters[] (id, name, description) — one solo card "
                    "each. NOT every character appears in every scene. "
                    "Different setting_id = DIFFERENT place (distinct architecture). "
                    "Group shots by setting_id. First shot of each setting: "
                    "keyframe_strategy=compose_from_solo_refs — composer places ONLY "
                    "on_screen cast with cast_actions (who is doing what). "
                    "Later same setting: edit_prior_keyframe (architecture locked); "
                    "storyboard updates on_screen / offscreen / cast_actions. "
                    "offscreen = in this scene but not in frame; never draw them. "
                    "Cast absent from a setting must not appear there. "
                    "NO empty scene plates. Crowd/extras persist across same-setting shots "
                    "unless they exit. Each shot needs timeline, camera, action, "
                    "on_screen, offscreen, cast_actions, featured_cast_ids, setting_id, "
                    "continuity_lock, keyframe_prompt, exiting_character_ids, "
                    "speech_by_character (map character_id→exact spoken line for THIS beat; "
                    "empty {} if silent), speech_line (joined fallback). "
                    "Film-wide locks: language_lock (e.g. en/zh — ALL dialogue in that "
                    "language), bgm_lock {mood,style,instruments,continuity,rule}, "
                    "include_speech, include_music. "
                    "Respond JSON only: "
                    '{"characters":[{"id":"char_1","name":"...","description":"..."}],'
                    '"shots":[{"shot_index":1,"timeline":"0-5s","camera":"...",'
                    '"action":"...","on_screen":["char_1"],"offscreen":["char_2"],'
                    '"featured_cast_ids":["char_1"],"cast_actions":{"char_1":"preaching"},'
                    '"ensemble_cast_ids":["char_1","char_2"],"setting_id":"set_1",'
                    '"keyframe_strategy":"compose_from_solo_refs",'
                    '"continuity_lock":{"forbid":"..."},"keyframe_prompt":"...",'
                    '"exiting_character_ids":[],'
                    '"speech_by_character":{"char_1":"exact line"},"speech_line":"..."}],'
                    '"language_lock":"en",'
                    '"bgm_lock":{"mood":"...","style":"...","instruments":"...",'
                    '"continuity":"same bed","rule":"non-vocal underscore"},'
                    '"include_speech":true,"include_music":true,'
                    '"storyboard_markdown":"...","notes":"...","target_shot_count":N,'
                    '"skip_scene_plate":true}'
                )
                try:
                    from jiuwenswarm.server.runtime.designer.video_styles import (
                        VIDEO_STYLE_FINAL_FRAME_REVERSE,
                        resolve_video_style,
                        video_style_clause,
                        video_style_skill_excerpt,
                    )

                    vs = resolve_video_style(graph, user_prompt)
                    if vs == VIDEO_STYLE_FINAL_FRAME_REVERSE:
                        system = (
                            system
                            + " "
                            + video_style_clause(vs)
                            + " "
                            + video_style_skill_excerpt(vs)
                        )
                except Exception:  # noqa: BLE001
                    pass
                from jiuwenswarm.server.runtime.designer.experiments.director_contract import (
                    infer_shot_budget,
                )

                sb_lock = _shot_expand_lock_count(
                    graph=graph, analysis=analysis, current_shots=shots
                )
                if sb_lock:
                    sb_rule = (
                        "Keep shot count EQUAL to the provided shots list. "
                        "Duration is timeline, not a license to add rows."
                    )
                else:
                    # No-LLM skeleton: the director owns the beat breakdown.
                    sb_rule = (
                        "The provided shots are a DRAFT skeleton, not the final count. "
                        "Break the story into as many shots as its beats need, up to "
                        f"{infer_shot_budget(user_prompt, analysis)}."
                    )
                    try:
                        from jiuwenswarm.server.runtime.designer.video_styles import (
                            resolve_video_style,
                            video_style_min_shots,
                        )

                        floor = video_style_min_shots(resolve_video_style(graph, user_prompt))
                        if floor >= 2:
                            sb_rule += (
                                f" This video style is never a single take: author at "
                                f"least {floor} shots — spatial push-in, key close-up(s), "
                                "deceleration/settle, then a final beat that matches the "
                                "reference composition."
                            )
                    except Exception:  # noqa: BLE001
                        pass
                result = await call_model_tool(
                    prompt=json.dumps(
                        {
                            "user_prompt": user_prompt,
                            "approved_brief": approved_brief,
                            "characters": characters,
                            "shots": shots,
                            "spatial_lock": meta.get("spatial_lock"),
                            "rule": sb_rule,
                        },
                        ensure_ascii=False,
                    ),
                    system=system,
                    optimize_for="quality",
                    max_tokens=65536,
                )
                parsed = _extract_json_object(str(result.get("text") or "")) or {}
                llm_chars = parsed.get("characters") if isinstance(parsed.get("characters"), list) else []
                if llm_chars:
                    cleaned_chars: list[dict[str, Any]] = []
                    for i, raw in enumerate(llm_chars, start=1):
                        if not isinstance(raw, dict):
                            continue
                        cid = str(raw.get("id") or f"char_{i}").strip() or f"char_{i}"
                        name = str(raw.get("name") or cid).strip() or cid
                        cleaned_chars.append(
                            {
                                "id": cid,
                                "name": name,
                                "description": str(raw.get("description") or name)[:600],
                            }
                        )
                    if cleaned_chars:
                        characters = cleaned_chars
                        analysis["characters"] = characters
                        analysis["source"] = "llm"
                        source = "llm"
                        notes = "Supervisor LLM authored cast + storyboard."
                llm_shots = parsed.get("shots") if isinstance(parsed.get("shots"), list) else []
                if llm_shots:
                    cleaned: list[dict[str, Any]] = []
                    for i, raw in enumerate(llm_shots, start=1):
                        if not isinstance(raw, dict):
                            continue
                        shot = dict(raw)
                        shot["shot_index"] = int(shot.get("shot_index") or i)
                        if not str(shot.get("timeline") or "").strip():
                            shot["timeline"] = f"{(i - 1) * 5:.1f}-{i * 5:.1f}s"
                        if isinstance(shot.get("continuity_lock"), dict):
                            shot["continuity_lock"] = {
                                str(k): str(v) for k, v in shot["continuity_lock"].items()
                            }
                        elif str(shot.get("action") or "").strip():
                            shot["continuity_lock"] = _infer_continuity_lock(
                                str(shot.get("action") or "")
                            )
                        if isinstance(shot.get("speech_by_character"), dict):
                            shot["speech_by_character"] = {
                                str(k): str(v)[:280]
                                for k, v in shot["speech_by_character"].items()
                                if str(v).strip()
                            }
                        if shot.get("speech_line"):
                            shot["speech_line"] = str(shot.get("speech_line"))[:500]
                        cleaned.append(shot)
                    if cleaned:
                        shots = _apply_llm_shot_list(
                            shots,
                            cleaned,
                            lock_count=_shot_expand_lock_count(
                                graph=graph,
                                analysis=analysis,
                                current_shots=shots,
                            ),
                        )
                        source = "llm"
                        analysis["source"] = "llm"
                        notes = str(parsed.get("notes") or "Supervisor LLM authored storyboard.")[
                            :1000
                        ]
                # Film-wide audio locks from Supervisor storyboard JSON.
                audio = dict(analysis.get("audio") or {})
                if parsed.get("language_lock"):
                    analysis["language_lock"] = str(parsed.get("language_lock"))[:16]
                    audio["language_lock"] = analysis["language_lock"]
                if isinstance(parsed.get("bgm_lock"), dict):
                    analysis["bgm_lock"] = {
                        str(k): str(v)[:280] for k, v in parsed["bgm_lock"].items()
                    }
                    audio["bgm_lock"] = analysis["bgm_lock"]
                if "include_speech" in parsed:
                    audio["include_speech"] = bool(parsed.get("include_speech"))
                if "include_music" in parsed:
                    audio["include_music"] = bool(parsed.get("include_music"))
                if audio.get("include_speech") and audio.get("include_music"):
                    audio["policy"] = "speech_and_music"
                elif audio.get("include_speech"):
                    audio["policy"] = "speech"
                elif audio.get("include_music"):
                    audio["policy"] = audio.get("policy") or "optional_music"
                analysis["audio"] = audio
                md_candidate = str(parsed.get("storyboard_markdown") or "").strip()
                if md_candidate and len(md_candidate) > 40:
                    sb_md = md_candidate
                    source = "llm"
                    analysis["source"] = "llm"
            except Exception:  # noqa: BLE001
                logger.info("Supervisor author_storyboard LLM failed", exc_info=True)

        from jiuwenswarm.server.runtime.designer.audio_locks import ensure_audio_locks_on_analysis

        analysis = ensure_audio_locks_on_analysis(analysis, user_prompt)
        shots = list(analysis.get("shots") or shots)
        characters = list(analysis.get("characters") or characters)
        meta["language_lock"] = str(analysis.get("language_lock") or "en")
        if isinstance(analysis.get("bgm_lock"), dict):
            meta["bgm_lock"] = analysis["bgm_lock"]

        if shots:
            analysis["shots"] = shots
            meta["script_analysis"] = analysis
        if not sb_md:
            sb_md = _write_storyboard_markdown(shots, characters)

        stamped = False
        for node in graph.get("nodes") or []:
            cfg = dict(node.get("config") or {})
            if _role_key(node) != "storyboard" and str(node.get("id") or "") != "n_storyboard":
                continue
            if cfg.get("skip_llm"):
                cfg["prewritten"] = sb_md
            else:
                cfg["draft_prewritten"] = sb_md
                cfg.pop("prewritten", None)
                cfg["skip_llm"] = False
            cfg["planned_shots"] = shots
            cfg["kind"] = "agent"
            cfg["delegate"] = "agent"
            node["config"] = cfg
            stamped = True
            break
        if shots:
            analysis["target_shot_count"] = len(shots)
            meta["script_analysis"] = analysis
        # Propagate timelines/continuity/audio locks into frame/clip configs once.
        from jiuwenswarm.server.runtime.designer.audio_locks import stamp_audio_fields_on_clip_config

        for node in graph.get("nodes") or []:
            cfg = dict(node.get("config") or {})
            if _role_key(node) not in {"frame", "clip", "keyframe"}:
                continue
            idx = int(cfg.get("shot_index") or 0)
            for shot in shots:
                if int(shot.get("shot_index") or 0) != idx:
                    continue
                if shot.get("action"):
                    cfg["shot_action"] = str(shot["action"])[:500]
                if shot.get("camera"):
                    cfg["camera"] = str(shot["camera"])[:120]
                if shot.get("timeline"):
                    cfg["timeline"] = str(shot["timeline"])[:40]
                if isinstance(shot.get("continuity_lock"), dict):
                    cfg["continuity_lock"] = shot["continuity_lock"]
                if isinstance(shot.get("character_ids"), list):
                    cfg["character_ids"] = [str(x) for x in shot["character_ids"] if str(x)]
                if _role_key(node) == "clip":
                    routing = meta.get("audio_routing") if isinstance(meta.get("audio_routing"), dict) else {}
                    cfg = stamp_audio_fields_on_clip_config(
                        cfg,
                        shot=shot,
                        analysis=analysis,
                        meta=meta,
                        clip_embedded=bool(routing.get("clip_embedded")),
                    )
                else:
                    if shot.get("speech_line"):
                        cfg["speech_line"] = str(shot.get("speech_line"))[:500]
                    if isinstance(shot.get("speech_by_character"), dict):
                        cfg["speech_by_character"] = shot["speech_by_character"]
                    cfg["language_lock"] = str(
                        shot.get("language_lock") or analysis.get("language_lock") or "en"
                    )
                node["config"] = cfg
                break

        meta["approved_storyboard"] = sb_md
        meta["supervisor_storyboard_ack"] = {
            "ok": True,
            "source": source,
            "notes": notes,
            "stamped": stamped,
            "shot_count": len(shots),
            "language_lock": meta.get("language_lock"),
            "bgm_lock": bool(meta.get("bgm_lock")),
        }
        graph["metadata"] = meta
        return dict(meta["supervisor_storyboard_ack"])

    async def design_execution_graph(
        self,
        graph: DesignerExecutionGraph,
        *,
        use_llm: bool = True,
        optimize_for: str = "quality",
    ) -> dict[str, Any]:
        """Supervisor owns flexible multi-shot topology from Brief+Storyboard.

        Not a frozen single-keyframe template: LLM expands shots from the locked
        storyboard, then materializes one frame+clip agent per shot.
        """
        from jiuwenswarm.server.runtime.designer.smart_graph import (
            apply_runtime_delegate,
            build_smart_video_graph,
        )

        meta = dict(graph.get("metadata") or {})
        analysis = (
            dict(meta.get("script_analysis") or {})
            if isinstance(meta.get("script_analysis"), dict)
            else {}
        )
        shots = [s for s in (analysis.get("shots") or []) if isinstance(s, dict)]
        characters = list(analysis.get("characters") or [])
        user_prompt = str(graph.get("description") or meta.get("user_prompt") or "")
        approved_brief = str(meta.get("approved_brief") or "")[:5000]
        approved_sb = str(meta.get("approved_storyboard") or "")[:5000]
        source = "storyboard"
        notes = ""

        if use_llm:
            try:
                system = (
                    "You are the Designer Supervisor (Director). Design the execution graph "
                    "from the approved Brief + Storyboard. Return JSON only. "
                    "MUST include characters[] — every named human gets one solo identity "
                    "card (id, name, description). NOT every character in every scene. "
                    "MUST include shots[] grouped by setting_id (distinct places). "
                    "YOU own target_shot_count (prefer ≤8, hard max 16; typical 1–6). "
                    "One clip = one continuous beat. Reuse one KF for local motion or ONE "
                    "camera move (pan/dolly/push); Wan I2V prompt = motion+camera only "
                    "(~3–5s preferred). New KF on hard cut, new setting, wardrobe/prop "
                    "change, large pose/framing jump, or on-screen cast change. "
                    "Qwen KF: lock identity+wardrobe; ≤2–3 people with refs; one variable "
                    "per new KF. Honor explicit N-shot / N分镜 as a HARD ceiling. "
                    "First KF of each setting: compose_from_solo_refs with on_screen + "
                    "cast_actions (composer decides who appears and what they are doing). "
                    "Later same setting: edit_prior_keyframe (architecture locked). "
                    "offscreen stay out of frame. skip_scene_plate=true. "
                    "Each shot: shot_index, timeline, camera, action, on_screen, offscreen, "
                    "cast_actions, featured_cast_ids, ensemble_cast_ids, setting_id, "
                    "keyframe_prompt, exiting_character_ids, keyframe_strategy. "
                    "Schema: "
                    '{"characters":[{"id":"char_1","name":"...","description":"..."}],'
                    '"shots":[...],"target_shot_count":N,"include_speech":bool,'
                    '"include_music":bool,"skip_scene_plate":true,"notes":"..."}'
                )
                from jiuwenswarm.server.runtime.designer.experiments.director_contract import (
                    infer_shot_budget,
                )

                shot_ceiling = infer_shot_budget(user_prompt, analysis)
                result = await call_model_tool(
                    prompt=json.dumps(
                        {
                            "user_prompt": user_prompt,
                            "approved_brief": approved_brief,
                            "approved_storyboard": approved_sb,
                            "characters": characters,
                            "current_shots": shots,
                            "target_shot_count": shot_ceiling or analysis.get("target_shot_count"),
                            "rule": (
                                "Decide shot count wisely: prefer fewer; merge same-cast "
                                "continuous motion into one beat. Explicit N-shot / "
                                "target_shot_count from the user is a hard ceiling. Soft prefer "
                                "≤8 shots. Do not invent extra keyframes or coverage views as "
                                "new nodes after the canvas is frozen. All solo cast cards "
                                "before keyframes; compose first KF per setting_id; edit_prior "
                                "only within the same setting_id. Per-shot on_screen is "
                                "authoritative for who appears — not every solo in every frame."
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    system=system,
                    optimize_for=optimize_for,
                    max_tokens=65536,
                )
                parsed = _extract_json_object(str(result.get("text") or "")) or {}
                llm_chars = parsed.get("characters") if isinstance(parsed.get("characters"), list) else []
                if llm_chars:
                    cleaned_chars: list[dict[str, Any]] = []
                    for i, raw in enumerate(llm_chars, start=1):
                        if not isinstance(raw, dict):
                            continue
                        cid = str(raw.get("id") or f"char_{i}").strip() or f"char_{i}"
                        name = str(raw.get("name") or cid).strip() or cid
                        cleaned_chars.append(
                            {
                                "id": cid,
                                "name": name,
                                "description": str(raw.get("description") or name)[:600],
                            }
                        )
                    if cleaned_chars:
                        characters = cleaned_chars
                        analysis["characters"] = characters
                        analysis["source"] = "llm"
                        source = "llm"
                llm_shots = parsed.get("shots") if isinstance(parsed.get("shots"), list) else []
                cleaned: list[dict[str, Any]] = []
                for i, raw in enumerate(llm_shots, start=1):
                    if not isinstance(raw, dict):
                        continue
                    shot = dict(raw)
                    shot["shot_index"] = int(shot.get("shot_index") or i)
                    if not str(shot.get("timeline") or "").strip():
                        shot["timeline"] = f"{(i - 1) * 5:.1f}-{i * 5:.1f}s"
                    cleaned.append(shot)
                if cleaned:
                    shots = _apply_llm_shot_list(
                        shots,
                        cleaned,
                        lock_count=_shot_expand_lock_count(
                            graph=graph,
                            analysis=analysis,
                            current_shots=shots,
                        ),
                    )
                    source = "llm"
                    analysis["source"] = "llm"
                    notes = str(parsed.get("notes") or "")[:1000]
                    from jiuwenswarm.server.runtime.designer.experiments.director_contract import (
                        _HARD_MAX_SHOTS,
                        _SOFT_MAX_SHOTS,
                        _explicit_shot_count_from_prompt,
                    )

                    explicit_n = int(_explicit_shot_count_from_prompt(user_prompt) or 0)
                    try:
                        llm_tsc = int(parsed.get("target_shot_count") or 0)
                    except (TypeError, ValueError):
                        llm_tsc = 0
                    # LLM redesign owns N; explicit user language is the only hard ceiling.
                    if explicit_n >= 1:
                        shots = shots[: min(explicit_n, _HARD_MAX_SHOTS)]
                    else:
                        cap = min(_SOFT_MAX_SHOTS, _HARD_MAX_SHOTS)
                        keep = min(cap, llm_tsc) if llm_tsc >= 1 else min(cap, len(shots))
                        shots = shots[:keep]
                    for i, sh in enumerate(shots, start=1):
                        sh["shot_index"] = i
                    analysis["target_shot_count"] = len(shots)
                    audio = dict(analysis.get("audio") or {})
                    if "include_speech" in parsed:
                        audio["include_speech"] = bool(parsed.get("include_speech"))
                    if "include_music" in parsed:
                        audio["include_music"] = bool(parsed.get("include_music"))
                    if audio.get("include_speech") and audio.get("include_music"):
                        audio["policy"] = "speech_and_music"
                    analysis["audio"] = audio
                    if parsed.get("skip_scene_plate") is not None:
                        analysis["skip_scene_plate"] = bool(parsed.get("skip_scene_plate"))
            except Exception:  # noqa: BLE001
                logger.info("Supervisor design_execution_graph LLM failed; using storyboard shots", exc_info=True)
                notes = "LLM graph design failed; materializing from storyboard shots."

        # Final clamp even on non-LLM path — soft max unless user asked for explicit N.
        try:
            from jiuwenswarm.server.runtime.designer.experiments.director_contract import (
                _HARD_MAX_SHOTS,
                _SOFT_MAX_SHOTS,
                _explicit_shot_count_from_prompt,
                infer_shot_budget,
            )

            explicit_n = int(_explicit_shot_count_from_prompt(user_prompt) or 0)
            if explicit_n >= 1:
                final_ceiling = min(explicit_n, _HARD_MAX_SHOTS)
            else:
                # Prefer live shot list length; soft-cap only.
                final_ceiling = min(
                    _SOFT_MAX_SHOTS,
                    max(len(shots), int(infer_shot_budget(user_prompt, analysis) or 0), 1),
                )
            if final_ceiling >= 1 and len(shots) > final_ceiling:
                shots = shots[:final_ceiling]
                for i, sh in enumerate(shots, start=1):
                    if isinstance(sh, dict):
                        sh["shot_index"] = i
        except Exception:  # noqa: BLE001
            pass
        if not shots:
            shots = [
                {
                    "shot_index": 1,
                    "action": user_prompt[:300],
                    "camera": "medium / eye-level",
                    "character_ids": [str(characters[0].get("id"))] if characters else ["char_1"],
                    "keyframe_prompt": user_prompt[:300],
                    "timeline": "0.0-5.0s",
                    "setting_id": "set_1",
                }
            ]
        analysis["shots"] = shots
        analysis["target_shot_count"] = len(shots)
        meta["script_analysis"] = analysis

        # Cast shrink guard: never replace a richer solo cast with a thinner redesign.
        old_solos = sum(
            1
            for n in (graph.get("nodes") or [])
            if str(n.get("id") or "").startswith("n_character")
        )
        new_humans = [
            c
            for c in characters
            if isinstance(c, dict)
            and c.get("id")
            and not c.get("is_prop")
            and str(c.get("cast_kind") or "") not in {"brand_mascot", "prop"}
        ]
        if old_solos > 1 and len(new_humans) < old_solos:
            logger.info(
                "design_execution_graph rejected cast shrink %s → %s; keeping current graph",
                old_solos,
                len(new_humans),
            )
            meta["pending_llm_analysis"] = False
            meta["pending_supervisor_graph"] = False
            meta["supervisor_composed_on_bootstrap"] = True
            meta["graph_designed_by_supervisor"] = False
            meta["supervisor_graph_ack"] = {
                "ok": True,
                "source": "kept_prior_cast",
                "notes": "Rejected redesign that would shrink solo cast.",
                "shot_count": len(shots),
            }
            graph["metadata"] = meta
            return {
                "ok": True,
                "source": "kept_prior_cast",
                "notes": meta["supervisor_graph_ack"]["notes"],
                "shot_count": len(shots),
            }

        old_id = str(graph.get("graph_id") or "")
        project_id = str(graph.get("project_id") or "project")
        rebuilt = build_smart_video_graph(
            project_id=project_id,
            prompt=user_prompt,
            analysis=analysis,
            title=str(graph.get("title") or "") or None,
            optimize_for=optimize_for,
            ai_mode=True,
        )
        rebuilt["graph_id"] = old_id or rebuilt.get("graph_id")
        rebuilt["project_id"] = project_id
        rmeta = dict(rebuilt.get("metadata") or {})
        for key in (
            "approved_brief",
            "approved_storyboard",
            "user_prompt",
            "capability_plan",
            "agent_runtime",
            "use_prior_feedback",
            "prior_feedback",
            "last_improvement_plan",
            "supervisor_skill_excerpt",
            "active_supervisor_skill",
            "manager_lock_ack",
        ):
            if key in meta and meta.get(key) is not None:
                rmeta[key] = meta.get(key)
        rmeta["script_analysis"] = analysis
        rmeta["supervisor_owns_graph"] = True
        rebuilt["metadata"] = rmeta
        # Uploads are user-authored assets: a redesign must not delete them.
        from jiuwenswarm.server.runtime.designer.user_references import carry_user_references

        carry_user_references(meta, rebuilt)
        rmeta = dict(rebuilt.get("metadata") or {})
        # Only freeze after the director actually authored shots. A heuristic
        # 1-shot skeleton must not lock Play/redesign to a single beat.
        rmeta["freeze_shot_topology"] = source == "llm" and len(shots) >= 1
        rmeta["lean_pipeline"] = False
        rmeta["graph_designed_by_supervisor"] = True
        rmeta["supervisor_graph_ack"] = {
            "ok": True,
            "source": source,
            "notes": notes,
            "shot_count": len(shots),
            "frame_nodes": sum(
                1
                for n in (rebuilt.get("nodes") or [])
                if str(n.get("id") or "").startswith("n_frame_")
            ),
        }
        rebuilt["metadata"] = rmeta
        rebuilt = apply_runtime_delegate(rebuilt)
        # Replace caller's graph contents in-place-friendly: return rebuilt via ack
        # and let executor assign graph = result.
        # Mutate graph dict so callers holding the same reference also see updates.
        graph.clear()
        graph.update(rebuilt)
        return dict(rmeta["supervisor_graph_ack"])


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
            max_tokens=8192,
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
        role = _role_key(node)
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
                if _role_key(node) != "storyboard":
                    continue
                cfg["prewritten"] = sb_md
                cfg["planned_shots"] = planned
                node["config"] = cfg
                notes.append(f"{node.get('id')}: storyboard refreshed from cast-focus fixes")
    except Exception:  # noqa: BLE001
        pass

    for node in graph.get("nodes") or []:
        cfg = dict(node.get("config") or {})
        role = _role_key(node)
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
                    if _role_key(other) != "character_design":
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
                    if _role_key(other) != "character_design":
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


def _ensure_all_solos_precede_keyframes(graph: DesignerExecutionGraph) -> list[str]:
    """Every identity solo sheet is a data predecessor of every keyframe node.

    Guarantees all character cards finish before any compose/edit keyframe runs.
    """
    notes: list[str] = []
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    by_id = {str(n.get("id") or ""): n for n in nodes if n.get("id")}
    solo_ids = [
        nid
        for nid, node in by_id.items()
        if _role_key(node) == "character_design"
        and not bool((node.get("config") or {}).get("combined_cast"))
    ]
    if not solo_ids:
        return notes
    edge_pairs = {
        (str(e.get("source") or ""), str(e.get("target") or ""))
        for e in (graph.get("edges") or [])
        if isinstance(e, dict)
    }
    for node in nodes:
        if _role_key(node) not in {"frame", "keyframe"}:
            continue
        fid = str(node.get("id") or "")
        if not fid:
            continue
        cfg = dict(node.get("config") or {})
        inputs = [str(x) for x in (cfg.get("inputs") or []) if str(x)]
        changed = False
        for sid in solo_ids:
            if sid not in inputs:
                inputs.append(sid)
                changed = True
            if (sid, fid) not in edge_pairs:
                graph.setdefault("edges", []).append(
                    {
                        "id": f"e_solo_{sid}_{fid}",
                        "source": sid,
                        "target": fid,
                        "kind": "data",
                        "label": "identity_solo",
                    }
                )
                edge_pairs.add((sid, fid))
                notes.append(f"{fid}: solo_gate <- {sid}")
                changed = True
        if changed:
            cfg["inputs"] = list(dict.fromkeys(inputs))
            node["config"] = cfg
    return notes


def _identity_consistency_patch(graph: DesignerExecutionGraph) -> list[str]:
    """Manager gate: solos first; every KF composes; same-setting prompt handoff."""
    notes: list[str] = []
    solo_by_cid: dict[str, str] = {}
    costume_by_cid: dict[str, str] = {}
    all_solo_ids: list[str] = []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        if _role_key(node) != "character_design":
            continue
        if cfg.get("combined_cast"):
            continue
        nid = str(node.get("id") or "")
        if nid:
            all_solo_ids.append(nid)
        oids = [str(x) for x in (cfg.get("character_ids") or []) if str(x)]
        if not oids and cfg.get("character_id"):
            oids = [str(cfg.get("character_id"))]
        if len(oids) == 1:
            solo_by_cid[oids[0]] = nid
            costume_by_cid[oids[0]] = str(cfg.get("costume_lock") or cfg.get("prompt") or "")[:200]

    meta = dict(graph.get("metadata") or {})
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    characters = list(analysis.get("characters") or [])
    id_to_name = {
        str(c.get("id")): str(c.get("name") or c.get("id"))
        for c in characters
        if isinstance(c, dict) and c.get("id")
    }
    scene_locks = (
        meta.get("scene_locks")
        if isinstance(meta.get("scene_locks"), dict)
        else (
            analysis.get("scene_locks")
            if isinstance(analysis.get("scene_locks"), dict)
            else {}
        )
    )

    notes.extend(_ensure_all_solos_precede_keyframes(graph))

    frame_nodes = sorted(
        [
            n
            for n in (graph.get("nodes") or [])
            if isinstance(n, dict) and _role_key(n) in {"frame", "keyframe"}
        ],
        key=lambda n: int((n.get("config") or {}).get("shot_index") or 0),
    )

    prev_frame_by_setting: dict[str, str] = {}
    scene_master_by_setting: dict[str, str] = {}
    spatial_meta = meta.get("spatial_lock") if isinstance(meta.get("spatial_lock"), dict) else {}
    spatial_by_setting = (
        meta.get("spatial_lock_by_setting")
        if isinstance(meta.get("spatial_lock_by_setting"), dict)
        else {}
    )
    existing_masters = (
        meta.get("scene_masters") if isinstance(meta.get("scene_masters"), dict) else {}
    )
    for sid, fid in existing_masters.items():
        if str(sid).strip() and str(fid).strip():
            scene_master_by_setting[str(sid).strip()] = str(fid).strip()

    setting_order: list[str] = []
    for n in frame_nodes:
        sid = str((n.get("config") or {}).get("setting_id") or "set_1").strip() or "set_1"
        if sid not in setting_order:
            setting_order.append(sid)
    setting_num = {sid: i + 1 for i, sid in enumerate(setting_order)}

    def _stamp_media_node(node: dict[str, Any], *, assign_strategy: bool) -> None:
        nonlocal notes
        cfg = dict(node.get("config") or {})
        role = _role_key(node)
        cids = [str(x) for x in (cfg.get("character_ids") or []) if str(x)]
        solo_nodes = [solo_by_cid[cid] for cid in cids if cid in solo_by_cid]
        if solo_nodes and list(cfg.get("character_node_ids") or []) != solo_nodes:
            cfg["character_node_ids"] = solo_nodes
            notes.append(f"{node.get('id')}: identity_refs -> solo sheets {solo_nodes}")
        names = [id_to_name.get(cid, cid) for cid in cids]
        from jiuwenswarm.server.runtime.designer.experiments.clothing_lock import (
            costume_lock_for_ids,
            enrich_character_clothing,
        )

        analysis_chars = list(
            ((graph.get("metadata") or {}).get("script_analysis") or {}).get("characters")
            or []
        )
        for ch in analysis_chars:
            if isinstance(ch, dict):
                enrich_character_clothing(ch)
        costume_lock = str(cfg.get("costume_lock") or "").strip()
        detailed = costume_lock_for_ids(analysis_chars, cids) if cids else ""
        if detailed and (
            not costume_lock
            or (
                "top=" not in costume_lock
                and "bottom=" not in costume_lock
                and "outfit=" not in costume_lock
            )
        ):
            costume_lock = detailed
            cfg["costume_lock"] = costume_lock
            notes.append(f"{node.get('id')}: clothing costume_lock stamped")
        elif not costume_lock and cids:
            costume_lock = "; ".join(
                f"{id_to_name.get(cid, cid)}: {costume_by_cid.get(cid, '')[:120]}".strip(": ")
                for cid in cids
            )[:720]
            cfg["costume_lock"] = costume_lock
            notes.append(f"{node.get('id')}: costume_lock stamped")

        setting_id = str(cfg.get("setting_id") or "set_1").strip() or "set_1"
        cfg["setting_id"] = setting_id
        strategy = "compose_from_solo_refs"
        is_master = False
        handoff_from = None
        if assign_strategy and role in {"frame", "keyframe"}:
            prior_in_set = prev_frame_by_setting.get(setting_id)
            if prior_in_set:
                is_master = False
                handoff_from = scene_master_by_setting.get(setting_id) or prior_in_set
                cfg["scene_prompt_handoff_from"] = handoff_from
                cfg["is_scene_master"] = False
                cfg["scene_master_frame_id"] = handoff_from
                cfg.pop("prior_keyframe_node_id", None)
                inputs = list(cfg.get("inputs") or [])
                if handoff_from and handoff_from not in inputs:
                    inputs.append(handoff_from)
                cfg["inputs"] = inputs
                notes.append(
                    f"{node.get('id')}: same-setting compose + prompt handoff from "
                    f"{handoff_from} ({setting_id})"
                )
            else:
                is_master = True
                cfg.pop("prior_keyframe_node_id", None)
                cfg.pop("scene_prompt_handoff_from", None)
                cfg["is_scene_master"] = True
                cfg["scene_master_frame_id"] = str(node.get("id") or "")
                notes.append(
                    f"{node.get('id')}: SCENE MASTER compose for setting {setting_id}"
                )
            cfg["keyframe_strategy"] = strategy
            nid = str(node.get("id") or "")
            if is_master and nid:
                scene_master_by_setting[setting_id] = nid
            if nid:
                prev_frame_by_setting[setting_id] = nid

        bible = cfg.get("scene_bible") if isinstance(cfg.get("scene_bible"), dict) else None
        if not bible and isinstance(scene_locks.get(setting_id), dict):
            bible = dict(scene_locks[setting_id])
            cfg["scene_bible"] = bible

        shot_spatial = (
            spatial_by_setting.get(setting_id)
            if isinstance(spatial_by_setting.get(setting_id), dict)
            else None
        )
        if isinstance(shot_spatial, dict):
            cfg["spatial_lock"] = dict(shot_spatial)
        elif not isinstance(cfg.get("spatial_lock"), dict) and spatial_meta:
            cfg["spatial_lock"] = dict(spatial_meta)

        master_frame = str(
            cfg.get("scene_master_frame_id")
            or scene_master_by_setting.get(setting_id)
            or ""
        ).strip() or None
        identity_refs = {
            "character_ids": cids,
            "character_node_ids": list(cfg.get("character_node_ids") or solo_nodes),
            "cast_names": names or list(cfg.get("cast_names") or []),
            "costume_lock": costume_lock,
            "scene_node_id": None,
            "master_scene_node_id": None,
            "scene_master_frame_id": master_frame,
            "is_scene_master": bool(cfg.get("is_scene_master")),
            "prior_keyframe_node_id": None,
            "scene_prompt_handoff_from": cfg.get("scene_prompt_handoff_from") or handoff_from,
            "keyframe_strategy": "compose_from_solo_refs",
            "setting_id": setting_id,
            "view_key": cfg.get("view_key"),
            "spatial_lock": cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else None,
            "occupancy": cfg.get("occupancy") if isinstance(cfg.get("occupancy"), dict) else None,
            "skip_scene_plate": True,
            "scene_continuity_mode": "compose_solos_shared_scene_prompt",
            "scene_bible": bible,
            "all_solo_node_ids": list(all_solo_ids),
        }
        cfg["identity_refs"] = identity_refs
        cfg["skip_scene_plate"] = True
        if names:
            cfg["cast_names"] = identity_refs["cast_names"]
        # Sensible LLM-style node names (Brief: … / Scene N: Shot M: …).
        from jiuwenswarm.server.runtime.designer.node_labels import (
            derive_shot_name,
            derive_story_name,
            label_character,
            label_clip,
            label_shot,
        )

        story_name = derive_story_name(
            analysis=analysis,
            prompt=str(graph.get("description") or ""),
            graph_title=str(graph.get("title") or ""),
        )
        scene_n = setting_num.get(setting_id, 1)
        shot_i = int(cfg.get("shot_index") or 0)
        # Per-setting shot ordinal from film order.
        shot_n = 0
        for fn in frame_nodes:
            fsid = str((fn.get("config") or {}).get("setting_id") or "set_1").strip() or "set_1"
            fi = int((fn.get("config") or {}).get("shot_index") or 0)
            if fsid != setting_id:
                continue
            if fi <= shot_i:
                shot_n += 1
        shot_n = max(1, shot_n or shot_i or 1)
        shot_name = derive_shot_name(
            {
                "title": cfg.get("shot_title"),
                "action": cfg.get("shot_action"),
                "keyframe_prompt": (cfg.get("generate") or {}).get("prompt")
                if isinstance(cfg.get("generate"), dict)
                else "",
            },
            fallback_index=shot_n,
        )
        if role in {"frame", "keyframe"} and shot_i:
            label = label_shot(
                scene_number=scene_n, shot_number=shot_n, shot_name=shot_name
            )
            node["label"] = label
            cfg["agent_name"] = label
        elif role == "clip" and shot_i:
            label = label_clip(
                scene_number=scene_n, clip_number=shot_n, clip_name=shot_name
            )
            node["label"] = label
            cfg["agent_name"] = label
        cfg["supervisor_task"] = (
            f"LOCKS: solo sheets {identity_refs['character_node_ids']} "
            f"(all solos ready: {all_solo_ids}). Costume lock: {costume_lock}. "
            f"Strategy=compose_from_solo_refs for setting {setting_id}. "
            f"Scene master/handoff={master_frame}. "
            "Respect scene_bible hierarchical views; no empty plates; no cross-setting."
        )
        gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
        prompt = str(gen.get("prompt") or "")
        lock_bits = []
        if costume_lock and "Costume lock" not in prompt and "CLOTHING LOCK" not in prompt:
            lock_bits.append(
                f"IDENTITY sheets={identity_refs['character_node_ids']}. "
                f"CLOTHING LOCK: {costume_lock}."
            )
        if "STRATEGY=" not in prompt:
            lock_bits.append(f"STRATEGY=compose_from_solo_refs setting={setting_id}.")
        if bible and "SCENE BIBLE" not in prompt:
            lock_bits.append(
                f"SCENE BIBLE: place={bible.get('place')}; lighting={bible.get('lighting')}; "
                f"objects={', '.join(str(x) for x in (bible.get('objects') or [])[:5])}; "
                f"views={list((bible.get('views') or {}).keys())}."
            )
        if handoff_from and "SCENE PROMPT HANDOFF" not in prompt:
            lock_bits.append(
                f"SCENE PROMPT HANDOFF from {handoff_from} — keep architecture; change view/cast only."
            )
        if lock_bits:
            gen["prompt"] = (prompt + " " + " ".join(lock_bits)).strip()[:1800]
            cfg["generate"] = gen
            notes.append(f"{node.get('id')}: identity/scene lock clause in generate.prompt")
        node["config"] = cfg

    for node in frame_nodes:
        _stamp_media_node(node, assign_strategy=True)

    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        if _role_key(node) != "clip":
            continue
        _stamp_media_node(node, assign_strategy=False)
        # Also rename character sheets if generic.
    from jiuwenswarm.server.runtime.designer.node_labels import (
        derive_story_name,
        label_brief,
        label_character,
        label_compose,
        label_storyboard,
    )

    story_name = derive_story_name(
        analysis=analysis,
        prompt=str(graph.get("description") or ""),
        graph_title=str(graph.get("title") or ""),
    )
    char_i = 0
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        role = _role_key(node)
        cfg = dict(node.get("config") or {})
        if role == "brief" or str(node.get("id") or "") == "n_brief":
            node["label"] = label_brief(story_name)
            cfg["agent_name"] = node["label"]
            node["config"] = cfg
            continue
        if role == "storyboard" or str(node.get("id") or "") == "n_storyboard":
            node["label"] = label_storyboard(story_name)
            cfg["agent_name"] = node["label"]
            node["config"] = cfg
            continue
        if role == "compose" or str(node.get("id") or "") in {"n_compose", "n_final"}:
            node["label"] = label_compose(story_name)
            cfg["agent_name"] = node["label"]
            node["config"] = cfg
            continue
        if role != "character_design":
            continue
        char_i += 1
        name = str(
            cfg.get("character_name")
            or (cfg.get("character_names") or [None])[0]
            or ""
        ).strip()
        label = label_character(char_i, name or f"Character {char_i}")
        node["label"] = label
        cfg["agent_name"] = label
        node["config"] = cfg

    plan = dict(meta.get("consistency_plan") or {})
    plan.update(
        {
            "character_identity": "solo_sheets_first",
            "multi_shot_compose": "compose_from_solo_refs",
            "sequential_keyframe": "prompt_handoff_same_setting_id_only",
            "scene_spatial": "shared_scene_bible_prompt_handoff",
            "empty_scene_plates": False,
            "solo_gate_before_keyframes": True,
            "hierarchical_views": True,
            "costume_lock": True,
            "validated_by_manager": True,
        }
    )
    meta["consistency_plan"] = plan
    meta["skip_scene_plate"] = True
    meta["scene_continuity_mode"] = "compose_solos_shared_scene_prompt"
    meta["scene_masters"] = dict(scene_master_by_setting)
    meta["scene_locks"] = dict(scene_locks)
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

    def review_leaf_media_prompt(
        self,
        graph: DesignerExecutionGraph,
        node: DesignerGraphNode,
    ) -> dict[str, Any]:
        """Gate every frame/clip media prompt against locks + prior handoff / already_done."""
        from jiuwenswarm.server.runtime.designer.experiments.clip_prompt_handoff import (
            collect_prior_clip_prompts,
            handoff_clause_for_prompt,
        )
        from jiuwenswarm.server.runtime.designer.experiments.continuity_card import (
            architecture_clause_from_bible,
            strip_prior_prompt_pastes,
        )

        cfg = dict(node.get("config") or {})
        role = _role_key(node)
        if role not in {"frame", "keyframe", "clip", "character", "character_design", "scene"}:
            return {"patched": False, "notes": "skip_non_media"}
        shot_index = int(cfg.get("shot_index") or 0)
        gen = dict(cfg.get("generate") or {})
        prompt = strip_prior_prompt_pastes(str(gen.get("prompt") or cfg.get("prompt") or "").strip())
        notes: list[str] = []
        changed = False
        if prompt != str(gen.get("prompt") or cfg.get("prompt") or "").strip():
            notes.append("strip_prior_prompt_pastes")
            changed = True

        setting_id = str(cfg.get("setting_id") or "set_1").strip() or "set_1"
        strategy = str(
            cfg.get("keyframe_strategy")
            or (cfg.get("identity_refs") or {}).get("keyframe_strategy")
            or ""
        ).strip()
        identity = cfg.get("identity_refs") if isinstance(cfg.get("identity_refs"), dict) else {}
        costume_lock = str(cfg.get("costume_lock") or identity.get("costume_lock") or "").strip()
        spatial = cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else {}
        if not spatial:
            meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
            spatial = meta.get("spatial_lock") if isinstance(meta.get("spatial_lock"), dict) else {}
        solo_ids = [
            str(x)
            for x in (
                identity.get("character_node_ids")
                or cfg.get("character_node_ids")
                or []
            )
            if str(x)
        ]
        prior_kf_id = str(
            cfg.get("prior_keyframe_node_id") or identity.get("prior_keyframe_node_id") or ""
        ).strip()

        # Enforce setting-locked compose + scene bible before any media tool call.
        if role in {"frame", "keyframe"}:
            strategy = "compose_from_solo_refs"
            cfg["keyframe_strategy"] = strategy
            bible = cfg.get("scene_bible") if isinstance(cfg.get("scene_bible"), dict) else None
            if not bible:
                meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
                locks = meta.get("scene_locks") if isinstance(meta.get("scene_locks"), dict) else {}
                if isinstance(locks.get(setting_id), dict):
                    bible = dict(locks[setting_id])
                    cfg["scene_bible"] = bible
            handoff = str(
                cfg.get("scene_prompt_handoff_from")
                or identity.get("scene_prompt_handoff_from")
                or prior_kf_id
                or ""
            ).strip()
            if "compose" not in prompt.lower() and "STRATEGY=compose_from_solo_refs" not in prompt:
                prompt = (
                    prompt
                    + f"\nLOCK: STRATEGY=compose_from_solo_refs setting={setting_id}. "
                    f"Compose solo sheets {', '.join(solo_ids) or 'all cast solos'} "
                    "INTO the locked scene bible — keep architecture across views."
                )
                notes.append("enforce_compose_solos_lock")
                changed = True
            if bible and "SCENE BIBLE" not in prompt:
                prompt = (
                    prompt
                    + f"\nSCENE BIBLE: place={bible.get('place')}; lighting={bible.get('lighting')}; "
                    f"objects={', '.join(str(x) for x in (bible.get('objects') or [])[:6])}; "
                    f"crowd={bible.get('crowd')}; coherence={bible.get('coherence_rule')}; "
                    f"active_view={cfg.get('view_key') or bible.get('active_view')}."
                )
                notes.append("enforce_scene_bible")
                changed = True
            if handoff and "SCENE PROMPT HANDOFF" not in prompt:
                arch = str(cfg.get("scene_architecture_clause") or "").strip() or architecture_clause_from_bible(bible)
                prompt = (
                    prompt
                    + f"\nSCENE PROMPT HANDOFF from {handoff}: "
                    + (arch or "reuse scene architecture only")
                    + " — change only camera view + on_screen cast/actions."
                )
                notes.append("enforce_scene_prompt_handoff")
                changed = True
            # Architecture only — never paste full master action prompt.
            arch = str(cfg.get("scene_architecture_clause") or "").strip()
            if not arch:
                arch = architecture_clause_from_bible(bible)
            master_prompt = str(cfg.get("scene_master_prompt") or "").strip()
            if master_prompt and (
                "Primary action" in master_prompt
                or "PRIOR KEYFRAME" in master_prompt
                or len(master_prompt) > 900
            ):
                # Contaminated full prompt — replace with architecture.
                master_prompt = arch
                cfg["scene_master_prompt"] = arch[:900] if arch else ""
                notes.append("slim_contaminated_scene_master_prompt")
                changed = True
            if arch and "SCENE ARCHITECTURE LOCK" not in prompt and "MASTER SCENE PROMPT" not in prompt:
                prompt = prompt + "\n" + arch
                notes.append("inject_scene_architecture")
                changed = True
            elif master_prompt and "SCENE ARCHITECTURE LOCK" not in prompt and "MASTER SCENE PROMPT" not in prompt:
                # Only allow if it already looks like an architecture clause.
                if master_prompt.startswith("SCENE ARCHITECTURE") or "place=" in master_prompt[:80]:
                    prompt = prompt + "\n" + master_prompt[:900]
                    notes.append("inject_scene_architecture_from_master")
                    changed = True

        if costume_lock and "Costume lock" not in prompt and "costume lock" not in prompt.lower():
            prompt = prompt + f"\nCostume lock (must keep): {costume_lock}"
            notes.append("inject_costume_lock")
            changed = True
        # Always reinforce garment-level clothing lock for frames + clips.
        try:
            from jiuwenswarm.server.runtime.designer.experiments.clothing_lock import (
                clothing_lock_clause,
                ensure_cfg_clothing_lock,
            )

            analysis_chars = list(
                ((graph.get("metadata") or {}).get("script_analysis") or {}).get("characters")
                or []
            )
            costume_lock = ensure_cfg_clothing_lock(cfg, characters=analysis_chars) or costume_lock
            cloth = clothing_lock_clause(
                costume_lock,
                for_clip=(role == "clip"),
            )
            if cloth and "CLOTHING LOCK" not in prompt and "CLOTHING HOLD" not in prompt:
                prompt = prompt + "\n" + cloth
                notes.append("inject_clothing_lock")
                changed = True
        except Exception:  # noqa: BLE001
            pass

        if spatial and "SPATIAL LOCK" not in prompt:
            prompt = (
                prompt
                + "\nSPATIAL LOCK: "
                + "; ".join(f"{k}={v}" for k, v in spatial.items() if str(v).strip())
            )
            notes.append("inject_spatial_lock")
            changed = True
            cfg["spatial_lock"] = spatial

        if solo_ids and "IDENTITY" not in prompt and "solo" not in prompt.lower():
            prompt = prompt + f"\nIDENTITY solo sheets (do not invent faces): {', '.join(solo_ids)}"
            notes.append("inject_identity_solos")
            changed = True

        # Prior keyframe: short continuity note only (never paste full prior generate.prompt).
        prior_kf_action = str(cfg.get("previous_keyframe_action") or "").strip()
        prior_kf_prompt = str(cfg.get("previous_keyframe_prompt") or "").strip()
        if not prior_kf_action and shot_index > 1:
            prev_id = prior_kf_id or f"n_frame_{shot_index - 1}"
            for n in graph.get("nodes") or []:
                if str(n.get("id") or "") != prev_id:
                    continue
                pcfg = n.get("config") if isinstance(n.get("config"), dict) else {}
                prior_kf_action = str(
                    pcfg.get("shot_action")
                    or pcfg.get("character_action")
                    or ""
                ).strip()
                prior_kf_prompt = str(
                    pcfg.get("last_approved_prompt")
                    or (pcfg.get("generate") or {}).get("prompt")
                    or ""
                ).strip()
                if prior_kf_action or prior_kf_prompt:
                    if prior_kf_action:
                        cfg["previous_keyframe_action"] = prior_kf_action[:300]
                    # Soft-dep readiness marker — do not inject full text into prompt.
                    if prior_kf_prompt and not str(cfg.get("previous_keyframe_prompt") or "").strip():
                        cfg["previous_keyframe_prompt"] = prior_kf_prompt[:800]
                    cfg["previous_keyframe_node_id"] = prev_id
                    notes.append("pull_prior_keyframe_action")
                    changed = True
                break
        if (
            (prior_kf_action or prior_kf_prompt)
            and "PREVIOUS KEYFRAME HAD" not in prompt
            and "PRIOR KEYFRAME PROMPT" not in prompt
        ):
            from jiuwenswarm.server.runtime.designer.experiments.clip_prompt_handoff import (
                keyframe_continuity_note,
            )

            kf_note = keyframe_continuity_note(
                shot_index=shot_index,
                this_action=str(cfg.get("shot_action") or cfg.get("character_action") or ""),
                this_camera=str(cfg.get("camera") or ""),
                prior_action=prior_kf_action or prior_kf_prompt[:220],
                prior_shot_index=shot_index - 1 if shot_index > 1 else None,
            )
            if kf_note:
                prompt = prompt + "\n\n" + kf_note
                notes.append("inject_prior_keyframe_note")
                changed = True

        already_done = [str(x) for x in (cfg.get("already_done") or []) if str(x)]
        if not already_done:
            analysis = (graph.get("metadata") or {}).get("script_analysis") or {}
            for s in analysis.get("shots") or []:
                if isinstance(s, dict) and int(s.get("shot_index") or 0) == shot_index:
                    already_done = [str(x) for x in (s.get("already_done") or []) if str(x)]
                    cfg["already_done"] = already_done
                    break
        if already_done and "ALREADY_DONE" not in prompt:
            prompt = (
                prompt
                + "\nALREADY_DONE (do not restage unless storyboard explicitly repeats): "
                + "; ".join(already_done[:12])
            )
            notes.append("inject_already_done")
            changed = True

        occupancy = cfg.get("occupancy") if isinstance(cfg.get("occupancy"), dict) else {}
        if not occupancy and isinstance(identity.get("occupancy"), dict):
            occupancy = dict(identity["occupancy"])
        config_on_screen = [
            str(x)
            for x in (
                cfg.get("on_screen")
                or occupancy.get("must_appear")
                or cfg.get("character_ids")
                or []
            )
            if str(x)
        ]
        if occupancy and "OCCUPANCY:" not in prompt:
            prompt = (
                prompt
                + f"\nOCCUPANCY: must_appear={occupancy.get('must_appear') or config_on_screen}; "
                f"featured={occupancy.get('featured')}; "
                f"offscreen={occupancy.get('offscreen') or cfg.get('offscreen') or []}."
            )
            notes.append("inject_occupancy")
            changed = True
        # Reject/rewrite when prompt names cast outside config on_screen.
        if role in {"frame", "keyframe", "clip"} and config_on_screen:
            analysis_cast = (
                (graph.get("metadata") or {}).get("script_analysis") or {}
            )
            id_to_name = {
                str(c.get("id")): str(c.get("name") or c.get("id"))
                for c in (analysis_cast.get("characters") or [])
                if isinstance(c, dict) and c.get("id")
            }
            allowed_names = {
                id_to_name.get(cid, cid).lower() for cid in config_on_screen
            }
            allowed_ids = set(config_on_screen)
            prompt_l = prompt.lower()
            leaked: list[str] = []
            for cid, name in id_to_name.items():
                if cid in allowed_ids:
                    continue
                nlow = name.lower()
                if len(nlow) < 3:
                    continue
                if nlow in prompt_l or cid.lower() in prompt_l:
                    leaked.append(name)
            if leaked:
                prompt = (
                    prompt
                    + "\nOCCUPANCY ENFORCE: draw ONLY "
                    + ", ".join(id_to_name.get(c, c) for c in config_on_screen)
                    + f". Do NOT draw or mention: {', '.join(leaked)}."
                )
                notes.append(f"reject_offscreen_in_prompt:{','.join(leaked[:6])}")
                changed = True
            # Keep solo_ids aligned to on_screen only.
            solo_by_cid = {
                str(c): sid
                for c, sid in zip(
                    cfg.get("character_ids") or [],
                    solo_ids,
                )
                if str(c)
            }
            # Prefer resolving from graph solos when available.
            for other in graph.get("nodes") or []:
                if not isinstance(other, dict):
                    continue
                oc = other.get("config") if isinstance(other.get("config"), dict) else {}
                if _role_key(other) not in {"character", "character_design"}:
                    continue
                if oc.get("combined_cast"):
                    continue
                cids = [str(x) for x in (oc.get("character_ids") or []) if str(x)]
                if len(cids) == 1:
                    solo_by_cid[cids[0]] = str(other.get("id") or "")
            aligned = [solo_by_cid[c] for c in config_on_screen if c in solo_by_cid]
            if aligned and aligned != solo_ids:
                solo_ids = aligned
                cfg["character_node_ids"] = list(aligned)
                if isinstance(identity, dict):
                    identity = dict(identity)
                    identity["character_node_ids"] = list(aligned)
                    identity["character_ids"] = list(config_on_screen)
                    cfg["identity_refs"] = identity
                notes.append("align_refs_to_on_screen")
                changed = True

        crowd = cfg.get("crowd_lock") if isinstance(cfg.get("crowd_lock"), dict) else {}
        if not crowd and isinstance(occupancy.get("crowd_lock"), dict):
            crowd = occupancy["crowd_lock"]
        if not crowd:
            meta0 = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
            analysis0 = (
                meta0.get("script_analysis")
                if isinstance(meta0.get("script_analysis"), dict)
                else {}
            )
            for s in analysis0.get("shots") or []:
                if isinstance(s, dict) and int(s.get("shot_index") or 0) == shot_index:
                    crowd = s.get("crowd_lock") if isinstance(s.get("crowd_lock"), dict) else {}
                    if crowd:
                        cfg["crowd_lock"] = crowd
                    break
        if crowd and "CROWD LOCK" not in prompt:
            prompt = (
                prompt
                + f"\nCROWD LOCK: present={crowd.get('present')}; "
                f"density={crowd.get('density')}. {str(crowd.get('rule') or '')[:280]}"
            )
            notes.append("inject_crowd_lock")
            changed = True

        detail_needles = ("gaze", "screen-left", "screen left", "motion direction", "looks at")
        if not any(n in prompt.lower() for n in detail_needles):
            prompt = (
                prompt
                + "\nDETAIL LOCK: specify motion direction, who each person looks at, "
                "relative screen L/R positioning, and prop/landmark anchors for this beat."
            )
            notes.append("inject_detail_lock")
            changed = True

        # Per-shot staging locks (positioning / action / relationships) — equal to clothing.
        try:
            from jiuwenswarm.server.runtime.designer.experiments.shot_staging_lock import (
                ensure_cfg_staging_locks,
            )

            analysis_chars = list(
                ((graph.get("metadata") or {}).get("script_analysis") or {}).get("characters")
                or []
            )
            shot_row = next(
                (
                    s
                    for s in (
                        ((graph.get("metadata") or {}).get("script_analysis") or {}).get("shots")
                        or []
                    )
                    if isinstance(s, dict) and int(s.get("shot_index") or 0) == shot_index
                ),
                None,
            )
            staging_clause = ensure_cfg_staging_locks(
                cfg,
                shot=shot_row if isinstance(shot_row, dict) else None,
                characters=analysis_chars,
            )
            if staging_clause and "STAGING LOCK" not in prompt:
                prompt = prompt + "\n" + staging_clause
                notes.append("inject_staging_lock")
                changed = True
        except Exception:  # noqa: BLE001
            pass

        # Film-wide aspect + style locks (Manager enforces; leaves cannot drop).
        meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
        analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
        aspect = cfg.get("aspect_lock") if isinstance(cfg.get("aspect_lock"), dict) else {}
        if not aspect:
            aspect = meta.get("aspect_lock") if isinstance(meta.get("aspect_lock"), dict) else {}
        if not aspect:
            aspect = analysis.get("aspect_lock") if isinstance(analysis.get("aspect_lock"), dict) else {}
        if not aspect:
            try:
                from jiuwenswarm.server.runtime.designer.experiments.axis_locks import (
                    infer_aspect_lock,
                )

                aspect = infer_aspect_lock(
                    str(graph.get("description") or meta.get("user_prompt") or "")
                )
            except Exception:  # noqa: BLE001
                aspect = {}
        style = cfg.get("style_lock") if isinstance(cfg.get("style_lock"), dict) else {}
        if not style:
            style = meta.get("style_lock") if isinstance(meta.get("style_lock"), dict) else {}
        if not style:
            style = analysis.get("style_lock") if isinstance(analysis.get("style_lock"), dict) else {}

        if aspect:
            cfg["aspect_lock"] = aspect
            if role in {"frame", "keyframe", "character", "character_design", "scene"}:
                img_size = str(aspect.get("image_size") or cfg.get("image_size") or "1K").strip()
                if str(cfg.get("image_size") or "") != img_size:
                    cfg["image_size"] = img_size
                    notes.append("stamp_image_size_aspect")
                    changed = True
            if role == "clip":
                vsize = str(aspect.get("video_size") or cfg.get("video_size") or "").strip()
                vres = str(aspect.get("video_resolution") or cfg.get("video_resolution") or "").strip()
                if vsize and (str(cfg.get("video_size") or "") != vsize or str(cfg.get("video_resolution") or "") != vres):
                    cfg["video_size"] = vsize
                    cfg["video_resolution"] = vres
                    notes.append("stamp_video_aspect")
                    changed = True
            ratio = str(aspect.get("ratio") or "").strip()
            rule = str(aspect.get("rule") or "").strip()
            if "ASPECT LOCK" not in prompt and (rule or ratio):
                prompt = (
                    prompt
                    + f"\nASPECT LOCK: {ratio or 'film ratio'} — "
                    + (rule or f"keep {ratio} on every still and clip; never change mid-film.")
                )
                notes.append("inject_aspect_lock")
                changed = True

        if style:
            cfg["style_lock"] = style
            if "STYLE LOCK" not in prompt and "STYLE HOLD" not in prompt:
                try:
                    from jiuwenswarm.server.runtime.designer.media_model_playbook import (
                        style_lock_clause,
                    )

                    clause = (style_lock_clause(style) or "").strip()
                except Exception:  # noqa: BLE001
                    clause = ""
                if not clause:
                    look = str(style.get("look") or style.get("medium") or "").strip()
                    clause = f"STYLE LOCK (film-wide): {look}" if look else ""
                if clause:
                    prompt = prompt + "\n" + clause
                    notes.append("inject_style_lock")
                    changed = True

        if role == "clip":
            # Scene bible + setting isolation for clips (same locks as keyframes).
            bible = cfg.get("scene_bible") if isinstance(cfg.get("scene_bible"), dict) else None
            if not bible:
                meta_b = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
                locks_b = meta_b.get("scene_locks") if isinstance(meta_b.get("scene_locks"), dict) else {}
                if isinstance(locks_b.get(setting_id), dict):
                    bible = dict(locks_b[setting_id])
                    cfg["scene_bible"] = bible
            if bible and "SCENE BIBLE" not in prompt:
                prompt = (
                    prompt
                    + f"\nSCENE BIBLE: place={bible.get('place')}; lighting={bible.get('lighting')}; "
                    f"objects={', '.join(str(x) for x in (bible.get('objects') or [])[:6])}; "
                    f"crowd={bible.get('crowd')}; coherence={bible.get('coherence_rule')}."
                )
                notes.append("enforce_scene_bible_clip")
                changed = True
            if setting_id and f"setting={setting_id}" not in prompt.lower() and "Setting lock" not in prompt:
                prompt = (
                    prompt
                    + f"\nSETTING LOCK: animate only `{setting_id}` from THIS shot's keyframe; "
                    "do not import architecture or cast from another scene."
                )
                notes.append("enforce_setting_lock_clip")
                changed = True
            # Pull prior clip beat (action) from continuity node — never paste full Wan text.
            cont_clip = str(cfg.get("continuity_clip_node_id") or cfg.get("previous_clip_node_id") or "").strip()
            if cont_clip and not str(cfg.get("previous_clip_action") or "").strip():
                for n in graph.get("nodes") or []:
                    if str(n.get("id") or "") != cont_clip:
                        continue
                    pcfg = n.get("config") if isinstance(n.get("config"), dict) else {}
                    prior_act = str(
                        pcfg.get("shot_action")
                        or pcfg.get("character_action")
                        or ""
                    ).strip()
                    prior_txt = str(
                        pcfg.get("last_wan_prompt")
                        or pcfg.get("last_approved_prompt")
                        or (pcfg.get("generate") or {}).get("prompt")
                        or ""
                    ).strip()
                    if prior_act or prior_txt:
                        if prior_act:
                            cfg["previous_clip_action"] = prior_act[:220]
                        if prior_txt and not str(cfg.get("previous_clip_wan_prompt") or "").strip():
                            cfg["previous_clip_wan_prompt"] = prior_txt[:800]
                        cfg["previous_clip_node_id"] = cont_clip
                        cfg["previous_clip_handoff_ready"] = True
                        notes.append("pull_prior_clip_action_from_dep")
                        changed = True
                    break
            prior_clips = collect_prior_clip_prompts(graph, shot_index=shot_index)
            if not prior_clips and str(cfg.get("previous_clip_action") or "").strip():
                prior_clips = [
                    {
                        "node_id": str(cfg.get("previous_clip_node_id") or ""),
                        "shot_index": int(
                            cfg.get("previous_clip_shot_index") or max(1, shot_index - 1)
                        ),
                        "shot_action": str(cfg.get("previous_clip_action") or ""),
                        "speech_line": str(cfg.get("previous_clip_speech") or ""),
                    }
                ]
            clause = handoff_clause_for_prompt(
                prior_clips,
                this_shot_index=shot_index,
                this_action=str(cfg.get("shot_action") or cfg.get("character_action") or ""),
                this_camera=str(cfg.get("camera") or ""),
                this_speech=str(cfg.get("speech_line") or ""),
                already_done=already_done,
            )
            if clause and "PREVIOUS CLIP HAD" not in prompt and "PRIOR CLIP CONTINUITY" not in prompt:
                prompt = prompt + "\n\n" + clause
                notes.append("inject_prior_clip_handoff")
                changed = True
            # Character consistency on every clip call (Manager enforce).
            cast_who = ", ".join(
                str(x)
                for x in (
                    cfg.get("on_screen")
                    or (cfg.get("occupancy") or {}).get("must_appear")
                    or cfg.get("character_ids")
                    or []
                )
                if str(x)
            )
            if "CHARACTER CONSISTENCY" not in prompt:
                prompt = (
                    prompt
                    + "\nCHARACTER CONSISTENCY LOCK: animate ONLY people already in Image 1 "
                    "(this shot's keyframe); keep the same faces, body types, ages, and "
                    f"costumes{(' for ' + cast_who) if cast_who else ''}. "
                    "Do not recast, redesign wardrobe, or invent a different hero. "
                    "IDENTITY solo sheets remain the face authority."
                )
                notes.append("inject_clip_character_consistency")
                changed = True
            if costume_lock and "Costume lock" not in prompt and "costume lock" not in prompt.lower():
                prompt = prompt + f"\nCostume lock (must keep on clip): {costume_lock}"
                notes.append("inject_clip_costume_lock")
                changed = True
            if costume_lock and "CLOTHING LOCK" not in prompt:
                from jiuwenswarm.server.runtime.designer.experiments.clothing_lock import (
                    clothing_lock_clause,
                )

                cloth = clothing_lock_clause(costume_lock, for_clip=True)
                if cloth:
                    prompt = prompt + "\n" + cloth
                    notes.append("inject_clip_clothing_lock")
                    changed = True
            # Storyboard beat for THIS shot only (avoid full-board mix).
            action = str(cfg.get("shot_action") or "").strip()
            if action and f"Primary action for shot {shot_index}" not in prompt:
                prompt = prompt + f"\nPrimary action for shot {shot_index}: {action}"
                notes.append("inject_this_shot_action")
                changed = True

            # Language / speech / BGM locks — Manager enforces on every clip.
            from jiuwenswarm.server.runtime.designer.audio_locks import (
                audio_lock_prompt_block,
                resolve_audio_intent_flags,
                stamp_audio_fields_on_clip_config,
            )

            analysis_a = (
                meta.get("script_analysis")
                if isinstance(meta.get("script_analysis"), dict)
                else {}
            )
            shot_row = next(
                (
                    s
                    for s in (analysis_a.get("shots") or [])
                    if isinstance(s, dict) and int(s.get("shot_index") or 0) == shot_index
                ),
                {},
            )
            routing = meta.get("audio_routing") if isinstance(meta.get("audio_routing"), dict) else {}
            cfg = stamp_audio_fields_on_clip_config(
                cfg,
                shot=shot_row if isinstance(shot_row, dict) else {},
                analysis=analysis_a,
                meta=meta,
                clip_embedded=bool(
                    routing.get("clip_embedded")
                    or cfg.get("clip_embedded_audio")
                    or meta.get("prefer_wan3_clip_audio")
                ),
            )
            flags = resolve_audio_intent_flags(meta, cfg)
            block = audio_lock_prompt_block(
                language_lock=str(flags.get("language_lock") or ""),
                speech_by_character=flags.get("speech_by_character") or {},
                speech_line=str(flags.get("speech_line") or ""),
                bgm_lock=flags.get("bgm_lock") or {},
                include_speech=bool(flags.get("include_speech")),
                include_music=bool(flags.get("include_music")),
                clip_embedded=bool(flags.get("clip_embedded")),
            )
            if block and ("LANGUAGE LOCK" not in prompt or "SPEECH LOCK" not in prompt or "BGM LOCK" not in prompt):
                # Replace soft fragments with full lock block once.
                if "LANGUAGE LOCK" not in prompt:
                    prompt = prompt + "\n" + block
                    notes.append("inject_audio_locks")
                    changed = True
                elif "SPEECH LOCK" not in prompt or "BGM LOCK" not in prompt:
                    prompt = prompt + "\n" + block
                    notes.append("reinforce_audio_locks")
                    changed = True

        # Soft anti-repeat: if prompt re-states a finished exit verb from already_done, flag.
        for item in already_done:
            low = item.lower()
            if "exited" in low or "leaving" in low or "walked out" in low:
                # Ensure explicit forbid clause once.
                if "do not show them leaving again" not in prompt.lower():
                    prompt = (
                        prompt
                        + "\nDo not show characters leaving again if already_done says they exited."
                    )
                    notes.append("enforce_no_repeat_exit")
                    changed = True
                    break

        # Always stamp Manager lock gate; patched=True when prompt text changed.
        cfg["manager_prompt_reviewed"] = True
        cfg["manager_lock_gate"] = {
            "setting_id": setting_id,
            "keyframe_strategy": strategy or cfg.get("keyframe_strategy"),
            "costume_lock": bool(costume_lock),
            "spatial_lock": bool(spatial),
            "aspect_lock": bool(aspect),
            "style_lock": bool(style),
            "aspect_ratio": (aspect or {}).get("ratio"),
            "image_size": cfg.get("image_size"),
            "video_size": cfg.get("video_size"),
            "video_resolution": cfg.get("video_resolution"),
            "solo_ids": solo_ids,
            "prior_keyframe_node_id": prior_kf_id or None,
            "language_lock": cfg.get("language_lock") or meta.get("language_lock"),
            "speech_lock": bool(cfg.get("speech_line") or cfg.get("speech_by_character")),
            "bgm_lock": bool(cfg.get("bgm_lock") or meta.get("bgm_lock")),
            "clip_embedded_audio": bool(cfg.get("clip_embedded_audio")),
            "video_audio": bool(cfg.get("video_audio") or cfg.get("prefer_wan3_clip_audio")),
        }
        gen["prompt"] = prompt.strip()
        cfg["generate"] = gen
        # Character/scene leaves read cfg.prompt; keep both in sync after Manager gate.
        if role in {"character", "character_design", "scene"} or not str(cfg.get("prompt") or "").strip():
            cfg["prompt"] = prompt.strip()[:6000]
        if changed:
            cfg["last_approved_prompt"] = prompt.strip()[:4000]
        else:
            cfg.setdefault("last_approved_prompt", prompt.strip()[:4000])
        node["config"] = cfg
        nid = str(node.get("id") or "")
        for n in graph.get("nodes") or []:
            if str(n.get("id") or "") == nid:
                n["config"] = cfg
                break
        return {
            "patched": changed,
            "notes": notes,
            "shot_index": shot_index,
            "lock_gate": cfg.get("manager_lock_gate"),
        }

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
        spatial_notes = _spatial_geography_lock_patch(graph)
        # Film-wide aspect/style/axis locks on every still + clip node.
        try:
            from jiuwenswarm.server.runtime.designer.experiments.axis_locks import (
                apply_axis_locks_to_graph,
            )

            axis_notes = apply_axis_locks_to_graph(graph)
        except Exception:  # noqa: BLE001
            axis_notes = []
        # User-added canvas nodes → LLM agents + tools + contribution wiring.
        try:
            user_onboard = SupervisorAgent().onboard_user_added_nodes(graph)
            axis_notes.extend(
                [f"user:{x}" for x in (user_onboard.get("notes") or []) if isinstance(x, str)][:20]
            )
        except Exception:  # noqa: BLE001
            pass
        from jiuwenswarm.server.runtime.designer.model_tools import llm_available

        # Prune unused nodes + keep graph coherent for compose; enforce agents when LLM up.
        prune_notes = _manager_prune_and_cohere(graph)
        agent_notes = self._enforce_leaf_agents(graph, use_agents=llm_available())
        # Re-apply audio agent policy after modality (backends may clear force_handler).
        audio_assign = assign_audio_node_agents(graph)
        # Second prune after audio assign (speech/music may be omitted).
        prune_notes.extend(_manager_prune_and_cohere(graph))
        ensure_notes = self.ensure_agents_and_prune(graph)
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
            + spatial_notes
            + axis_notes
            + prune_notes
            + agent_notes
            + list(ensure_notes.get("notes") or []),
            "cast_focus_fixes": cast_notes,
            "continuity_fixes": continuity_notes,
            "identity_fixes": identity_notes,
            "spatial_lock_fixes": spatial_notes,
            "axis_lock_fixes": axis_notes,
            "pruned_nodes": list(
                dict.fromkeys(
                    [x.split(":", 1)[-1] for x in prune_notes if x.startswith("pruned:")]
                    + list(ensure_notes.get("pruned") or [])
                )
            ),
            "agent_enforcement": agent_notes,
            "ensure_agents": ensure_notes,
            "audio_assignment": audio_assign,
            "rating_modality": rating_mod,
            "can_vision": bool((modality or {}).get("can_vision")),
            "can_video": bool((modality or {}).get("can_video")),
            "can_speech": bool((modality or {}).get("can_speech")),
            "can_music": bool((modality or {}).get("can_music")),
            "notes": (
                f"Manager start validation: rating_modality={rating_mod}. "
                f"Cast-focus={len(cast_notes)}. Continuity={len(continuity_notes)}. "
                f"Identity={len(identity_notes)}. Spatial={len(spatial_notes)}. "
                f"Prune/cohere={len(prune_notes)}. Agents={len(agent_notes)}. "
                f"Ensure={len(ensure_notes.get('notes') or [])}. "
                f"{str((modality or {}).get('reason') or '')[:400]}"
            ),
            "source": "heuristic",
        }
        meta = dict(graph.get("metadata") or {})
        meta["manager_plan_ack"] = ack
        graph["metadata"] = meta
        return ack

    def ensure_agents_and_prune(self, graph: DesignerExecutionGraph) -> dict[str, Any]:
        """For every node: kind=agent, tools via _tools_for_node, delegate=agent when LLM up."""
        from jiuwenswarm.server.runtime.designer.model_tools import llm_available
        from jiuwenswarm.server.runtime.designer.smart_graph import (
            prune_non_contributing_nodes,
            prune_shot_nodes_beyond_analysis,
        )

        use_agents = bool(llm_available())
        notes: list[str] = []
        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            cfg = dict(node.get("config") or {})
            nid = str(node.get("id") or "")
            if not nid:
                continue
            cfg["kind"] = "agent"
            tools = _tools_for_node(node)
            if list(cfg.get("tools") or []) != tools:
                notes.append(f"tools:{nid}")
            cfg["tools"] = tools
            if use_agents:
                cfg.pop("force_handler", None)
                cfg["delegate"] = "agent"
                cfg["skip_llm"] = False
                if cfg.get("prewritten") and not cfg.get("draft_prewritten"):
                    cfg["draft_prewritten"] = cfg.pop("prewritten")
                else:
                    cfg.pop("prewritten", None)
            else:
                cfg["delegate"] = "handler"
            node["config"] = cfg

        extra_shots = prune_shot_nodes_beyond_analysis(graph)
        notes.extend([f"pruned_extra_shot:{x}" for x in extra_shots])
        pruned = prune_non_contributing_nodes(graph)
        notes.extend([f"pruned:{x}" for x in pruned])

        # Ensure compose is a sink: every clip (and audio) edge into compose when present.
        ids = {str(n.get("id") or "") for n in (graph.get("nodes") or []) if isinstance(n, dict)}
        if "n_compose" in ids:
            edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
            existing = {
                (str(e.get("source") or ""), str(e.get("target") or "")) for e in edges
            }
            for node in graph.get("nodes") or []:
                if not isinstance(node, dict):
                    continue
                nid = str(node.get("id") or "")
                role = _role_key(node)
                cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
                if cfg.get("user_added"):
                    continue
                if role in {"clip", "speech", "music"} or nid.startswith("n_clip"):
                    key = (nid, "n_compose")
                    if key not in existing and nid != "n_compose":
                        edges.append(
                            {
                                "id": f"e_{nid}_compose",
                                "source": nid,
                                "target": "n_compose",
                                "kind": "data",
                            }
                        )
                        existing.add(key)
                        notes.append(f"compose_sink:{nid}")
            graph["edges"] = edges
            # Compose inputs list stays in sync.
            for node in graph.get("nodes") or []:
                if str(node.get("id") or "") != "n_compose":
                    continue
                cfg = dict(node.get("config") or {})
                inputs = [
                    str(e.get("source") or "")
                    for e in (graph.get("edges") or [])
                    if str(e.get("target") or "") == "n_compose"
                ]
                cfg["inputs"] = [x for x in inputs if x]
                node["config"] = cfg
                break

        result = {
            "ok": True,
            "notes": notes[:60],
            "pruned": list(pruned),
            "use_agents": use_agents,
        }
        meta = dict(graph.get("metadata") or {})
        meta["manager_ensure_agents"] = result
        graph["metadata"] = meta
        return result

    def audit_contribution_for_run(self, graph: DesignerExecutionGraph) -> dict[str, Any]:
        """Warn (do not block) when user-added nodes never reach the final compose/clip."""
        from jiuwenswarm.server.runtime.designer.smart_graph import (
            find_non_contributing_node_ids,
        )

        # Re-onboard (tools/agents only; no auto-wire) before auditing.
        try:
            SupervisorAgent().onboard_user_added_nodes(graph)
        except Exception:  # noqa: BLE001
            logger.debug("user node onboard during run audit failed", exc_info=True)
        orphans = []
        for nid in find_non_contributing_node_ids(graph):
            for n in graph.get("nodes") or []:
                if not isinstance(n, dict) or str(n.get("id") or "") != nid:
                    continue
                cfg = n.get("config") if isinstance(n.get("config"), dict) else {}
                if cfg.get("user_added"):
                    orphans.append(nid)
                break
        warning = ""
        if orphans:
            warning = (
                "Warning: user-added nodes do not contribute to the final clip/compose ("
                + ", ".join(orphans)
                + "). Connect them into the pipeline if you want them in the film. "
                "Running anyway."
            )
        meta = dict(graph.get("metadata") or {})
        if orphans:
            meta["non_contributing_user_nodes"] = orphans
            meta["contribution_warning"] = warning
        else:
            meta.pop("non_contributing_user_nodes", None)
            meta.pop("contribution_warning", None)
        meta["manager_contribution_audit"] = {
            "ok": True,
            "orphans": orphans,
            "warning": warning,
            "blocked": False,
        }
        graph["metadata"] = meta
        return dict(meta["manager_contribution_audit"])

    def _enforce_leaf_agents(
        self, graph: DesignerExecutionGraph, *, use_agents: bool
    ) -> list[str]:
        """Every leaf is an LLM agent with tools when models exist (framework rule)."""
        notes: list[str] = []
        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            cfg = dict(node.get("config") or {})
            nid = str(node.get("id") or "")
            if not nid:
                continue
            cfg["kind"] = "agent"
            tools = _tools_for_node(node)
            if list(cfg.get("tools") or []) != tools:
                notes.append(f"tools:{nid}")
            cfg["tools"] = tools
            if use_agents:
                cfg.pop("force_handler", None)
                cfg["delegate"] = "agent"
                cfg["skip_llm"] = False
                if cfg.get("prewritten") and not cfg.get("draft_prewritten"):
                    cfg["draft_prewritten"] = cfg.pop("prewritten")
                else:
                    cfg.pop("prewritten", None)
            else:
                cfg["delegate"] = "handler"
            node["config"] = cfg
        return notes

    async def patch_plan_one_pass(
        self,
        plan: dict[str, Any],
        *,
        user_prompt: str,
        optimize_for: str = "quality",
    ) -> dict[str, Any]:
        """One Manager LLM fidelity patch on plan JSON — occupancy/setting/locks only."""
        out = dict(plan)
        if not user_prompt.strip():
            return out
        system = (
            "You are the Designer Manager. Diff-only fidelity patch on plan.v1. "
            "Do NOT invent characters or plot. Do NOT paste the full user_prompt into actions. "
            "Fix: empty on_screen, wrong setting_id inheritance across meet/leave/exterior, "
            "cast bleed (later people into early on_screen), missing scene_locks, "
            "desynced character_ids vs on_screen (update on_screen/character_ids/occupancy together). "
            "Respond JSON only: "
            '{"ok":true,"characters":[...],"shots":[...],"scene_locks":{...},'
            '"brief_markdown":"...","storyboard_markdown":"...","notes":"..."} '
            "Omit unchanged top-level keys; include full shots[] if any shot changes."
        )
        try:
            result = await call_model_tool(
                prompt=json.dumps(
                    {
                        "user_prompt": user_prompt[:2000],
                        "plan": {
                            "characters": out.get("characters") or [],
                            "shots": out.get("shots") or [],
                            "scenes": out.get("scenes") or [],
                            "scene_locks": out.get("scene_locks") or {},
                            "brief_markdown": str(out.get("brief_markdown") or "")[:2000],
                            "storyboard_markdown": str(out.get("storyboard_markdown") or "")[:2000],
                        },
                    },
                    ensure_ascii=False,
                ),
                system=system,
                optimize_for=optimize_for,
                max_tokens=32768,
            )
            parsed = _extract_json_object(str(result.get("text") or "")) or {}
        except Exception:  # noqa: BLE001
            logger.info("patch_plan_one_pass LLM failed", exc_info=True)
            parsed = {}
        if not isinstance(parsed, dict) or not parsed:
            return out
        # Never invent cast — only allow subset/rename of existing ids unless Supervisor had none.
        existing_ids = {
            str(c.get("id") or "")
            for c in (out.get("characters") or [])
            if isinstance(c, dict) and c.get("id")
        }
        if isinstance(parsed.get("characters"), list) and parsed["characters"]:
            cleaned_chars: list[dict[str, Any]] = []
            for i, raw in enumerate(parsed["characters"], start=1):
                if not isinstance(raw, dict):
                    continue
                cid = str(raw.get("id") or f"char_{i}").strip() or f"char_{i}"
                if existing_ids and cid not in existing_ids:
                    continue
                cleaned_chars.append(
                    {
                        "id": cid,
                        "name": str(raw.get("name") or cid).strip() or cid,
                        "description": str(raw.get("description") or "")[:600],
                    }
                )
            if cleaned_chars:
                out["characters"] = cleaned_chars
                existing_ids = {str(c.get("id")) for c in cleaned_chars}
        if isinstance(parsed.get("shots"), list) and parsed["shots"]:
            cleaned_shots: list[dict[str, Any]] = []
            for i, raw in enumerate(parsed["shots"], start=1):
                if not isinstance(raw, dict):
                    continue
                shot = dict(raw)
                shot["shot_index"] = int(shot.get("shot_index") or i)
                on_screen = [
                    str(x)
                    for x in (
                        shot.get("on_screen")
                        or shot.get("visible_cast_ids")
                        or shot.get("featured_cast_ids")
                        or []
                    )
                    if str(x) and (not existing_ids or str(x) in existing_ids)
                ]
                shot["on_screen"] = on_screen
                shot["visible_cast_ids"] = list(on_screen)
                shot["character_ids"] = list(on_screen)
                off = [
                    str(x)
                    for x in (shot.get("offscreen") or shot.get("off_screen_cast_ids") or [])
                    if str(x) and str(x) not in on_screen
                    and (not existing_ids or str(x) in existing_ids)
                ]
                shot["offscreen"] = off
                shot["off_screen_cast_ids"] = off
                if not str(shot.get("setting_id") or "").strip():
                    shot["setting_id"] = f"set_{i}"
                cleaned_shots.append(shot)
            if cleaned_shots:
                out["shots"] = cleaned_shots
        if isinstance(parsed.get("scene_locks"), dict) and parsed["scene_locks"]:
            out["scene_locks"] = parsed["scene_locks"]
        for key in ("brief_markdown", "storyboard_markdown", "notes"):
            if isinstance(parsed.get(key), str) and parsed[key].strip():
                out[key] = parsed[key]
        out["manager_patched"] = True
        return out

    async def review_brief(
        self, graph: DesignerExecutionGraph, *, use_llm: bool = False
    ) -> dict[str, Any]:
        """One-pass fidelity check of approved brief vs user prompt; patch if needed."""
        meta = dict(graph.get("metadata") or {})
        user_prompt = str(graph.get("description") or "")
        brief = str(meta.get("approved_brief") or "")
        analysis = (
            dict(meta.get("script_analysis") or {})
            if isinstance(meta.get("script_analysis"), dict)
            else {}
        )
        characters = list(analysis.get("characters") or [])
        ack: dict[str, Any] = {
            "ok": True,
            "source": "heuristic",
            "notes": "Brief fidelity pass.",
            "patched": [],
        }
        patched: list[str] = []
        # Heuristic: ensure each character name appears in the brief.
        missing: list[str] = []
        low = brief.lower()
        for c in characters:
            name = str(c.get("name") or "").strip()
            if name and name.lower() not in low:
                missing.append(name)
        if missing:
            extra = "\n".join(f"- **{n}:** must appear with identity lock" for n in missing)
            brief = (brief.rstrip() + "\n\n**Manager cast fidelity:**\n" + extra + "\n")[:8000]
            patched.append("cast_names")
        # Heuristic: mention multi-view / shot coverage when prompt is long.
        if len(user_prompt) > 120 and "shot" not in low and "view" not in low:
            brief = (
                brief.rstrip()
                + "\n\n**Shot views:** cover establishing, mid, reaction close-ups "
                "for every major prompt beat.\n"
            )[:8000]
            patched.append("shot_views")

        if use_llm:
            try:
                system = (
                    "You are the Designer Manager. Review the creative brief once for fidelity "
                    "to the user prompt. Flag missing characters or insufficient shot views. "
                    "Patch the brief markdown if needed — do not invent new plot. "
                    "Respond JSON only: "
                    '{"ok":true,"patched_brief_markdown":"...","notes":"...","issues":["..."]}'
                )
                result = await call_model_tool(
                    prompt=json.dumps(
                        {
                            "user_prompt": user_prompt,
                            "brief": brief[:6000],
                            "characters": characters,
                            "shots": analysis.get("shots"),
                        },
                        ensure_ascii=False,
                    ),
                    system=system,
                    optimize_for="quality",
                    max_tokens=32768,
                )
                parsed = _extract_json_object(str(result.get("text") or "")) or {}
                patched_md = str(parsed.get("patched_brief_markdown") or "").strip()
                if patched_md and len(patched_md) > 80:
                    brief = patched_md[:8000]
                    patched.append("llm_brief")
                    ack["source"] = "llm"
                ack["notes"] = str(parsed.get("notes") or ack["notes"])[:1000]
                ack["issues"] = list(parsed.get("issues") or [])[:20]
            except Exception:  # noqa: BLE001
                logger.info("Manager review_brief LLM failed; keeping heuristic", exc_info=True)

        for node in graph.get("nodes") or []:
            cfg = dict(node.get("config") or {})
            if _role_key(node) != "brief" and str(node.get("id") or "") != "n_brief":
                continue
            if cfg.get("skip_llm"):
                cfg["prewritten"] = brief
            else:
                cfg["draft_prewritten"] = brief
                cfg["prewritten"] = brief
            node["config"] = cfg
            break
        meta["approved_brief"] = brief
        ack["patched"] = patched[:20]
        meta["manager_brief_ack"] = ack
        graph["metadata"] = meta
        return ack

    async def review_storyboard(
        self, graph: DesignerExecutionGraph, *, use_llm: bool = False
    ) -> dict[str, Any]:
        """Pre-run one-pass storyboard fidelity + enhancements (crowd, beauty, duration, continuity)."""
        meta = dict(graph.get("metadata") or {})
        if meta.get("storyboard_pre_reviewed"):
            return dict(meta.get("manager_storyboard_pre_ack") or {"ok": True, "skipped": True})
        # Clear mid-run once-flag so review_storyboard_once applies patches now.
        meta.pop("storyboard_reviewed", None)
        graph["metadata"] = meta
        ack = await self.review_storyboard_once(
            graph, node_states=None, use_llm=use_llm
        )
        meta = dict(graph.get("metadata") or {})
        meta["storyboard_pre_reviewed"] = True
        meta["manager_storyboard_pre_ack"] = ack
        # Allow a second pass after the storyboard leaf completes during the ready-queue.
        meta["storyboard_reviewed"] = False
        graph["metadata"] = meta
        return ack

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
            # Never paste the full user_prompt into short actions — that injects
            # later-meet cast into early beats. Sparse actions stay sparse;
            # LLM shot_fixes below may enrich without copying the whole brief.

        if use_llm:
            try:
                system = (
                    "You are the Designer Manager. Review the storyboard once for best quality "
                    "while remaining completely faithful to the user prompt, approved brief, and "
                    "story beats (no new plot). Fix missing characters/views, enhance sparse shots "
                    "(crowd, atmosphere), set shot durations, enforce time-coherent continuity, and "
                    "keep geography locked (same landmarks/layout/light across views). "
                    "Also approve/enforce film audio locks: language_lock (one language for all "
                    "speech), per-shot speech_by_character (exact lines or {} if silent), and "
                    "film-wide bgm_lock. Respond JSON only: "
                    '{"ok":true,"shot_fixes":[{"shot_index":1,"action":"...","camera":"...",'
                    '"timeline":"0-5s","continuity_lock":{"forbid":"..."},'
                    '"character_ids":["char_1"],'
                    '"speech_by_character":{"char_1":"exact line"},"speech_line":"..."}],'
                    '"language_lock":"en",'
                    '"bgm_lock":{"mood":"...","style":"...","rule":"..."},'
                    '"include_speech":true,"include_music":true,"notes":"..."}'
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
                    max_tokens=32768,
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
                        for key in ("action", "camera", "timeline", "keyframe_prompt", "speech_line"):
                            if fix.get(key):
                                shot[key] = str(fix[key])[:600]
                        if isinstance(fix.get("continuity_lock"), dict):
                            shot["continuity_lock"] = {
                                str(k): str(v) for k, v in fix["continuity_lock"].items()
                            }
                        if isinstance(fix.get("character_ids"), list):
                            shot["character_ids"] = [str(x) for x in fix["character_ids"] if str(x)]
                        if isinstance(fix.get("speech_by_character"), dict):
                            shot["speech_by_character"] = {
                                str(k): str(v)[:280]
                                for k, v in fix["speech_by_character"].items()
                                if str(v).strip()
                            }
                        patched.append(f"llm:shot{idx}")
                if parsed.get("language_lock"):
                    analysis["language_lock"] = str(parsed.get("language_lock"))[:16]
                    patched.append("language_lock")
                if isinstance(parsed.get("bgm_lock"), dict):
                    analysis["bgm_lock"] = {
                        str(k): str(v)[:280] for k, v in parsed["bgm_lock"].items()
                    }
                    patched.append("bgm_lock")
                audio = dict(analysis.get("audio") or {})
                if "include_speech" in parsed:
                    audio["include_speech"] = bool(parsed.get("include_speech"))
                if "include_music" in parsed:
                    audio["include_music"] = bool(parsed.get("include_music"))
                if parsed.get("language_lock"):
                    audio["language_lock"] = str(parsed.get("language_lock"))[:16]
                if isinstance(parsed.get("bgm_lock"), dict):
                    audio["bgm_lock"] = analysis.get("bgm_lock")
                analysis["audio"] = audio
                ack["source"] = "llm"
                ack["notes"] = str(parsed.get("notes") or ack["notes"])[:1000]
            except Exception:  # noqa: BLE001
                logger.info("Manager storyboard LLM review failed; keeping heuristic", exc_info=True)

        from jiuwenswarm.server.runtime.designer.audio_locks import ensure_audio_locks_on_analysis

        analysis = ensure_audio_locks_on_analysis(analysis, user_prompt)
        shots = list(analysis.get("shots") or shots)
        meta["language_lock"] = str(analysis.get("language_lock") or meta.get("language_lock") or "en")
        if isinstance(analysis.get("bgm_lock"), dict):
            meta["bgm_lock"] = analysis["bgm_lock"]
        patched.append("audio_locks_approved")

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
                from jiuwenswarm.server.runtime.designer.audio_locks import (
                    stamp_audio_fields_on_clip_config,
                )

                for node in graph.get("nodes") or []:
                    cfg = dict(node.get("config") or {})
                    role = _role_key(node)
                    if role == "storyboard":
                        if cfg.get("skip_llm"):
                            cfg["prewritten"] = sb_md
                        else:
                            cfg["draft_prewritten"] = sb_md
                        cfg["planned_shots"] = shots
                        node["config"] = cfg
                        continue
                    if role not in {"frame", "clip", "keyframe"}:
                        continue
                    idx = int(cfg.get("shot_index") or 0)
                    for shot in shots:
                        if int(shot.get("shot_index") or 0) != idx:
                            continue
                        if shot.get("action"):
                            cfg["shot_action"] = str(shot["action"])[:500]
                        if shot.get("camera"):
                            cfg["camera"] = str(shot["camera"])[:120]
                        if shot.get("timeline"):
                            cfg["timeline"] = str(shot["timeline"])[:40]
                        if isinstance(shot.get("continuity_lock"), dict):
                            cfg["continuity_lock"] = shot["continuity_lock"]
                        if role == "clip":
                            routing = (
                                meta.get("audio_routing")
                                if isinstance(meta.get("audio_routing"), dict)
                                else {}
                            )
                            cfg = stamp_audio_fields_on_clip_config(
                                cfg,
                                shot=shot,
                                analysis=analysis,
                                meta=meta,
                                clip_embedded=bool(
                                    routing.get("clip_embedded")
                                    or meta.get("prefer_wan3_clip_audio")
                                ),
                            )
                        node["config"] = cfg
                        break
                meta["approved_storyboard"] = sb_md
            except Exception:  # noqa: BLE001
                logger.info("Manager storyboard patch of leaf configs failed", exc_info=True)

        # After storyboard edits: prune unused + keep spatial graph coherent for final clip.
        prune_notes = _manager_prune_and_cohere(graph)
        spatial_notes = _spatial_geography_lock_patch(graph)
        patched.extend(prune_notes)
        patched.extend(spatial_notes)

        ack["patched"] = patched[:40]
        ack["pruned_nodes"] = [x.split(":", 1)[-1] for x in prune_notes if x.startswith("pruned:")]
        ack["spatial_lock_fixes"] = spatial_notes
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
            "You are the Designer Manager Agent. Validate once (no loops) for best cinematic "
            "quality while remaining completely faithful to the user prompt, brief, and "
            "storyboard — do not invent plot, cast, or geography. Check: "
            "(1) each shot's character_ids match that beat's focus subjects, "
            "(2) later beats do not reuse the wrong earlier cast, "
            "(3) enough shots cover every major character and prompt beat, "
            "(4) brief/storyboard are comprehensive enough for keyframe and clip prompting, "
            "(5) SPATIAL CONTINUITY: motion + geography — landmarks/layout/light must "
            "match the master scene plate across shot views (edit/ref, not new buildings), "
            "(6) IDENTITY CONSISTENCY: every frame/clip must reference canonical SOLO character "
            "sheets (identity_refs.character_node_ids), not reinvent costumes, "
            "(7) GRAPH USEFULNESS: every node must be useful for the final compose clip — "
            "list prune_ids for unused/orphan nodes; after prune the remaining graph must stay "
            "coherent (master scene → shot views → frames → clips → compose). "
            "Respond JSON only: "
            '{"ok":true|false,"issues":["..."],"prune_ids":["n_unused"],'
            '"spatial_lock":{"landmarks":"...","layout":"...","light":"...","static_rule":"..."},'
            '"shot_fixes":[{"shot_index":1,"character_ids":["char_1"],'
            '"action":"...","continuity_lock":{"motion":"...","facing":"...","forbid":"..."},'
            '"costume_lock":"...","camera":"..."}],"notes":"..."}'
        )
        prompt = json.dumps(
            {
                "user_prompt": graph.get("description"),
                "script_analysis": analysis,
                "continuity_locks": (graph.get("metadata") or {}).get("continuity_locks"),
                "spatial_lock": (graph.get("metadata") or {}).get("spatial_lock"),
                "nodes": [
                    {
                        "id": n.get("id"),
                        "label": n.get("label"),
                        "role": _role_key(n),
                        "character_ids": (n.get("config") or {}).get("character_ids"),
                        "cast_names": (n.get("config") or {}).get("cast_names"),
                        "shot_action": (n.get("config") or {}).get("shot_action"),
                        "camera": (n.get("config") or {}).get("camera"),
                        "continuity_lock": (n.get("config") or {}).get("continuity_lock"),
                        "spatial_lock": (n.get("config") or {}).get("spatial_lock"),
                        "scene_strategy": (n.get("config") or {}).get("scene_strategy"),
                        "inputs": (n.get("config") or {}).get("inputs"),
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
                max_tokens=32768,
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
                if _role_key(node) not in {"frame", "clip", "keyframe"}:
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
                    if _role_key(node) != "storyboard":
                        continue
                    if cfg.get("skip_llm"):
                        cfg["prewritten"] = sb_md
                    else:
                        cfg["draft_prewritten"] = sb_md
                    cfg["planned_shots"] = shots
                    node["config"] = cfg
            except Exception:  # noqa: BLE001
                pass

        # Re-stamp continuity + identity + spatial after LLM fixes; prune unused nodes.
        if isinstance(parsed.get("spatial_lock"), dict):
            meta = dict(graph.get("metadata") or {})
            meta["spatial_lock"] = {
                str(k): str(v)[:400]
                for k, v in parsed["spatial_lock"].items()
                if str(v).strip()
            }
            analysis2 = dict(meta.get("script_analysis") or {})
            analysis2["spatial_lock"] = meta["spatial_lock"]
            meta["script_analysis"] = analysis2
            graph["metadata"] = meta
        # Explicit prune_ids from manager LLM (then structural prune).
        # Never drop shot clips or required audio — every shot must reach the final film with sound.
        protected = {
            str(n.get("id"))
            for n in (graph.get("nodes") or [])
            if isinstance(n, dict)
            and (
                str(n.get("id") or "").startswith("n_clip")
                or str(n.get("id") or "").startswith("n_frame")
                or str(n.get("id") or "") in {"n_scene", "n_compose", "n_speech", "n_music"}
                or _role_key(n)
                in {"clip", "frame", "keyframe", "compose", "speech", "music"}
                # User uploads are authored assets, not generated drafts.
                or bool((n.get("config") or {}).get("user_reference_id"))
            )
        }
        prune_ids = [
            str(x)
            for x in (parsed.get("prune_ids") or [])
            if str(x).strip() and str(x) not in protected and str(x) != "n_compose"
        ]
        if prune_ids:
            drop = set(prune_ids)
            graph["nodes"] = [
                n
                for n in (graph.get("nodes") or [])
                if not isinstance(n, dict) or str(n.get("id")) not in drop
            ]
            graph["edges"] = [
                e
                for e in (graph.get("edges") or [])
                if str(e.get("source") or "") not in drop
                and str(e.get("target") or "") not in drop
            ]
            applied.extend([f"manager_prune:{x}" for x in prune_ids])

        continuity_notes = _spatial_continuity_patch(graph)
        identity_notes = _identity_consistency_patch(graph)
        spatial_notes = _spatial_geography_lock_patch(graph)
        prune_notes = _manager_prune_and_cohere(graph)
        ensure_notes = self.ensure_agents_and_prune(graph)

        ack = {
            **ack,
            "ok": bool(parsed.get("ok", True)),
            "llm_issues": list(parsed.get("issues") or [])[:20],
            "llm_notes": str(parsed.get("notes") or "")[:1000],
            "patched": list(ack.get("patched") or [])
            + applied
            + continuity_notes
            + identity_notes
            + spatial_notes
            + prune_notes
            + list(ensure_notes.get("notes") or []),
            "continuity_fixes": list(ack.get("continuity_fixes") or []) + continuity_notes,
            "identity_fixes": list(ack.get("identity_fixes") or []) + identity_notes,
            "spatial_lock_fixes": list(ack.get("spatial_lock_fixes") or []) + spatial_notes,
            "pruned_nodes": list(
                dict.fromkeys(
                    [x.split(":", 1)[-1] for x in prune_notes if x.startswith("pruned:")]
                    + list(ensure_notes.get("pruned") or [])
                )
            ),
            "ensure_agents": ensure_notes,
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
            max_tokens=32768,
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
                        max_tokens=8192,
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
        recommendations = [
            str(aggregated.get("feedback_supervisor") or "").strip(),
            str(aggregated.get("feedback_manager") or "").strip(),
            str(aggregated.get("graph_design") or "").strip(),
            *[f"{nid}: {txt}" for nid, txt in list(feedback_nodes.items())[:12]],
        ]
        aggregated["aggregated_recommendations"] = [r for r in recommendations if r][:20]
        manager_review = dict(manager_review)
        manager_review["suggestions"] = suggestions
        manager_review["dual_raters"] = aggregated
        manager_review["aggregated_recommendations"] = aggregated["aggregated_recommendations"]
        manager_review["aggregated_overall"] = aggregated["aggregated_overall"]
        return manager_review

    async def assign_dual_raters(
        self,
        graph: DesignerExecutionGraph,
        *,
        agent_feedback: dict[str, dict[str, Any]],
        supervisor_final: dict[str, Any],
        manager_review: dict[str, Any],
        node_states: dict[str, Any] | None,
        use_llm: bool = False,
    ) -> dict[str, Any]:
        """Public alias: two independent raters → dual_raters + aggregated_recommendations."""
        return await self.dual_rate_final(
            graph,
            agent_feedback=agent_feedback,
            supervisor_final=supervisor_final,
            manager_review=manager_review,
            node_states=node_states,
            use_llm=use_llm,
        )


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
            max_tokens=32768,
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
            "aggregated_recommendations": manager_review.get("aggregated_recommendations")
            or (manager_review.get("dual_raters") or {}).get("aggregated_recommendations")
            or [],
            "aggregated_overall": manager_review.get("aggregated_overall"),
        },
        "dual_raters": manager_review.get("dual_raters") or {},
        "aggregated_recommendations": manager_review.get("aggregated_recommendations")
        or (manager_review.get("dual_raters") or {}).get("aggregated_recommendations")
        or [],
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
