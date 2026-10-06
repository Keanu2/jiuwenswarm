# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Analyze user prompts into cast, shots, scenes, and audio for smart graph build.

Prefer LLM when configured models are available; otherwise use general heuristics
(not scenario-specific templates).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_MAX_CHARS = 6
_MAX_SHOTS = 6
_MAX_SCENES = 3
# Keep under typical UI bootstrap budgets while still allowing a real LLM call.
_DEFAULT_LLM_TIMEOUT_SEC = 12.0

# Generic role nouns — not tied to any one story.
_ROLE_NOUNS = (
    "preacher|pastor|priest|minister|teacher|doctor|nurse|soldier|officer|"
    "courier|messenger|king|queen|prince|princess|knight|wizard|witch|"
    "chef|pilot|driver|farmer|scientist|engineer|artist|singer|dancer|"
    "detective|spy|robot|android|hero|villain|warrior|hunter|merchant|"
    "mother|father|parent|brother|sister|friend|stranger|leader|captain|"
    "boy|girl|child|kid|man|woman|person|guy|lady"
)


def _clamp_list(items: list[Any], limit: int) -> list[Any]:
    return items[: max(1, min(limit, len(items) or 1))]


def _title_case_label(raw: str) -> str:
    cleaned = re.sub(r"\s+", " ", raw.strip())
    if not cleaned:
        return "Lead"
    return cleaned[:1].upper() + cleaned[1:]


def _strip_prompt_filler(text: str) -> str:
    """Drop leading ask-phrases so storyboard actions are cinematic, not meta."""
    cleaned = re.sub(
        r"^(?:i\s+want(?:\s+the)?(?:\s+video)?(?:\s+of)?|please\s+(?:make|create)|"
        r"create(?:\s+a)?(?:\s+video)?(?:\s+of)?|make(?:\s+a)?(?:\s+video)?(?:\s+of)?)\s+",
        "",
        text.strip(),
        flags=re.I,
    )
    return cleaned.strip(" ,.")


def _contextual_character_name(role: str, clause: str, *, another: bool = False) -> str:
    """General labels from role + optional 'another' / motion cues (domain-agnostic)."""
    role_l = role.lower()
    cl = clause.lower()
    base = _title_case_label(role)
    if another or re.search(r"\b(?:another|second|other)\b", cl):
        if re.search(r"\b(?:leaves?|gets?\s+up|exits?|walks?\s+out|departs?)\b", cl):
            return f"{base} leaving"
        return f"{base} 2"
    return base


def _match_terms_for_character(name: str, description: str) -> list[str]:
    """Build general match phrases from the character name/description only.

    No domain-specific dictionaries (religion, occupations, etc.) — only lexical cues
    derived from this character's own text so heuristics stay scenario-agnostic.
    """
    name_l = name.lower().strip()
    # Ignore continuity tails after 'while' so they do not bleed into another subject.
    desc_l = re.split(r"\bwhile\b", (description or "").lower(), maxsplit=1)[0]
    terms: list[str] = []

    def add(*xs: str) -> None:
        for x in xs:
            x = x.strip().lower()
            if x and x not in terms and len(x) > 2:
                terms.append(x)

    stop = {
        "with", "from", "that", "this", "their", "there", "about", "while", "when",
        "then", "into", "onto", "have", "been", "were", "what", "which", "where",
        "your", "they", "them", "than", "also", "just", "only", "very", "some",
    }

    if name_l:
        add(name_l)
        for tok in re.split(r"\W+", name_l):
            if len(tok) > 2 and tok not in stop:
                add(tok)

    # Prefer multi-word snippets from the description (first ~12 content words).
    words = [w for w in re.split(r"\W+", desc_l) if len(w) > 2 and w not in stop]
    for i, w in enumerate(words[:12]):
        add(w)
        if i + 1 < len(words):
            add(f"{w} {words[i + 1]}")

    # Generic disambiguators for numbered / alternate subjects.
    if name_l.endswith(" 2") or "another" in desc_l or "leaving" in name_l:
        add("another", "second", "other")
        for verb in ("gets up", "get up", "leaves", "leaving", "exits", "enters", "walks"):
            if verb in desc_l or verb in name_l:
                add(verb)

    terms.sort(key=len, reverse=True)
    return terms


