# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Storyboard start/end state — sole continuity authority (domain-agnostic).

Each shot is a closed window:
  start_state → action/camera/speech → end_state
Same-setting chain: shot N start_state must match shot N-1 end_state.
No prior-clip Wan text required.
"""

from __future__ import annotations

import re
from typing import Any


def _short(text: str, *, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())[:limit]


def _as_state(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        return {"pose": raw.strip()[:280]}
    return {}


def normalize_shot_state(raw: Any) -> dict[str, Any]:
    """Normalize start_state / end_state to a compact structured dict."""
    st = _as_state(raw)
    out: dict[str, Any] = {}
    pose = _short(str(st.get("pose") or st.get("blocking") or st.get("holds") or ""), limit=280)
    if pose:
        out["pose"] = pose
    seats = st.get("seats") if isinstance(st.get("seats"), dict) else {}
    if not seats and isinstance(st.get("seat_anchors"), dict):
        seats = st["seat_anchors"]
    if seats:
        cleaned: dict[str, Any] = {}
        for cid, anchor in list(seats.items())[:12]:
            key = str(cid or "").strip()
            if not key:
                continue
            if isinstance(anchor, dict):
                cleaned[key] = {
                    str(k): str(v)[:120]
                    for k, v in anchor.items()
                    if str(k).strip() and str(v).strip()
                }
            elif str(anchor).strip():
                cleaned[key] = {"place": str(anchor).strip()[:120]}
        if cleaned:
            out["seats"] = cleaned
    facing = _short(str(st.get("facing") or st.get("gaze") or ""), limit=160)
    if facing:
        out["facing"] = facing
    camera = _short(str(st.get("camera") or ""), limit=120)
    if camera:
        out["camera"] = camera
    exited = [
        str(x).strip()
        for x in (st.get("exited") or st.get("exited_ids") or [])
        if str(x).strip()
    ]
    if exited:
        out["exited"] = exited[:12]
    speech_done = _short(str(st.get("speech_done") or st.get("speech") or ""), limit=160)
    if speech_done:
        out["speech_done"] = speech_done
    on_screen = [str(x).strip() for x in (st.get("on_screen") or []) if str(x).strip()]
    if on_screen:
        out["on_screen"] = on_screen[:12]
    offscreen = [str(x).strip() for x in (st.get("offscreen") or []) if str(x).strip()]
    if offscreen:
        out["offscreen"] = offscreen[:12]
    return out


def _infer_start_from_shot(shot: dict[str, Any]) -> dict[str, Any]:
    staging = shot.get("staging") if isinstance(shot.get("staging"), dict) else {}
    pose = _short(
        str(
            shot.get("start_pose")
            or staging.get("positioning_lock")
            or shot.get("blocking")
            or ""
        ),
        limit=280,
    )
    seats = shot.get("seat_anchors") if isinstance(shot.get("seat_anchors"), dict) else {}
    out: dict[str, Any] = {}
    if pose:
        out["pose"] = pose
    if seats:
        out["seats"] = seats
    camera = _short(str(shot.get("camera") or ""), limit=120)
    if camera:
        out["camera"] = camera
    on_screen = [str(x) for x in (shot.get("on_screen") or []) if str(x)]
    if on_screen:
        out["on_screen"] = on_screen
    offscreen = [str(x) for x in (shot.get("offscreen") or []) if str(x)]
    if offscreen:
        out["offscreen"] = offscreen
    return out


def _infer_end_from_shot(shot: dict[str, Any]) -> dict[str, Any]:
    action = _short(str(shot.get("action") or shot.get("keyframe_prompt") or ""), limit=220)
    speech = _short(str(shot.get("speech_line") or ""), limit=160)
    exiting = [
        str(x)
        for x in (shot.get("exiting_character_ids") or shot.get("exiting") or [])
        if str(x)
    ]
    out: dict[str, Any] = {}
    if action:
        out["pose"] = f"After this beat: {action}"
    if speech:
        out["speech_done"] = speech
    if exiting:
        out["exited"] = exiting
    # Remaining on_screen after exits.
    on_screen = [str(x) for x in (shot.get("on_screen") or []) if str(x)]
    remain = [c for c in on_screen if c not in set(exiting)]
    if remain:
        out["on_screen"] = remain
    offscreen = [str(x) for x in (shot.get("offscreen") or []) if str(x)]
    if offscreen or exiting:
        out["offscreen"] = list(dict.fromkeys(offscreen + exiting))
    camera = _short(str(shot.get("camera") or ""), limit=120)
    if camera:
        out["camera"] = camera
    seats = shot.get("seat_anchors") if isinstance(shot.get("seat_anchors"), dict) else {}
    if seats:
        # Drop exited seats.
        out["seats"] = {
            k: v for k, v in seats.items() if str(k) not in set(exiting)
        }
    return out


def ensure_shot_start_end_states(
    shots: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Fill start_state/end_state and chain same-setting start from prior end."""
    out: list[dict[str, Any]] = []
    last_end_by_setting: dict[str, dict[str, Any]] = {}
    for raw in shots or []:
        if not isinstance(raw, dict):
            continue
        shot = dict(raw)
        sid = str(shot.get("setting_id") or "set_1").strip() or "set_1"
        start = normalize_shot_state(shot.get("start_state"))
        end = normalize_shot_state(shot.get("end_state"))
        if not start:
            # Prefer previous end in same setting.
            prior_end = last_end_by_setting.get(sid) or {}
            start = dict(prior_end) if prior_end else _infer_start_from_shot(shot)
        if not end:
            end = _infer_end_from_shot(shot)
        # Carry seats from start into end when end omitted seats.
        if start.get("seats") and not end.get("seats"):
            exited = set(end.get("exited") or [])
            end["seats"] = {
                k: v for k, v in (start.get("seats") or {}).items() if k not in exited
            }
        shot["start_state"] = start
        shot["end_state"] = end
        # Seat anchors for this clip open = start seats.
        if isinstance(start.get("seats"), dict) and start["seats"]:
            shot["seat_anchors"] = dict(start["seats"])
        last_end_by_setting[sid] = end
        out.append(shot)
    return out


