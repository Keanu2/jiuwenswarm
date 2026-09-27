# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Build prompt-aware Designer graphs from cast/shot analysis.

Quality layout (default, forward-only):
  Brief → Storyboard → solo cast sheets
  → Scene specs per setting_id (room only, no people)
  → Clips-as-shots: every clip is Wan R2V from on-screen solos plus that empty plate
  → optional Speech/Music → Film (ffmpeg assemble)

``scene_continuity_mode = scene_card_plus_clip_shots``. Solo sheets are identity
locks. Manager prunes any node that cannot reach ``n_compose`` and re-edits
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
    from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
        film_duration_for_graph,
    )

    stamped = film_duration_for_graph(prompt, analysis)
    if stamped >= 1:
        return stamped
    return max(6, min(24, max(1, len(characters)) * 3 + 4))


def default_spatial_lock(scene: dict[str, Any] | None = None) -> dict[str, str]:
    """Geography lock without baking in a church/interior template."""
    scene = scene if isinstance(scene, dict) else {}
    return {
        "setting": str(scene.get("name") or "Primary setting"),
        "architecture": str(scene.get("description") or "keep one coherent place"),
        "static_rule": (
            "STATIC OBJECTS LOCKED to the scene specs: landmarks, terrain, buildings, "
            "props, and light direction stay fixed across hierarchical views "
            "(front/left/right/side/top/bottom). Only camera/framing and on-screen cast change. "
            "Never invent an empty environment plate; never borrow architecture from another setting_id."
        ),
        "crowd_rule": (
            "No scene specs. Scene master prompt + solos define the scene. Later "
            "same-setting keyframes reuse the scene specs; keep extras silhouette unless "
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
    from jiuwenswarm.server.runtime.designer.model_tools import (
        chat_model_billing_block,
        demote_config_to_handler,
        ensure_chat_model_reachable,
        llm_available,
    )
    from jiuwenswarm.server.runtime.designer.user_references import (
        is_user_reference_node,
    )

    ensure_chat_model_reachable()
    use_agents = llm_available()
    delegate = CONFIG_DELEGATE_AGENT if use_agents else CONFIG_DELEGATE_HANDLER
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        config = node.setdefault("config", {})
        if not isinstance(config, dict):
            continue
        if is_user_reference_node(node):
            config["delegate"] = CONFIG_DELEGATE_HANDLER
            config["force_handler"] = True
            config["skip_llm"] = True
            config["read_only"] = True
            config["immutable_source"] = True
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
        else:
            demote_config_to_handler(config)
    meta = dict(graph.get("metadata") or {})
    meta["ai_agent_pipeline"] = use_agents
    meta["runtime_delegate"] = delegate
    meta["all_nodes_agents"] = use_agents
    block = chat_model_billing_block()
    if block:
        meta["chat_model_unavailable"] = block[:300]
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
        f"- Motion / continuity: time-coherent across shots "
        f"(do not undo a completed beat on a later shot)\n"
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
        "**scene specs + master prompt** (compose scene + only on-screen cast) — not an "
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
            lines.append(f"### Shot {idx} — {shot.get('title') or f'Shot {idx}'}")
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
            speech_line = str(shot.get("speech_line") or "").strip()
            by_char = shot.get("speech_by_character") if isinstance(shot.get("speech_by_character"), dict) else {}
            if by_char:
                bits = "; ".join(f"{cid}: {line}" for cid, line in by_char.items() if str(line).strip())
                if bits:
                    lines.append(f"- Speech by character: {bits}")
            elif speech_line:
                lines.append(f"- Speech: {speech_line}")
            if shot.get("language_lock"):
                lines.append(f"- Language lock: {shot.get('language_lock')}")
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
    """Honor explicit target_shot_count as a HARD ceiling — never invent extra keyframes."""
    n = len(shots) or 1
    try:
        target = int(analysis.get("target_shot_count") or 0)
    except (TypeError, ValueError):
        target = 0
    # Also honor user-prompt N-shot / N分镜 language stamped on analysis.
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.director_contract import (
            _explicit_shot_count_from_prompt,
        )

        prompt = str(
            analysis.get("user_prompt")
            or analysis.get("summary")
            or ""
        )
        explicit = _explicit_shot_count_from_prompt(prompt)
        if explicit >= 1:
            target = explicit if target < 1 else min(target, explicit)
    except Exception:  # noqa: BLE001
        pass
    if target >= 1:
        return max(1, min(_MAX_LEAN_SHOTS, target, n))
    return max(1, min(_MAX_LEAN_SHOTS, n))


def _ensure_characters_referenced(
    characters: list[dict[str, Any]], shots: list[dict[str, Any]]
) -> None:
    """Only attach uncovered cast to a shot that already lists them on_screen/ids.

    Never dump onto shot 1 by lexical score — that puts later-meet cast into the
    establishing beat (e.g. woman into alone-in-office).
    """
    covered = {
        str(cid)
        for s in shots
        if isinstance(s, dict)
        for cid in (
            list(s.get("on_screen") or [])
            + list(s.get("visible_cast_ids") or [])
            + list(s.get("character_ids") or [])
            + list(s.get("offscreen") or [])
            + list(s.get("off_screen_cast_ids") or [])
        )
        if str(cid)
    }
    for ch in characters:
        cid = str(ch.get("id") or "")
        if not cid or cid in covered or not shots:
            continue
        # Already listed somewhere under another field — sync character_ids only.
        placed = False
        for shot in shots:
            if not isinstance(shot, dict):
                continue
            listed = {
                str(x)
                for x in (
                    list(shot.get("on_screen") or [])
                    + list(shot.get("visible_cast_ids") or [])
                    + list(shot.get("offscreen") or [])
                    + list(shot.get("off_screen_cast_ids") or [])
                )
                if str(x)
            }
            if cid not in listed:
                continue
            shot["character_ids"] = list(
                dict.fromkeys([*(shot.get("character_ids") or []), cid])
            )
            covered.add(cid)
            placed = True
            break
        if not placed:
            # Leave uncovered — Manager/validators must reject or Supervisor must list them.
            continue


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
        from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
            enrich_character_clothing,
        )

        costume = enrich_character_clothing(ch) or (
            desc[:240] if desc else f"canonical look for {name}"
        )
        sheets.append(
            {
                "character_ids": [cid],
                "character_names": [name],
                "combined_cast": False,
                "identity_source": True,
                "label": name[:48],
                "prompt_body": (
                    f"{name}: {desc}. Wearing {costume}."
                    if desc
                    else f"{name}. Wearing {costume}."
                ),
                "costume_lock": costume[:320],
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
            from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
                costume_lock_for_ids,
                enrich_character_clothing,
            )

            for m in members:
                enrich_character_clothing(m)
            cast_costume = costume_lock_for_ids(
                members, [str(m.get("id")) for m in members]
            )
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
                    "costume_lock": cast_costume[:480]
                    or "; ".join(
                        f"{m.get('name')}: {str(m.get('costume_lock') or m.get('description') or '')[:100]}"
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

    If a combined sheet never sources an edge to a scene/clip/compose/storyboard,
    wire it to ``n_clip_1`` (then scene / compose / legacy frame) and append to that
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
        NODE_ROLE_SCENE,
        NODE_ROLE_FRAME,
        "keyframe",
        NODE_ROLE_CLIP,
        NODE_ROLE_COMPOSE,
        NODE_ROLE_STORYBOARD,
        "scene",
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
            or t in {"n_compose", "n_storyboard", "n_scene"}
            or t.startswith("n_scene_")
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
    has_compose = "n_compose" in ids or any(
        node_pipeline(by_id[i]) == NODE_ROLE_COMPOSE
        for i in ids
    )
    fallback = next(
        (
            cand
            for cand in (
                "n_clip_1",
                "n_scene_1",
                "n_scene",
                "n_frame_1",
                "n_compose",
            )
            if cand in ids
        ),
        next(
            (
                i
                for i in sorted(ids)
                if node_pipeline(by_id[i]) == NODE_ROLE_COMPOSE
            ),
            None,
        ),
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
    from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
        costume_lock_for_ids,
        enrich_character_clothing,
    )

    for ch in characters:
        if isinstance(ch, dict):
            enrich_character_clothing(ch)
    detailed = costume_lock_for_ids(characters, character_ids)
    if detailed:
        return detailed
    parts: list[str] = []
    id_to = {str(c.get("id")): c for c in characters if isinstance(c, dict)}
    for cid in character_ids:
        ch = id_to.get(str(cid))
        if not ch:
            continue
        name = str(ch.get("name") or cid)
        desc = str(ch.get("description") or ch.get("costume_lock") or "").strip()
        parts.append(f"{name}: {desc[:200]}" if desc else name)
    return "; ".join(parts)[:720]


def _ensure_setting_ids(
    shots: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
) -> None:
    """Stamp setting_id on every shot.

    Prefer explicit setting_id / scene_id. Only inherit the previous setting when
    the shot clearly stays in the same place; meet/leave/exterior language gets a
    new setting id so later cast is not folded into the office ensemble.
    """
    default = "set_1"
    if scenes:
        default = str(scenes[0].get("id") or "set_1").strip() or "set_1"
    scene_ids = [
        str(s.get("id") or "").strip()
        for s in scenes
        if isinstance(s, dict) and str(s.get("id") or "").strip()
    ]
    prev = default
    new_place_re = re.compile(
        r"\b("
        r"meet|meets|meeting|later|then|outside|street|exterior|outdoor|"
        r"leaves?|leaving|exit|exits|arrive|arrives|another (place|room|location)|"
        r"new (place|scene|location)|cut to|elsewhere"
        r")\b",
        re.I,
    )
    for i, shot in enumerate(shots):
        if not isinstance(shot, dict):
            continue
        sid = str(shot.get("setting_id") or shot.get("scene_id") or "").strip()
        if sid:
            prev = sid
            shot["setting_id"] = sid
            continue
        blob = f"{shot.get('action') or ''} {shot.get('keyframe_prompt') or ''} {shot.get('title') or ''}"
        if i > 0 and new_place_re.search(blob):
            # Prefer next unused scene id from analysis; else synthesize.
            used = {
                str(s.get("setting_id") or "")
                for s in shots
                if isinstance(s, dict) and s.get("setting_id")
            }
            candidate = next((x for x in scene_ids if x not in used and x != prev), "")
            if not candidate:
                candidate = f"set_{i + 1}"
            sid = candidate
        else:
            sid = prev
        shot["setting_id"] = sid
        prev = sid


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
    from jiuwenswarm.server.runtime.designer.pipeline.keyframe_policy import (
        apply_compose_solos_setting_policy,
    )
    from jiuwenswarm.server.runtime.designer.model_tools import (
        ensure_chat_model_reachable,
        llm_available,
    )

    ensure_chat_model_reachable()
    if ai_mode is None:
        ai_mode = llm_available()
    prompt_text = prompt.strip()
    mode = "cost" if str(optimize_for).strip().lower() == "cost" else "quality"
    from jiuwenswarm.server.runtime.designer.audio_locks import ensure_audio_locks_on_analysis

    analysis = ensure_audio_locks_on_analysis(dict(analysis or {}), prompt_text)
    analysis["user_prompt"] = prompt_text
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
            apply_shot_scope,
        )

        analysis = apply_shot_scope(analysis, prompt_text)
    except Exception:  # noqa: BLE001
        pass
    from jiuwenswarm.server.runtime.designer.node_labels import (
        derive_shot_name,
        derive_story_name,
        label_brief,
        label_character,
        label_clip,
        label_compose,
        label_scene,
        label_storyboard,
    )

    story_name = derive_story_name(
        analysis=analysis,
        prompt=prompt_text,
        graph_title=str(title or ""),
    )
    analysis["story_name"] = story_name
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
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.storyboard_shot_state import (
            ensure_shot_start_end_states,
        )

        shots = ensure_shot_start_end_states(shots)
        analysis["shots"] = shots
    except Exception:  # noqa: BLE001
        pass
    # Continuity: scene specs + on-screen solos; storyboard start/end owns continuity.
    skip_scene_specs = False
    analysis["skip_scene_specs"] = False
    analysis["scene_continuity_mode"] = "scene_card_plus_clip_shots"

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
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.production_bible import (
            append_bible_to_markdown,
            build_production_bible,
        )

        bible = build_production_bible(analysis, user_prompt=prompt_text)
        analysis["production_bible"] = bible
        brief_md = append_bible_to_markdown(brief_md, bible)
        storyboard_md = append_bible_to_markdown(storyboard_md, bible)
    except Exception:  # noqa: BLE001
        pass

    graph_id = new_graph_id()
    now = utc_now_ms()
    graph_title = title.strip() if isinstance(title, str) and title.strip() else prompt_text[:80]

    nodes: list[dict[str, Any]] = [
        {
            "id": "n_brief",
            "type": NODE_TYPE_TEXT,
            "label": label_brief(story_name),
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
                "agent_name": label_brief(story_name),
                "kind": "agent",
                "skill_id": "brief",
                "delegate": ("agent" if ai_mode else "handler"),
                "supervisor_task": (
                    "Author a DETAILED creative brief from the user prompt: every named "
                    "character with wardrobe/face locks, scene geography, language/speech, "
                    "opening blocking, motion/continuity rules, shot-view coverage, audio. "
                    "Preserve every named beat. Obey and include the PRODUCTION LOCK BIBLE."
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
        "agent_name": label_storyboard(story_name),
        "kind": "agent",
        "skill_id": "storyboard",
        "delegate": ("agent" if ai_mode else "handler"),
        "supervisor_task": (
            "Build a time-coherent DETAILED storyboard from the approved brief: "
            "per-shot duration, camera/view, on-screen cast, full blocking/action, "
            "exact speech_line, language lock, continuity forbids "
            "(do not undo a completed beat). Each row is THAT window in full detail — "
            "not a camera restage of the whole prompt, and not a stripped one-liner."
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
            "label": label_storyboard(story_name),
            "config": sb_cfg,
            "layout": {"x": 340, "y": 220, "width": 280, "height": 150},
        }
    )
    edges.append(_edge("e_brief_storyboard", "n_brief", "n_storyboard"))

    from jiuwenswarm.server.runtime.designer.pipeline.wan_r2v_best_practices import (
        ensure_photoreal_style_lock,
    )
    from jiuwenswarm.server.runtime.designer.media_model_playbook import style_lock_clause

    film_style = ensure_photoreal_style_lock(
        analysis.get("style_lock") if isinstance(analysis.get("style_lock"), dict) else None,
        prompt=prompt_text,
    )
    film_style_line = (style_lock_clause(film_style) or "").strip()

    char_node_ids: list[str] = []
    sheet_by_id: dict[str, dict[str, Any]] = {}
    for i, sheet in enumerate(cast_sheets, start=1):
        nid = str(sheet["node_id"])
        char_node_ids.append(nid)
        sheet_by_id[nid] = sheet
        names = [str(n) for n in sheet.get("character_names") or []]
        # Namecard: character display name (Manager-approved via analysis cast).
        display = (names[0] if names else str(sheet.get("label") or f"Character {i}")).strip()
        label = label_character(i, display)
        from jiuwenswarm.server.runtime.designer.pipeline.clothing_lock import (
            extract_clothing_parts,
        )

        wardrobe_parts = extract_clothing_parts(
            f"{sheet.get('costume_lock') or ''} {sheet.get('prompt_body') or ''}"
        )
        wardrobe = ", ".join(
            str(v).strip() for v in wardrobe_parts.values() if str(v).strip()
        )
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
                compose_character_sheet_prompt,
            )

            seed_cfg = {
                "role": NODE_ROLE_CHARACTER_DESIGN,
                "character_name": names[0] if names else display,
                "character_names": names,
                "costume_lock": wardrobe or str(sheet.get("costume_lock") or ""),
                "style_lock": dict(film_style),
                "image_size": _IMAGE_SIZE,
            }
            prompt = compose_character_sheet_prompt(cfg=seed_cfg, graph=None, seed="")
        except Exception:  # noqa: BLE001
            prompt = (
                f"One person only: {display}, full or three-quarter body on a plain "
                "empty studio backdrop. Solid neutral background, identity and costume only. "
                + (f"Wearing {wardrobe}. " if wardrobe else "")
                + (film_style_line + " " if film_style_line else "")
                + "One clear image."
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
                    "generate": {"prompt": prompt},
                    "character_id": (sheet["character_ids"][0] if len(sheet["character_ids"]) == 1 else None),
                    "character_ids": list(sheet["character_ids"]),
                    "character_name": (names[0] if names else display),
                    "character_names": names,
                    "combined_cast": False,
                    "identity_source": True,
                    "costume_lock": str(sheet.get("costume_lock") or ""),
                    "style_lock": dict(film_style),
                    "image_size": _IMAGE_SIZE,
                    "max_image_calls": 1,
                    "inputs": ["n_brief", "n_storyboard"],
                    "optimize_for": mode,
                    "agent_name": agent,
                    "kind": "agent",
                    "skill_id": "character",
                    "tools": ["call_image_model", "read_upstream", "call_model"],
                    "delegate": ("agent" if ai_mode else "handler"),
                    "supervisor_task": (
                        f"Solo identity sheet for {display}. "
                        "Write a positive Qwen-ready studio portrait from the locks "
                        "(face, wardrobe, style, aspect) — no LOCK banners or negatives. "
                        "Then call_image_model with that prompt only."
                    ),
                },
                "layout": {"x": 680, "y": float(40 + (i - 1) * 160), "width": 240, "height": 140},
            }
        )
        edges.append(_edge(f"e_sb_{nid}", "n_storyboard", nid))
        edges.append(_edge(f"e_brief_{nid}", "n_brief", nid))

    # Spatial lock text (weak env hint only). Scene cards are built per setting_id;
    # clips use them as Wan reference images (last env ref) with solos as character1…
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
    shot_ord_by_setting: dict[str, int] = {sid: 0 for sid in setting_order}

    all_solo_ids = [
        nid
        for nid in char_node_ids
        if not bool(sheet_by_id.get(nid, {}).get("combined_cast"))
    ] or list(dict.fromkeys(char_node_ids))

    scene_by_id: dict[str, dict[str, Any]] = {}
    for sc in scenes:
        if not isinstance(sc, dict):
            continue
        for key in (sc.get("id"), sc.get("setting_id")):
            sid_k = str(key or "").strip()
            if sid_k:
                scene_by_id.setdefault(sid_k, sc)

    def _scene_name_for_setting(sid: str) -> str:
        sc = scene_by_id.get(sid) or {}
        name = str(sc.get("scene_name") or sc.get("name") or sc.get("place") or "").strip()
        if name:
            return name
        place = str(setting_places.get(sid) or "").strip()
        if place:
            return place
        for sh in shots:
            if str(sh.get("setting_id") or "") != sid:
                continue
            title = str(sh.get("title") or "").strip()
            if title:
                return title
        return f"Scene {setting_num.get(sid, 1)}"

    from jiuwenswarm.server.runtime.designer.script_analysis import (
        _cast_id_maps,
        resolve_cast_token_list,
    )

    _valid_cast, _by_name_cast = _cast_id_maps(characters)
    id_to_node_early: dict[str, str] = {}
    for s in cast_sheets:
        if s.get("combined_cast"):
            continue
        ids = [str(x) for x in (s.get("character_ids") or []) if str(x)]
        if len(ids) == 1 and s.get("node_id"):
            id_to_node_early[ids[0]] = str(s["node_id"])

    def _ensemble_cids_for_setting(sid: str) -> list[str]:
        first = next(
            (
                sh
                for sh in shots
                if (str(sh.get("setting_id") or "set_1").strip() or "set_1") == sid
            ),
            None,
        )
        raw: list[Any] = []
        if isinstance(first, dict):
            occ0 = first.get("occupancy") if isinstance(first.get("occupancy"), dict) else {}
            raw = list(
                first.get("on_screen")
                or first.get("character_ids")
                or occ0.get("must_appear")
                or []
            )
        if not raw:
            for sh in shots:
                if (str(sh.get("setting_id") or "set_1").strip() or "set_1") != sid:
                    continue
                raw.extend(sh.get("on_screen") or sh.get("character_ids") or [])
        return resolve_cast_token_list(raw, valid_ids=_valid_cast, by_name=_by_name_cast)

    scene_id_by_setting: dict[str, str] = {}
    scene_master_by_setting: dict[str, str] = {}
    for sid in setting_order:
        scene_num = setting_num.get(sid, 1)
        scene_nid = f"n_scene_{scene_num}"
        scene_id_by_setting[sid] = scene_nid
        scene_master_by_setting[sid] = scene_nid
        scene_specs_setting = (
            scene_locks_meta.get(sid)
            if isinstance(scene_locks_meta.get(sid), dict)
            else {}
        )
        shot_spatial_scene = spatial_by_setting.get(sid) or spatial_lock
        lock_line_scene = _lock_line_for(sid)
        scene_name = _scene_name_for_setting(sid)
        scene_label = label_scene(scene_number=scene_num, scene_name=scene_name)
        sc_rec = scene_by_id.get(sid) or {}
        env_desc = str(
            sc_rec.get("description")
            or sc_rec.get("name")
            or setting_places.get(sid)
            or scene_name
        ).strip()[:400]
        ensemble_cids = _ensemble_cids_for_setting(sid)
        ensemble_nids = [id_to_node_early[c] for c in ensemble_cids if c in id_to_node_early]
        ensemble_names = [
            str(c.get("name") or c.get("id"))
            for c in characters
            if str(c.get("id")) in set(ensemble_cids)
        ]
        first_shot = next(
            (
                sh
                for sh in shots
                if (str(sh.get("setting_id") or "set_1").strip() or "set_1") == sid
            ),
            {},
        )
        opening_action = str(
            (first_shot or {}).get("action") or (first_shot or {}).get("keyframe_prompt") or ""
        )[:280]
        opening_cast = ", ".join(ensemble_names) or "named cast"
        tod: dict[str, str] = {}
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import (
                infer_time_of_day_lock,
            )
            from jiuwenswarm.server.runtime.designer.pipeline.image_prompt_practice import (
                compose_scene_specs_prompt,
            )

            tod = infer_time_of_day_lock(
                prompt_text,
                str(
                    (scene_specs_setting or {}).get("scene_name")
                    or env_desc
                    or ""
                ),
            )
            if isinstance(scene_specs_setting, dict) and scene_specs_setting.get("time_of_day"):
                tod["time_of_day"] = str(scene_specs_setting.get("time_of_day"))
            if isinstance(scene_specs_setting, dict) and scene_specs_setting.get("lighting"):
                tod["lighting"] = str(scene_specs_setting.get("lighting"))
            seed_cfg = {
                "role": NODE_ROLE_SCENE,
                "setting_id": sid,
                "style_lock": dict(film_style),
                "scene_specs": {
                    **(scene_specs_setting or {}),
                    "scene_name": (scene_specs_setting or {}).get("scene_name") or env_desc,
                },
                "spatial_lock": shot_spatial_scene,
                "time_of_day_lock": tod,
                "image_size": _IMAGE_SIZE,
            }
            scene_prompt = compose_scene_specs_prompt(cfg=seed_cfg, graph=None, seed="")
        except Exception:  # noqa: BLE001
            tod = {}
            scene_prompt = (
                f"Empty environment plate of {env_desc or sid}: furniture, walls, "
                "windows, light, and props only. One clear image."
            )
            if film_style_line:
                scene_prompt = film_style_line + " " + scene_prompt
        _ = (opening_cast, opening_action, ensemble_nids, lock_line_scene)
        scene_inputs = ["n_brief", "n_storyboard"]
        nodes.append(
            {
                "id": scene_nid,
                "type": NODE_TYPE_IMAGE,
                "label": scene_label,
                "config": {
                    "role": NODE_ROLE_SCENE,
                    "setting_id": sid,
                    "composed_scene": False,
                    "style_lock": dict(film_style),
                    "on_screen": [],
                    "character_ids": [],
                    "character_node_ids": [],
                    "cast_names": [],
                    "scene_specs": scene_specs_setting or None,
                    "spatial_lock": shot_spatial_scene,
                    "time_of_day_lock": tod if isinstance(tod, dict) else None,
                    "is_scene_master": True,
                    "skip_scene_specs": False,
                    "generate": {"prompt": scene_prompt},
                    "prompt": scene_prompt,
                    "image_size": _IMAGE_SIZE,
                    "max_image_calls": 1,
                    "inputs": scene_inputs,
                    "optimize_for": mode,
                    "agent_name": scene_label,
                    "kind": "agent",
                    "skill_id": "scene",
                    "tools": ["call_image_model", "read_upstream"],
                    "delegate": ("agent" if ai_mode else "handler"),
                    "supervisor_task": (
                        f"Empty environment plate for setting {sid}. "
                        "Write a positive Qwen-ready plate prompt from the locks "
                        "(place, lighting, style, aspect) — no LOCK banners or negatives. "
                        "Then call_image_model with that prompt only."
                    ),
                },
                "layout": {"x": 940, "y": float(40 + (scene_num - 1) * 180), "width": 240, "height": 140},
            }
        )
        for src in scene_inputs:
            edges.append(_edge(f"e_{src}_{scene_nid}", src, scene_nid))

    clip_ids: list[str] = []
    prev_clip_global = ""
    prev_clip_by_setting: dict[str, str] = {}
    camera_cycle = (
        "wide / establishing",
        "medium / eye-level",
        "close-up / eye-level",
        "medium / slow pan",
    )
    for shot in shots:
        idx = int(shot.get("shot_index") or (len(clip_ids) + 1))
        clip_id = f"n_clip_{idx}"
        clip_ids.append(clip_id)
        setting_id = str(shot.get("setting_id") or "set_1").strip() or "set_1"
        scene_nid = scene_id_by_setting.get(setting_id) or ""
        focus_char_nodes = list(dict.fromkeys(shot_cast_nodes.get(str(idx), [])))
        focus_char_nodes = [
            nid
            for nid in focus_char_nodes
            if not bool(sheet_by_id.get(nid, {}).get("combined_cast"))
        ]
        from jiuwenswarm.server.runtime.designer.script_analysis import (
            _cast_id_maps,
            resolve_cast_token_list,
        )

        _valid, _by_name = _cast_id_maps(characters)
        occ0 = shot.get("occupancy") if isinstance(shot.get("occupancy"), dict) else {}
        visible_cids = resolve_cast_token_list(
            shot.get("on_screen")
            or shot.get("visible_cast_ids")
            or occ0.get("must_appear")
            or shot.get("compose_cast_ids")
            or shot.get("character_ids")
            or [],
            valid_ids=_valid,
            by_name=_by_name,
        )
        offscreen_cids = resolve_cast_token_list(
            shot.get("offscreen")
            or shot.get("off_screen_cast_ids")
            or occ0.get("offscreen")
            or [],
            valid_ids=_valid,
            by_name=_by_name,
        )
        offscreen_cids = [c for c in offscreen_cids if c not in visible_cids]
        focus_cids = list(dict.fromkeys(visible_cids))
        featured_cids = [
            str(x)
            for x in (shot.get("featured_cast_ids") or focus_cids[:1] or [])
            if str(x)
        ]
        featured_cids = resolve_cast_token_list(
            featured_cids or focus_cids[:1],
            valid_ids=_valid,
            by_name=_by_name,
        ) or list(focus_cids[:1])
        cast_actions = (
            shot.get("cast_actions")
            if isinstance(shot.get("cast_actions"), dict)
            else (occ0.get("cast_actions") if isinstance(occ0.get("cast_actions"), dict) else {})
        )
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
        focus_char_nodes = list(dict.fromkeys(rebuilt)) if rebuilt else []
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
        action = str(shot.get("action") or shot.get("keyframe_prompt") or "").strip()
        if not action:
            from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
                window_beat,
            )

            action = window_beat(prompt_text, idx, max(1, len(shots)))
            shot["action"] = action
        action = action[:800]
        costume_lock = _costume_lock_for_ids(characters, focus_cids)
        from jiuwenswarm.server.runtime.designer.pipeline.shot_staging_lock import (
            enrich_shot_staging,
            staging_lock_clause,
        )

        staging = enrich_shot_staging(shot, characters)
        staging_bits = staging_lock_clause(
            positioning_lock=staging.get("positioning_lock") or "",
            action_lock=staging.get("action_lock") or "",
            relationship_lock=staging.get("relationship_lock") or "",
            shot_index=idx,
            setting_id=setting_id,
            for_clip=True,
        )
        shot_spatial = spatial_by_setting.get(setting_id) or spatial_lock
        lock_line = _lock_line_for(setting_id)
        keyframe_strategy = "clip_from_scene_and_solos"
        # Storyboard-owned continuity: deps = storyboard + on-screen solos + scene only.
        # No prior-clip edge — same-setting clips can run concurrently.
        clip_inputs = ["n_storyboard", *focus_char_nodes]
        if scene_nid:
            clip_inputs.append(scene_nid)
        clip_inputs = list(dict.fromkeys([x for x in clip_inputs if x]))
        y = 40 + (idx - 1) * 160
        occupancy = shot.get("occupancy") if isinstance(shot.get("occupancy"), dict) else {}
        already_done = [
            str(x)
            for x in (shot.get("already_done") or [])
            if str(x).strip()
        ]
        # First clip of a setting must not inherit prior-room already_done notes.
        if not prev_clip_by_setting.get(setting_id):
            already_done = []
        elif setting_id:
            # Drop notes that clearly name a different setting_id.
            filtered: list[str] = []
            for note in already_done:
                low = note.lower()
                if "setting=" in low and f"setting={setting_id.lower()}" not in low:
                    continue
                filtered.append(note)
            already_done = filtered
        scene_specs = (
            shot.get("scene_specs")
            if isinstance(shot.get("scene_specs"), dict)
            else (scene_locks_meta.get(setting_id) if isinstance(scene_locks_meta.get(setting_id), dict) else {})
        )
        from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
            ANGLE_VIEWS,
            user_asked_coverage,
        )

        raw_view = str(shot.get("view_key") or "").strip()
        relation = str(shot.get("shot_relation") or "").strip().lower()
        show_angle = raw_view.lower() in ANGLE_VIEWS and (
            user_asked_coverage(prompt_text) or relation == "angle_variant"
        )
        view_key = raw_view if show_angle else ""
        shot["view_key"] = view_key
        timeline = str(shot.get("timeline") or "").strip() or f"{(idx - 1) * 5:.1f}-{idx * 5:.1f}s"
        bible_line = ""
        if scene_specs:
            views = scene_specs.get("views") if isinstance(scene_specs.get("views"), dict) else {}
            view_line = ""
            if show_angle:
                view_line = str(views.get(view_key) or "")[:220]
            bible_line = (
                f"SCENE SPECS `{setting_id}`: scene={scene_specs.get('scene_name') or scene_specs.get('place')}; "
                f"lighting={scene_specs.get('lighting')}; "
                f"objects={', '.join(str(x) for x in (scene_specs.get('objects') or [])[:6])}; "
                f"crowd={scene_specs.get('crowd')}; "
                f"{scene_specs.get('coherence_rule')}; "
                + (f"ACTIVE {view_line}. " if view_line else "")
            )
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
        scene_bits = ""
        if shot.get("scene_distinctness"):
            scene_bits = f"SCENE: {str(shot.get('scene_distinctness'))[:220]} "
        elif shot.get("setting_lock") and isinstance(shot.get("setting_lock"), dict):
            scene_bits = f"SCENE LOCK: {str((shot.get('setting_lock') or {}).get('rule') or '')[:220]} "
        detail_bits = (
            "DETAIL REQUIRED: screen L/R for each person in frame, gaze target, "
            "motion direction, relative props/landmarks. "
        )
        staging_prompt = (staging_bits + " ") if staging_bits else ""
        solo_ref_list = ", ".join(focus_char_nodes) or "none"
        first_of_setting = int(shot_ord_by_setting.get(setting_id) or 0) == 0
        layout_bits = (
            f"Reference clip, setting {setting_id}: character sheets {solo_ref_list} "
            f"for {cast_who}, then scene specs {scene_nid or 'scene'} as the room. "
            "Place those people into that empty room for THIS storyboard shot. "
            "Keep the film STYLE LOCK. "
        )
        if not first_of_setting:
            layout_bits += (
                f"CONTINUATION of setting {setting_id}: same room, same faces, same wardrobe. "
                "This window continues the story. "
            )
        if show_angle and view_key:
            shot_head = (
                f"Film shot {idx} (setting {setting_id}, timeline {timeline}, view {view_key}). "
            )
        else:
            shot_head = (
                f"Film shot {idx} (setting {setting_id}, timeline {timeline}). "
                "THIS time window only. "
            )
        clip_prompt_body = (
            (film_style_line + "\n" if film_style_line else "")
            + shot_head
            + layout_bits
            + (f"WHO DOES WHAT: {doing_line}. " if doing_line else "")
            + f"People in frame: {cast_who}. "
            f"{lock_line} {bible_line} {scene_bits}"
            f"{staging_prompt}"
            f"Action: {action}. Camera: {camera}. "
            + (f"CONTINUITY: {cont_bits}. " if cont_bits else "")
            + occ_bits
            + crowd_bits
            + done_bits
            + detail_bits
            + "ANTI-CLONE: one instance per named person. "
            + f"STRATEGY={keyframe_strategy}. first_of_setting={first_of_setting}. "
            + "Keep clothing / language / occupancy / position locks. "
            + "Keep this Wan prompt ≤4000 characters. "
            + "Real motion — animate this shot only."
        )
        identity_refs = {
            "character_ids": focus_cids,
            "character_node_ids": list(focus_char_nodes),
            "cast_names": list(focus_names),
            "costume_lock": costume_lock,
            "scene_node_id": scene_nid or None,
            "master_scene_node_id": scene_nid or None,
            "scene_master_frame_id": None,
            "is_scene_master": False,
            "prior_keyframe_node_id": None,
            "scene_prompt_handoff_from": scene_nid or None,
            "keyframe_strategy": keyframe_strategy,
            "setting_id": setting_id,
            "view_key": view_key,
            "spatial_lock": shot_spatial,
            "occupancy": occupancy or None,
            "crowd_lock": crowd or None,
            "skip_scene_specs": False,
            "on_screen": list(focus_cids),
            "offscreen": list(shot.get("offscreen") or []),
            "cast_actions": cast_actions or None,
            "setting_lock": shot.get("setting_lock")
            if isinstance(shot.get("setting_lock"), dict)
            else None,
            "scene_distinctness": shot.get("scene_distinctness"),
            "scene_continuity_mode": "scene_card_plus_clip_shots",
            "scene_specs": scene_specs or None,
            "first_of_setting": first_of_setting,
            "composed_scene": False,
            "style_lock": dict(film_style),
        }
        scene_n = setting_num.get(setting_id, 1)
        shot_ord_by_setting[setting_id] = int(shot_ord_by_setting.get(setting_id) or 0) + 1
        shot_n = shot_ord_by_setting[setting_id]
        shot_name = derive_shot_name(shot, fallback_index=idx)
        clip_label = label_clip(
            scene_number=scene_n, clip_number=shot_n, clip_name=shot_name
        )
        from jiuwenswarm.server.runtime.designer.audio_locks import (
            normalize_speech_by_character,
            speech_line_from_by_character,
        )

        speech_by_character = normalize_speech_by_character(shot, characters)
        speech_line = speech_line_from_by_character(speech_by_character) or str(
            shot.get("speech_line") or shot.get("dialogue") or ""
        ).strip()
        # Film-wide / shot time-of-day on every clip config (story weave reads this).
        tod_clip: dict[str, str] = {}
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.axis_locks import (
                infer_time_of_day_lock,
            )

            tod_clip = infer_time_of_day_lock(
                prompt_text,
                str(
                    (scene_specs or {}).get("scene_name")
                    or shot.get("setting_description")
                    or action
                    or ""
                ),
            )
            if isinstance(shot.get("time_of_day_lock"), dict):
                tod_clip = {
                    **tod_clip,
                    **{
                        k: str(v)
                        for k, v in shot["time_of_day_lock"].items()
                        if str(v).strip()
                    },
                }
            if isinstance(scene_specs, dict):
                if scene_specs.get("time_of_day") and (
                    not tod_clip.get("time_of_day")
                    or tod_clip.get("time_of_day") == "unspecified"
                ):
                    tod_clip["time_of_day"] = str(scene_specs.get("time_of_day"))
                if scene_specs.get("lighting") and not tod_clip.get("lighting"):
                    tod_clip["lighting"] = str(scene_specs.get("lighting"))
                # Keep bible lighting aligned with ToD when still generic.
                if tod_clip.get("lighting") and (
                    not scene_specs.get("lighting")
                    or "motivated key light"
                    in str(scene_specs.get("lighting") or "").lower()
                ):
                    scene_specs = dict(scene_specs)
                    scene_specs["lighting"] = tod_clip["lighting"]
                    if tod_clip.get("time_of_day"):
                        scene_specs.setdefault("time_of_day", tod_clip["time_of_day"])
        except Exception:  # noqa: BLE001
            tod_clip = {}
        clip_cfg: dict[str, Any] = {
            "role": NODE_ROLE_CLIP,
            "shot_index": idx,
            "shot_title": shot.get("title"),
            "shot_action": action,
            "timeline": timeline,
            "camera": camera,
            "setting_id": setting_id,
            "view_key": view_key,
            "scene_node_id": scene_nid or None,
            "scene_specs": scene_specs or None,
            "character_ids": focus_cids,
            "on_screen": list(focus_cids),
            "offscreen": list(shot.get("offscreen") or []),
            "character_node_ids": focus_char_nodes,
            "cast_names": focus_names,
            "identity_refs": identity_refs,
            "costume_lock": costume_lock,
            "positioning_lock": staging.get("positioning_lock"),
            "action_lock": staging.get("action_lock"),
            "relationship_lock": staging.get("relationship_lock"),
            "blocking": shot.get("blocking") if isinstance(shot.get("blocking"), dict) else None,
            "cast_actions": cast_actions or None,
            "continuity_lock": continuity or None,
            "occupancy": occupancy or None,
            "crowd_lock": crowd or None,
            "already_done": already_done or None,
            "speech_line": speech_line,
            "speech_by_character": speech_by_character,
            "language_lock": str(
                shot.get("language_lock")
                or (analysis.get("language_lock") if isinstance(analysis, dict) else "")
                or (audio.get("language_lock") if isinstance(audio, dict) else "")
                or "en"
            ),
            "bgm_lock": (
                shot.get("bgm_lock")
                if isinstance(shot.get("bgm_lock"), dict)
                else (
                    analysis.get("bgm_lock")
                    if isinstance(analysis.get("bgm_lock"), dict)
                    else (audio.get("bgm_lock") if isinstance(audio.get("bgm_lock"), dict) else None)
                )
            ),
            "spatial_lock": shot_spatial,
            "time_of_day_lock": tod_clip or None,
            "master_scene_node_id": scene_nid or None,
            "keyframe_strategy": keyframe_strategy,
            "first_of_setting": first_of_setting,
            "composed_scene": False,
            "style_lock": dict(film_style),
            "allow_still_clip_fallback": False,
            "generate": {"prompt": clip_prompt_body},
            "max_video_calls": 1,
            "inputs": clip_inputs,
            "optimize_for": mode,
            "agent_name": clip_label,
            "kind": "agent",
            "skill_id": "clip",
            "tools": ["call_video_model", "read_upstream"],
            "delegate": ("agent" if ai_mode else "handler"),
            "supervisor_task": (
                f"Shot {idx}: on-screen character sheets plus scene specs {scene_nid}. "
                "Write ONE positive story-form video prompt from THIS storyboard row "
                "(start_state → action/camera/speech → end_state). "
                "No LOCK banners, no negatives, no prior-clip paste. "
                "Then call_video_model with that prompt only."
            ),
        }
        # Storyboard owns continuity — never wire prior clip as a schedule/data parent.
        clip_cfg.pop("continuity_clip_node_id", None)
        clip_cfg["previous_clip_handoff_ready"] = False
        try:
            from jiuwenswarm.server.runtime.designer.pipeline.storyboard_shot_state import (
                stamp_shot_states_on_clip_cfg,
            )

            clip_cfg = stamp_shot_states_on_clip_cfg(clip_cfg, shot=shot)
        except Exception:  # noqa: BLE001
            pass
        from jiuwenswarm.server.runtime.designer.pipeline.video_prompt_practice import (
            compose_practice_prompt,
        )

        clip_cfg["generate"] = {
            "prompt": compose_practice_prompt(
                cfg=clip_cfg,
                graph={"metadata": {"script_analysis": analysis}, "nodes": nodes},
                action=action,
                camera=camera,
            )
        }
        nodes.append(
            {
                "id": clip_id,
                "type": NODE_TYPE_VIDEO,
                "label": clip_label,
                "config": clip_cfg,
                "layout": {"x": 1320, "y": float(y), "width": 240, "height": 140},
            }
        )
        for src in clip_inputs:
            edges.append(_edge(f"e_{src}_{clip_id}", src, clip_id))
        prev_clip_by_setting[setting_id] = clip_id
        prev_clip_global = clip_id

    audio_ids: list[str] = []
    film_sec = max(6, int(film_duration or max(6, len(shots) * 5)))
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
            # BGM is one film-wide bed mixed after concat, so the node survives
            # and writes a silent placeholder until a music API is configured.
            clip_embedded = True
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
                        "delegate": (
                            ("agent" if ai_mode else "handler") if can_music else "handler"
                        ),
                        "force_handler": not can_music,
                        "placeholder_until_api": not can_music,
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
            from jiuwenswarm.server.runtime.designer.audio_locks import stamp_audio_fields_on_clip_config

            for n in nodes:
                if not isinstance(n, dict):
                    continue
                cfg = n.get("config") if isinstance(n.get("config"), dict) else {}
                if str(cfg.get("role") or "") != NODE_ROLE_CLIP:
                    continue
                idx = int(cfg.get("shot_index") or 0)
                shot_row = next(
                    (
                        s
                        for s in shots
                        if isinstance(s, dict) and int(s.get("shot_index") or 0) == idx
                    ),
                    {},
                )
                cfg = stamp_audio_fields_on_clip_config(
                    dict(cfg),
                    shot=shot_row if isinstance(shot_row, dict) else {},
                    analysis=analysis,
                    meta={"audio_intent": audio, "audio_routing": {"can_speech": can_speech, "can_music": can_music}},
                    clip_embedded=True,
                )
                n["config"] = cfg
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.clip_last_frame_handoff import (
                    chain_prior_speech_across_clips,
                )

                # nodes is a flat list; wrap briefly as a graph for the chain helper.
                chain_prior_speech_across_clips({"nodes": nodes})
            except Exception:  # noqa: BLE001
                pass
            # Prefer clip-native audio when TTS/BGM backends are missing (capability-gated).
            # (Stamped again on metadata below.)
            pass

    compose_inputs = [*clip_ids, *audio_ids]
    nodes.append(
        {
            "id": "n_compose",
            "type": NODE_TYPE_VIDEO,
            "label": label_compose(story_name),
            "config": {
                "role": NODE_ROLE_COMPOSE,
                "inputs": compose_inputs,
                "optimize_for": mode,
                "agent_name": label_compose(story_name),
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
                "can_video_audio": bool(backends.get("can_video_audio", True)),
                "video_audio_model": str(backends.get("video_audio_model") or ""),
                "speech_nodes": "n_speech" in audio_ids,
                "music_nodes": "n_music" in audio_ids,
            },
            "language_lock": str(
                analysis.get("language_lock") or audio.get("language_lock") or "en"
            ),
            "bgm_lock": (
                analysis.get("bgm_lock")
                if isinstance(analysis.get("bgm_lock"), dict)
                else (audio.get("bgm_lock") if isinstance(audio.get("bgm_lock"), dict) else {})
            ),
            "prefer_clip_native_audio": bool(clip_embedded),
            "prefer_wan3_clip_audio": bool(clip_embedded),  # legacy alias
            "skip_scene_specs": False,
            "scene_continuity_mode": "scene_card_plus_clip_shots",
            "scene_masters": dict(scene_master_by_setting),
            "scene_locks": dict(scene_locks_meta),
            "lean_pipeline": False,
            # Storyboard must not rebuild the node set (that spawned extra nodes).
            "freeze_shot_topology": True,
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
                "multi_shot_compose": "scene_card_plus_clip_shots",
                "scene_spatial": "shared_scene_bible_prompt_handoff",
                "sequential_keyframe": "prompt_handoff_same_setting_id_only",
                "costume_lock": True,
                "identity_refs_on_frame_clip": True,
                "clip_from_scene_and_solos": True,
                "solo_gate_before_keyframes": True,
                "per_shot_scene_views": True,
                "hierarchical_views": True,
                "empty_scene_plates": True,
                "prior_prompt_handoff": True,
                "notes": (
                    "Continuity: Brief+LOCK BIBLE→Storyboard→solo sheets→scene specs→"
                    "every clip is R2V (character sheets + empty room) with STYLE LOCK. "
                    "Later clips continue who is in the room and this window's action."
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