def _heuristic_characters(prompt: str) -> list[dict[str, str]]:
    """General cast extraction from role nouns / a|the X phrases."""
    text = prompt.strip()
    found: list[dict[str, Any]] = []
    used: set[str] = set()

    def add(name: str, desc: str) -> None:
        key = name.lower()
        if key in used or not name.strip():
            return
        used.add(key)
        found.append(
            {
                "id": f"char_{len(found) + 1}",
                "name": name,
                "description": desc[:300],
                "match_terms": _match_terms_for_character(name, desc),
            }
        )

    for match in re.finditer(
        rf"\b(?:a|an|the)\s+((?:{_ROLE_NOUNS}))\b",
        text,
        flags=re.I,
    ):
        role = match.group(1)
        ahead = text[max(0, match.start() - 8) : match.start()].lower()
        if ahead.rstrip().endswith("what"):
            continue
        start_i = match.start()
        end_i = min(len(text), match.end() + 80)
        clause = text[start_i:end_i].split(".")[0].strip(" ,.;")
        nxt = re.search(
            rf"\b(?:a|an|the|another|a second|the other)\s+(?:{_ROLE_NOUNS})\b",
            clause[len(match.group(0)) :],
            flags=re.I,
        )
        if nxt:
            clause = clause[: len(match.group(0)) + nxt.start()].strip(" ,.;")
        if re.match(r"^(?:the|a|an)\s+man\s+is\s+saying\b", clause, flags=re.I):
            continue
        label = _contextual_character_name(role, clause, another=False)
        if label.lower() in used and role.lower() == "man":
            continue
        add(label, clause or f"{label} from the user prompt")
        if len(found) >= _MAX_CHARS:
            break

    for match in re.finditer(
        rf"\b(?:another|a second|the other)\s+((?:{_ROLE_NOUNS}))\b([^.!?\n]{{0,80}})",
        text,
        flags=re.I,
    ):
        role = match.group(1)
        clause = match.group(0).strip(" ,.;")
        clause = re.split(r"\bwhile\b", clause, maxsplit=1)[0].strip(" ,.;") or clause
        label = _contextual_character_name(role, clause, another=True)
        add(label, clause)

    for ch in found:
        if not ch.get("match_terms"):
            ch["match_terms"] = _match_terms_for_character(
                str(ch.get("name") or ""), str(ch.get("description") or "")
            )

    if not found:
        add("Lead", "primary subject inferred from the user prompt")
    return _clamp_list(found, _MAX_CHARS)


def _score_character_in_text(ch: dict[str, Any], text: str) -> int:
    cl = text.lower()
    score = 0
    for term in ch.get("match_terms") or []:
        if term and term in cl:
            score += max(3, len(term))
    name = str(ch.get("name") or "").lower().strip()
    if name and re.search(rf"\b{re.escape(name)}\b", cl):
        score += max(6, len(name))
    return score


def _focus_character_ids(chunk: str, characters: list[dict[str, Any]]) -> list[str]:
    """Assign only characters this beat is actually about (no shared 'man' leak)."""
    cl = chunk.lower()
    scored: list[tuple[int, str]] = []
    for ch in characters:
        sc = _score_character_in_text(ch, chunk)
        name = str(ch.get("name") or "").lower().strip()
        if name and re.search(rf"\b{re.escape(name)}\b", cl):
            sc = max(sc, 8)
        if sc > 0:
            scored.append((sc, str(ch["id"])))
    if not scored:
        return []
    scored.sort(key=lambda x: (-x[0], x[1]))
    # Absolute floor so co-focus (woman + child) survives a high-scoring lead.
    focus = [cid for sc, cid in scored if sc >= 4]
    if not focus:
        focus = [scored[0][1]]
    return list(dict.fromkeys(focus))


