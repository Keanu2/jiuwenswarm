# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Pass slim continuity cards to later clip leaves (domain-agnostic).

After each clip is authored, stamp ALREADY_DONE / CONTINUITY LOCK onto later
clip nodes. Do NOT paste the full prior Wan prompt into the next leaf — that
contaminates shot identity (shot N looks like shot N-1).
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.server.runtime.designer.experiments.continuity_card import (
    continuity_card_clause,
    continuity_card_from_prior,
    merge_already_done,
)


def _clip_nodes(graph: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = []
    for n in graph.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        cfg = n.get("config") if isinstance(n.get("config"), dict) else {}
        if str(cfg.get("role") or "") == "clip":
            nodes.append(n)
    return sorted(
        nodes,
        key=lambda n: int((n.get("config") or {}).get("shot_index") or 0) or 99,
    )


def wan_prompt_from_clip_node(node: dict[str, Any] | None) -> str:
    if not isinstance(node, dict):
        return ""
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    for key in ("last_wan_prompt", "clip_prompt_preview", "agent_authored_prompt_text"):
        text = str(cfg.get(key) or "").strip()
        if text and key != "agent_authored_prompt_text":
            return text
    gen = cfg.get("generate") if isinstance(cfg.get("generate"), dict) else {}
    return str(gen.get("prompt") or cfg.get("prompt") or "").strip()


def _action_from_clip_node(node: dict[str, Any] | None) -> str:
    if not isinstance(node, dict):
        return ""
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    action = str(cfg.get("shot_action") or "").strip()
    if action:
        return action
    return str(cfg.get("character_action") or "").strip()


def collect_prior_clip_prompts(
    graph: dict[str, Any],
    *,
    shot_index: int,
    max_chars_each: int = 1600,
    max_clips: int = 6,
) -> list[dict[str, Any]]:
    """Earlier clip continuity seeds (oldest → newest). Prefer action, keep wan for internal use only."""
    out: list[dict[str, Any]] = []
    for node in _clip_nodes(graph):
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        idx = int(cfg.get("shot_index") or 0) or 0
        if idx <= 0 or idx >= int(shot_index or 0):
            continue
        action = _action_from_clip_node(node)
        prompt = wan_prompt_from_clip_node(node)
        card = cfg.get("continuity_card") if isinstance(cfg.get("continuity_card"), dict) else None
        if not action and not prompt and not card:
            continue
        bible = cfg.get("scene_bible") if isinstance(cfg.get("scene_bible"), dict) else None
        if card is None:
            card = continuity_card_from_prior(
                prior_action=action,
                prior_prompt=prompt,
                shot_index=idx,
                node_id=str(node.get("id") or ""),
                speech_line=str(cfg.get("speech_line") or ""),
                bible=bible,
                storyboard_hints=action,
            )
        out.append(
            {
                "node_id": str(node.get("id") or ""),
                "shot_index": idx,
                # Keep wan_prompt for readiness/debug — never inject full text into next prompt.
                "wan_prompt": (prompt or "")[: max(200, int(max_chars_each))],
                "shot_action": (action or "")[:200],
                "speech_line": str(cfg.get("speech_line") or "")[:200],
                "continuity_card": card,
            }
        )
    return out[-max(1, int(max_clips)) :]


def stamp_wan_prompt_handoff(
    graph: dict[str, Any],
    *,
    shot_index: int,
    prompt: str,
    node_id: str = "",
    shot_action: str = "",
    speech_line: str = "",
) -> list[str]:
    """Save this clip's Wan prompt on self; stamp slim continuity cards onto later clips."""
    notes: list[str] = []
    text = (prompt or "").strip()
    if not text and not shot_action:
        return notes
    meta = dict(graph.get("metadata") or {})
    log = dict(meta.get("clip_wan_prompt_log") or {})
    key = str(node_id or f"n_clip_{shot_index}")
    src_node = next(
        (n for n in _clip_nodes(graph) if str(n.get("id") or "") == key),
        None,
    )
    src_cfg = src_node.get("config") if isinstance(src_node, dict) else {}
    action = (shot_action or _action_from_clip_node(src_node) or "").strip()
    speech = (speech_line or str((src_cfg or {}).get("speech_line") or "")).strip()
    bible = (src_cfg or {}).get("scene_bible") if isinstance((src_cfg or {}).get("scene_bible"), dict) else None
    card = continuity_card_from_prior(
        prior_action=action,
        prior_prompt=text,
        shot_index=int(shot_index or 0),
        node_id=key,
        speech_line=speech,
        bible=bible,
        storyboard_hints=action,
    )
    log[key] = {
        "shot_index": int(shot_index or 0),
        "prompt": text[:4000],
        "continuity_card": card,
    }
    meta["clip_wan_prompt_log"] = log
    graph["metadata"] = meta

    for node in _clip_nodes(graph):
        cfg = dict(node.get("config") or {})
        idx = int(cfg.get("shot_index") or 0) or 0
        nid = str(node.get("id") or "")
        if idx == int(shot_index or 0) or nid == key:
            if text:
                cfg["last_wan_prompt"] = text[:4000]
                cfg["clip_prompt_preview"] = text[:1200]
            cfg["continuity_card"] = card
            cfg["handoff_artifact_ready"] = True
            node["config"] = cfg
            notes.append(f"{nid}: saved last_wan_prompt + continuity_card")
            continue
        if idx <= int(shot_index or 0):
            continue
        is_immediate_next = idx == int(shot_index or 0) + 1
        prev_id = str(cfg.get("continuity_clip_node_id") or "")
        has_card = isinstance(cfg.get("previous_clip_continuity_card"), dict)
        if is_immediate_next or prev_id == key or not has_card:
            if is_immediate_next or prev_id == key or idx == int(shot_index or 0) + 1:
                cfg["previous_clip_continuity_card"] = card
                cfg["previous_clip_node_id"] = key
                cfg["previous_clip_shot_index"] = int(shot_index or 0)
                # Artifact readiness marker without pasting the full prior prompt.
                cfg["previous_clip_handoff_ready"] = True
                # Clear legacy full-prompt stamp so Manager/handlers cannot re-inject it.
                cfg.pop("previous_clip_wan_prompt", None)
                cfg["already_done"] = merge_already_done(
                    cfg.get("already_done") if isinstance(cfg.get("already_done"), list) else None,
                    card.get("already_done") if isinstance(card.get("already_done"), list) else None,
                )
                if isinstance(card.get("continuity_lock"), dict):
                    cfg["continuity_lock"] = dict(card["continuity_lock"])
                node["config"] = cfg
                notes.append(f"{nid}: received continuity_card from {key}")
    return notes


def handoff_clause_for_prompt(prior: list[dict[str, Any]] | None) -> str:
    """Slim continuity clause — already_done / forbid only; never paste full Wan text."""
    items = [p for p in (prior or []) if isinstance(p, dict)]
    if not items:
        return ""
    latest = items[-1]
    card = latest.get("continuity_card") if isinstance(latest.get("continuity_card"), dict) else None
    if card is None:
        card = continuity_card_from_prior(
            prior_action=str(latest.get("shot_action") or ""),
            prior_prompt=str(latest.get("wan_prompt") or ""),
            shot_index=int(latest.get("shot_index") or 0) or None,
            node_id=str(latest.get("node_id") or ""),
            speech_line=str(latest.get("speech_line") or ""),
            storyboard_hints=str(latest.get("shot_action") or ""),
        )
    clause = continuity_card_clause(card)
    if len(items) > 1:
        earlier_done: list[str] = []
        for p in items[:-1]:
            c = p.get("continuity_card") if isinstance(p.get("continuity_card"), dict) else None
            if c and isinstance(c.get("already_done"), list):
                earlier_done.extend(str(x) for x in c["already_done"] if str(x).strip())
            else:
                act = str(p.get("shot_action") or "")[:80]
                if act:
                    earlier_done.append(f"shot {p.get('shot_index')}: already covered — {act}")
        if earlier_done:
            clause = (
                clause
                + "\nEarlier ALREADY_DONE:\n"
                + "\n".join(f"  - {x}" for x in earlier_done[:8])
            )
    return clause
