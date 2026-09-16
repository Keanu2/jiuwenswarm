# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Film-wide axis / occupancy / aspect locks — domain-agnostic.

Fixes observed failures without scene-specific rules:
  - identity under occlusion (partially hidden person must keep sex/age/hair)
  - 180-degree screen L/R (camera pan must not flip who sits left vs right)
  - same aspect ratio on every still and clip
  - landmark placements stay put; extras do not spawn/vanish
LLM may refine heuristic locks; heuristics always provide a floor.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

_MALE_RE = re.compile(
    r"\b(?:boy|son|father|dad|man|male|brother|husband|gentleman|"
    r"young\s+man)\b",
    re.I,
)
# Note: bare "he/him/his" omitted — "his partner" was flipping partner to male.
_FEMALE_RE = re.compile(
    r"\b(?:girl|daughter|mother|mom|woman|female|she|her|hers|sister|wife|lady|"
    r"partner|girlfriend|dress|gown|blouse)\b",
    re.I,
)
_CHILD_RE = re.compile(r"\b(?:child|kid|boy|girl|son|daughter|toddler|infant)\b", re.I)
_ADULT_RE = re.compile(r"\b(?:father|mother|dad|mom|man|woman|adult|teen)\b", re.I)
_CROSS_RE = re.compile(
    r"\b(?:cross(?:es|ing)?|walks?\s+to\s+the\s+other|swaps?\s+sides|moves?\s+left|moves?\s+right)\b",
    re.I,
)
_VERTICAL_RE = re.compile(
    r"\b(?:vertical|9\s*[:x]\s*16|tiktok|reels|shorts|portrait\s+video)\b", re.I
)
_SQUARE_RE = re.compile(r"\b(?:1\s*[:x]\s*1|square\s+(?:frame|video))\b", re.I)


def infer_aspect_lock(prompt: str) -> dict[str, str]:
    """One aspect for every sheet, keyframe, and clip in the film."""
    text = prompt or ""
    if _VERTICAL_RE.search(text):
        return {
            "ratio": "9:16",
            # ~1K budget (portrait): keep short side near 480–576, not 2K.
            "image_size": "576x1024",
            "video_size": "480*854",
            "video_resolution": "480P",
            "rule": "EVERY still and clip MUST be 9:16 portrait at ~1K/480P. Do not letterbox, crop to 16:9, or square-crop.",
        }
    if _SQUARE_RE.search(text):
        return {
            "ratio": "1:1",
            "image_size": "1K",
            "video_size": "480*480",
            "video_resolution": "480P",
            "rule": "EVERY still and clip MUST be 1:1 at 1K/480P. Do not change aspect mid-film.",
        }
    return {
        "ratio": "16:9",
        # ~1K budget (landscape): prefer 1024-wide over 1280/2K.
        "image_size": "1024x576",
        "video_size": "854*480",
        "video_resolution": "480P",
        "rule": "EVERY still and clip MUST be 16:9 landscape at ~1K/480P. Do not square-crop or switch to 9:16.",
    }


def infer_demographics(ch: dict[str, Any]) -> dict[str, str]:
    blob = " ".join(
        str(ch.get(k) or "") for k in ("name", "role", "description", "costume_lock")
    )
    # Drop possessive "his/her X" so "his partner" does not mark the partner male.
    blob_sex = re.sub(r"\b(?:his|her|their)\s+", " ", blob, flags=re.I)
    male = bool(_MALE_RE.search(blob_sex))
    female = bool(_FEMALE_RE.search(blob_sex))
    if male and not female:
        sex = "male"
    elif female and not male:
        sex = "female"
    else:
        sex = "unspecified"
    if _CHILD_RE.search(blob) and not _ADULT_RE.search(blob):
        age = "child"
    elif re.search(r"\bteen", blob, re.I):
        age = "teen"
    else:
        age = "adult"
    return {
        "sex": sex,
        "age_band": age,
        "occlusion_rule": (
            f"sex={sex}, age={age} even if body is cropped, behind furniture, "
            "out of focus, or only a silhouette — never recast as a different sex/age"
        ),
    }