def _split_prompt_beats(prompt: str) -> list[str]:
    """Split into cinematic beats; avoid treating continuity 'while still…' as a new shot."""
    text = _strip_prompt_filler(prompt)
    if not text:
        return ["Establish the scene"]

    # Do not use bare \bnext\b — it false-splits on "next to her".
    split_re = re.compile(
        r"(?:"
        r"\b(?:and then|after that|finally|afterward|afterwards|之后|然后|接着)\b"
        r"|\bnext(?:ly)?\s*,"
        r"|\bnext\s+(?:we|shot|scene|beat|the camera)\b"
        r"|\bwhile\s+(?:another|a second|the other)\b"
        r"|(?<=[.!?])\s+"
        r"|\b(?:the camera\s+(?:then\s+)?)?pans?\s+to\b"
        r"|\bcut(?:s)?\s+to\b"
        r")",
        flags=re.I,
    )
    parts: list[str] = []
    last = 0
    for m in split_re.finditer(text):
        left = text[last : m.start()].strip(" ,.")
        if left and len(left) > 8:
            parts.append(left)
        cue = m.group(0).strip().lower()
        last = m.start() if re.search(r"\b(?:pan|cut)\b", cue) else m.end()
    tail = text[last:].strip(" ,.")
    if tail and len(tail) > 8:
        parts.append(tail)

    merged: list[str] = []
    for part in parts:
        if (
            merged
            and len(part) < 70
            and re.match(
                r"^(?:this|that|the same)\s+(?:man|woman|person)\b|"
                r"^to\s+her\b|"
                r"^listening\b",
                part,
                flags=re.I,
            )
        ):
            merged[-1] = f"{merged[-1]}, {part}"
            continue
        if merged and len(part) < 25:
            merged[-1] = f"{merged[-1]} {part}"
            continue
        merged.append(part)

    if len(merged) < 2:
        beats: list[str] = []
        for match in re.finditer(
            r"[^.!?\n]*(?:\b(?:pan|zoom|cut|tilt|track|dolly|close-?up|wide shot|"
            r"enters?|exits?|leaves?|walks?|runs?|speaks?|looks?|turns?|sits?|"
            r"stands?|cries?|smiles?|listens?|nods?)\b)[^.!?\n]*",
            text,
            flags=re.I,
        ):
            piece = match.group(0).strip(" ,.")
            if len(piece) > 10:
                beats.append(piece)
        merged = beats or [text[:280]]

    out: list[str] = []
    for p in merged:
        key = re.sub(r"\s+", " ", p.lower())[:80]
        if out and key in re.sub(r"\s+", " ", out[-1].lower()):
            continue
        out.append(p)
    return _clamp_list(out, _MAX_SHOTS)


