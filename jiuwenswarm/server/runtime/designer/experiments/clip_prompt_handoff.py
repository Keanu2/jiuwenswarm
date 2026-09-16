# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Pass completed clip Wan prompts to later clip leaves (domain-agnostic).

Keeps Plan A v12 leaf freedom: no frozen Clip Prompt Board. After each clip is
authored/generated, stamp the prompt onto the next clip node so the next leaf
LLM sees what already happened (beats, exits, L/R, style) and does not redo it.
"""

from __future__ import annotations

from typing import Any


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


def collect_prior_clip_prompts(
    graph: dict[str, Any],
    *,
    shot_index: int,
    max_chars_each: int = 1600,
    max_clips: int = 6,
) -> list[dict[str, Any]]:
    """All earlier clip Wan prompts (oldest → newest) for this film."""
    out: list[dict[str, Any]] = []
    for node in _clip_nodes(graph):
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        idx = int(cfg.get("shot_index") or 0) or 0
        if idx <= 0 or idx >= int(shot_index or 0):
            continue
        prompt = wan_prompt_from_clip_node(node)
        if not prompt:
            continue
        out.append(
            {
                "node_id": str(node.get("id") or ""),
                "shot_index": idx,
                "wan_prompt": prompt[: max(200, int(max_chars_each))],
                "shot_action": str(cfg.get("shot_action") or "")[:200],
                "speech_line": str(cfg.get("speech_line") or "")[:200],
            }
        )
    return out[-max(1, int(max_clips)) :]


def stamp_wan_prompt_handoff(
    graph: dict[str, Any],
    *,
    shot_index: int,
    prompt: str,
    node_id: str = "",
) -> list[str]:
    """Save this clip's Wan prompt and inject it onto later clip nodes' context."""
    notes: list[str] = []
    text = (prompt or "").strip()
    if not text:
        return notes
    meta = dict(graph.get("metadata") or {})
    log = dict(meta.get("clip_wan_prompt_log") or {})
    key = str(node_id or f"n_clip_{shot_index}")
    log[key] = {
        "shot_index": int(shot_index or 0),
        "prompt": text[:4000],
    }
    meta["clip_wan_prompt_log"] = log
    graph["metadata"] = meta

    for node in _clip_nodes(graph):
        cfg = dict(node.get("config") or {})
        idx = int(cfg.get("shot_index") or 0) or 0
        nid = str(node.get("id") or "")
        if idx == int(shot_index or 0) or nid == key:
            cfg["last_wan_prompt"] = text[:4000]
            cfg["clip_prompt_preview"] = text[:1200]
            node["config"] = cfg
            notes.append(f"{nid}: saved last_wan_prompt")
            continue
        if idx <= int(shot_index or 0):
            continue
        # Immediate next clip gets the full previous prompt; later clips get chain via collect.
        prev_id = str(cfg.get("continuity_clip_node_id") or "")
        is_immediate_next = idx == int(shot_index or 0) + 1 or prev_id == key
        if is_immediate_next or idx == int(shot_index or 0) + 1:
            cfg["previous_clip_wan_prompt"] = text[:3500]
            cfg["previous_clip_node_id"] = key
            cfg["previous_clip_shot_index"] = int(shot_index or 0)
            node["config"] = cfg
            notes.append(f"{nid}: received previous_clip_wan_prompt from {key}")
    return notes


def handoff_clause_for_prompt(prior: list[dict[str, Any]] | None) -> str:
    """Text block injected into Wan / leaf context — do not redo prior beats."""
    items = [p for p in (prior or []) if isinstance(p, dict) and p.get("wan_prompt")]
    if not items:
        return ""
    latest = items[-1]
    bits = [
        "PRIOR CLIP CONTINUITY (do NOT redo these beats; continue the film forward):",
        f"- Previous shot {latest.get('shot_index')} ({latest.get('node_id')}): "
        f"{str(latest.get('wan_prompt') or '')[:1200]}",
    ]
    if latest.get("speech_line"):
        bits.append(f"- Prior speech already delivered: {str(latest.get('speech_line'))[:160]}")
    if len(items) > 1:
        earlier = "; ".join(
            f"shot {p.get('shot_index')}: {(p.get('shot_action') or p.get('wan_prompt') or '')[:80]}"
            for p in items[:-1]
        )
        bits.append(f"- Earlier clips already covered: {earlier}")
    bits.append(
        "STATE CARRY: keep STYLE HOLD, screen L/R seats, landmarks, wardrobe, crowd/extras, "
        "and exit state from prior prompts. If someone left or moved screen-right, they must "
        "stay gone or stay on the right — never reset to the opening blocking. "
        "Animate THIS shot's keyframe only with NEW motion/gaze/camera for this beat."
    )
    return "\n".join(bits)