def _zone_to_axis(zone: str) -> str:
    z = (zone or "").lower()
    if "left" in z:
        return "screen_left"
    if "right" in z:
        return "screen_right"
    if "back" in z or "bg" in z:
        return "background_center"
    return "screen_center"


def stamp_axis_locks(analysis: dict[str, Any]) -> dict[str, Any]:
    """Heuristic 180-degree + occupancy + landmark floor on analysis (mutates copy)."""
    out = analysis if isinstance(analysis, dict) else {}
    characters = [c for c in (out.get("characters") or []) if isinstance(c, dict)]
    shots = [s for s in (out.get("shots") or []) if isinstance(s, dict)]
    by_id = {str(c.get("id")): c for c in characters if c.get("id")}

    for ch in characters:
        demo = infer_demographics(ch)
        attrs = dict(ch.get("identity_attrs") or {}) if isinstance(ch.get("identity_attrs"), dict) else {}
        attrs.update({k: v for k, v in demo.items() if v})
        ch["identity_attrs"] = attrs

    canonical_axis: dict[str, str] = {}
    landmarks: list[str] = []
    already_gone: set[str] = set()
    for shot in shots:
        blk = shot.get("blocking") if isinstance(shot.get("blocking"), dict) else {}
        lm = str(blk.get("landmark") or "").strip()
        if lm and lm not in landmarks:
            landmarks.append(lm)
        on = [str(x) for x in (shot.get("character_ids") or []) if str(x).strip()]
        axis_map: dict[str, str] = {}
        for p in blk.get("positions") or []:
            if not isinstance(p, dict):
                continue
            cid = str(p.get("character_id") or "")
            if not cid:
                continue
            axis = _zone_to_axis(str(p.get("zone") or ""))
            action = str(shot.get("action") or "")
            if cid in canonical_axis and not _CROSS_RE.search(action):
                axis = canonical_axis[cid]
            else:
                canonical_axis[cid] = axis
            axis_map[cid] = axis
        for cid in on:
            if cid not in axis_map:
                axis_map[cid] = canonical_axis.get(cid, "screen_center")
                canonical_axis.setdefault(cid, axis_map[cid])
        shot["screen_axis"] = axis_map
        prev_bits = [
            f"{(by_id.get(cid) or {}).get('name') or cid}={axis_map[cid]}"
            for cid in axis_map
        ]
        shot["screen_positions"] = (
            (str(shot.get("screen_positions") or "") + " ").strip()
            + (" SCREEN AXIS (180-rule, pan-stable): " + "; ".join(prev_bits) if prev_bits else "")
        )[:280]
        exiting_now = [
            str(x)
            for x in (shot.get("exiting_character_ids") or shot.get("exiting") or [])
            if str(x)
        ]
        # Already-gone from earlier beats only. Current leavers still appear while exiting.
        # Draw on_screen / featured only — not the whole film cast.
        off = [
            str(x)
            for x in (shot.get("offscreen") or shot.get("off_screen_cast_ids") or [])
            if str(x)
        ]
        must = [c for c in (on or []) if c not in already_gone]
        prev_occ = shot.get("occupancy") if isinstance(shot.get("occupancy"), dict) else {}
        shot["occupancy"] = {
            **prev_occ,
            "must_appear": must or list(prev_occ.get("must_appear") or []),
            "offscreen": off,
            "must_not_appear": sorted(set(already_gone) | {c for c in off if c not in must}),
            "featured": list(on) or list(prev_occ.get("featured") or []),
            "rule": (
                "OCCUPANCY: must_appear / on_screen are drawn; offscreen stay out of frame; "
                "cast from other scenes (must_not_appear) never appear. Same setting_id keeps "
                "architecture from the compose keyframe; only camera + cast_actions change."
            ),
        }
        already_gone.update(exiting_now)

    spatial = dict(out.get("spatial_lock") or {}) if isinstance(out.get("spatial_lock"), dict) else {}
    if landmarks:
        spatial["landmarks"] = landmarks[:8]
        spatial["landmark_rule"] = (
            "Each named landmark keeps its place vs the scene plate "
            "(front/back/left/right). Camera move reframes — it does not teleport furniture."
        )
    out["spatial_lock"] = spatial
    out["axis_lock"] = {
        "canonical_screen_axis": canonical_axis,
        "landmarks": landmarks[:8],
        "rule": (
            "180-degree: a person on screen_left stays screen_left across pans/cuts "
            "unless the beat says they cross. Occupancy: no random appear/disappear."
        ),
    }
    out["characters"] = characters
    out["shots"] = shots
    return out


