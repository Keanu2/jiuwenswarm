# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Build prompt-aware Designer graphs from cast/shot analysis.

Quality layout (default, forward-only):
  Brief → Storyboard
  → Solo cast sheet(s) + canonical Scene master plate
  → Per-shot Scene views (edit/ref from master — spatial lock)
  → Keyframes (compose solos + shot scene + master; optional edit prior KF)
  → Clips (I2V) + optional Speech/Music → Film (ffmpeg assemble)

Cast: always one canonical solo sheet per character. Scene geography is locked
by the master plate; later views must not invent a new building. Manager
prunes any node that cannot reach ``n_compose``.
"""

from __future__ import annotations

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
    utc_now_ms,
)
from jiuwenswarm.server.runtime.designer.skills_loader import attach_skills_metadata

_MAX_LEAN_SHOTS = 5
_MAX_SPLIT_CHARS = 6
_IMAGE_SIZE = "1024x1024"


def apply_runtime_delegate(graph: DesignerExecutionGraph) -> DesignerExecutionGraph:
    """Prefer node agents + tools when an LLM is configured; else role handlers.

    Nodes with ``config.force_handler=True`` stay on the fast handler path
    (music/speech beds when no TTS/music backend is configured). Manager
    capability detection and supervisor ``assign_audio_node_agents`` clear
    ``force_handler`` automatically when backends exist.
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
        if config.get("force_handler"):
            config["delegate"] = CONFIG_DELEGATE_HANDLER
        else:
            config["delegate"] = delegate
        if use_agents and config.get("skip_llm") and not config.get("force_handler"):
            config["skip_llm"] = False
            if config.get("prewritten") and not config.get("draft_prewritten"):
                config["draft_prewritten"] = config.get("prewritten")
    meta = dict(graph.get("metadata") or {})
    meta["ai_agent_pipeline"] = use_agents
    meta["runtime_delegate"] = delegate
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


