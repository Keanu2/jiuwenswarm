# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Build prompt-aware Designer graphs from cast/shot analysis.

Quality layout (default, forward-only):
  Brief → Storyboard → solo cast sheets
  → Keyframes per setting_id (ALL compose from character solos;
    first KF authors scene bible/prompt; later same-setting KFs receive
    prompt handoff + hierarchical view locks — never empty plates)
  → Clips (I2V) + optional Speech/Music → Film (ffmpeg assemble)

Empty scene plates are skipped (``skip_scene_plate``). Solo sheets are identity
locks only. Manager prunes any node that cannot reach ``n_compose`` and re-edits
Brief / Storyboard / locks afterward.
"""

from __future__ import annotations

import re
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    EDGE_KIND_DATA,
    GRAPH_SOURCE_PROMPT,
    NODE_ROLE_BRIEF,
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_ROLE_CLIP,
    NODE_ROLE_COMPOSE,
    NODE_ROLE_FRAME,
    NODE_ROLE_SCENE,
    NODE_ROLE_STORYBOARD,
    NODE_TYPE_AUDIO,
    NODE_TYPE_IMAGE,
    NODE_TYPE_TABLE,
    NODE_TYPE_TEXT,
    NODE_TYPE_VIDEO,
    SCHEMA_VERSION,
    DesignerExecutionGraph,
    normalize_execution_graph,
    new_graph_id,
    node_pipeline,
    utc_now_ms,
)
from jiuwenswarm.server.runtime.designer.skills_loader import attach_skills_metadata

_MAX_LEAN_SHOTS = 16
_MAX_SPLIT_CHARS = 12
_IMAGE_SIZE = "1K"  # cost-save: ~1024 class, not 2K/4K


def _duration_sec_for_graph(prompt: str, analysis: dict[str, Any], characters: list[dict[str, Any]]) -> int:
    raw = analysis.get("target_duration_sec")
    if isinstance(raw, (int, float)) and 1 <= int(raw) <= 30:
        return int(raw)
    match = re.search(r"\b(\d{1,2}(?:\.\d+)?)\s*-?\s*sec(?:ond)?s?\b", (prompt or "").lower())
    if not match:
        match = re.search(r"(\d{1,2}(?:\.\d+)?)\s*秒", prompt or "")
    if match:
        try:
            sec = int(round(float(match.group(1))))
        except (TypeError, ValueError):
            sec = 0
        if 1 <= sec <= 30:
            return sec
    return max(6, min(24, max(1, len(characters)) * 3 + 4))


def default_spatial_lock(scene: dict[str, Any] | None = None) -> dict[str, str]:
    """Geography lock without baking in a church/interior template."""
    scene = scene if isinstance(scene, dict) else {}
    return {
        "setting": str(scene.get("name") or "Primary setting"),
        "architecture": str(scene.get("description") or "keep one coherent place"),
        "static_rule": (
            "STATIC OBJECTS LOCKED to the scene bible: landmarks, terrain, buildings, "
            "props, and light direction stay fixed across hierarchical views "
            "(front/left/right/side/top/bottom). Only camera/framing and on-screen cast change. "
            "Never invent an empty environment plate; never borrow architecture from another setting_id."
        ),
        "crowd_rule": (
            "No empty scene plates. Scene master prompt + solos define the place. Later "
            "same-setting keyframes reuse the scene bible; keep extras silhouette unless "
            "storyboard exits them. Featured cast are distinct people — never clone faces."
        ),
    }


def _character_display_name(character: dict[str, Any], prompt: str) -> str:
    from jiuwenswarm.server.runtime.designer.script_analysis import infer_primary_subject_name

    name = str(character.get("name") or "").strip()
    if name.lower() in {"", "lead"}:
        return infer_primary_subject_name(prompt)
    return name or infer_primary_subject_name(prompt)


def apply_runtime_delegate(graph: DesignerExecutionGraph) -> DesignerExecutionGraph:
    """All creative nodes are LLM agents with tools when a chat model exists.

    Handlers remain only as media-backend materializers after the agent authors
    the creative spec — never as the primary delegate when LLM is available.
    """
    from jiuwenswarm.common.schema.designer_graph import (
        CONFIG_DELEGATE_AGENT,
        CONFIG_DELEGATE_HANDLER,
    )
    from jiuwenswarm.server.runtime.designer.model_tools import llm_available

    use_agents = llm_available()
    delegate = CONFIG_DELEGATE_AGENT if use_agents else CONFIG_DELEGATE_HANDLER
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        config = node.setdefault("config", {})
        if not isinstance(config, dict):
            continue
        if use_agents:
            config.pop("force_handler", None)
            config["delegate"] = CONFIG_DELEGATE_AGENT
            config["kind"] = "agent"
            if config.get("skip_llm"):
                config["skip_llm"] = False
            if config.get("prewritten") and not config.get("draft_prewritten"):
                config["draft_prewritten"] = config.pop("prewritten")
            elif config.get("prewritten"):
                config.pop("prewritten", None)
        elif config.get("force_handler"):
            config["delegate"] = CONFIG_DELEGATE_HANDLER
        else:
            config["delegate"] = delegate
    meta = dict(graph.get("metadata") or {})
    meta["ai_agent_pipeline"] = use_agents
    meta["runtime_delegate"] = delegate
    meta["all_nodes_agents"] = use_agents
    graph["metadata"] = meta
    return normalize_execution_graph(graph)


def _edge(
    eid: str,
    source: str,
    target: str,
    *,
    kind: str = EDGE_KIND_DATA,
    label: str | None = None,
) -> dict[str, Any]:
    edge: dict[str, Any] = {"id": eid, "source": source, "target": target, "kind": kind}
    if label:
        edge["label"] = label
    return edge


def find_non_contributing_node_ids(graph: DesignerExecutionGraph) -> list[str]:
    """Node ids that cannot reach the final compose sink (does not mutate graph)."""
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict) and n.get("id")]
    edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
    ids = {str(n.get("id")) for n in nodes}
    if not ids:
        return []
    sinks = {i for i in ids if i == "n_compose" or i.startswith("n_compose")}
    if not sinks:
        sinks = {
            str(n.get("id"))
            for n in nodes
            if node_pipeline(n) == NODE_ROLE_COMPOSE
        }
    if not sinks:
        return []
    preds: dict[str, set[str]] = {i: set() for i in ids}
    for e in edges:
        s, t = str(e.get("source") or ""), str(e.get("target") or "")
        if s in ids and t in ids:
            preds[t].add(s)
    contributing: set[str] = set()
    stack = list(sinks)
    while stack:
        cur = stack.pop()
        if cur in contributing:
            continue
        contributing.add(cur)
        for p in preds.get(cur) or []:
            if p not in contributing:
                stack.append(p)
    return sorted(ids - contributing)


def prune_non_contributing_nodes(graph: DesignerExecutionGraph) -> list[str]:
    """Remove nodes/edges that cannot reach the final compose (or any sink).

    Forward-only graphs must not keep orphan leaves — except ``user_added`` nodes,
    which Manager keeps and warns about on Run instead of deleting.
    Returns pruned node ids (user_added orphans are NOT pruned).
    """
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict) and n.get("id")]
    edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
    ids = {str(n.get("id")) for n in nodes}
    if not ids:
        return []
    # Prefer compose as the sole required sink; else keep nodes that reach any video sink.
    sinks = {i for i in ids if i == "n_compose" or i.startswith("n_compose")}
    if not sinks:
        sinks = {
            str(n.get("id"))
            for n in nodes
            if node_pipeline(n) == NODE_ROLE_COMPOSE
        }
    if not sinks:
        return []
    # Reverse adjacency: target ← sources
    preds: dict[str, set[str]] = {i: set() for i in ids}
    for e in edges:
        s, t = str(e.get("source") or ""), str(e.get("target") or "")
        if s in ids and t in ids:
            preds[t].add(s)
    contributing: set[str] = set()
    stack = list(sinks)
    while stack:
        cur = stack.pop()
        if cur in contributing:
            continue
        contributing.add(cur)
        for p in preds.get(cur) or []:
            if p not in contributing:
                stack.append(p)
    user_added = {
        str(n.get("id"))
        for n in nodes
        if isinstance((n.get("config") or {}), dict)
        and bool((n.get("config") or {}).get("user_added"))
    }
    orphan = ids - contributing
    preserved = sorted(orphan & user_added)
    pruned = sorted(orphan - user_added)
    meta = dict(graph.get("metadata") or {})
    if preserved:
        meta["non_contributing_user_nodes"] = preserved
        meta["contribution_warning"] = (
            "User-added nodes do not feed the final clip/compose: "
            + ", ".join(preserved)
            + ". Connect them into the pipeline if you want them in the film."
        )
    else:
        meta.pop("non_contributing_user_nodes", None)
        # Keep warning only when still relevant
        if not orphan:
            meta.pop("contribution_warning", None)
    if not pruned and not preserved:
        graph["metadata"] = meta
        return []
    keep = contributing | user_added
    if pruned:
        graph["nodes"] = [n for n in nodes if str(n.get("id")) in keep]
        graph["edges"] = [
            e
            for e in edges
            if str(e.get("source") or "") in keep
            and str(e.get("target") or "") in keep
        ]
    notes = list(meta.get("prune_notes") or [])
    notes.extend([f"pruned:{nid}" for nid in pruned])
    if preserved:
        notes.extend([f"kept_user_orphan:{nid}" for nid in preserved])
    meta["prune_notes"] = notes[-40:]
    graph["metadata"] = meta
    return pruned


def _write_brief_markdown(
    prompt: str,
    characters: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
    audio: dict[str, Any],
    duration_sec: int | None = None,
) -> str:
    cast_lines = []
    for c in characters:
        name = _character_display_name(c, prompt)
        desc = str(c.get("description") or "").strip()
        cast_lines.append(f"- **{name}:** {desc or 'lock face, hair, body, costume'}")
    setting = str((scenes[0] if scenes else {}).get("name") or "Primary setting")
    setting_desc = str((scenes[0] if scenes else {}).get("description") or "")
    policy = str(audio.get("policy") or "optional_music")
    duration = int(duration_sec or max(6, min(24, max(1, len(characters)) * 3 + 4)))
    fallback_cast = _character_display_name(
        characters[0] if characters else {},
        prompt,
    )
    return (
        f"# Brief\n\n"
        f"**User prompt (verbatim intent):** {prompt.strip()[:600]}\n\n"
        f"**Logline:** {prompt.strip()[:280]}\n\n"
        f"**Cast (solo identity locks — one sheet each, never concatenate):**\n"
        + ("\n".join(cast_lines) or f"- **{fallback_cast}:** lock face, hair, body, costume")
        + "\n\n"
        f"**Setting:** {setting} — {setting_desc}\n\n"
        f"**Consistency gates:**\n"
        f"- Character: same face/wardrobe every shot unless brief says change\n"
        f"- Scene: shared architecture/lighting across shot views\n"
        f"- Motion / continuity: time-coherent (e.g. after a man stands and leaves, "
        f"later shots must not show him seated again)\n"
        f"- Camera: distinct views per shot covering the prompt beats\n\n"
        f"**Duration:** ~{duration}s short film\n\n"
        f"**Visual style:** cinematic, coherent lighting, no subtitles\n\n"
        f"**Audio policy:** {policy}\n\n"
        f"**Avoid:** comic grids, watermark text, identity drift, orphan graph nodes\n"
    )


def _write_storyboard_markdown(shots: list[dict[str, Any]], characters: list[dict[str, Any]]) -> str:
    id_to_name = {str(c.get("id")): str(c.get("name") or c.get("id")) for c in characters}
    # Hierarchical: scenes (setting_id) → keyframes/shots.
    by_set: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        sid = str(shot.get("setting_id") or "set_1").strip() or "set_1"
        if sid not in by_set:
            by_set[sid] = []
            order.append(sid)
        by_set[sid].append(shot)
    lines = [
        "# Storyboard Scenario",
        "",
        "Hierarchy: **Scene (setting_id)** → **Keyframes/shots**.",
        "Different scenes = different places. First keyframe of each scene authors the "
        "**scene bible + master prompt** (compose place + only on-screen cast) — not an "
        "empty plate. Later same-scene keyframes **compose again from character solos** "
        "using that shared scene prompt/view locks (architecture locked). "
        "Never borrow another setting_id. Not every cast member is in every scene.",
        "",
    ]
    for sid in order:
        scene_shots = by_set.get(sid) or []
        place = ""
        for shot in scene_shots:
            place = str(shot.get("setting_description") or "").strip()
            if place:
                break
        lines.append(f"## Scene `{sid}`" + (f" — {place}" if place else ""))
        lines.append("")
        for shot in scene_shots:
            idx = int(shot.get("shot_index") or 0)
            strategy = str(shot.get("keyframe_strategy") or "")
            visible = [
                id_to_name.get(str(cid), str(cid))
                for cid in (
                    shot.get("on_screen")
                    or shot.get("visible_cast_ids")
                    or shot.get("character_ids")
                    or []
                )
            ]
            offscreen = [
                id_to_name.get(str(cid), str(cid))
                for cid in (shot.get("offscreen") or shot.get("off_screen_cast_ids") or [])
            ]
            featured = [
                id_to_name.get(str(cid), str(cid))
                for cid in (shot.get("featured_cast_ids") or [])
            ]
            actions = shot.get("cast_actions") if isinstance(shot.get("cast_actions"), dict) else {}
            doing_lines = [
                f"{id_to_name.get(str(cid), str(cid))}: {act}"
                for cid, act in actions.items()
                if str(act).strip()
            ]
            crowd = shot.get("crowd_lock") if isinstance(shot.get("crowd_lock"), dict) else {}
            lines.append(f"### Shot {idx} — {shot.get('title') or f'Beat {idx}'}")
            lines.append(f"- Timeline: {shot.get('timeline') or ''}")
            lines.append(f"- Strategy: `{strategy}`")
            lines.append(f"- Camera: {shot.get('camera') or ''}")
            lines.append(f"- On screen (visible): {', '.join(visible) or '—'}")
            lines.append(f"- Offscreen (in scene, not in frame): {', '.join(offscreen) or '—'}")
            lines.append(f"- Featured (camera focus): {', '.join(featured) or '—'}")
            if doing_lines:
                lines.append(f"- Doing: {'; '.join(doing_lines)}")
            lines.append(f"- Action: {shot.get('action') or shot.get('keyframe_prompt') or ''}")
            if shot.get("scene_distinctness"):
                lines.append(f"- Scene note: {shot.get('scene_distinctness')}")
            if crowd:
                lines.append(
                    f"- Crowd lock: present={crowd.get('present')}; "
                    f"{str(crowd.get('density') or '')}"
                )
            done = shot.get("already_done") or []
            if done:
                lines.append(f"- Already done: {'; '.join(str(x) for x in done[:8])}")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def _shot_budget(analysis: dict[str, Any], shots: list[dict[str, Any]]) -> int:
    """Supervisor / storyboard shot count wins — never collapse to 1 for short films."""
    n = len(shots) or 1
    try:
        target = int(analysis.get("target_shot_count") or 0)
    except (TypeError, ValueError):
        target = 0
    # Prefer the richer of target vs existing shots (storyboard may expand).
    want = max(n, target) if target >= 1 else n
    # Soft ceiling only for runaway graphs — not a creative lock.
    return max(1, min(_MAX_LEAN_SHOTS, want))


def _ensure_characters_referenced(
    characters: list[dict[str, Any]], shots: list[dict[str, Any]]
) -> None:
    """Attach uncovered cast to the best-matching shot (never blindly dump onto shot 1)."""
    from jiuwenswarm.server.runtime.designer.script_analysis import _score_character_in_text

    covered = {str(cid) for s in shots for cid in (s.get("character_ids") or [])}
    for ch in characters:
        cid = str(ch.get("id") or "")
        if not cid or cid in covered or not shots:
            continue
        best_i = len(shots) - 1
        best_sc = -1
        for i, shot in enumerate(shots):
            blob = f"{shot.get('action') or ''} {shot.get('keyframe_prompt') or ''}"
            sc = _score_character_in_text(ch, blob)
            if sc > best_sc:
                best_sc = sc
                best_i = i
        shots[best_i]["character_ids"] = list(
            dict.fromkeys([*(shots[best_i].get("character_ids") or []), cid])
        )
        covered.add(cid)


def _plan_cast_sheets(
    characters: list[dict[str, Any]],
    shots: list[dict[str, Any]],
    *,
    prefer_combined: bool,
    prompt: str = "",
) -> tuple[list[dict[str, Any]], dict[str, list[str]], str]:
    """Plan cast postcard nodes and per-shot node refs.

    Identity rule: **always** one solo sheet per character (canonical look).
    Optional combined sheets are compose aids only — keyframes/clips must
    reference solo sheets via ``character_node_ids`` so wardrobe cannot drift
    when a multi-person postcard is regenerated independently. Combined aids
    are still wired into matching frame/clip edges by the graph builder.
    """
    id_to_char = {str(c.get("id")): c for c in characters if str(c.get("id") or "")}
    groups: list[frozenset[str]] = []
    seen_groups: set[frozenset[str]] = set()
    for shot in shots:
        cids = [
            str(x)
            for x in (shot.get("character_ids") or [])
            if str(x) in id_to_char
        ]
        cids = list(dict.fromkeys(cids))
        if len(cids) >= 2:
            key = frozenset(cids)
            if key not in seen_groups:
                seen_groups.add(key)
                groups.append(key)

    sheets: list[dict[str, Any]] = []
    budget = max(_MAX_SPLIT_CHARS, len(characters) + len(groups))

    # 1) Canonical solo identity sheets — always.
    for ch in characters:
        cid = str(ch.get("id") or "")
        if not cid or cid not in id_to_char:
            continue
        if len(sheets) >= budget:
            break
        name = _character_display_name(ch, prompt) or cid
        desc = str(ch.get("description") or "").strip()
        sheets.append(
            {
                "character_ids": [cid],
                "character_names": [name],
                "combined_cast": False,
                "identity_source": True,
                "label": name[:48],
                "prompt_body": f"{name}: {desc}" if desc else name,
                "costume_lock": desc[:240] if desc else f"canonical look for {name}",
            }
        )

    # 2) Optional combined compose aids (not the identity source of truth).
    use_combined = bool(prefer_combined and groups)
    if use_combined:
        for g in groups:
            if len(sheets) >= budget:
                break
            members = [id_to_char[cid] for cid in sorted(g) if cid in id_to_char]
            if not members:
                continue
            names = [str(m.get("name") or m.get("id")) for m in members]
            sheets.append(
                {
                    "character_ids": [str(m.get("id")) for m in members],
                    "character_names": names,
                    "combined_cast": True,
                    "identity_source": False,
                    "label": (" & ".join(names))[:48],
                    "prompt_body": "; ".join(
                        f"{m.get('name')}: {m.get('description')}" for m in members
                    ),
                    "costume_lock": "; ".join(
                        f"{m.get('name')}: {str(m.get('description') or '')[:80]}"
                        for m in members
                    )[:320],
                }
            )

    if not sheets and characters:
        ch = characters[0]
        name = _character_display_name(ch, prompt)
        sheets.append(
            {
                "character_ids": [str(ch.get("id") or "char_1")],
                "character_names": [name],
                "combined_cast": False,
                "identity_source": True,
                "label": name[:48],
                "prompt_body": f"{name}: {ch.get('description')}",
                "costume_lock": str(ch.get("description") or name)[:240],
            }
        )

    # Assign node ids — solos first (n_character / n_character_i), combined as n_cast_*.
    solo_count = sum(1 for s in sheets if not s["combined_cast"])
    solo_i = 0
    cast_i = 0
    for sheet in sheets:
        if sheet["combined_cast"]:
            cast_i += 1
            sheet["node_id"] = f"n_cast_{cast_i}"
        else:
            solo_i += 1
            if solo_count == 1:
                sheet["node_id"] = "n_character"
            else:
                sheet["node_id"] = f"n_character_{solo_i}"

    solo_by_id: dict[str, str] = {}
    costume_by_id: dict[str, str] = {}
    for sheet in sheets:
        ids = [str(x) for x in sheet["character_ids"]]
        if not sheet["combined_cast"] and len(ids) == 1:
            solo_by_id[ids[0]] = str(sheet["node_id"])
            costume_by_id[ids[0]] = str(sheet.get("costume_lock") or "")

    # Per-shot refs = solo sheets only (compose multi-char keyframes from individuals).
    shot_to_nodes: dict[str, list[str]] = {}
    for shot in shots:
        idx = str(int(shot.get("shot_index") or 0))
        cids = [
            str(x)
            for x in (shot.get("character_ids") or [])
            if str(x) in id_to_char
        ]
        cids = list(dict.fromkeys(cids))
        nodes = [solo_by_id[cid] for cid in cids if cid in solo_by_id]
        if not nodes:
            nodes = [str(s["node_id"]) for s in sheets if not s["combined_cast"]] or [
                str(s["node_id"]) for s in sheets
            ]
        shot_to_nodes[idx] = list(dict.fromkeys(nodes))

    if use_combined and solo_count:
        layout = "solo_first_with_combined_aids"
    elif solo_count <= 1:
        layout = "single"
    else:
        layout = "solo_first"
    # Stash costume map on first sheet metadata for graph builder (returned via sheets).
    for sheet in sheets:
        sheet["_costume_by_id"] = costume_by_id
    return sheets, shot_to_nodes, layout


def _combined_aid_nodes_for_shot(
    cast_sheets: list[dict[str, Any]],
    focus_cids: list[str],
) -> list[str]:
    """Combined cast postcard ids that intersect this shot's character_ids.

    Compose aids only — never identity sources. Caller must attach them to
    ``frame_inputs`` / ``clip_inputs`` (and edges) but leave
    ``identity_refs.character_node_ids`` as solos.
    """
    focus_set = {str(x) for x in focus_cids if str(x)}
    if not focus_set:
        return []
    aids: list[str] = []
    for sheet in cast_sheets:
        if not sheet.get("combined_cast"):
            continue
        sheet_ids = {str(x) for x in (sheet.get("character_ids") or []) if str(x)}
        if sheet_ids & focus_set:
            aids.append(str(sheet["node_id"]))
    return list(dict.fromkeys(aids))


def ensure_combined_cast_reach_compose(graph: DesignerExecutionGraph) -> list[str]:
    """Safety net: every combined-cast node must be an edge source into the DAG.

    If a combined sheet never sources an edge to a frame/clip/compose/storyboard,
    wire it to ``n_frame_1`` (or ``n_compose`` if no frames) and append to that
    target's ``config.inputs``.
    """
    notes: list[str] = []
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
    by_id = {str(n.get("id") or ""): n for n in nodes if n.get("id")}
    ids = set(by_id)
    if not ids:
        return notes

    sink_roles = {
        NODE_ROLE_FRAME,
        "keyframe",
        NODE_ROLE_CLIP,
        NODE_ROLE_COMPOSE,
        NODE_ROLE_STORYBOARD,
        "frame",
        "clip",
        "compose",
        "storyboard",
    }
    wired_sources: set[str] = set()
    edge_keys: set[tuple[str, str]] = set()
    for e in edges:
        s, t = str(e.get("source") or ""), str(e.get("target") or "")
        if s not in ids or t not in ids:
            continue
        edge_keys.add((s, t))
        trole = node_pipeline(by_id[t])
        if (
            trole in sink_roles
            or t in {"n_compose", "n_storyboard"}
            or t.startswith("n_frame_")
            or t.startswith("n_clip_")
        ):
            wired_sources.add(s)

    combined_ids = [
        str(sheet_id)
        for sheet_id, node in by_id.items()
        if bool((node.get("config") or {}).get("combined_cast"))
        or sheet_id.startswith("n_cast_")
    ]
    has_frame_1 = "n_frame_1" in ids
    has_compose = "n_compose" in ids or any(
        node_pipeline(by_id[i]) == NODE_ROLE_COMPOSE
        for i in ids
    )
    fallback = "n_frame_1" if has_frame_1 else (
        "n_compose" if "n_compose" in ids else next(
            (
                i
                for i in sorted(ids)
                if node_pipeline(by_id[i])
                == NODE_ROLE_COMPOSE
            ),
            None,
        )
    )
    if not fallback and not has_compose:
        return notes

    changed = False
    for cid in combined_ids:
        if cid in wired_sources:
            continue
        target = fallback
        if not target or target not in ids:
            continue
        if (cid, target) not in edge_keys:
            edges.append(_edge(f"e_{cid}_{target}", cid, target))
            edge_keys.add((cid, target))
        tnode = by_id[target]
        tcfg = dict(tnode.get("config") or {})
        inputs = list(tcfg.get("inputs") or [])
        if cid not in inputs:
            inputs.append(cid)
            tcfg["inputs"] = inputs
            tnode["config"] = tcfg
        notes.append(f"{cid}: safety-wired -> {target}")
        changed = True
        wired_sources.add(cid)

    if changed:
        graph["edges"] = edges
        graph["nodes"] = nodes
    return notes


def _cameras_compatible(a: str, b: str) -> bool:
    """True when sequential keyframe edit is safer than a full recompose."""
    la = (a or "").strip().lower()
    lb = (b or "").strip().lower()
    if not la or not lb:
        return False
    if la == lb:
        return True
    # Treat generic medium/eye-level variants as compatible.
    mediumish = ("medium", "eye-level", "eye level")
    if any(m in la for m in mediumish) and any(m in lb for m in mediumish):
        if "close" in la or "close" in lb or "wide" in la or "wide" in lb:
            return False
        return True
    return False


def _costume_lock_for_ids(
    characters: list[dict[str, Any]], character_ids: list[str]
) -> str:
    parts: list[str] = []
    id_to = {str(c.get("id")): c for c in characters if isinstance(c, dict)}
    for cid in character_ids:
        ch = id_to.get(str(cid))
        if not ch:
            continue
        name = str(ch.get("name") or cid)
        desc = str(ch.get("description") or "").strip()
        parts.append(f"{name}: {desc[:160]}" if desc else name)
    return "; ".join(parts)[:480]


def _ensure_setting_ids(
    shots: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
) -> None:
    """Stamp setting_id on every shot (domain-agnostic).

    Prefer explicit setting_id / scene_id; otherwise carry previous setting so
    book/search/plan language does not invent a location jump.
    """
    default = "set_1"
    if scenes:
        default = str(scenes[0].get("id") or "set_1").strip() or "set_1"
    prev = default
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        sid = str(shot.get("setting_id") or shot.get("scene_id") or "").strip()
        if sid:
            prev = sid
        else:
            sid = prev
        shot["setting_id"] = sid


def build_smart_video_graph(
    *,
    project_id: str,
    prompt: str,
    analysis: dict[str, Any],
    title: str | None = None,
    optimize_for: str = "quality",
    ai_mode: bool | None = None,
) -> DesignerExecutionGraph:
    """Lean multi-shot video DAG: few image gens + brief/storyboard.

    When ``ai_mode`` is True (LLM available), Brief/Storyboard are authored by
    node agents (no skip_llm). Heuristic prewrites are only used as drafts/fallback.
    """
    from jiuwenswarm.server.runtime.designer.experiments.keyframe_policy import (
        apply_compose_solos_setting_policy,
    )
    from jiuwenswarm.server.runtime.designer.model_tools import llm_available

    if ai_mode is None:
        ai_mode = llm_available()
    prompt_text = prompt.strip()
    mode = "cost" if str(optimize_for).strip().lower() == "cost" else "quality"
    characters = list(analysis.get("characters") or [])
    scenes = list(analysis.get("scenes") or [])
    shots = list(analysis.get("shots") or [])
    audio = dict(analysis.get("audio") or {})
    if not characters:
        from jiuwenswarm.server.runtime.designer.script_analysis import infer_primary_subject_name

        characters = [
            {
                "id": "char_1",
                "name": infer_primary_subject_name(prompt_text),
                "description": prompt_text[:200],
            }
        ]
    if not scenes:
        scenes = [{"id": "scene_1", "name": "Setting", "description": "primary setting"}]
    if not shots:
        shots = [
            {
                "shot_index": 1,
                "title": "Shot 1",
                "action": prompt_text[:300],
                "camera": "medium / eye-level",
                "character_ids": [characters[0]["id"]],
                "keyframe_prompt": prompt_text[:300],
                "timeline": "0.0-2.0s",
            }
        ]
    shots = shots[: _shot_budget(analysis, shots)]
    analysis["target_shot_count"] = len(shots)
    for i, shot in enumerate(shots, start=1):
        shot["shot_index"] = i
    _ensure_characters_referenced(characters, shots)
    _ensure_setting_ids(shots, scenes)
    analysis["characters"] = characters
    analysis["scenes"] = scenes
    analysis["shots"] = shots
    analysis = apply_compose_solos_setting_policy(analysis)
    characters = list(analysis.get("characters") or characters)
    scenes = list(analysis.get("scenes") or scenes)
    shots = list(analysis.get("shots") or shots)
    # Continuity: NEVER build empty scene plates. Every KF composes from solos;
    # first KF/setting authors scene bible; later same-set KFs get prompt handoff.
    skip_scene_plate = True
    analysis["skip_scene_plate"] = True
    analysis["scene_continuity_mode"] = "compose_solos_shared_scene_prompt"

    # Quality path: solo identity sheets only — keyframes compose multi-person.
    prefer_combined = False
    cast_sheets, shot_cast_nodes, cast_layout = _plan_cast_sheets(
        characters, shots, prefer_combined=prefer_combined, prompt=prompt_text
    )
    # Drop any combined sheets that slipped through (no concatenation).
    cast_sheets = [s for s in cast_sheets if not s.get("combined_cast")]
    film_duration = _duration_sec_for_graph(prompt_text, analysis, characters)
    brief_md = _write_brief_markdown(
        prompt_text,
        characters,
        scenes,
        audio,
        duration_sec=film_duration,
    )
    storyboard_md = _write_storyboard_markdown(shots, characters)

    graph_id = new_graph_id()
    now = utc_now_ms()
    graph_title = title.strip() if isinstance(title, str) and title.strip() else prompt_text[:80]
    speed = (
        "Be fast: one clear image, simple clean background, no grid, no text overlays. "
        "Prioritize identity lock over ornate detail."
    )

    nodes: list[dict[str, Any]] = [
        {
            "id": "n_brief",
            "type": NODE_TYPE_TEXT,
            "label": "Brief",
            "config": {
                "role": NODE_ROLE_BRIEF,
                "prompt": prompt_text,
                # AI mode: agents write the brief; draft is a hint only.
                **(
                    {"draft_prewritten": brief_md, "skip_llm": False, "tools": ["call_model", "write_artifact"]}
                    if ai_mode
                    else {"prewritten": brief_md, "skip_llm": True, "tools": ["write_artifact"]}
                ),
                "optimize_for": mode,
                "agent_name": "Brief Agent",
                "kind": "agent",
                "skill_id": "brief",
                "delegate": ("agent" if ai_mode else "handler"),
                "supervisor_task": (
                    "Author a detailed creative brief from the user prompt: cast identity locks, "
                    "scene geography, motion/continuity rules, shot-view coverage, audio policy. "
                    "Preserve every named beat from the user prompt."
                ),
            },
            "layout": {"x": 40, "y": 220, "width": 260, "height": 140},
        }
    ]
    edges: list[dict[str, Any]] = []

    # Storyboard after brief (manager will gate fidelity before cast/scene run).
    sb_cfg: dict[str, Any] = {
        "role": NODE_ROLE_STORYBOARD,
        "prompt": prompt_text,
        "planned_shots": shots,
        "inputs": ["n_brief"],
        "optimize_for": mode,
        "agent_name": "Storyboard Agent",
        "kind": "agent",
        "skill_id": "storyboard",
        "delegate": ("agent" if ai_mode else "handler"),
        "supervisor_task": (
            "Build a time-coherent storyboard from the approved brief: shot duration, "
            "camera/view, cast on screen, action, and continuity forbids "
            "(e.g. after standing/leaving, do not reseat the same man)."
        ),
    }
    if ai_mode:
        sb_cfg["draft_prewritten"] = storyboard_md
        sb_cfg["skip_llm"] = False
        sb_cfg["tools"] = ["call_model", "write_artifact"]
    else:
        sb_cfg["prewritten"] = storyboard_md
        sb_cfg["skip_llm"] = True
        sb_cfg["tools"] = ["write_artifact"]
    nodes.append(
        {
            "id": "n_storyboard",
            "type": NODE_TYPE_TABLE,
            "label": "Storyboard",
            "config": sb_cfg,
            "layout": {"x": 340, "y": 220, "width": 280, "height": 150},
        }
    )
    edges.append(_edge("e_brief_storyboard", "n_brief", "n_storyboard"))

    char_node_ids: list[str] = []
    sheet_by_id: dict[str, dict[str, Any]] = {}
    for i, sheet in enumerate(cast_sheets, start=1):
        nid = str(sheet["node_id"])
        char_node_ids.append(nid)
        sheet_by_id[nid] = sheet
        names = [str(n) for n in sheet.get("character_names") or []]
        # Namecard: character display name (Manager-approved via analysis cast).
        display = (names[0] if names else str(sheet.get("label") or "Character")).strip()
        label = f"character: {display}"
        prompt = (
            f"{speed}\nCANONICAL character identity postcard for "
            f"{display} — lock face, hair, body type, and costume. "
            f"Solo sheet only (never group/concat portraits). "
            f"Do not invent alternate wardrobe. {sheet.get('prompt_body')}. "
            f"Story context: {prompt_text[:180]}"
        )
        agent = label
        nodes.append(
            {
                "id": nid,
                "type": NODE_TYPE_IMAGE,
                "label": label,
                "config": {
                    "role": NODE_ROLE_CHARACTER_DESIGN,
                    "prompt": prompt,
                    "character_id": (sheet["character_ids"][0] if len(sheet["character_ids"]) == 1 else None),
                    "character_ids": list(sheet["character_ids"]),
                    "character_name": (names[0] if names else label),
                    "character_names": names,
                    "combined_cast": False,
                    "identity_source": True,
                    "costume_lock": str(sheet.get("costume_lock") or ""),
                    "image_size": _IMAGE_SIZE,
                    "max_image_calls": 1,
                    "inputs": ["n_brief", "n_storyboard"],
                    "optimize_for": mode,
                    "agent_name": agent,
                    "kind": "agent",
                    "skill_id": "character",
                    "tools": ["call_image_model", "read_upstream", "call_model"],
                    "delegate": ("agent" if ai_mode else "handler"),
                },
                "layout": {"x": 680, "y": float(40 + (i - 1) * 160), "width": 240, "height": 140},
            }
        )
        edges.append(_edge(f"e_sb_{nid}", "n_storyboard", nid))
        edges.append(_edge(f"e_brief_{nid}", "n_brief", nid))

    # Spatial lock text (weak env hint only). Empty scene plates are never built —
    # first KF per setting_id is the scene master (setting + visible cast).
    scene_base = scenes[0] if scenes else {"id": "scene_1", "name": "Setting", "description": ""}
    spatial_lock = default_spatial_lock(scene_base if isinstance(scene_base, dict) else None)
    prior_lock = analysis.get("spatial_lock") if isinstance(analysis.get("spatial_lock"), dict) else {}
    for k, v in prior_lock.items():
        if str(v).strip():
            spatial_lock[str(k)] = str(v).strip()[:400]
    setting_places = (
        analysis.get("setting_places")
        if isinstance(analysis.get("setting_places"), dict)
        else {}
    )
    spatial_by_setting: dict[str, dict[str, Any]] = {}
    for sid, place in setting_places.items():
        sid_s = str(sid).strip() or "set_1"
        base = dict(spatial_lock)
        base["setting"] = sid_s
        if str(place).strip():
            base["architecture"] = str(place).strip()[:400]
            base["static_rule"] = (
                f"SETTING `{sid_s}` place lock: {str(place).strip()[:220]}. "
                "Same-setting edits keep this architecture; other setting_ids must look different."
            )
        spatial_by_setting[sid_s] = base

    def _lock_line_for(sid: str) -> str:
        lock = spatial_by_setting.get(sid) or spatial_lock
        return (
            f"SPATIAL LOCK (text hint only — not an empty plate): setting={lock.get('setting')}; "
            f"{lock.get('architecture')}; {lock.get('static_rule')} "
            f"{lock.get('crowd_rule')}"
        )

    master_id = ""
    scene_node_by_shot: dict[int, str] = {}
    # skip_scene_plate always True under safer continuity — no n_scene empty plates.
    scene_locks_meta: dict[str, Any] = (
        analysis.get("scene_locks")
        if isinstance(analysis.get("scene_locks"), dict)
        else {}
    )
    setting_order: list[str] = []
    for _shot in shots:
        _sid = str((_shot or {}).get("setting_id") or "set_1").strip() or "set_1"
        if _sid not in setting_order:
            setting_order.append(_sid)
    setting_num = {sid: i + 1 for i, sid in enumerate(setting_order)}

    clip_ids: list[str] = []
    prev_frame_by_setting: dict[str, str] = {}
    scene_master_by_setting: dict[str, str] = {}
    prev_clip_by_setting: dict[str, str] = {}
    prev_clip_global = ""
    camera_cycle = (
        "wide / establishing",
        "medium / eye-level",
        "close-up / eye-level",
        "medium / slow pan",
    )
    for shot in shots:
        idx = int(shot.get("shot_index") or (len(clip_ids) + 1))
        frame_id = f"n_frame_{idx}"
        clip_id = f"n_clip_{idx}"
        clip_ids.append(clip_id)
        setting_id = str(shot.get("setting_id") or "set_1").strip() or "set_1"
        focus_char_nodes = list(
            dict.fromkeys(shot_cast_nodes.get(str(idx), list(char_node_ids)))
        )
        # Prefer solo identity sheets only.
        focus_char_nodes = [
            nid
            for nid in focus_char_nodes
            if not bool(sheet_by_id.get(nid, {}).get("combined_cast"))
        ] or [
            str(s["node_id"])
            for s in cast_sheets
            if not s.get("combined_cast")
        ] or list(dict.fromkeys(char_node_ids))
        # Visible / on_screen for THIS shot only — not the whole film cast.
        occ0 = shot.get("occupancy") if isinstance(shot.get("occupancy"), dict) else {}
        visible_cids = [
            str(x)
            for x in (
                shot.get("on_screen")
                or shot.get("visible_cast_ids")
                or occ0.get("must_appear")
                or shot.get("compose_cast_ids")
                or shot.get("character_ids")
                or []
            )
            if str(x)
        ]
        offscreen_cids = [
            str(x)
            for x in (
                shot.get("offscreen")
                or shot.get("off_screen_cast_ids")
                or occ0.get("offscreen")
                or []
            )
            if str(x)
        ]
        # Compose / edit both draw only visible people; offscreen stay out of frame.
        focus_cids = list(dict.fromkeys(visible_cids))
        featured_cids = [
            str(x)
            for x in (shot.get("featured_cast_ids") or focus_cids[:1] or [])
            if str(x)
        ] or list(focus_cids)
        cast_actions = (
            shot.get("cast_actions")
            if isinstance(shot.get("cast_actions"), dict)
            else (occ0.get("cast_actions") if isinstance(occ0.get("cast_actions"), dict) else {})
        )
        # Domain-agnostic: if action text names a cast member, keep them visible.
        action_l = str(shot.get("action") or shot.get("keyframe_prompt") or "").lower()
        for c in characters:
            cid = str(c.get("id") or "")
            name = str(c.get("name") or "").lower()
            if not cid or cid in focus_cids:
                continue
            tokens = [t for t in name.replace("-", " ").split() if len(t) > 2]
            if tokens and any(t in action_l for t in tokens):
                focus_cids.append(cid)
                if cid in offscreen_cids:
                    offscreen_cids = [x for x in offscreen_cids if x != cid]
        shot["character_ids"] = list(dict.fromkeys(focus_cids))
        shot["on_screen"] = list(shot["character_ids"])
        shot["offscreen"] = [c for c in offscreen_cids if c not in shot["character_ids"]]
        id_to_node: dict[str, str] = {}
        for s in cast_sheets:
            if s.get("combined_cast"):
                continue
            ids = [str(x) for x in (s.get("character_ids") or []) if str(x)]
            if len(ids) == 1 and s.get("node_id"):
                id_to_node[ids[0]] = str(s["node_id"])
        rebuilt = [id_to_node[c] for c in focus_cids if c in id_to_node]
        if rebuilt:
            focus_char_nodes = list(dict.fromkeys(rebuilt))
        focus_names = [
            str(c.get("name"))
            for c in characters
            if str(c.get("id")) in focus_cids
        ]
        if not focus_names:
            for nid in focus_char_nodes:
                focus_names.extend(sheet_by_id.get(nid, {}).get("character_names") or [])
            focus_names = list(dict.fromkeys([n for n in focus_names if n]))
        cast_who = ", ".join(focus_names) or "main cast"
        doing_line = "; ".join(
            f"{(next((c.get('name') for c in characters if str(c.get('id'))==cid), cid))}:"
            f" {cast_actions[cid]}"
            for cid in focus_cids
            if cast_actions.get(cid)
        )
        multi = len(focus_names) > 1
        camera = str(shot.get("camera") or "").strip() or camera_cycle[(idx - 1) % len(camera_cycle)]
        shot["camera"] = camera
        action = str(shot.get("action") or shot.get("keyframe_prompt") or "")[:300]
        costume_lock = _costume_lock_for_ids(characters, focus_cids)
        shot_spatial = spatial_by_setting.get(setting_id) or spatial_lock
        lock_line = _lock_line_for(setting_id)
        # Continuity: first KF of a setting = SCENE MASTER (compose + bible).
        # Later same-setting KFs also compose from solos, but depend on the master
        # for prompt handoff only (not as an image edit source).
        prior_in_set = prev_frame_by_setting.get(setting_id)
        scene_master_id = scene_master_by_setting.get(setting_id)
        if prior_in_set:
            keyframe_strategy = "compose_from_solo_refs"
            is_scene_master = False
            prompt_handoff_from = scene_master_id or prior_in_set
        else:
            keyframe_strategy = "compose_from_solo_refs"
            is_scene_master = True
            prompt_handoff_from = None
        prev_frame_id = prompt_handoff_from
        _ = prev_frame_id
        shot_scene_id = scene_node_by_shot.get(idx) or ""
        # ALL solo identity sheets must finish before ANY keyframe.
        all_solo_ids = [
            nid
            for nid in char_node_ids
            if not bool(sheet_by_id.get(nid, {}).get("combined_cast"))
        ] or list(dict.fromkeys(char_node_ids))
        frame_inputs = [*all_solo_ids, "n_storyboard", "n_brief"]
        # Soft dep: wait for scene-master KF so its prompt can be handed off.
        if prompt_handoff_from:
            frame_inputs.append(prompt_handoff_from)
        frame_inputs = list(dict.fromkeys([x for x in frame_inputs if x]))
        clip_inputs = ["n_storyboard", frame_id]
        if prompt_handoff_from:
            clip_inputs.append(prompt_handoff_from)
        clip_inputs = list(dict.fromkeys(clip_inputs))
        y = 40 + (idx - 1) * 160
        occupancy = shot.get("occupancy") if isinstance(shot.get("occupancy"), dict) else {}
        already_done = [
            str(x)
            for x in (shot.get("already_done") or [])
            if str(x).strip()
        ]
        scene_bible = (
            shot.get("scene_bible")
            if isinstance(shot.get("scene_bible"), dict)
            else (scene_locks_meta.get(setting_id) if isinstance(scene_locks_meta.get(setting_id), dict) else {})
        )
        view_key = str(shot.get("view_key") or (scene_bible or {}).get("active_view") or "front")
        bible_line = ""
        if scene_bible:
            views = scene_bible.get("views") if isinstance(scene_bible.get("views"), dict) else {}
            view_line = str(views.get(view_key) or views.get("front") or "")[:220]
            bible_line = (
                f"SCENE BIBLE `{setting_id}`: place={scene_bible.get('place')}; "
                f"lighting={scene_bible.get('lighting')}; "
                f"objects={', '.join(str(x) for x in (scene_bible.get('objects') or [])[:6])}; "
                f"crowd={scene_bible.get('crowd')}; "
                f"{scene_bible.get('coherence_rule')}; "
                f"ACTIVE {view_line}. "
            )
        if prompt_handoff_from and not is_scene_master:
            cast_ref_line = (
                f"IDENTITY: COMPOSE from solo sheets {', '.join(focus_char_nodes)} for ONLY "
                f"{cast_who}. SCENE PROMPT HANDOFF from {prompt_handoff_from} locks architecture "
                f"for setting `{setting_id}` (view={view_key}). "
                f"Do NOT edit a prior image — regenerate the still from characters + scene bible. "
                f"Offscreen (do not draw): {', '.join(offscreen_cids) or 'none'}. "
                f"Costume lock: {costume_lock or cast_who}."
            )
        else:
            cast_ref_line = (
                f"SCENE MASTER COMPOSE for `{setting_id}` (view={view_key}): GENERATE this "
                f"DISTINCT place AND composite solo sheets {', '.join(focus_char_nodes)} for "
                f"ONLY {cast_who} into ONE still. This still's prompt IS the scene bible handoff "
                f"for later same-setting shots. Offscreen (in scene, not drawn): "
                f"{', '.join(offscreen_cids) or 'none'}. "
                f"Do not include cast from other scenes. "
                f"Costume lock: {costume_lock or cast_who}. "
                f"All of {cast_who} must be DISTINCT people — NEVER clone one face."
            )
        cast_ref_line += f" STRATEGY={keyframe_strategy}."
        continuity = shot.get("continuity_lock") if isinstance(shot.get("continuity_lock"), dict) else {}
        if not continuity:
            from jiuwenswarm.server.runtime.designer.continuity import infer_continuity_lock

            continuity = infer_continuity_lock(action)
            shot["continuity_lock"] = continuity
        cont_bits = ", ".join(f"{k}={v}" for k, v in continuity.items()) if continuity else ""
        occ_bits = ""
        if occupancy:
            occ_bits = (
                f"OCCUPANCY: must_appear={occupancy.get('must_appear') or focus_cids}; "
                f"offscreen={occupancy.get('offscreen') or offscreen_cids}; "
                f"must_not_appear={occupancy.get('must_not_appear')}; "
                f"featured={occupancy.get('featured') or featured_cids}. "
                f"doing={occupancy.get('doing') or doing_line}. "
                f"{str(occupancy.get('rule') or '')[:280]} "
            )
        crowd = shot.get("crowd_lock") if isinstance(shot.get("crowd_lock"), dict) else {}
        if not crowd and isinstance(occupancy.get("crowd_lock"), dict):
            crowd = occupancy["crowd_lock"]
        crowd_bits = ""
        if crowd:
            crowd_bits = (
                f"CROWD LOCK: present={crowd.get('present')}; "
                f"density={crowd.get('density')}; {str(crowd.get('rule') or '')[:220]} "
            )
        done_bits = ""
        if already_done:
            done_bits = "ALREADY_DONE (do not restage): " + "; ".join(already_done[:12]) + ". "
        scene_bits = ""
        if shot.get("scene_distinctness"):
            scene_bits = f"SCENE: {str(shot.get('scene_distinctness'))[:220]} "
        elif shot.get("setting_lock") and isinstance(shot.get("setting_lock"), dict):
            scene_bits = f"SCENE LOCK: {str((shot.get('setting_lock') or {}).get('rule') or '')[:220]} "
        detail_bits = (
            "DETAIL REQUIRED: screen L/R for each ON-SCREEN person, gaze target, "
            "motion direction, relative props/landmarks, and which bodies remain from "
            "prior keyframe. Do not draw offscreen or other-scene cast. "
        )
        guide = (
            f"UNIQUE shot {idx} keyframe (setting {setting_id}, view {view_key}). "
            f"{cast_ref_line} {lock_line} {bible_line} {scene_bits}"
            f"Action: {action}. "
            + (f"Per-character doing: {doing_line}. " if doing_line else "")
            + f"Camera: {camera}. "
            + (f"CONTINUITY: {cont_bits}. " if cont_bits else "")
            + occ_bits
            + crowd_bits
            + done_bits
            + detail_bits
            + "ANTI-CLONE: one instance per named person. "
            + "Must look different from other shots in pose/action/view, not in identity or architecture. "
            "Be fast, one still only."
        )
        identity_refs = {
            "character_ids": focus_cids,
            "character_node_ids": list(focus_char_nodes),
            "cast_names": list(focus_names),
            "costume_lock": costume_lock,
            "scene_node_id": None,
            "master_scene_node_id": None,
            "scene_master_frame_id": scene_master_id or (frame_id if is_scene_master else None),
            "is_scene_master": is_scene_master,
            "prior_keyframe_node_id": None,
            "scene_prompt_handoff_from": prompt_handoff_from,
            "keyframe_strategy": keyframe_strategy,
            "setting_id": setting_id,
            "view_key": view_key,
            "spatial_lock": shot_spatial,
            "occupancy": occupancy or None,
            "crowd_lock": crowd or None,
            "skip_scene_plate": True,
            "on_screen": list(focus_cids),
            "offscreen": list(shot.get("offscreen") or []),
            "cast_actions": cast_actions or None,
            "setting_lock": shot.get("setting_lock")
            if isinstance(shot.get("setting_lock"), dict)
            else None,
            "scene_distinctness": shot.get("scene_distinctness"),
            "scene_continuity_mode": "compose_solos_shared_scene_prompt",
            "scene_bible": scene_bible or None,
        }
        scene_n = setting_num.get(setting_id, 1)
        frame_label = f"scene {scene_n}: keyframe {idx}"
        if focus_names:
            frame_label += f" · {focus_names[0]}"
        if view_key:
            frame_label += f" · {view_key}"
        nodes.append(
            {
                "id": frame_id,
                "type": NODE_TYPE_IMAGE,
                "label": frame_label,
                "config": {
                    "role": NODE_ROLE_FRAME,
                    "shot_index": idx,
                    "shot_title": shot.get("title"),
                    "shot_action": action,
                    "camera": camera,
                    "setting_id": setting_id,
                    "view_key": view_key,
                    "scene_bible": scene_bible or None,
                    "character_ids": focus_cids,
                    "featured_cast_ids": featured_cids,
                    "character_node_ids": focus_char_nodes,
                    "cast_names": focus_names,
                    "identity_refs": identity_refs,
                    "costume_lock": costume_lock,
                    "prior_keyframe_node_id": None,
                    "scene_prompt_handoff_from": prompt_handoff_from,
                    "scene_master_frame_id": scene_master_id
                    or (frame_id if is_scene_master else None),
                    "is_scene_master": is_scene_master,
                    "keyframe_strategy": keyframe_strategy,
                    "continuity_lock": continuity or None,
                    "occupancy": occupancy or None,
                    "crowd_lock": crowd or None,
                    "already_done": already_done or None,
                    "spatial_lock": shot_spatial,
                    "master_scene_node_id": None,
                    "skip_scene_plate": True,
                    "generate": {"prompt": guide},
                    "image_size": _IMAGE_SIZE,
                    "max_image_calls": 1,
                    "inputs": frame_inputs,
                    "optimize_for": mode,
                    "agent_name": frame_label,
                    "kind": "agent",
                    "skill_id": "frame",
                    "tools": ["call_image_model", "read_upstream"],
                    "delegate": ("agent" if ai_mode else "handler"),
                    "supervisor_task": (
                        (
                            f"Compose SCENE MASTER for setting {setting_id} from solo sheets "
                            f"{focus_char_nodes}. Author detailed scene bible (objects/lighting/"
                            f"crowd/views). Later same-setting shots will reuse this prompt."
                        )
                        if is_scene_master
                        else (
                            f"Compose from solos using SCENE PROMPT HANDOFF from "
                            f"{prompt_handoff_from} for setting {setting_id} (view={view_key}). "
                            "Keep architecture; update on_screen + cast_actions only."
                        )
                    ),
                },
                "layout": {"x": 1020, "y": float(y), "width": 240, "height": 140},
            }
        )
        timeline = str(shot.get("timeline") or "").strip() or f"{(idx-1)*5:.1f}-{idx*5:.1f}s"
        speech_line = str(shot.get("speech_line") or shot.get("dialogue") or "").strip()
        clip_cfg: dict[str, Any] = {
            "role": NODE_ROLE_CLIP,
            "shot_index": idx,
            "shot_title": shot.get("title"),
            "shot_action": action,
            "camera": camera,
            "setting_id": setting_id,
            "character_ids": focus_cids,
            "character_node_ids": focus_char_nodes,
            "cast_names": focus_names,
            "identity_refs": identity_refs,
            "costume_lock": costume_lock,
            "continuity_lock": continuity or None,
            "occupancy": occupancy or None,
            "crowd_lock": crowd or None,
            "already_done": already_done or None,
            "speech_line": speech_line,
            "spatial_lock": shot_spatial,
            "master_scene_node_id": None,
            "scene_master_frame_id": scene_master_id
            or (frame_id if is_scene_master else None),
            "is_scene_master_keyframe": is_scene_master,
            "allow_still_clip_fallback": False,
            "generate": {
                "prompt": (
                    f"Film shot {idx} only ({timeline}). Setting {setting_id}. "
                    f"Camera {camera}. Action: {action}. "
                    f"Cast on screen: {cast_who}. "
                    + (f"All of {cast_who} must be visible and acting. " if multi else "")
                    + f"Animate ONLY keyframe {frame_id} as first frame — do NOT re-attach solo "
                    + "sheets (prevents cloning). "
                    + "ANTI-CLONE: one body per person. "
                    + f"Costume lock: {costume_lock}. "
                    + f"{lock_line} "
                    + (f"CONTINUITY: {cont_bits}. " if cont_bits else "")
                    + occ_bits
                    + crowd_bits
                    + done_bits
                    + (f"Speech this beat: {speech_line}. " if speech_line else "")
                    + "DETAIL: motion direction, gaze targets, relative L/R from prior beat. "
                    + "Real I2V motion required — never still freezes. "
                    + "Distinct action from other clips; same faces/costumes/architecture/crowd."
                )
            },
            "max_video_calls": 1,
            "inputs": clip_inputs,
            "optimize_for": mode,
            "agent_name": f"scene {setting_num.get(setting_id, 1)}: clip {idx}",
            "kind": "agent",
            "skill_id": "clip",
            "tools": ["call_video_model", "read_upstream"],
            "delegate": ("agent" if ai_mode else "handler"),
            "supervisor_task": (
                f"I2V from keyframe {frame_id}; keep identity ({cast_who}), scene_bible, "
                "crowd_lock, and prior-clip handoff. Real video only. Do not redo already_done."
            ),
        }
        if prompt_handoff_from:
            clip_cfg["continuity_frame_node_id"] = prompt_handoff_from
        # Temporal handoff: next clip will read this node's Wan prompt via clip_prompt_handoff.
        prev_clip_id = prev_clip_by_setting.get(setting_id) or prev_clip_global
        if prev_clip_id:
            clip_cfg["continuity_clip_node_id"] = prev_clip_id
        nodes.append(
            {
                "id": clip_id,
                "type": NODE_TYPE_VIDEO,
                "label": f"scene {setting_num.get(setting_id, 1)}: clip {idx}",
                "config": clip_cfg,
                "layout": {"x": 1320, "y": float(y), "width": 240, "height": 140},
            }
        )
        for src in frame_inputs:
            edges.append(_edge(f"e_{src}_{frame_id}", src, frame_id))
        for src in clip_inputs:
            edges.append(_edge(f"e_{src}_{clip_id}", src, clip_id))
        if is_scene_master:
            scene_master_by_setting[setting_id] = frame_id
        prev_frame_by_setting[setting_id] = frame_id
        prev_clip_by_setting[setting_id] = clip_id
        prev_clip_global = clip_id

    # Post-pass: force same-setting prompt-handoff wires (master → later compose KFs).
    frame_nodes_ordered = sorted(
        [
            n
            for n in nodes
            if isinstance(n, dict)
            and str(n.get("id") or "").startswith("n_frame_")
        ],
        key=lambda n: int((n.get("config") or {}).get("shot_index") or 0),
    )
    edge_pairs = {
        (str(e.get("source") or ""), str(e.get("target") or ""))
        for e in edges
        if isinstance(e, dict)
    }
    repair_prev: dict[str, str] = {}
    repair_master: dict[str, str] = dict(scene_master_by_setting)
    for node in frame_nodes_ordered:
        cfg = dict(node.get("config") or {})
        nid = str(node.get("id") or "")
        sid = str(cfg.get("setting_id") or "set_1").strip() or "set_1"
        prior = repair_prev.get(sid)
        master = repair_master.get(sid)
        if prior:
            cfg["keyframe_strategy"] = "compose_from_solo_refs"
            cfg["is_scene_master"] = False
            cfg.pop("prior_keyframe_node_id", None)
            cfg["scene_prompt_handoff_from"] = master or prior
            cfg["scene_master_frame_id"] = master or prior
            inputs = list(cfg.get("inputs") or [])
            dep = master or prior
            if dep and dep not in inputs and dep != nid:
                inputs.append(dep)
            if dep and dep != nid and (dep, nid) not in edge_pairs:
                edges.append(_edge(f"e_{dep}_{nid}", dep, nid))
                edge_pairs.add((dep, nid))
            cfg["inputs"] = inputs
            irefs = dict(cfg.get("identity_refs") or {})
            irefs["keyframe_strategy"] = "compose_from_solo_refs"
            irefs["is_scene_master"] = False
            irefs["prior_keyframe_node_id"] = None
            irefs["scene_prompt_handoff_from"] = master or prior
            irefs["scene_master_frame_id"] = master or prior
            irefs["skip_scene_plate"] = True
            irefs["scene_continuity_mode"] = "compose_solos_shared_scene_prompt"
            cfg["identity_refs"] = irefs
        else:
            cfg["keyframe_strategy"] = "compose_from_solo_refs"
            cfg["is_scene_master"] = True
            cfg["scene_master_frame_id"] = nid
            cfg.pop("prior_keyframe_node_id", None)
            cfg.pop("scene_prompt_handoff_from", None)
            irefs = dict(cfg.get("identity_refs") or {})
            irefs["keyframe_strategy"] = "compose_from_solo_refs"
            irefs["is_scene_master"] = True
            irefs["scene_master_frame_id"] = nid
            irefs["prior_keyframe_node_id"] = None
            irefs["scene_prompt_handoff_from"] = None
            irefs["skip_scene_plate"] = True
            irefs["scene_continuity_mode"] = "compose_solos_shared_scene_prompt"
            cfg["identity_refs"] = irefs
            repair_master[sid] = nid
        node["config"] = cfg
        repair_prev[sid] = nid
    scene_master_by_setting = repair_master

    audio_ids: list[str] = []
    film_sec = max(6, min(30, len(shots) * 5))
    # Audio routing: separate nodes only when backends exist; else fold into clips.
    from jiuwenswarm.server.runtime.designer.capabilities import detect_audio_backends

    backends = detect_audio_backends()
    can_speech = bool(backends.get("can_speech"))
    can_music = bool(backends.get("can_music"))
    clip_embedded = False
    if audio.get("policy") != "silent":
        want_speech = bool(audio.get("include_speech"))
        want_music = bool(
            audio.get("include_music")
            or audio.get("policy") in {"optional_music", "music", "speech_and_music"}
        )
        if want_speech and not can_speech:
            clip_embedded = True
            want_speech = False
        if want_music and not can_music:
            clip_embedded = True
            want_music = False
        if want_speech:
            audio_ids.append("n_speech")
            nodes.append(
                {
                    "id": "n_speech",
                    "type": NODE_TYPE_AUDIO,
                    "label": "Speech / TTS",
                    "config": {
                        "role": "speech",
                        "prompt": prompt_text,
                        "inputs": ["n_brief", "n_storyboard"],
                        "optimize_for": mode,
                        "agent_name": "Speech Agent",
                        "kind": "agent",
                        "skill_id": "speech_tts",
                        "tools": ["call_speech_model", "read_upstream", "call_model"],
                        "delegate": ("agent" if ai_mode else "handler"),
                        "film_duration_sec": film_sec,
                    },
                    "layout": {"x": 1320, "y": float(40 + len(shots) * 160), "width": 220, "height": 120},
                }
            )
            edges.append(_edge("e_sb_speech", "n_storyboard", "n_speech"))
            edges.append(_edge("e_brief_speech", "n_brief", "n_speech"))
        if want_music:
            audio_ids.append("n_music")
            nodes.append(
                {
                    "id": "n_music",
                    "type": NODE_TYPE_AUDIO,
                    "label": "Music / BGM",
                    "config": {
                        "role": "music",
                        "prompt": prompt_text,
                        "inputs": ["n_brief", "n_storyboard"],
                        "optimize_for": mode,
                        "agent_name": "Music Agent",
                        "kind": "agent",
                        "skill_id": "audio_bed",
                        "tools": ["call_music_model", "read_upstream", "call_model"],
                        "delegate": ("agent" if ai_mode else "handler"),
                        "film_duration_sec": film_sec,
                    },
                    "layout": {
                        "x": 1320,
                        "y": float(40 + (len(shots) + 1) * 160),
                        "width": 220,
                        "height": 120,
                    },
                }
            )
            edges.append(_edge("e_sb_music", "n_storyboard", "n_music"))
            edges.append(_edge("e_brief_music", "n_brief", "n_music"))
        if clip_embedded:
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                cfg = n.get("config") if isinstance(n.get("config"), dict) else {}
                if str(cfg.get("role") or "") != NODE_ROLE_CLIP:
                    continue
                gen = dict(cfg.get("generate") or {})
                prompt_c = str(gen.get("prompt") or "")
                speech_bit = str(cfg.get("speech_line") or "").strip()
                extra = (
                    " AUDIO (clip-embedded — no separate TTS/BGM backend): include natural "
                    "diegetic speech timing and light underscoring mood in this clip when the "
                    "storyboard calls for sound."
                )
                if speech_bit:
                    extra += f" Spoken line this beat: {speech_bit}."
                gen["prompt"] = (prompt_c + extra).strip()
                cfg["generate"] = gen
                cfg["clip_embedded_audio"] = True
                n["config"] = cfg

    compose_inputs = [*clip_ids, *audio_ids]
    nodes.append(
        {
            "id": "n_compose",
            "type": NODE_TYPE_VIDEO,
            "label": "Film",
            "config": {
                "role": NODE_ROLE_COMPOSE,
                "inputs": compose_inputs,
                "optimize_for": mode,
                "agent_name": "Compose / FFmpeg Agent",
                "kind": "agent",
                "skill_id": "compose",
                "audio_policy": audio.get("policy"),
                "tools": ["ffmpeg_compose", "mix_audio", "read_upstream", "call_model"],
                "delegate": ("agent" if ai_mode else "handler"),
                "supervisor_task": (
                    "Concatenate shot clips in storyboard order; mux speech/music when present. "
                    "Output a real non-empty .mp4 only — never markdown. Use ffmpeg_compose tool."
                ),
            },
            "layout": {"x": 1620, "y": 180, "width": 260, "height": 150},
        }
    )
    for src in compose_inputs:
        edges.append(_edge(f"e_{src}_compose", src, "n_compose"))

    graph: DesignerExecutionGraph = {
        "schema_version": SCHEMA_VERSION,
        "graph_id": graph_id,
        "project_id": project_id,
        "title": graph_title or "Designer Project",
        "description": prompt_text,
        "source": GRAPH_SOURCE_PROMPT,
        "nodes": nodes,  # type: ignore[typeddict-item]
        "edges": edges,  # type: ignore[typeddict-item]
        "metadata": {
            "bootstrap": "designer.graph.smart_video.quality.v5",
            "scenario": "video",
            "optimize_for": mode,
            "agentic": True,
            "skill_guided": True,
            "script_analysis": analysis,
            "audio_intent": audio,
            "audio_routing": {
                "clip_embedded": bool(clip_embedded),
                "can_speech": can_speech,
                "can_music": can_music,
                "speech_nodes": "n_speech" in audio_ids,
                "music_nodes": "n_music" in audio_ids,
            },
            "skip_scene_plate": True,
            "scene_continuity_mode": "compose_solos_shared_scene_prompt",
            "scene_masters": dict(scene_master_by_setting),
            "scene_locks": dict(scene_locks_meta),
            "lean_pipeline": False,
            # Flexible: Supervisor may expand frame/clip nodes from storyboard.
            "freeze_shot_topology": False,
            "supervisor_owns_graph": True,
            "ai_agent_pipeline": bool(ai_mode),
            "all_nodes_agents": bool(ai_mode),
            "allow_still_clip_fallback": False,
            "combined_cast": False,
            "cast_layout": cast_layout,
            "spatial_lock": spatial_lock,
            "spatial_lock_by_setting": spatial_by_setting,
            "consistency_plan": {
                "character_identity": "solo_sheets_only",
                "multi_shot_compose": "compose_solos_into_generated_setting",
                "scene_spatial": "shared_scene_bible_prompt_handoff",
                "sequential_keyframe": "prompt_handoff_same_setting_id_only",
                "costume_lock": True,
                "identity_refs_on_frame_clip": True,
                "solo_gate_before_keyframes": True,
                "per_shot_scene_views": True,
                "hierarchical_views": True,
                "empty_scene_plates": False,
                "prior_prompt_handoff": True,
                "notes": (
                    "Continuity: Brief→Storyboard→Manager→ALL solo sheets→"
                    "first KF per setting_id = SCENE MASTER prompt+bible→"
                    "same-setting compose from solos with prompt handoff + view locks→"
                    "clips→film. No empty plates; no prior-image edit."
                ),
            },
            "max_shots": len(shots),
            "target_shot_count": len(shots),
            "prewritten_brief": not bool(ai_mode),
            "prewritten_storyboard": not bool(ai_mode),
            "image_size": _IMAGE_SIZE,
            "max_image_calls_per_node": 1,
            "orchestration": {
                "supervisor_id": "supervisor",
                "manager_id": "manager",
                "planner": "supervisor_llm" if ai_mode else "heuristic_fallback",
                "flow": (
                    "supervisor_brief→manager_brief→supervisor_storyboard→manager_lock→"
                    "supervisor_graph→manager_prune_reedit→leaf_prompt_gate→dual_raters"
                ),
                "notes": (
                    "Supervisor=Director, Manager=Producer. Heuristics only if LLM unavailable. "
                    "One-pass forward; ratings write-only and apply on Run again."
                ),
            },
        },
        "created_at": now,
        "updated_at": now,
    }
    graph = normalize_execution_graph(graph)
    prune_non_contributing_nodes(graph)
    return attach_skills_metadata(graph, prompt_text)