def format_style_clause(analysis: dict[str, Any] | None) -> str:
    """Film-wide style lock line for stills and clips."""
    analysis = analysis if isinstance(analysis, dict) else {}
    style = analysis.get("style_lock") if isinstance(analysis.get("style_lock"), dict) else {}
    if not style:
        return ""
    try:
        from jiuwenswarm.server.runtime.designer.media_model_playbook import style_lock_clause

        return (style_lock_clause(style) or "").strip()
    except Exception:  # noqa: BLE001
        look = str(style.get("look") or style.get("medium") or "").strip()
        return f"STYLE LOCK (film-wide): {look[:280]}" if look else ""


def format_axis_clause(shot: dict[str, Any] | None, analysis: dict[str, Any] | None) -> str:
    shot = shot if isinstance(shot, dict) else {}
    analysis = analysis if isinstance(analysis, dict) else {}
    aspect = analysis.get("aspect_lock") if isinstance(analysis.get("aspect_lock"), dict) else {}
    axis = shot.get("screen_axis") if isinstance(shot.get("screen_axis"), dict) else {}
    occ = shot.get("occupancy") if isinstance(shot.get("occupancy"), dict) else {}
    spatial = analysis.get("spatial_lock") if isinstance(analysis.get("spatial_lock"), dict) else {}
    chars = {str(c.get("id")): c for c in (analysis.get("characters") or []) if isinstance(c, dict)}
    bits: list[str] = []
    style_bit = format_style_clause(analysis)
    if style_bit and "STYLE LOCK" in style_bit:
        bits.append(style_bit)
    if aspect.get("rule"):
        bits.append(
            f"ASPECT LOCK: {aspect.get('ratio')} — {aspect.get('rule')} "
            f"(image_size={aspect.get('image_size') or ''}; "
            f"video={aspect.get('video_size') or ''} @ {aspect.get('video_resolution') or '480P'})."
        )
    if axis:
        named = []
        for cid, side in axis.items():
            nm = str((chars.get(cid) or {}).get("name") or cid)
            attrs = (chars.get(cid) or {}).get("identity_attrs") or {}
            sex = str(attrs.get("sex") or "")
            age = str(attrs.get("age_band") or "")
            demo = f"{sex}/{age}" if sex or age else ""
            named.append(f"{nm}@{side}" + (f" ({demo})" if demo else ""))
        bits.append(
            "SCREEN AXIS (do not flip on pan/cut): " + "; ".join(named)
        )
    must = [str((chars.get(x) or {}).get("name") or x) for x in (occ.get("must_appear") or [])]
    no = [str((chars.get(x) or {}).get("name") or x) for x in (occ.get("must_not_appear") or [])]
    if must:
        bits.append("OCCUPANCY must appear: " + ", ".join(must))
    if no:
        bits.append("OCCUPANCY must NOT appear: " + ", ".join(no))
    bits.append(
        str(occ.get("rule") or "No extra named people; no dropping listed leads.")
    )
    lms = spatial.get("landmarks") or []
    if lms:
        bits.append(
            "LANDMARKS frozen vs scene plate: "
            + ", ".join(str(x) for x in lms[:6])
            + ". "
            + str(spatial.get("landmark_rule") or "")
        )
    bits.append(
        "OCCLUSION: a partly hidden person is still the same identity "
        "(sex/age/hair/wardrobe) as the solo sheet — never recast."
    )
    return " ".join(b for b in bits if str(b).strip())[:900]


def apply_aspect_to_node_config(cfg: dict[str, Any], aspect: dict[str, Any] | None) -> None:
    if not isinstance(cfg, dict) or not isinstance(aspect, dict):
        return
    role = str(cfg.get("role") or "")
    if role in {"character", "character_design", "scene", "frame", "keyframe"}:
        if aspect.get("image_size"):
            cfg["image_size"] = aspect["image_size"]
    if role == "clip":
        if aspect.get("video_size"):
            cfg["video_size"] = aspect["video_size"]
        if aspect.get("video_resolution"):
            cfg["video_resolution"] = aspect["video_resolution"]