def prune_non_contributing_nodes(graph: DesignerExecutionGraph) -> list[str]:
    """Remove nodes/edges that cannot reach the final compose (or any sink).

    Forward-only graphs must not keep orphan leaves. Returns pruned node ids.
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
            if str((n.get("config") or {}).get("role") or "") == NODE_ROLE_COMPOSE
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
    pruned = sorted(ids - contributing)
    if not pruned:
        return []
    graph["nodes"] = [n for n in nodes if str(n.get("id")) in contributing]
    graph["edges"] = [
        e
        for e in edges
        if str(e.get("source") or "") in contributing
        and str(e.get("target") or "") in contributing
    ]
    meta = dict(graph.get("metadata") or {})
    notes = list(meta.get("prune_notes") or [])
    notes.extend([f"pruned:{nid}" for nid in pruned])
    meta["prune_notes"] = notes[-40:]
    graph["metadata"] = meta
    return pruned


def _write_brief_markdown(prompt: str, characters: list[dict[str, Any]], scenes: list[dict[str, Any]], audio: dict[str, Any]) -> str:
    cast_lines = []
    for c in characters:
        name = str(c.get("name") or "Lead")
        desc = str(c.get("description") or "").strip()
        cast_lines.append(f"- **{name}:** {desc or 'lock face, hair, body, costume'}")
    setting = str((scenes[0] if scenes else {}).get("name") or "Primary setting")
    setting_desc = str((scenes[0] if scenes else {}).get("description") or "")
    policy = str(audio.get("policy") or "optional_music")
    return (
        f"# Brief\n\n"
        f"**User prompt (verbatim intent):** {prompt.strip()[:600]}\n\n"
        f"**Logline:** {prompt.strip()[:280]}\n\n"
        f"**Cast (solo identity locks — one sheet each, never concatenate):**\n"
        + ("\n".join(cast_lines) or "- Lead")
        + "\n\n"
        f"**Setting:** {setting} — {setting_desc}\n\n"
        f"**Consistency gates:**\n"
        f"- Character: same face/wardrobe every shot unless brief says change\n"
        f"- Scene: shared architecture/lighting across shot views\n"
        f"- Motion / continuity: time-coherent (e.g. after a man stands and leaves, "
        f"later shots must not show him seated again)\n"
        f"- Camera: distinct views per shot covering the prompt beats\n\n"
        f"**Duration:** ~{max(6, min(24, len(characters) * 3 + 4))}s short film\n\n"
        f"**Visual style:** cinematic, coherent lighting, no subtitles\n\n"
        f"**Audio policy:** {policy}\n\n"
        f"**Avoid:** comic grids, watermark text, identity drift, orphan graph nodes\n"
    )


def _write_storyboard_markdown(shots: list[dict[str, Any]], characters: list[dict[str, Any]]) -> str:
    id_to_name = {str(c.get("id")): str(c.get("name") or c.get("id")) for c in characters}
    camera_cycle = (
        "wide / establishing",
        "medium / eye-level",
        "close-up / eye-level",
        "medium / slow pan",
    )
    rows = [
        "## Storyboard\n",
        "| Shot | Timeline | Camera | Move | Character action | Continuity | Comment |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, shot in enumerate(shots, start=1):
        if not isinstance(shot, dict):
            continue
        cids = [str(x) for x in (shot.get("character_ids") or [])]
        names = ", ".join(id_to_name.get(cid, cid) for cid in cids) or "cast"
        action = str(shot.get("action") or shot.get("title") or "")[:140]
        camera = str(shot.get("camera") or "").strip() or camera_cycle[(i - 1) % len(camera_cycle)]
        move = "static"
        if "pan" in camera.lower():
            move = "slow pan"
        elif "close" in camera.lower():
            move = "hold"
        elif i > 1:
            move = "cut"
        lock = shot.get("continuity_lock") if isinstance(shot.get("continuity_lock"), dict) else {}
        if not lock:
            from jiuwenswarm.server.runtime.designer.continuity import infer_continuity_lock

            lock = infer_continuity_lock(action)
            shot["continuity_lock"] = lock
        cont = "; ".join(f"{k}={v}" for k, v in list(lock.items())[:3]) or "hold continuity"
        rows.append(
            "| {shot} | {tl} | {cam} | {move} | {action} | {cont} | {kf} |".format(
                shot=i,
                tl=str(shot.get("timeline") or f"{(i - 1) * 2:.1f}-{i * 2:.1f}s"),
                cam=camera,
                move=move,
                action=f"{names}: {action}"[:140],
                cont=cont[:120],
                kf=str(shot.get("keyframe_prompt") or action)[:180],
            )
        )
    return "\n".join(rows) + "\n"


def _shot_budget(analysis: dict[str, Any], shots: list[dict[str, Any]]) -> int:
    try:
        target = int(analysis.get("target_shot_count") or 0)
    except (TypeError, ValueError):
        target = 0
    if target < 1:
        target = min(len(shots) or 1, _MAX_LEAN_SHOTS)
    return max(1, min(_MAX_LEAN_SHOTS, target, len(shots) or 1))


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
        name = str(ch.get("name") or cid)
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
        name = str(ch.get("name") or "Lead")
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
        trole = str((by_id[t].get("config") or {}).get("role") or "")
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
        str((by_id[i].get("config") or {}).get("role") or "") == NODE_ROLE_COMPOSE
        for i in ids
    )
    fallback = "n_frame_1" if has_frame_1 else (
        "n_compose" if "n_compose" in ids else next(
            (
                i
                for i in sorted(ids)
                if str((by_id[i].get("config") or {}).get("role") or "")
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
        characters = [{"id": "char_1", "name": "Lead", "description": prompt_text[:200]}]
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
    for i, shot in enumerate(shots, start=1):
        shot["shot_index"] = i
    _ensure_characters_referenced(characters, shots)

    # Quality path: solo identity sheets only — keyframes compose multi-person.
    prefer_combined = False
    cast_sheets, shot_cast_nodes, cast_layout = _plan_cast_sheets(
        characters, shots, prefer_combined=prefer_combined
    )
    # Drop any combined sheets that slipped through (no concatenation).
    cast_sheets = [s for s in cast_sheets if not s.get("combined_cast")]
    brief_md = _write_brief_markdown(prompt_text, characters, scenes, audio)
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
        label = str(sheet.get("label") or "Cast")
        prompt = (
            f"{speed}\nCANONICAL character identity postcard for "
            f"{names[0] if names else label} — lock face, hair, body type, and costume. "
            f"Solo sheet only (never group/concat portraits). "
            f"Do not invent alternate wardrobe. {sheet.get('prompt_body')}. "
            f"Story context: {prompt_text[:180]}"
        )
        agent = f"{(names[0] if names else 'Character')} Agent"
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

    # Canonical sanctuary plate, then per-shot views DERIVED from it (spatial lock).
    scene_base = scenes[0] if scenes else {"id": "scene_1", "name": "Setting", "description": ""}
    spatial_lock = {
        "setting": str(scene_base.get("name") or "Primary setting"),
        "architecture": str(scene_base.get("description") or "keep one coherent interior"),
        "static_rule": (
            "STATIC OBJECTS LOCKED: pulpit/altar/windows/aisle/pews/floor/light direction "
            "must match the master plate in every shot view — only camera/framing may change."
        ),
        "crowd_rule": (
            "Environment plates: empty pews (no faces). Keyframes/clips: SAME locked congregation "
            "silhouette in the pews across every shot (same coats/positions); do not invent a new "
            "crowd per shot. Featured cast are distinct people — never clone the preacher."
        ),
    }
    # Prefer analysis-provided lock if supervisor already stamped one.
    prior_lock = analysis.get("spatial_lock") if isinstance(analysis.get("spatial_lock"), dict) else {}
    for k, v in prior_lock.items():
        if str(v).strip():
            spatial_lock[str(k)] = str(v).strip()[:400]

    lock_line = (
        f"SPATIAL LOCK: setting={spatial_lock.get('setting')}; "
        f"{spatial_lock.get('architecture')}; {spatial_lock.get('static_rule')} "
        f"{spatial_lock.get('crowd_rule')}"
    )
    master_id = "n_scene"
    nodes.append(
        {
            "id": master_id,
            "type": NODE_TYPE_IMAGE,
            "label": f"Scene master — {scene_base.get('name') or 'Setting'}",
            "config": {
                "role": NODE_ROLE_SCENE,
                "prompt": (
                    f"{speed}\nCANONICAL environment plate (no people): {scene_base.get('name')} — "
                    f"{scene_base.get('description')}. Establishing wide of the full space. "
                    f"Empty pews only (no crowd faces). Freeze architecture for all later views. "
                    f"{lock_line} Story: {prompt_text[:160]}"
                ),
                "scene_id": scene_base.get("id"),
                "shot_index": 0,
                "scene_strategy": "master_plate",
                "spatial_lock": spatial_lock,
                "image_size": _IMAGE_SIZE,
                "max_image_calls": 1,
                "inputs": ["n_brief", "n_storyboard"],
                "optimize_for": mode,
                "agent_name": "Scene Master Agent",
                "kind": "agent",
                "skill_id": "scene",
                "tools": ["call_image_model", "read_upstream", "call_model"],
                "delegate": ("agent" if ai_mode else "handler"),
                "supervisor_task": (
                    "Generate the single canonical environment plate. Lock pulpit side, aisle, "
                    "windows, floor, and lighting. No people."
                ),
            },
            "layout": {"x": 680, "y": float(40 + len(cast_sheets) * 160), "width": 260, "height": 150},
        }
    )
    edges.append(_edge("e_sb_scene_master", "n_storyboard", master_id))
    edges.append(_edge("e_brief_scene_master", "n_brief", master_id))

    scene_node_by_shot: dict[int, str] = {}
    for shot in shots:
        idx = int(shot.get("shot_index") or 0)
        if idx < 1:
            continue
        scene_id = f"n_scene_{idx}"
        scene_node_by_shot[idx] = scene_id
        camera = str(shot.get("camera") or "medium / eye-level")
        action = str(shot.get("action") or shot.get("keyframe_prompt") or "")[:220]
        nodes.append(
            {
                "id": scene_id,
                "type": NODE_TYPE_IMAGE,
                "label": f"Scene view {idx}",
                "config": {
                    "role": NODE_ROLE_SCENE,
                    "prompt": (
                        f"{speed}\nDERIVE this camera view from the master plate (edit/ref — "
                        f"do NOT invent a new building). Environment only, no people. "
                        f"Camera: {camera}. Beat context: {action}. "
                        f"{lock_line} Same pulpit location, aisle, windows, materials, light."
                    ),
                    "scene_id": scene_base.get("id"),
                    "shot_index": idx,
                    "camera": camera,
                    "scene_strategy": "edit_master_view",
                    "master_scene_node_id": master_id,
                    "spatial_lock": spatial_lock,
                    "image_size": _IMAGE_SIZE,
                    "max_image_calls": 1,
                    "inputs": ["n_brief", "n_storyboard", master_id],
                    "optimize_for": mode,
                    "agent_name": f"Scene-{idx} Agent",
                    "kind": "agent",
                    "skill_id": "scene",
                    "tools": ["call_image_model", "read_upstream", "call_model"],
                    "delegate": ("agent" if ai_mode else "handler"),
                    "supervisor_task": (
                        f"Edit/ref-guide from {master_id} only: reframe to camera '{camera}'. "
                        "Keep every static object faithful to the master."
                    ),
                },
                "layout": {
                    "x": 680,
                    "y": float(40 + (len(cast_sheets) + idx) * 160),
                    "width": 260,
                    "height": 150,
                },
            }
        )
        edges.append(_edge(f"e_sb_{scene_id}", "n_storyboard", scene_id))
        edges.append(_edge(f"e_brief_{scene_id}", "n_brief", scene_id))
        edges.append(_edge(f"e_master_{scene_id}", master_id, scene_id, label="spatial_ref"))

    if not scene_node_by_shot:
        scene_node_by_shot[1] = master_id

    clip_ids: list[str] = []
    prev_frame_id: str | None = None
    prev_camera: str = ""
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
        focus_cids = [str(x) for x in (shot.get("character_ids") or []) if str(x)]
        # If this beat keeps someone on screen while another acts (e.g. preacher still
        # speaking while a man leaves), pull every named cast id mentioned in the action.
        action_l = str(shot.get("action") or shot.get("keyframe_prompt") or "").lower()
        if any(k in action_l for k in ("still speak", "still preaching", "at the pulpit", "leaves", "leaving")):
            for c in characters:
                cid = str(c.get("id") or "")
                name = str(c.get("name") or "").lower()
                if not cid:
                    continue
                tokens = [t for t in name.replace("-", " ").split() if len(t) > 2]
                if any(t in action_l for t in tokens) or (
                    any(k in name for k in ("preach", "pastor", "pulpit"))
                    and any(k in action_l for k in ("preach", "pulpit", "speak", "bible"))
                ) or (
                    any(k in name for k in ("leav", "depart", "walk"))
                    and any(k in action_l for k in ("leav", "stand", "walk", "gets up"))
                ):
                    if cid not in focus_cids:
                        focus_cids.append(cid)
            shot["character_ids"] = focus_cids
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
        multi = len(focus_names) > 1
        camera = str(shot.get("camera") or "").strip() or camera_cycle[(idx - 1) % len(camera_cycle)]
        if (
            idx > 1
            and prev_camera
            and camera.lower() == prev_camera.lower()
            and camera.lower() in {"medium / eye-level", "medium", "eye-level"}
        ):
            camera = camera_cycle[(idx - 1) % len(camera_cycle)]
        shot["camera"] = camera
        action = str(shot.get("action") or shot.get("keyframe_prompt") or "")[:300]
        costume_lock = _costume_lock_for_ids(characters, focus_cids)
        use_prior_edit = bool(
            prev_frame_id and _cameras_compatible(prev_camera, camera)
        )
        if prev_frame_id and not use_prior_edit:
            hard_cut = any(
                k in camera.lower() for k in ("close-up", "close up", "wide", "establishing")
            ) and any(
                k in (prev_camera or "").lower()
                for k in ("close-up", "close up", "wide", "establishing")
            ) and not _cameras_compatible(prev_camera, camera)
            if not hard_cut and focus_cids:
                fam_a = "close" if "close" in (prev_camera or "").lower() else (
                    "wide" if "wide" in (prev_camera or "").lower() else "medium"
                )
                fam_b = "close" if "close" in camera.lower() else (
                    "wide" if "wide" in camera.lower() else "medium"
                )
                if fam_a == fam_b:
                    use_prior_edit = True
        keyframe_strategy = (
            "edit_prior_keyframe" if use_prior_edit else "compose_from_solo_refs"
        )
        shot_scene_id = scene_node_by_shot.get(idx) or master_id
        frame_inputs = [
            *focus_char_nodes,
            master_id,
            shot_scene_id,
            "n_storyboard",
        ]
        # Dedupe if shot view is master fallback
        frame_inputs = list(dict.fromkeys(frame_inputs))
        if use_prior_edit and prev_frame_id:
            frame_inputs.append(prev_frame_id)
        # Clip depends on keyframe (+ storyboard for continuity text); identity via KF.
        clip_inputs = ["n_storyboard", frame_id, master_id]
        if shot_scene_id != master_id:
            clip_inputs.append(shot_scene_id)
        if prev_frame_id:
            clip_inputs.append(prev_frame_id)
        clip_inputs = list(dict.fromkeys(clip_inputs))
        y = 40 + (idx - 1) * 160
        if multi:
            cast_ref_line = (
                f"IDENTITY: compose individual solo sheets for {cast_who} "
                f"(node refs {', '.join(focus_char_nodes)}) into this keyframe with scene "
                f"view {shot_scene_id} (master {master_id}). Do NOT invent new costumes or "
                f"relocate static architecture. "
                f"Costume lock: {costume_lock or cast_who}. "
                f"The keyframe MUST show ALL of them as DISTINCT people with DIFFERENT faces: "
                f"{cast_who}. NEVER clone one face onto two bodies."
            )
        else:
            cast_ref_line = (
                f"IDENTITY: use solo cast sheet(s) {', '.join(focus_char_nodes)} for {cast_who} "
                f"with scene view {shot_scene_id} (master {master_id}). "
                f"Costume lock: {costume_lock or cast_who}. Do not change wardrobe or move the pulpit."
            )
        if use_prior_edit and prev_frame_id:
            cast_ref_line += (
                f" STRATEGY={keyframe_strategy}: edit prior keyframe {prev_frame_id} "
                f"for this beat (same/compatible camera); keep faces, costumes, and spatial_lock."
            )
        else:
            cast_ref_line += (
                f" STRATEGY={keyframe_strategy}: compose solo cast into the locked scene view "
                f"(derived from master plate)."
            )
        continuity = shot.get("continuity_lock") if isinstance(shot.get("continuity_lock"), dict) else {}
        cont_bits = ", ".join(f"{k}={v}" for k, v in continuity.items()) if continuity else ""
        guide = (
            f"UNIQUE shot {idx} keyframe. {cast_ref_line} "
            f"{lock_line} "
            f"Action (everyone listed must participate as described): {action}. "
            f"Camera: {camera}. "
            + (f"CONTINUITY: {cont_bits}. " if cont_bits else "")
            + "ANTI-CLONE: one instance per named person. "
            + "CROWD LOCK: same seated congregation across shots when the brief needs a crowd. "
            + "Must look different from other shots in pose/action, not in identity, architecture, or crowd layout. "
            "Be fast, one still only."
        )
        identity_refs = {
            "character_ids": focus_cids,
            "character_node_ids": list(focus_char_nodes),
            "cast_names": list(focus_names),
            "costume_lock": costume_lock,
            "scene_node_id": shot_scene_id,
            "master_scene_node_id": master_id,
            "prior_keyframe_node_id": prev_frame_id if use_prior_edit else None,
            "keyframe_strategy": keyframe_strategy,
            "spatial_lock": spatial_lock,
        }
        nodes.append(
            {
                "id": frame_id,
                "type": NODE_TYPE_IMAGE,
                "label": f"Keyframe {idx}",
                "config": {
                    "role": NODE_ROLE_FRAME,
                    "shot_index": idx,
                    "shot_title": shot.get("title"),
                    "shot_action": action,
                    "camera": camera,
                    "character_ids": focus_cids,
                    "character_node_ids": focus_char_nodes,
                    "cast_names": focus_names,
                    "identity_refs": identity_refs,
                    "costume_lock": costume_lock,
                    "prior_keyframe_node_id": prev_frame_id if use_prior_edit else None,
                    "keyframe_strategy": keyframe_strategy,
                    "continuity_lock": continuity or None,
                    "spatial_lock": spatial_lock,
                    "master_scene_node_id": master_id,
                    "generate": {"prompt": guide},
                    "image_size": _IMAGE_SIZE,
                    "max_image_calls": 1,
                    "inputs": frame_inputs,
                    "optimize_for": mode,
                    "agent_name": f"Keyframe-{idx} Agent",
                    "kind": "agent",
                    "skill_id": "frame",
                    "tools": ["call_image_model", "read_upstream", "call_model"],
                    "delegate": ("agent" if ai_mode else "handler"),
                    "supervisor_task": (
                        f"Compose keyframe {idx} from solo sheets {focus_char_nodes} "
                        f"into locked scene {shot_scene_id} (master {master_id}). "
                        f"Strategy={keyframe_strategy}. Obey spatial_lock."
                    ),
                },
                "layout": {"x": 1020, "y": float(y), "width": 240, "height": 140},
            }
        )
        timeline = str(shot.get("timeline") or "").strip() or f"{(idx-1)*5:.1f}-{idx*5:.1f}s"
        clip_cfg: dict[str, Any] = {
            "role": NODE_ROLE_CLIP,
            "shot_index": idx,
            "shot_title": shot.get("title"),
            "shot_action": action,
            "camera": camera,
            "character_ids": focus_cids,
            "character_node_ids": focus_char_nodes,
            "cast_names": focus_names,
            "identity_refs": identity_refs,
            "costume_lock": costume_lock,
            "continuity_lock": continuity or None,
            "spatial_lock": spatial_lock,
            "master_scene_node_id": master_id,
            "allow_still_clip_fallback": False,
            "generate": {
                "prompt": (
                    f"Film shot {idx} only ({timeline}). Camera {camera}. Action: {action}. "
                    f"Cast on screen: {cast_who}. "
                    + (f"All of {cast_who} must be visible and acting. " if multi else "")
                    + f"Animate ONLY keyframe {frame_id} as first frame — do NOT re-attach solo "
                    + "sheets (prevents cloning). "
                    + "ANTI-CLONE: one body per person; never show the same preacher both walking "
                    + "and still at the pulpit. Keep the same congregation as the keyframe. "
                    + f"Costume lock: {costume_lock}. "
                    + f"{lock_line} "
                    + (f"CONTINUITY: {cont_bits}. " if cont_bits else "")
                    + "Real I2V motion required — never still freezes. "
                    + "Do not relocate pulpit/aisle/windows. "
                    + "Distinct action from other clips; same faces/costumes/architecture/crowd."
                )
            },
            "max_video_calls": 1,
            "inputs": clip_inputs,
            "optimize_for": mode,
            "agent_name": f"Clip-{idx} Agent",
            "kind": "agent",
            "skill_id": "clip",
            "tools": ["call_video_model", "read_upstream", "call_model"],
            "delegate": ("agent" if ai_mode else "handler"),
            "supervisor_task": (
                f"I2V from keyframe {frame_id}; keep identity ({cast_who}) and spatial_lock. "
                "Real video only."
            ),
        }
        if prev_frame_id:
            clip_cfg["continuity_frame_node_id"] = prev_frame_id
        nodes.append(
            {
                "id": clip_id,
                "type": NODE_TYPE_VIDEO,
                "label": f"Clip {idx}",
                "config": clip_cfg,
                "layout": {"x": 1320, "y": float(y), "width": 240, "height": 140},
            }
        )
        for src in frame_inputs:
            edges.append(_edge(f"e_{src}_{frame_id}", src, frame_id))
        for src in clip_inputs:
            edges.append(_edge(f"e_{src}_{clip_id}", src, clip_id))
        prev_frame_id = frame_id
        prev_camera = camera

    audio_ids: list[str] = []
    film_sec = max(6, min(30, len(shots) * 5))
    if audio.get("policy") != "silent":
        if audio.get("include_speech"):
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
                        "modality": "audio",
                        "delegate": "handler",
                        "force_handler": True,
                        "duration_sec": film_sec,
                        "max_audio_sec": film_sec,
                        "tools": ["call_speech_model", "read_upstream", "call_model"],
                    },
                    "layout": {"x": 1320, "y": float(40 + len(shots) * 160), "width": 240, "height": 120},
                }
            )
            edges.append(_edge("e_brief_speech", "n_brief", "n_speech"))
            edges.append(_edge("e_sb_speech", "n_storyboard", "n_speech"))
        if audio.get("include_music") or audio.get("policy") in {
            "optional_music",
            "music",
            "speech_and_music",
        }:
            if "n_music" not in audio_ids:
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
                            "modality": "audio",
                            "delegate": "handler",
                            "force_handler": True,
                            "max_audio_sec": film_sec,
                            "duration_sec": film_sec,
                            "tools": ["call_music_model", "read_upstream", "call_model"],
                        },
                        "layout": {
                            "x": 1320,
                            "y": float(40 + (len(shots) + 1) * 160),
                            "width": 240,
                            "height": 120,
                        },
                    }
                )
                edges.append(_edge("e_brief_music", "n_brief", "n_music"))
                edges.append(_edge("e_sb_music", "n_storyboard", "n_music"))

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
                "tools": ["ffmpeg_compose", "mix_audio", "read_upstream"],
                "delegate": ("agent" if ai_mode else "handler"),
                "force_handler": True,  # always materialize real mp4 via ffmpeg handler
                "supervisor_task": (
                    "Concatenate shot clips in storyboard order; mux speech/music when present. "
                    "Output a real non-empty .mp4 only — never markdown."
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
            "bootstrap": "designer.graph.smart_video.quality.v4",
            "scenario": "video",
            "optimize_for": mode,
            "agentic": True,
            "skill_guided": True,
            "script_analysis": analysis,
            "audio_intent": audio,
            "lean_pipeline": True,
            # Freeze shot topology at Play time — do not expand_shot_nodes mid-run
            # (that rewires ALL cast/scenes into every frame and leaves orphans).
            "freeze_shot_topology": True,
            "ai_agent_pipeline": bool(ai_mode),
            "allow_still_clip_fallback": False,
            "combined_cast": False,
            "cast_layout": cast_layout,
            "spatial_lock": spatial_lock,
            "consistency_plan": {
                "character_identity": "solo_sheets_only",
                "multi_shot_compose": "compose_solos_plus_shot_scene",
                "scene_spatial": "master_plate_then_edit_views",
                "sequential_keyframe": "edit_prior_when_camera_compatible",
                "costume_lock": True,
                "identity_refs_on_frame_clip": True,
                "per_shot_scene_views": True,
                "notes": (
                    "Brief→Storyboard→solo cast + master scene→derived shot scenes→"
                    "keyframes→clips→film. Shot scenes must ref-edit the master plate. "
                    "Manager prunes nodes that cannot reach compose."
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
                    "supervisor_brief→manager_brief→storyboard_leaf→manager_storyboard→"
                    "supervisor_graph→manager_prune→forward_ready_queue→dual_raters"
                ),
                "notes": (
                    "AI-first when models are configured. Heuristics only if LLM unavailable. "
                    "One-pass forward only; ratings write-only and apply on Run again."
                ),
            },
        },
        "created_at": now,
        "updated_at": now,
    }
    graph = normalize_execution_graph(graph)
    prune_non_contributing_nodes(graph)
    return attach_skills_metadata(graph, prompt_text)
