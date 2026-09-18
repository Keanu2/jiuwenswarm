# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Domain-agnostic Hollywood leaf-agent instructions (safe for any brief).

Used by Plan A leaf DeepAgents (deepseek-flash etc.) that may lack vision.
Agents must parse brief + storyboard text, optionally inspect upstream images,
then author a single production-ready visual-model prompt.
"""

from __future__ import annotations

from typing import Any


def hollywood_leaf_instructions(role: str) -> str:
    """Compact continuity bible for leaf agents — no scene-genre hardcodes."""
    r = str(role or "").strip().lower()
    shared = (
        "You are a leaf craft artist under a film hierarchy: "
        "Supervisor = Director (creative authority: brief, storyboard, shot prompts); "
        "Manager = Producer (gates, budgets, identity/spatial consistency checks). "
        "You MUST read BRIEF + STORYBOARD and obey the Director's on_screen cast + setting_id. "
        "If vision tools exist, inspect upstream sheets/scene/prior KF and reconcile conflicts "
        "toward the storyboard (never invent a new cast or set).\n"
        "Non-negotiables:\n"
        "1) BRIEF + STORYBOARD + PRODUCTION LOCK BIBLE first — call read_upstream on "
        "n_brief and n_storyboard BEFORE any image/video tool. Obey style, landmarks, "
        "lighting, crowd, speech_line, on_screen cast, setting_id.\n"
        "2) CAST IDENTITY: one body per character id; match wardrobe/face from the FEW attached "
        "solo sheets only. Never swap heroes; never clone one face onto two bodies. "
        "Do not request extra solo sheets for background people.\n"
        "3) SET: same setting_id → same architecture, furniture, window/wall layout, light side, "
        "and landmark screen-side (never teleport pulpit/table/windows).\n"
        "4) STYLE: obey style_lock / LOCK BIBLE for the entire film; "
        "no mid-film medium switch (3D<->2D<->photoreal).\n"
        "5) BLOCKING: honor zones + landmark; featured subjects face the landmark/action focus.\n"
        "6) SCREEN AXIS: keep L/R seats stable under pans (180-degree). Do not flip who is left/right.\n"
        "7) SETTING MASTER: first KF of a setting places ALL named cast clearly visible. "
        "Later edits reframe that master — keep must_appear people unless exiting. "
        "Solos = few identity refs only; no empty scene plate. "
        "Occlusion of a placed person keeps the same sex/age/wardrobe.\n"
        "8) LANGUAGE: use storyboard speech_line exactly (empty = silent — do not invent lines).\n"
        "9) ASPECT: obey film aspect_lock on every still/clip.\n"
        "10) IMAGE-N: if references exist, name Image 1, Image 2… in attach order in your prompt.\n"
        "11) Finish with designer_node_complete(text=<FINAL visual-model prompt only>). "
        "The pipeline materializes Qwen/Wan from that text — make it shot-ready, not a memo.\n"
    )
    if r in {"character", "character_design"}:
        return (
            shared
            + "ROLE=character sheet: PLAIN studio portrait, seamless neutral backdrop, "
            "NO room furniture, NO set, NO text overlays. Lock face/hair/body/wardrobe only."
        )
    if r == "scene":
        return (
            shared
            + "ROLE=environment plate: empty set (no featured-cast faces). "
            "Architecture + materials + light direction freeze for later keyframes."
        )
    if r in {"frame", "keyframe"}:
        return (
            shared
            + "ROLE=keyframe still. "
            "If strategy=compose_from_solo_refs: GENERATE the setting from storyboard/"
            "style_lock AND place every ensemble cast solo into that still — NO empty "
            "scene-plate node; solos = identity only. "
            "If strategy=edit_prior_keyframe: prior KF is Image 1 — REPOSE/reframe/zoom/pose; "
            "keep architecture + remaining ensemble unless exiting. "
            "PROMPT ORDER: REPOSE FIRST, THEN identity. "
            "New setting_id → new compose (do not edit across settings). "
            "If vision unavailable, use storyboard text — no VQA retries. "
            "Forbid cutout/paste collage; integrated lighting and floor contact."
        )
    if r == "clip":
        return (
            shared
            + "ROLE=I2V clip: Image 1 = this shot's keyframe. Motion/camera creativity OK — "
            "but you are LOCKED to Image 1's cast, sex/identity, wardrobe, set, and props. "
            "If previous_clip_action / occupancy.must_appear exist: people who did NOT "
            "leave must still be present; do not erase them. "
            "Brand logos/mascots stay on phone/app UI if that is how the keyframe shows them — "
            "FORBIDDEN: invent a free-flying mascot or swap the human hero for another sex. "
            "Obey Director camera_framing_rule / on_camera vs off_camera lists. "
            "I2V is first-frame only (no extra solo/scene Omni refs). "
            "If vision exists, inspect once; if unavailable, use storyboard text — no VQA retries. "
            "Keep STYLE HOLD + continuity guide + motion_detail/speech_line at the top."
        )
    return shared


def collect_production_context(
    *,
    brief_text: str = "",
    storyboard_text: str = "",
    continuity_guide: str = "",
    skill_excerpt: str = "",
    generate_prompt: str = "",
    max_chars: int = 4500,
) -> dict[str, str]:
    """Trim production texts for leaf agent JSON context."""

    def _trim(s: str, n: int) -> str:
        t = (s or "").strip()
        return t[:n] if t else ""

    return {
        "brief_excerpt": _trim(brief_text, 1600),
        "storyboard_excerpt": _trim(storyboard_text, 2200),
        "continuity_guide": _trim(continuity_guide, 1200),
        "skill_excerpt": _trim(skill_excerpt, 900),
        "current_generate_prompt": _trim(generate_prompt, 1200),
        "hollywood_instructions": hollywood_leaf_instructions(""),
    }


def merge_supervisor_task(existing: str, planned: str) -> str:
    """Keep graph-built Hollywood tasks; append planner task if useful."""
    old = (existing or "").strip()
    new = (planned or "").strip()
    if not old:
        return new
    if not new:
        return old
    markers = (
        "MOVIE CONTINUITY",
        "Image 1",
        "STYLE LOCK",
        "environment plate",
        "solo sheet",
        "I2V",
        "keyframe",
    )
    if any(m.lower() in old.lower() for m in markers):
        if new and new not in old and "Execute " in new:
            return old
        if new and new not in old:
            return f"{old}\nPlanner note: {new}"[:2000]
        return old
    return new or old


def upstream_image_paths(
    ctx: Any,
    pred_ids: list[str],
    *,
    limit: int = 8,
) -> list[dict[str, str]]:
    """Resolve upstream image file paths for vision inspect / prompt binding."""
    from jiuwenswarm.server.runtime.designer.handlers.common import path_from_uri

    run = getattr(ctx, "run", None) or {}
    states = run.get("node_states") or {}
    graph = getattr(ctx, "graph", None) or {}
    role_by_id = {
        str(n.get("id") or ""): str((n.get("config") or {}).get("role") or "")
        for n in (graph.get("nodes") or [])
        if isinstance(n, dict)
    }
    out: list[dict[str, str]] = []
    for nid in pred_ids:
        st = states.get(nid) or {}
        refs: list[Any] = []
        if isinstance(st.get("output_ref"), dict):
            refs.append(st["output_ref"])
        for r in st.get("output_refs") or []:
            if isinstance(r, dict):
                refs.append(r)
        for ref in refs:
            p = path_from_uri(str(ref.get("uri") or ""))
            if p is None or not p.is_file():
                continue
            if p.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                continue
            out.append(
                {
                    "node_id": str(nid),
                    "role": role_by_id.get(str(nid), ""),
                    "path": str(p),
                }
            )
            break
        if len(out) >= limit:
            break
    return out
