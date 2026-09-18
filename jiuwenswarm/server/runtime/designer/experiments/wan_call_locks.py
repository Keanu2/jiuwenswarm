# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Locks injected at Wan I2V call time (domain-agnostic).

Leaf agents may rewrite creative motion text, but the final API prompt MUST
retain style / aspect / occupancy / prop / set-orientation locks.
"""

from __future__ import annotations

from typing import Any


def _style_from(cfg: dict[str, Any], graph: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    for src in (
        cfg.get("style_lock"),
        (graph.get("metadata") or {}).get("style_lock") if isinstance(graph.get("metadata"), dict) else None,
        analysis.get("style_lock"),
    ):
        if isinstance(src, dict) and src:
            return src
    return {}


def _aspect_from(cfg: dict[str, Any], graph: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    for src in (
        cfg.get("aspect_lock"),
        (graph.get("metadata") or {}).get("aspect_lock") if isinstance(graph.get("metadata"), dict) else None,
        analysis.get("aspect_lock"),
    ):
        if isinstance(src, dict) and src:
            return src
    return {}


def set_orientation_lock_clause() -> str:
    """Keep Image-1 room orientation stable (fixes spin/flip drift)."""
    return (
        "SET/ORIENTATION LOCK: keep Image 1's exact room geometry, wall/window sides, "
        "furniture layout, and camera roll/horizon — FORBIDDEN: spin or rotate the set, "
        "mirror/flip architecture left-right, tilt the world, or rebuild a different room. "
        "Camera may push-in/pan slightly only if it does not reorient the space."
    )


def apply_keyframe_call_locks(
    prompt: str,
    *,
    cfg: dict[str, Any] | None = None,
    graph: dict[str, Any] | None = None,
) -> str:
    """Prepend mandatory aspect/style/spatial locks for still keyframe generation."""
    cfg = cfg if isinstance(cfg, dict) else {}
    graph = graph if isinstance(graph, dict) else {}
    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    text = str(prompt or "").strip()
    head: list[str] = []

    style = _style_from(cfg, graph, analysis)
    aspect = _aspect_from(cfg, graph, analysis)

    try:
        from jiuwenswarm.server.runtime.designer.media_model_playbook import style_lock_clause

        clause = style_lock_clause(style)
        if clause and "STYLE LOCK" not in text:
            head.append(clause.strip())
        elif style and "STYLE LOCK" not in text:
            look = str(style.get("look") or style.get("medium") or "").strip()
            if look:
                head.append(f"STYLE LOCK (film-wide): {look[:280]}")
    except Exception:  # noqa: BLE001
        pass

    if aspect:
        rule = str(aspect.get("rule") or "").strip()
        ratio = str(aspect.get("ratio") or "").strip()
        size = str(aspect.get("image_size") or cfg.get("image_size") or "").strip()
        bit = rule or (f"ASPECT LOCK: keep {ratio} for every still." if ratio else "")
        if bit and "ASPECT" not in text.upper():
            if size:
                bit = f"{bit} Target image_size={size}."
            head.append(bit)

    spatial = cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else {}
    if not spatial:
        spatial = meta.get("spatial_lock") if isinstance(meta.get("spatial_lock"), dict) else {}
    if spatial and "SPATIAL LOCK" not in text:
        head.append(
            "SPATIAL LOCK: "
            + "; ".join(f"{k}={v}" for k, v in spatial.items() if str(v).strip())
        )

    try:
        from jiuwenswarm.server.runtime.designer.experiments.clothing_lock import (
            clothing_lock_clause,
            ensure_cfg_clothing_lock,
        )

        chars = list(analysis.get("characters") or [])
        costume = ensure_cfg_clothing_lock(cfg, characters=chars) or str(
            cfg.get("costume_lock")
            or ((cfg.get("identity_refs") or {}).get("costume_lock") if isinstance(cfg.get("identity_refs"), dict) else "")
            or ""
        ).strip()
        if costume and "CLOTHING LOCK" not in text and "CLOTHING HOLD" not in text:
            cloth = clothing_lock_clause(costume, for_clip=False)
            if cloth:
                head.append(cloth)
    except Exception:  # noqa: BLE001
        pass

    try:
        from jiuwenswarm.server.runtime.designer.experiments.shot_staging_lock import (
            ensure_cfg_staging_locks,
        )

        staging = ensure_cfg_staging_locks(
            cfg, characters=list(analysis.get("characters") or [])
        )
        if staging and "STAGING LOCK" not in text:
            head.append(staging)
    except Exception:  # noqa: BLE001
        pass

    if not head:
        return text[:6000]
    return ("\n\n".join(head) + "\n\n" + text).strip()[:6000]


def apply_wan_call_locks(
    prompt: str,
    *,
    cfg: dict[str, Any] | None = None,
    graph: dict[str, Any] | None = None,
    shot_index: int = 0,
    has_first_frame: bool = True,
) -> str:
    """Prepend mandatory locks so Wan always sees them, even after leaf rewrites."""
    cfg = cfg if isinstance(cfg, dict) else {}
    graph = graph if isinstance(graph, dict) else {}
    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    text = str(prompt or "").strip()

    style = _style_from(cfg, graph, analysis)
    aspect = _aspect_from(cfg, graph, analysis)
    shots_map = {
        int(s.get("shot_index") or 0): s
        for s in (analysis.get("shots") or [])
        if isinstance(s, dict)
    }
    shot = shots_map.get(int(shot_index) or 0) or {}
    if not shot:
        # Fall back to cfg-stamped occupancy/prop from smart_graph.
        shot = {
            "occupancy": cfg.get("occupancy"),
            "prop_presentation": cfg.get("prop_presentation"),
            "prop_ids": cfg.get("prop_ids"),
        }

    head: list[str] = []

    # Style — always first for I2V.
    try:
        from jiuwenswarm.server.runtime.designer.media_model_playbook import (
            style_lock_clause,
        )
        from jiuwenswarm.server.runtime.designer.experiments.movie_continuity_guide import (
            style_hold_for_i2v,
        )

        hold = style_hold_for_i2v(style)
        clause = style_lock_clause(style)
        if hold and "STYLE HOLD" not in text:
            head.append(hold)
        if clause and "STYLE LOCK" not in text:
            head.append(clause.strip())
        elif style and "STYLE HOLD" not in text and "STYLE LOCK" not in text:
            look = str(style.get("look") or style.get("medium") or "").strip()
            if look:
                head.append(f"STYLE LOCK (film-wide): {look[:280]}")
    except Exception:  # noqa: BLE001
        if "STYLE HOLD" not in text:
            head.append(
                "STYLE HOLD: match Image 1 art medium and grade exactly; do not restyle mid-clip."
            )

    if has_first_frame and "Image 1" not in text:
        head.append(
            "[References]: Image 1 is the FIRST-FRAME keyframe — animate THIS exact image; "
            "preserve art style, faces, wardrobe, architecture, and lighting."
        )

    # Aspect
    if aspect:
        rule = str(aspect.get("rule") or "").strip()
        ratio = str(aspect.get("ratio") or "").strip()
        bit = rule or (f"ASPECT LOCK: keep {ratio} for the whole clip." if ratio else "")
        if bit and "ASPECT" not in text.upper() and ratio not in text:
            head.append(bit)

    # Set orientation (rotation / inconsistent room)
    if has_first_frame and "SET/ORIENTATION LOCK" not in text:
        head.append(set_orientation_lock_clause())

    # Occupancy + prop
    try:
        from jiuwenswarm.server.runtime.designer.experiments.cast_prop_locks import (
            occupancy_clause_for_clip,
        )

        occ = occupancy_clause_for_clip(
            shot if isinstance(shot, dict) else {},
            list(analysis.get("characters") or []),
        )
        if occ and "KEYFRAME CAST LOCK" not in text and "MUST STILL BE PRESENT" not in text:
            head.append(occ)
        prop_bit = str(
            cfg.get("prop_presentation")
            or (shot.get("prop_presentation") if isinstance(shot, dict) else "")
            or ""
        ).strip()
        if prop_bit and "PROP/UI LOCK" not in text:
            head.append(prop_bit)
    except Exception:  # noqa: BLE001
        pass

    # Prior-clip handoff (style/cast continuity text only)
    try:
        from jiuwenswarm.server.runtime.designer.experiments.clip_prompt_handoff import (
            collect_prior_clip_prompts,
            handoff_clause_for_prompt,
        )

        if (
            "PREVIOUS CLIP HAD" not in text
            and "PRIOR CLIP CONTINUITY" not in text
            and "prior clip" not in text.lower()
        ):
            prior = collect_prior_clip_prompts(graph, shot_index=int(shot_index) or 0)
            if not prior:
                prev_act = str(cfg.get("previous_clip_action") or "").strip()
                prev_one = str(cfg.get("previous_clip_wan_prompt") or "").strip()
                if prev_act or prev_one:
                    prior = [
                        {
                            "node_id": str(cfg.get("previous_clip_node_id") or ""),
                            "shot_index": int(
                                cfg.get("previous_clip_shot_index") or (int(shot_index) or 1) - 1
                            ),
                            "shot_action": prev_act or prev_one[:220],
                            "speech_line": str(cfg.get("previous_clip_speech") or ""),
                        }
                    ]
            handoff = handoff_clause_for_prompt(
                prior,
                this_shot_index=int(shot_index) or 0,
                this_action=str(cfg.get("shot_action") or cfg.get("character_action") or ""),
                this_camera=str(cfg.get("camera") or ""),
                this_speech=str(cfg.get("speech_line") or ""),
                already_done=[
                    str(x) for x in (cfg.get("already_done") or []) if str(x)
                ],
            )
            if handoff:
                head.append(handoff)
    except Exception:  # noqa: BLE001
        pass

    # Clothing must re-state at Wan call time (leaf agents often drop wardrobe tokens).
    try:
        from jiuwenswarm.server.runtime.designer.experiments.clothing_lock import (
            clothing_lock_clause,
            ensure_cfg_clothing_lock,
        )

        costume = ensure_cfg_clothing_lock(cfg, characters=list(analysis.get("characters") or []))
        if not costume:
            identity = cfg.get("identity_refs") if isinstance(cfg.get("identity_refs"), dict) else {}
            costume = str(cfg.get("costume_lock") or identity.get("costume_lock") or "").strip()
        if costume and "CLOTHING LOCK" not in text and "CLOTHING HOLD" not in text:
            cloth = clothing_lock_clause(costume, for_clip=True)
            if cloth:
                head.append(cloth)
        elif has_first_frame and "CLOTHING HOLD" not in text and "wardrobe" not in text.lower():
            from jiuwenswarm.server.runtime.designer.experiments.clothing_lock import (
                clothing_hold_rule,
            )

            head.append(
                clothing_hold_rule()
                + " Match Image 1 shirt/top, trousers/bottom, shoes, and accessories exactly."
            )
    except Exception:  # noqa: BLE001
        pass

    try:
        from jiuwenswarm.server.runtime.designer.experiments.shot_staging_lock import (
            ensure_cfg_staging_locks,
        )

        staging = ensure_cfg_staging_locks(
            cfg, characters=list(analysis.get("characters") or [])
        )
        if staging and "STAGING LOCK" not in text:
            head.append(staging)
    except Exception:  # noqa: BLE001
        pass

    if not head:
        return text[:6000]
    return ("\n\n".join(head) + "\n\n" + text).strip()[:6000]