def _heuristic_shots(prompt: str, characters: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Split prompt into camera/action beats with focus cast per beat."""
    chunks = _split_prompt_beats(prompt)
    shots: list[dict[str, Any]] = []
    for idx, chunk in enumerate(chunks, start=1):
        cl = chunk.lower()
        focus = _focus_character_ids(chunk, characters)
        camera = "medium / eye-level"
        if re.search(r"\bpan\b", cl):
            camera = "medium / slow pan"
        elif re.search(r"\b(?:wide|crowd|establishing)\b", cl):
            camera = "wide / slight high"
        elif re.search(r"\b(?:close-?up|close up|detail|tears?)\b", cl):
            camera = "close-up / eye-level"
        action = _strip_prompt_filler(chunk)[:400]
        shots.append(
            {
                "shot_index": idx,
                "title": f"Shot {idx}",
                "action": action,
                "camera": camera,
                "character_ids": focus,
                "keyframe_prompt": action[:500],
                "timeline": f"{(idx - 1) * 2.0:.1f}-{idx * 2.0:.1f}s",
            }
        )

    # Ensure every character has a dedicated or shared beat — prefer new shot over
    # dumping them onto an unrelated establishing shot.
    covered = {cid for s in shots for cid in s.get("character_ids") or []}
    for ch in characters:
        cid = str(ch.get("id") or "")
        if not cid or cid in covered:
            continue
        desc = str(ch.get("description") or ch.get("name") or "")
        best_i = None
        best_sc = 0
        for i, shot in enumerate(shots):
            sc = _score_character_in_text(ch, str(shot.get("action") or ""))
            if sc > best_sc:
                best_sc = sc
                best_i = i
        if best_i is not None and best_sc >= 4:
            shots[best_i]["character_ids"] = list(
                dict.fromkeys([*(shots[best_i].get("character_ids") or []), cid])
            )
            covered.add(cid)
            continue
        if len(shots) < _MAX_SHOTS:
            shots.append(
                {
                    "shot_index": len(shots) + 1,
                    "title": f"Focus {ch.get('name')}",
                    "action": f"Feature {ch.get('name')}: {desc}"[:400],
                    "camera": "medium / eye-level",
                    "character_ids": [cid],
                    "keyframe_prompt": desc[:500],
                    "timeline": f"{len(shots) * 2.0:.1f}-{(len(shots) + 1) * 2.0:.1f}s",
                }
            )
            covered.add(cid)
        elif shots:
            # Last resort: attach to the shot with best lexical overlap, else last shot
            # (never force onto shot 1 if another beat scores higher).
            target = best_i if best_i is not None else (len(shots) - 1)
            shots[target]["character_ids"] = list(
                dict.fromkeys([*(shots[target].get("character_ids") or []), cid])
            )
            covered.add(cid)

    for i, shot in enumerate(shots, start=1):
        shot["shot_index"] = i
        shot["title"] = shot.get("title") or f"Shot {i}"
        # If a beat still has no cast, pick the single best character — not a round-robin leak.
        if not shot.get("character_ids") and characters:
            ranked = sorted(
                (
                    (_score_character_in_text(ch, str(shot.get("action") or "")), str(ch["id"]))
                    for ch in characters
                ),
                reverse=True,
            )
            if ranked and ranked[0][0] > 0:
                shot["character_ids"] = [ranked[0][1]]
            else:
                shot["character_ids"] = [str(characters[(i - 1) % len(characters)]["id"])]
    return _clamp_list(shots, _MAX_SHOTS)


def _heuristic_scenes(prompt: str) -> list[dict[str, str]]:
    """Pick a primary setting from common place nouns; otherwise generic."""
    lower = prompt.lower()
    place_patterns: list[tuple[str, str, str]] = [
        (r"\b(?:church|cathedral|chapel|temple|mosque|synagogue)\b", "Religious interior", "interior of the place of worship described"),
        (r"\b(?:office|boardroom|classroom|lab|laboratory|hospital)\b", "Indoor workplace", "indoor workplace setting from the prompt"),
        (r"\b(?:kitchen|living room|bedroom|apartment|house|home)\b", "Domestic interior", "home / domestic interior"),
        (r"\b(?:street|alley|road|sidewalk|plaza|market)\b", "Street / outdoor", "outdoor urban environment"),
        (r"\b(?:forest|beach|mountain|desert|lake|river|field)\b", "Nature", "natural outdoor setting"),
        (r"\b(?:cafe|restaurant|bar|shop|store|mall)\b", "Public venue", "public commercial venue"),
        (r"\b(?:spaceship|station|bridge|cockpit)\b", "Sci-fi interior", "vehicle / station interior"),
    ]
    scenes: list[dict[str, str]] = []
    for pattern, name, desc in place_patterns:
        if re.search(pattern, lower):
            scenes.append({"id": f"scene_{len(scenes) + 1}", "name": name, "description": desc})
            break
    if not scenes:
        scenes.append(
            {
                "id": "scene_1",
                "name": "Primary setting",
                "description": "main location inferred from the user prompt",
            }
        )
    return _clamp_list(scenes, _MAX_SCENES)


def _select_shots_for_budget(
    shots: list[dict[str, Any]],
    budget: int,
    characters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep up to budget shots while preserving cast coverage (do not drop woman/child)."""
    if len(shots) <= budget:
        return list(shots)
    selected: list[dict[str, Any]] = []
    covered: set[str] = set()
    remaining = list(shots)

    def take(idx: int) -> None:
        shot = remaining.pop(idx)
        selected.append(shot)
        covered.update(str(x) for x in (shot.get("character_ids") or []))

    # Always keep first establishing beat when present.
    if remaining:
        take(0)
    while len(selected) < budget and remaining:
        # Prefer a shot that introduces uncovered cast.
        best_i = 0
        best_gain = -1
        for i, shot in enumerate(remaining):
            cids = {str(x) for x in (shot.get("character_ids") or [])}
            gain = len(cids - covered)
            # Slight preference for earlier story order when gain ties.
            score = gain * 10 - i
            if score > best_gain:
                best_gain = score
                best_i = i
        take(best_i)

    # If still missing cast and we have room somehow, already at budget — fold
    # missing ids into the best-matching kept shot (not always shot 0).
    all_ids = [str(c.get("id")) for c in characters if c.get("id")]
    missing = [cid for cid in all_ids if cid not in covered]
    id_to_ch = {str(c.get("id")): c for c in characters}
    for cid in missing:
        ch = id_to_ch.get(cid) or {}
        best_i = 0
        best_sc = -1
        for i, shot in enumerate(selected):
            sc = _score_character_in_text(ch, str(shot.get("action") or ""))
            if sc > best_sc:
                best_sc = sc
                best_i = i
        selected[best_i]["character_ids"] = list(
            dict.fromkeys([*(selected[best_i].get("character_ids") or []), cid])
        )
    for i, shot in enumerate(selected, start=1):
        shot["shot_index"] = i
    return selected


def _supervisor_pipeline_decisions(
    prompt: str,
    characters: list[dict[str, Any]],
    shots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Supervisor-style layout + shot budget from prompt length and cast coverage."""
    n_chars = len(characters)
    n_shots = max(1, len(shots))
    words = len((prompt or "").split())
    if words < 40:
        budget = min(2, n_shots)
    elif words < 120:
        budget = min(3, n_shots)
    else:
        budget = min(4, n_shots)
    # Multi-cast stories need enough beats so later subjects (woman/child) survive.
    if n_chars >= 3:
        budget = max(budget, min(4, n_shots, max(3, n_chars - 1)))
    budget = max(1, min(_MAX_SHOTS, budget))

    multi = any(len(s.get("character_ids") or []) >= 2 for s in shots if isinstance(s, dict))
    solo = any(len(s.get("character_ids") or []) == 1 for s in shots if isinstance(s, dict))
    if n_chars <= 1:
        cast_layout = "single"
        prefer_combined = False
        prefer_split = False
    elif multi and solo:
        cast_layout = "hybrid"
        prefer_combined = True
        prefer_split = True
    elif multi:
        cast_layout = "combined"
        prefer_combined = True
        prefer_split = False
    else:
        cast_layout = "split"
        prefer_combined = False
        prefer_split = True

    return {
        "target_shot_count": budget,
        "cast_layout": cast_layout,
        "prefer_combined_cast": prefer_combined,
        "prefer_split_cast": prefer_split,
    }


def heuristic_analysis(prompt: str) -> dict[str, Any]:
    from jiuwenswarm.server.runtime.designer.skills_loader import detect_audio_intent

    characters = _heuristic_characters(prompt)
    scenes = _heuristic_scenes(prompt)
    shots = _heuristic_shots(prompt, characters)
    audio = detect_audio_intent(prompt)
    lower = prompt.lower()
    if any(
        w in lower
        for w in ("speaking", "speaks", "says", "said", "voice", "dialogue", "talking", "narrat")
    ):
        if audio.get("policy") != "silent":
            audio = {
                **audio,
                "include_speech": True,
                "policy": "speech_and_music" if audio.get("include_music") else "speech",
                "notes": "Dialogue/speech cues detected; include speech.",
            }
    decisions = _supervisor_pipeline_decisions(prompt, characters, shots)
    shots = _select_shots_for_budget(
        shots, int(decisions["target_shot_count"]), characters
    )
    for i, shot in enumerate(shots, start=1):
        shot["shot_index"] = i
    decisions = _supervisor_pipeline_decisions(prompt, characters, shots)
    return {
        "schema_version": "designer-script-analysis.v1",
        "source": "heuristic",
        "characters": characters,
        "scenes": scenes,
        "shots": shots,
        "audio": audio,
        "summary": (
            f"{len(characters)} characters, {len(scenes)} scenes, {len(shots)} shots, "
            f"cast={decisions['cast_layout']}"
        ),
        **decisions,
    }


def _llm_configured() -> bool:
    try:
        from jiuwenswarm.server.runtime.designer.model_tools import llm_available

        return llm_available()
    except Exception:  # noqa: BLE001
        return False


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Parse the first JSON object from model output.

    Tolerates markdown fences, leading prose, and trailing chatter so slight
    messiness does not force a heuristic fallback.
    """
    raw = (text or "").strip()
    if not raw:
        return None

    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, flags=re.IGNORECASE)
    if fence:
        raw = fence.group(1).strip()
    elif raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```\s*$", "", raw).strip()

    def _as_dict(value: Any) -> dict[str, Any] | None:
        return value if isinstance(value, dict) else None

    try:
        return _as_dict(json.loads(raw))
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    if start < 0:
        return None

    try:
        data, _end = json.JSONDecoder().raw_decode(raw[start:])
        return _as_dict(data)
    except json.JSONDecodeError:
        pass

    # Brace-balanced fallback when raw_decode fails on lightly broken JSON tails.
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                chunk = raw[start : i + 1]
                try:
                    return _as_dict(json.loads(chunk))
                except json.JSONDecodeError:
                    return None
    return None


def _prompt_mentions_duration(prompt: str) -> tuple[bool, int | None]:
    """Detect short-clip / duration cues; return (is_short_clip, target_duration_sec)."""
    low = (prompt or "").lower()
    m = re.search(r"\b(\d{1,2}(?:\.\d+)?)\s*-?\s*sec(?:ond)?s?\b", low)
    if m:
        try:
            sec = int(round(float(m.group(1))))
        except (TypeError, ValueError):
            sec = None
        if sec is not None and 1 <= sec <= 30:
            return True, sec
    if re.search(r"\b(short|one[- ]shot|single[- ]shot|movie clip|6s)\b", low):
        return True, 6
    return False, None


def _normalize_llm_analysis(parsed: dict[str, Any], base: dict[str, Any]) -> dict[str, Any] | None:
    characters = parsed.get("characters") if isinstance(parsed.get("characters"), list) else []
    scenes = parsed.get("scenes") if isinstance(parsed.get("scenes"), list) else []
    shots = parsed.get("shots") if isinstance(parsed.get("shots"), list) else []
    if len(characters) < 1 or len(shots) < 1:
        return None
    norm_chars: list[dict[str, Any]] = []
    for i, ch in enumerate(characters[:_MAX_CHARS], start=1):
        if not isinstance(ch, dict):
            continue
        name = str(ch.get("name") or f"Character {i}")
        desc = str(ch.get("description") or ch.get("name") or "")
        entry: dict[str, Any] = {
            "id": str(ch.get("id") or f"char_{i}"),
            "name": name,
            "description": desc,
            "match_terms": _match_terms_for_character(name, desc),
        }
        norm_chars.append(entry)
    if not norm_chars:
        return None
    valid_ids = {c["id"] for c in norm_chars}
    norm_scenes: list[dict[str, str]] = []
    for i, sc in enumerate(scenes[:_MAX_SCENES], start=1):
        if not isinstance(sc, dict):
            continue
        norm_scenes.append(
            {
                "id": str(sc.get("id") or f"scene_{i}"),
                "name": str(sc.get("name") or f"Scene {i}"),
                "description": str(sc.get("description") or ""),
            }
        )
    if not norm_scenes:
        norm_scenes = list(base.get("scenes") or [])
    norm_shots: list[dict[str, Any]] = []
    for i, sh in enumerate(shots[:_MAX_SHOTS], start=1):
        if not isinstance(sh, dict):
            continue
        cids = [str(x) for x in (sh.get("character_ids") or []) if str(x) in valid_ids]
        if not cids and norm_chars:
            cids = [norm_chars[(i - 1) % len(norm_chars)]["id"]]
        norm_shots.append(
            {
                "shot_index": i,
                "title": str(sh.get("title") or f"Shot {i}"),
                "action": str(sh.get("action") or "")[:500],
                "camera": str(sh.get("camera") or "medium / eye-level"),
                "character_ids": cids,
                "keyframe_prompt": str(sh.get("keyframe_prompt") or sh.get("action") or "")[:600],
                "timeline": str(sh.get("timeline") or f"{(i - 1) * 2:.1f}-{i * 2:.1f}s"),
            }
        )
    if not norm_shots:
        return None
    audio = parsed.get("audio") if isinstance(parsed.get("audio"), dict) else base.get("audio")
    try:
        tsc = int(parsed.get("target_shot_count") or 0)
    except (TypeError, ValueError):
        tsc = 0
    decisions = _supervisor_pipeline_decisions("", norm_chars, norm_shots)
    if 1 <= tsc <= _MAX_SHOTS:
        decisions["target_shot_count"] = tsc
    # Honor explicit LLM layout only when it matches co-appearance reality.
    raw_layout = str(parsed.get("cast_layout") or "").strip().lower()
    if raw_layout in {"single", "split", "combined", "hybrid"}:
        multi = any(len(s.get("character_ids") or []) >= 2 for s in norm_shots)
        if raw_layout == "combined" and multi:
            decisions["cast_layout"] = "combined"
            decisions["prefer_combined_cast"] = True
            decisions["prefer_split_cast"] = False
        elif raw_layout == "hybrid" and multi:
            decisions["cast_layout"] = "hybrid"
            decisions["prefer_combined_cast"] = True
            decisions["prefer_split_cast"] = True
        elif raw_layout == "split" and len(norm_chars) > 1 and not multi:
            decisions["cast_layout"] = "split"
            decisions["prefer_combined_cast"] = False
            decisions["prefer_split_cast"] = True
        elif raw_layout == "single" and len(norm_chars) <= 1:
            decisions["cast_layout"] = "single"
            decisions["prefer_combined_cast"] = False
            decisions["prefer_split_cast"] = False
    # Prefer coverage-preserving selection over naive first-N truncate + dump onto shot 1.
    if 1 <= tsc <= _MAX_SHOTS:
        decisions["target_shot_count"] = max(int(decisions["target_shot_count"]), tsc)
        decisions["target_shot_count"] = min(decisions["target_shot_count"], _MAX_SHOTS)
    norm_shots = _select_shots_for_budget(
        norm_shots, int(decisions["target_shot_count"]), norm_chars
    )
    decisions = _supervisor_pipeline_decisions("", norm_chars, norm_shots)
    if 1 <= tsc <= _MAX_SHOTS:
        decisions["target_shot_count"] = min(max(tsc, len(norm_shots)), _MAX_SHOTS)
    out: dict[str, Any] = {
        "schema_version": "designer-script-analysis.v1",
        "source": "llm",
        "characters": norm_chars,
        "scenes": norm_scenes,
        "shots": norm_shots,
        "audio": audio,
        "summary": str(parsed.get("summary") or "")[:500]
        or f"{len(norm_chars)} characters, {len(norm_shots)} shots",
        **decisions,
    }
    try:
        tds = int(parsed.get("target_duration_sec") or 0)
    except (TypeError, ValueError):
        tds = 0
    if 1 <= tds <= 30:
        out["target_duration_sec"] = tds
    return out


async def analyze_creative_brief(
    prompt: str,
    *,
    use_llm: bool = True,
    timeout_sec: float = _DEFAULT_LLM_TIMEOUT_SEC,
) -> dict[str, Any]:
    """LLM cast/shot analysis when models are available; else general heuristics."""
    base = heuristic_analysis(prompt)
    if not use_llm or not _llm_configured():
        return base

    short_clip, target_duration_sec = _prompt_mentions_duration(prompt)
    duration_sec = target_duration_sec or 6
    try:
        from jiuwenswarm.server.runtime.designer.model_tools import call_model_tool
        from jiuwenswarm.server.runtime.designer.skills_loader import load_orchestration_skill

        skill = load_orchestration_skill("supervisor")
        if short_clip:
            duration_rule = (
                f"User asked for a ~{duration_sec}-second film/clip: set target_duration_sec={duration_sec}, "
                "target_shot_count=1 (or 2 max), timeline covering ~0.0-"
                f"{duration_sec:.1f}s total, prefer a single continuous beat. "
            )
        else:
            duration_rule = (
                "Set target_shot_count high enough to cover every major character beat (usually 3-4). "
                "Omit target_duration_sec unless the prompt states a duration. "
            )
        system = ((skill[:1800] + "\n\n") if skill else "") + (
            "You are the Designer Casting & Shot Planner (supervisor). "
            "Extract distinct characters (not extras), primary scenes, and only the shots needed. "
            "Give each person a clear, distinct name from the prompt "
            "(e.g. Speaker vs Visitor leaving vs Woman vs Child — use whatever roles the prompt implies). "
            "For each shot, list ONLY the character_ids who are the visual focus of that beat — "
            "do not reuse an earlier character on a later beat about someone else. "
            "Background continuity is not a reason to force them into character_ids "
            "unless they are clearly on screen as subjects. "
            "Decide cast_layout: 'single' | 'combined' | 'hybrid' | 'split'. "
            "Set prefer_combined_cast true when any shot has 2+ focus characters. "
            "Write precise keyframe_prompt that names every focus person and their action. "
            + duration_rule
            + "CRITICAL: Reply with a single raw JSON object only. "
            "No markdown fences, no prose before or after, no second JSON object. Schema: "
            '{"characters":[{"id":"char_1","name":"...","description":"..."}],'
            '"scenes":[{"id":"scene_1","name":"...","description":"..."}],'
            '"shots":[{"shot_index":1,"title":"...","action":"...","camera":"...",'
            '"character_ids":["char_1"],"keyframe_prompt":"...","timeline":"0.0-'
            + f"{duration_sec:.1f}"
            + 's"}],'
            '"cast_layout":"combined|hybrid|split|single",'
            + (
                f'"target_shot_count":1,"target_duration_sec":{duration_sec},'
                if short_clip
                else '"target_shot_count":3,'
            )
            + '"prefer_combined_cast":false,"prefer_split_cast":false,'
            '"audio":{"policy":"speech|music|speech_and_music|silent|optional_music",'
            '"include_speech":false,"include_music":true,"notes":"..."},'
            '"summary":"..."}'
        )
        user_payload = {
            "user_prompt": prompt,
            "instructions": (
                "Return JSON only matching the schema. "
                + (
                    f"This is a {duration_sec}s film clip — keep shot count minimal."
                    if short_clip
                    else "Cover all major character beats."
                )
            ),
            "short_clip": short_clip,
            "target_duration_sec": duration_sec if short_clip else None,
            "heuristic_hint": {
                "characters": base.get("characters"),
                "scenes": base.get("scenes"),
                "shot_count_hint": 1 if short_clip else len(base.get("shots") or []),
            },
        }

        async def _call(*, reinforce_json: bool = False) -> dict[str, Any]:
            sys_msg = system
            payload = dict(user_payload)
            if reinforce_json:
                sys_msg = (
                    system
                    + "\nYour previous reply was invalid. Output ONLY the JSON object. "
                    "Start with '{' and end with '}'. Nothing else."
                )
                payload["retry"] = True
            result = await call_model_tool(
                prompt=json.dumps(payload, ensure_ascii=False),
                system=sys_msg,
                optimize_for="quality",
                # Script JSON can be large; avoid mid-object truncation.
                max_tokens=3200,
            )
            if result.get("fallback") or not result.get("ok"):
                logger.info(
                    "LLM script analysis tool fallback/err (fallback=%s err=%s)",
                    result.get("fallback"),
                    result.get("error"),
                )
                return base
            text = str(result.get("text") or "")
            parsed = _extract_json_object(text)
            if not parsed:
                logger.info(
                    "LLM script analysis returned non-JSON (len=%s); %s",
                    len(text),
                    "will retry" if not reinforce_json else "using heuristic",
                )
                return base
            chars = parsed.get("characters") if isinstance(parsed.get("characters"), list) else []
            placeholder = False
            for ch in chars:
                if not isinstance(ch, dict):
                    continue
                name = str(ch.get("name") or "").strip()
                if name in {"", "...", "…", "string", "name"}:
                    placeholder = True
                    break
            if placeholder or not chars:
                logger.info("LLM script analysis looked like schema echo; using heuristic base")
                return base
            if short_clip:
                parsed.setdefault("target_shot_count", 1)
                parsed.setdefault("target_duration_sec", duration_sec)
                try:
                    if int(parsed.get("target_shot_count") or 0) > 2:
                        parsed["target_shot_count"] = 1
                except (TypeError, ValueError):
                    parsed["target_shot_count"] = 1
            normalized = _normalize_llm_analysis(parsed, base)
            if not normalized:
                return base
            if short_clip and normalized.get("shots"):
                keep = max(1, min(2, int(normalized.get("target_shot_count") or 1)))
                normalized["shots"] = list(normalized["shots"])[:keep]
                for i, shot in enumerate(normalized["shots"], start=1):
                    shot["shot_index"] = i
                    if not str(shot.get("timeline") or "").strip():
                        if len(normalized["shots"]) == 1:
                            shot["timeline"] = f"0.0-{duration_sec:.1f}s"
                        else:
                            half = duration_sec / len(normalized["shots"])
                            shot["timeline"] = f"{(i - 1) * half:.1f}-{i * half:.1f}s"
                normalized["target_duration_sec"] = duration_sec
                normalized["target_shot_count"] = len(normalized["shots"])
            # Successful LLM parse must never report heuristic.
            normalized["source"] = "llm"
            return normalized

        first = await asyncio.wait_for(_call(), timeout=max(3.0, float(timeout_sec)))
        if first.get("source") == "llm":
            return first
        # One reinforced retry when first parse failed soft (returned heuristic base).
        remaining = max(5.0, float(timeout_sec) * 0.5)
        return await asyncio.wait_for(
            _call(reinforce_json=True), timeout=remaining
        )
    except asyncio.TimeoutError:
        logger.info("LLM script analysis timed out after %.1fs; using heuristic", timeout_sec)
        return base
    except Exception as exc:  # noqa: BLE001
        logger.info("LLM script analysis failed, using heuristic: %s", exc)
        return base


def analyze_creative_brief_sync(
    prompt: str,
    *,
    use_llm: bool = True,
    timeout_sec: float = _DEFAULT_LLM_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Sync wrapper for bootstrap threads (safe if no running loop)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            analyze_creative_brief(prompt, use_llm=use_llm, timeout_sec=timeout_sec)
        )
    # Already on a loop — fall back to heuristics to avoid nested asyncio.run.
    if use_llm and _llm_configured():
        logger.info("Skipping nested LLM analysis on running loop; using heuristic")
    return heuristic_analysis(prompt)