async def llm_refine_axis_locks(
    prompt: str,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    """Ask the chat LLM to fill sex/age, screen L/R, occupancy, landmarks. Merge onto analysis."""
    from jiuwenswarm.server.runtime.designer.model_tools import call_model_tool, llm_available

    if not llm_available():
        return stamp_axis_locks(deepcopy(analysis))
    base = stamp_axis_locks(deepcopy(analysis))
    payload = {
        "user_prompt": (prompt or "")[:2500],
        "characters": [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "description": str(c.get("description") or "")[:200],
                "identity_attrs": c.get("identity_attrs"),
            }
            for c in (base.get("characters") or [])
            if isinstance(c, dict)
        ],
        "shots": [
            {
                "shot_index": s.get("shot_index"),
                "action": str(s.get("action") or "")[:240],
                "character_ids": s.get("character_ids"),
                "camera": s.get("camera"),
                "blocking": s.get("blocking"),
                "screen_axis": s.get("screen_axis"),
                "occupancy": s.get("occupancy"),
                "setting_id": s.get("setting_id"),
            }
            for s in (base.get("shots") or [])
            if isinstance(s, dict)
        ],
        "spatial_lock": base.get("spatial_lock"),
    }
    system = (
        "You are a script supervisor. Output JSON only. Domain-agnostic: no genre cliches. "
        "Lock: (1) each character sex male|female|unspecified and age_band child|teen|adult from text, "
        "(2) per-shot screen_axis map character_id -> screen_left|screen_center|screen_right "
        "that obeys the 180-degree rule across pans (do not flip L/R unless action says they cross), "
        "(3) occupancy must_appear / must_not_appear ids, "
        "(4) landmark_placements [{name, where}] relative to the set (front/back/left/right), "
        "(5) occlusion_rule per character. "
        'Schema: {"characters":[{"id":"char_1","sex":"male","age_band":"adult",'
        '"occlusion_rule":"..."}],'
        '"shots":[{"shot_index":1,"screen_axis":{"char_1":"screen_left"},'
        '"must_appear":["char_1"],"must_not_appear":[]}],'
        '"landmark_placements":[{"name":"...","where":"..."}]}'
    )
    try:
        result = await call_model_tool(
            prompt=json.dumps(payload, ensure_ascii=False)[:7000],
            system=system,
            optimize_for="quality",
            max_tokens=1200,
        )
        text = str(result.get("text") or "")
        parsed = _extract_json_obj(text)
    except Exception:  # noqa: BLE001
        return base
    if not isinstance(parsed, dict):
        return base
    by_id = {str(c.get("id")): c for c in (base.get("characters") or []) if isinstance(c, dict)}
    for row in parsed.get("characters") or []:
        if not isinstance(row, dict):
            continue
        ch = by_id.get(str(row.get("id") or ""))
        if not ch:
            continue
        attrs = dict(ch.get("identity_attrs") or {})
        if row.get("sex") in {"male", "female", "unspecified"}:
            attrs["sex"] = str(row["sex"])
        if row.get("age_band") in {"child", "teen", "adult"}:
            attrs["age_band"] = str(row["age_band"])
        if row.get("occlusion_rule"):
            attrs["occlusion_rule"] = str(row["occlusion_rule"])[:240]
        ch["identity_attrs"] = attrs
    shots_by_i = {
        int(s.get("shot_index") or 0): s
        for s in (base.get("shots") or [])
        if isinstance(s, dict)
    }
    for row in parsed.get("shots") or []:
        if not isinstance(row, dict):
            continue
        shot = shots_by_i.get(int(row.get("shot_index") or 0))
        if not shot:
            continue
        axis = row.get("screen_axis") if isinstance(row.get("screen_axis"), dict) else {}
        if axis:
            shot["screen_axis"] = {str(k): str(v) for k, v in axis.items() if str(v)}
        occ = dict(shot.get("occupancy") or {})
        if isinstance(row.get("must_appear"), list):
            occ["must_appear"] = [str(x) for x in row["must_appear"] if str(x)]
        if isinstance(row.get("must_not_appear"), list):
            occ["must_not_appear"] = [str(x) for x in row["must_not_appear"] if str(x)]
        shot["occupancy"] = occ
    places = parsed.get("landmark_placements")
    if isinstance(places, list) and places:
        spatial = dict(base.get("spatial_lock") or {})
        spatial["landmark_placements"] = [
            {"name": str(p.get("name") or ""), "where": str(p.get("where") or "")}
            for p in places
            if isinstance(p, dict) and str(p.get("name") or "").strip()
        ][:8]
        base["spatial_lock"] = spatial
    base["axis_lock_source"] = "llm+heuristic"
    return base