def validate_storyboard_state_chain(
    shots: list[dict[str, Any]] | None,
) -> list[str]:
    """Return domain-agnostic validation notes (empty = ok)."""
    notes: list[str] = []
    last_end_by_setting: dict[str, dict[str, Any]] = {}
    for shot in shots or []:
        if not isinstance(shot, dict):
            continue
        idx = int(shot.get("shot_index") or 0) or "?"
        sid = str(shot.get("setting_id") or "").strip() or "set_1"
        start = normalize_shot_state(shot.get("start_state"))
        end = normalize_shot_state(shot.get("end_state"))
        if not start:
            notes.append(f"shot{idx}:missing_start_state")
        if not end:
            notes.append(f"shot{idx}:missing_end_state")
        prior = last_end_by_setting.get(sid)
        if prior and start:
            # Soft structural check: prior exited should not be on_screen at start
            # unless storyboard returned them.
            prior_exited = {str(x) for x in (prior.get("exited") or []) if str(x)}
            start_on = {str(x) for x in (start.get("on_screen") or shot.get("on_screen") or []) if str(x)}
            leaked = sorted(prior_exited & start_on)
            # Return is allowed — only flag if start explicitly lists them in exited too.
            start_exited = {str(x) for x in (start.get("exited") or []) if str(x)}
            bad = sorted(prior_exited & start_exited & start_on)
            if bad:
                notes.append(f"shot{idx}:exited_cast_marked_on_screen:{','.join(bad)}")
        last_end_by_setting[sid] = end or prior or {}
    return notes


def stamp_shot_states_on_clip_cfg(
    cfg: dict[str, Any],
    *,
    shot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy this storyboard row's start/end onto clip config."""
    out = dict(cfg or {})
    shot = shot if isinstance(shot, dict) else {}
    start = normalize_shot_state(shot.get("start_state") or out.get("start_state"))
    end = normalize_shot_state(shot.get("end_state") or out.get("end_state"))
    if start:
        out["start_state"] = start
        if isinstance(start.get("seats"), dict) and start["seats"]:
            out["seat_anchors"] = dict(start["seats"])
        holds = [str(x) for x in (out.get("pose_holds") or []) if str(x).strip()]
        pose = str(start.get("pose") or "").strip()
        if pose:
            hold = pose if pose.lower().startswith("already") else f"Opening hold: {pose}"
            if hold not in holds:
                holds.insert(0, hold[:200])
            out["pose_holds"] = holds[:8]
        facing = str(start.get("facing") or "").strip()
        if facing:
            hold = f"Opening facing: {facing}"
            if hold not in (out.get("pose_holds") or []):
                out.setdefault("pose_holds", [])
                out["pose_holds"] = [hold, *list(out.get("pose_holds") or [])][:8]
    if end:
        out["end_state"] = end
        # Exited after this shot — for next storyboard row, not prior-clip handoff.
        exited = [str(x) for x in (end.get("exited") or []) if str(x)]
        if exited:
            out["exiting_character_ids"] = exited
    # Storyboard row is authority — do not require prior clip wiring.
    out.pop("continuity_clip_node_id", None)
    out["previous_clip_handoff_ready"] = False
    return out


def start_end_story_lines(cfg: dict[str, Any] | None) -> list[str]:
    """Positive story-form lines from start_state / end_state for Wan bodies."""
    cfg = cfg if isinstance(cfg, dict) else {}
    lines: list[str] = []
    start = normalize_shot_state(cfg.get("start_state"))
    end = normalize_shot_state(cfg.get("end_state"))
    if start.get("pose"):
        lines.append(str(start["pose"]).rstrip(".") + ".")
    elif start.get("facing"):
        lines.append(f"Opening: facing {start['facing']}.")
    seats = start.get("seats") if isinstance(start.get("seats"), dict) else {}
    for cid, anchor in list(seats.items())[:4]:
        if isinstance(anchor, dict):
            place = anchor.get("place") or anchor.get("zone") or anchor.get("seat") or ""
            if place:
                lines.append(f"{cid} begins at {place}.")
        elif str(anchor).strip():
            lines.append(f"{cid} begins at {anchor}.")
    if end.get("pose") and end.get("pose") != start.get("pose"):
        lines.append(str(end["pose"]).rstrip(".") + ".")
    return lines[:6]
