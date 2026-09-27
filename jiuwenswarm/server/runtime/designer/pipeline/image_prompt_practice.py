# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Positive still prompts for Qwen / DashScope / MiniMax image backends.

Locks live on node config. The image API body is a concise positive description —
no LOCK banners, forbid lists, or worked examples. Domain-agnostic.
"""

from __future__ import annotations

import re
from typing import Any

_BAD = re.compile(
    r"(?i)(\bforbid\b|\bforbidden\b|\bdo not\b|\bdon't\b|\bnever\b|"
    r"\bmust not\b|\bno people\b|\bno faces\b|\bno bodies\b|"
    r"\bstyle lock\b|\bspatial lock\b|\baspect lock\b|\btime of day lock\b|"
    r"\bclothing lock\b|\bstaging lock\b|\bfor example\b|\be\.g\.\b)"
)


def _cfg(cfg: dict[str, Any] | None) -> dict[str, Any]:
    return cfg if isinstance(cfg, dict) else {}


def _style_look(cfg: dict[str, Any]) -> str:
    style = cfg.get("style_lock") if isinstance(cfg.get("style_lock"), dict) else {}
    look = str(style.get("look") or style.get("medium") or "").strip()
    look = re.split(r"\s+[—–-]\s+|\bnever\b|\bno style\b", look, maxsplit=1, flags=re.I)[0]
    return look.strip(" .;") or "photoreal cinematic"


def _aspect_phrase(cfg: dict[str, Any], graph: dict[str, Any] | None) -> str:
    aspect = cfg.get("aspect_lock") if isinstance(cfg.get("aspect_lock"), dict) else {}
    if not aspect and isinstance(graph, dict):
        meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
        aspect = meta.get("aspect_lock") if isinstance(meta.get("aspect_lock"), dict) else {}
    ratio = str((aspect or {}).get("ratio") or "").strip()
    size = str(cfg.get("image_size") or (aspect or {}).get("image_size") or "").strip()
    bits = []
    if ratio:
        bits.append(f"{ratio} framing")
    if size:
        bits.append(f"size {size}")
    return ", ".join(bits)


def _tod_phrase(cfg: dict[str, Any]) -> str:
    tod = cfg.get("time_of_day_lock") if isinstance(cfg.get("time_of_day_lock"), dict) else {}
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    label = str((tod or {}).get("time_of_day") or bible.get("time_of_day") or "").strip()
    lighting = str((tod or {}).get("lighting") or bible.get("lighting") or "").strip()
    lighting = re.split(r"(?i)\b(?:do not|don't|never|forbid)\b", lighting, maxsplit=1)[0]
    lighting = lighting.strip(" ,;.—-")
    if label and label.lower() != "unspecified":
        if lighting and label.lower() in lighting.lower():
            return lighting.rstrip(".") + "."
        if lighting:
            return f"It is {label}; {lighting.rstrip('.')}."
        return f"It is {label}."
    if lighting:
        return lighting.rstrip(".") + "."
    return ""


def _scene_phrase(cfg: dict[str, Any]) -> str:
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    place = str(bible.get("scene_name") or bible.get("place") or "").strip()
    if place:
        return place.split(".")[0].strip()[:120]
    spatial = cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else {}
    for key in ("architecture", "setting", "static_rule"):
        val = str((spatial or {}).get(key) or "").strip()
        if val:
            return val[:160]
    return "the empty room"


def _props_phrase(cfg: dict[str, Any]) -> str:
    bible = cfg.get("scene_specs") if isinstance(cfg.get("scene_specs"), dict) else {}
    objects = bible.get("objects") if isinstance(bible.get("objects"), list) else []
    props = [str(x).strip() for x in objects[:6] if str(x).strip() and not _BAD.search(str(x))]
    return ", ".join(props)


def looks_like_lock_essay(prompt: str) -> bool:
    text = str(prompt or "")
    if not text.strip():
        return True
    if _BAD.search(text):
        return True
    if re.search(r"(?i)\b(?:STYLE|SPATIAL|ASPECT|TIME OF DAY|CLOTHING|STAGING)\s+LOCK\b", text):
        return True
    return False


def compose_scene_specs_prompt(
    *,
    cfg: dict[str, Any] | None,
    graph: dict[str, Any] | None = None,
    seed: str = "",
) -> str:
    """Positive empty-environment plate for the image model."""
    cfg = _cfg(cfg)
    place = _scene_phrase(cfg)
    tod = _tod_phrase(cfg)
    props = _props_phrase(cfg)
    look = _style_look(cfg)
    aspect = _aspect_phrase(cfg, graph)
    bits = [
        f"Empty scene specs of {place}: furniture, walls, windows, light, and props only.",
        "Clear establishing view of the room as a single photograph.",
    ]
    if tod:
        bits.append(tod if tod.endswith(".") else tod + ".")
    if props:
        bits.append(f"Visible props include {props}.")
    spatial = cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else {}
    arch = str((spatial or {}).get("architecture") or "").strip()
    if arch and arch.casefold() not in place.casefold():
        clean = re.split(r"(?i)\b(?:do not|don't|never|forbid)\b", arch, maxsplit=1)[0]
        clean = clean.strip(" ,;.—-")
        if clean and not _BAD.search(clean):
            bits.append(clean.rstrip(".") + ".")
    if look and not _BAD.search(look):
        bits.append(f"{look}.")
    if aspect:
        bits.append(f"Framing: {aspect}.")
    bits.append("One clear image.")
    # Optional seed: only keep positive fragments that add place detail.
    seed_text = str(seed or "").strip()
    if seed_text and not looks_like_lock_essay(seed_text) and len(seed_text) < 400:
        bits.insert(1, seed_text.rstrip(".") + ".")
    text = " ".join(b for b in bits if b)
    return re.sub(r"\s+", " ", text).strip()[:2200]


def compose_character_sheet_prompt(
    *,
    cfg: dict[str, Any] | None,
    graph: dict[str, Any] | None = None,
    seed: str = "",
) -> str:
    """Positive one-person studio sheet for the image model."""
    del graph
    cfg = _cfg(cfg)
    name = str(
        cfg.get("character_name")
        or (cfg.get("character_names") or [None])[0]
        or cfg.get("character_id")
        or "the character"
    ).strip()
    costume = str(cfg.get("costume_lock") or "").strip()
    costume = re.split(r"(?i)\b(?:do not|don't|never|forbid)\b", costume, maxsplit=1)[0]
    costume = costume.strip(" ,;.—-")[:200]
    look = _style_look(cfg)
    aspect = _aspect_phrase(cfg, None)
    bits = [
        f"One person only: {name}, full or three-quarter body on a plain empty studio backdrop.",
        "Solid neutral background, identity and costume only.",
    ]
    if costume and not _BAD.search(costume):
        bits.append(f"Wearing {costume}.")
    if look and not _BAD.search(look):
        bits.append(f"{look}.")
    if aspect:
        bits.append(f"Framing: {aspect}.")
    bits.append("One clear image.")
    seed_text = str(seed or "").strip()
    if seed_text and not looks_like_lock_essay(seed_text) and len(seed_text) < 300:
        bits.insert(1, seed_text.rstrip(".") + ".")
    text = " ".join(b for b in bits if b)
    return re.sub(r"\s+", " ", text).strip()[:2200]


def ensure_still_tool_prompt(
    prompt: str,
    *,
    role: str,
    cfg: dict[str, Any] | None,
    graph: dict[str, Any] | None = None,
) -> tuple[str, list[str]]:
    """Rewrite lock essays into positive still prompts before the image API."""
    cfg = _cfg(cfg)
    role_l = str(role or cfg.get("role") or "").lower()
    notes: list[str] = []
    text = str(prompt or "").strip()
    if role_l in {"scene"} or "scene" in role_l:
        if looks_like_lock_essay(text) or not text:
            text = compose_scene_specs_prompt(cfg=cfg, graph=graph, seed="")
            notes.append("still_rewrote_scene_plate")
        else:
            # Soft fill missing ToD / style as positive prose.
            pl = text.lower()
            tod = _tod_phrase(cfg)
            if tod and not any(w in pl for w in ("night", "dawn", "dusk", "morning", "evening", "daylight", "it is ")):
                if tod.lower() not in pl:
                    text = (text.rstrip(".") + ". " + tod).strip()
                    notes.append("still_cover_tod")
            look = _style_look(cfg)
            if look and look.lower() not in pl and "photoreal" not in pl:
                text = (text.rstrip(".") + f". {look}.").strip()
                notes.append("still_cover_style")
    elif role_l in {"character", "character_design"} or "character" in role_l:
        if looks_like_lock_essay(text) or not text:
            text = compose_character_sheet_prompt(cfg=cfg, graph=graph, seed="")
            notes.append("still_rewrote_character_sheet")
    return text[:2200], notes