def _extract_json_obj(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None


def apply_axis_locks_to_graph(graph: dict[str, Any]) -> list[str]:
    """Stamp aspect + style + axis clauses onto scene/frame/clip configs."""
    notes: list[str] = []
    meta = dict(graph.get("metadata") or {})
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    analysis = stamp_axis_locks(analysis)
    aspect = analysis.get("aspect_lock") if isinstance(analysis.get("aspect_lock"), dict) else {}
    if not aspect:
        aspect = infer_aspect_lock(str(graph.get("description") or meta.get("user_prompt") or ""))
        analysis["aspect_lock"] = aspect
    style = analysis.get("style_lock") if isinstance(analysis.get("style_lock"), dict) else {}
    if not style:
        style = meta.get("style_lock") if isinstance(meta.get("style_lock"), dict) else {}
    if not style:
        try:
            from jiuwenswarm.server.runtime.designer.media_model_playbook import default_style_lock

            style = default_style_lock(
                str(graph.get("description") or meta.get("user_prompt") or ""),
                str((analysis.get("scene") or {}).get("description") or ""),
            )
            analysis["style_lock"] = style
        except Exception:  # noqa: BLE001
            style = {}
    shots = {
        int(s.get("shot_index") or 0): s
        for s in (analysis.get("shots") or [])
        if isinstance(s, dict)
    }
    clause_global = format_axis_clause({}, analysis)
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = dict(node.get("config") or {})
        role = str(
            cfg.get("role")
            or node.get("pipeline")
            or node.get("type")
            or ""
        ).lower()
        # Normalize role aliases used by graph builders.
        if "frame" in role or "keyframe" in role:
            cfg.setdefault("role", "keyframe" if "keyframe" in role else "frame")
            role = str(cfg.get("role") or role)
        elif "clip" in role or role == "video":
            cfg.setdefault("role", "clip")
            role = "clip"
        elif "character" in role:
            cfg.setdefault("role", "character_design")
            role = "character_design"
        elif "scene" in role:
            cfg.setdefault("role", "scene")
            role = "scene"
        apply_aspect_to_node_config(cfg, aspect)
        idx = int(cfg.get("shot_index") or 0)
        clause = format_axis_clause(shots.get(idx), analysis) if idx else clause_global
        if role in {"scene", "frame", "keyframe", "clip", "character", "character_design"} and clause:
            gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
            prev = str(gen.get("prompt") or cfg.get("prompt") or "")
            needs = (
                "ASPECT LOCK" not in prev
                or "STYLE LOCK" not in prev
                or ("SCREEN AXIS" not in prev and "OCCUPANCY" not in prev)
            )
            if needs and ("ASPECT LOCK" not in prev or "STYLE LOCK" not in prev):
                stamped = (prev + "\n" + clause).strip()[:6000]
                if role in {"frame", "keyframe", "clip"}:
                    gen["prompt"] = stamped
                    cfg["generate"] = gen
                else:
                    cfg["prompt"] = stamped
                notes.append(f"axis:{node.get('id')}")
            cfg["axis_clause"] = clause[:900]
            if idx and shots.get(idx):
                cfg["screen_axis"] = shots[idx].get("screen_axis")
                cfg["occupancy"] = shots[idx].get("occupancy")
        cfg["aspect_lock"] = aspect
        if style:
            cfg["style_lock"] = style
        node["config"] = cfg
    meta["script_analysis"] = analysis
    meta["aspect_lock"] = aspect
    if style:
        meta["style_lock"] = style
    meta["axis_lock"] = analysis.get("axis_lock")
    graph["metadata"] = meta
    return notes
