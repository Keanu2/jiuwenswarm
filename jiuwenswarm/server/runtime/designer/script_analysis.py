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

_MAX_CHARS = 12
_MAX_SHOTS = 16
_MAX_SCENES = 8
# Keep under typical UI bootstrap budgets while still allowing a real LLM call.
_DEFAULT_LLM_TIMEOUT_SEC = 90.0

# Generic role nouns — not tied to any one story.
_ROLE_NOUNS = (
    "preacher|pastor|priest|minister|teacher|doctor|nurse|soldier|officer|"
    "courier|messenger|king|queen|prince|princess|knight|wizard|witch|"
    "chef|pilot|driver|farmer|scientist|engineer|artist|singer|dancer|"
    "detective|spy|robot|android|hero|villain|warrior|hunter|merchant|"
    "mother|father|parent|brother|sister|friend|stranger|leader|captain|"
    "partner|girlfriend|boyfriend|wife|husband|date|spouse|"
    "boy|girl|child|kid|man|woman|person|guy|lady"
)


def _clamp_list(items: list[Any], limit: int) -> list[Any]:
    return items[: max(1, min(limit, len(items) or 1))]


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _is_cjk_heavy(text: str) -> bool:
    """CJK prose needs different beat thresholds: one glyph carries a whole word."""
    stripped = re.sub(r"\s+", "", text or "")
    if not stripped:
        return False
    return len(_CJK_RE.findall(stripped)) * 2 >= len(stripped)


def _title_case_label(raw: str) -> str:
    cleaned = re.sub(r"\s+", " ", raw.strip())
    if not cleaned:
        return ""
    return cleaned[:1].upper() + cleaned[1:]


def infer_primary_subject_name(prompt: str) -> str:
    """Best-effort subject label from the user prompt when cast extraction is empty."""
    text = _strip_prompt_filler(_strip_reference_appendix(prompt))
    match = re.search(
        rf"\b((?:young|old|elderly|little|small|tall|beautiful|handsome|pretty|"
        rf"sad|happy|angry|scared|lonely|brave)\s+)?(({_ROLE_NOUNS}))\b",
        text,
        flags=re.I,
    )
    if match:
        phrase = f"{match.group(1) or ''}{match.group(2)}".strip()
        label = _title_case_label(phrase)
        if label:
            return label
    art = re.search(rf"\b(?:a|an|the)\s+(({_ROLE_NOUNS}))\b", text, flags=re.I)
    if art:
        label = _title_case_label(art.group(1))
        if label:
            return label
    chunk = text.split(".")[0].split(",")[0].strip()[:48]
    if chunk and len(chunk.split()) <= 6:
        label = _title_case_label(chunk)
        if label:
            return label
    return "Subject"


def _strip_reference_appendix(text: str) -> str:
    """Drop attachment roster so it is not treated as a cinematic beat."""
    cleaned = re.split(
        r"\n+\s*(?:User attached reference media|REFERENCE_MEDIA)\b",
        text or "",
        maxsplit=1,
        flags=re.I,
    )[0]
    return cleaned.strip()


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


def _cast_id_maps(
    characters: list[dict[str, Any]],
) -> tuple[set[str], dict[str, str]]:
    """valid ids + lowercase name/alias → id (exact name match only)."""
    valid: set[str] = set()
    by_name: dict[str, str] = {}
    for ch in characters:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("id") or "").strip()
        if not cid:
            continue
        valid.add(cid)
        by_name[cid.lower()] = cid
        name = str(ch.get("name") or "").strip()
        if name:
            by_name[name.lower()] = cid
        for alias in ch.get("aliases") or []:
            a = str(alias or "").strip()
            if a:
                by_name[a.lower()] = cid
    return valid, by_name


def resolve_cast_token(token: object, *, valid_ids: set[str], by_name: dict[str, str]) -> str:
    """Map a cast token (id or display name) to a character id; else ''."""
    raw = str(token or "").strip()
    if not raw:
        return ""
    if raw in valid_ids:
        return raw
    hit = by_name.get(raw.lower())
    if hit:
        return hit
    m = re.search(r"(char_\d+)", raw, flags=re.I)
    if m and m.group(1) in valid_ids:
        return m.group(1)
    return ""


def resolve_cast_token_list(
    tokens: object,
    *,
    valid_ids: set[str],
    by_name: dict[str, str],
) -> list[str]:
    if not isinstance(tokens, list):
        return []
    out: list[str] = []
    for tok in tokens:
        cid = resolve_cast_token(tok, valid_ids=valid_ids, by_name=by_name)
        if cid and cid not in out:
            out.append(cid)
    return out


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
        # Allow 0–2 adjectives before a role noun from _ROLE_NOUNS.
        rf"\b(?:a|an|the|his|her|their)\s+(?:[A-Za-z-]+\s+){{0,2}}((?:{_ROLE_NOUNS}))\b",
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
            rf"\b(?:a|an|the|another|a second|the other|his|her|their)\s+"
            rf"(?:[A-Za-z-]+\s+){{0,2}}(?:{_ROLE_NOUNS})\b",
            clause[len(match.group(0)) :],
            flags=re.I,
        )
        if nxt:
            clause = clause[: len(match.group(0)) + nxt.start()].strip(" ,.;")
        if re.match(r"^(?:the|a|an)\s+man\s+is\s+saying\b", clause, flags=re.I):
            continue
        label = _contextual_character_name(role, clause, another=False)
        # Prefer fuller phrase when adjectives present ("Young Man").
        full = match.group(0).strip()
        full_label = _title_case_label(
            re.sub(r"^(?:a|an|the|his|her|their)\s+", "", full, flags=re.I)
        )
        if full_label and len(full_label.split()) <= 4:
            label = full_label
        # "a date" (appointment) is not a character; "his partner/date" is.
        if role.lower() == "date" and not re.search(
            r"\b(?:his|her|their)\s+date\b", match.group(0), flags=re.I
        ):
            continue
        if label.lower() in used and role.lower() == "man":
            continue
        add(label, clause or f"{label} from the user prompt")
        if len(found) >= _MAX_CHARS:
            break

    # Bare role mentions without article (any role from _ROLE_NOUNS).
    for match in re.finditer(
        rf"(?:^|[.!?]\s+|,\s+)((?:[A-Za-z-]+\s+){{0,1}}(?:{_ROLE_NOUNS}))\b",
        text,
        flags=re.I,
    ):
        phrase = match.group(1).strip()
        role = phrase.split()[-1].lower()
        if role in {"person", "people", "guy", "date"}:
            continue
        label = _title_case_label(phrase)
        if label.lower() in used:
            continue
        if len(label.split()) > 3:
            continue
        if re.match(r"^(?:while|when|and|or|as|if|with)\b", label, flags=re.I):
            continue
        add(label, phrase)
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
        add(infer_primary_subject_name(text), "primary subject inferred from the user prompt")
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
    # Absolute floor so co-focus cast survives a high-scoring lead.
    focus = [cid for sc, cid in scored if sc >= 4]
    if not focus:
        focus = [scored[0][1]]
    return list(dict.fromkeys(focus))


def _split_prompt_beats(prompt: str) -> list[str]:
    """Split into cinematic beats; avoid treating continuity 'while still…' as a new shot."""
    text = _strip_prompt_filler(_strip_reference_appendix(prompt))
    if not text:
        return ["Establish the scene"]

    cjk = _is_cjk_heavy(text)
    # CJK carries no word boundaries, so \b cues never fire on Chinese prose.
    min_beat_len = 5 if cjk else 8
    merge_below = 10 if cjk else 25
    # Do not use bare \bnext\b — it false-splits on "next to her".
    split_re = re.compile(
        r"(?:"
        r"\b(?:and then|after that|finally|afterward|afterwards)\b"
        r"|(?:紧接着|接着|然后|随后|其后|之后|最后|最终|突然|忽然|"
        r"画面(?:切换|切至|切到|转向|转为)|镜头(?:切换|切至|切到|转向|摇向)|"
        r"下一(?:幕|镜|个镜头))"
        r"|(?<=[。！？；])\s*"
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
        if m.start() < last:
            continue
        left = text[last : m.start()].strip(" ,.，。")
        if left and len(left) > min_beat_len:
            parts.append(left)
        cue = m.group(0).strip().lower()
        last = m.start() if re.search(r"\b(?:pan|cut)\b", cue) else m.end()
    tail = text[last:].strip(" ,.，。")
    if tail and len(tail) > min_beat_len:
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
        if merged and len(part) < merge_below:
            merged[-1] = f"{merged[-1]} {part}"
            continue
        merged.append(part)

    if len(merged) < 2 and cjk:
        # Long Chinese paragraph with no terminators: comma clauses are the beats.
        clauses = [c.strip() for c in re.split(r"[，,、；;]", text) if len(c.strip()) > 8]
        if len(clauses) >= 2:
            merged = clauses

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
    # Respect explicit N-shot / N分镜 ceiling when present.
    try:
        from jiuwenswarm.server.runtime.designer.experiments.director_contract import (
            _explicit_shot_count_from_prompt,
        )

        shot_ceiling = _explicit_shot_count_from_prompt(prompt) or _MAX_SHOTS
    except Exception:  # noqa: BLE001
        shot_ceiling = _MAX_SHOTS
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
        if len(shots) < min(_MAX_SHOTS, shot_ceiling):
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
        elif shots and best_i is not None and best_sc >= 4:
            # Budget full: only attach when the action already mentions them.
            shots[best_i]["character_ids"] = list(
                dict.fromkeys([*(shots[best_i].get("character_ids") or []), cid])
            )
            covered.add(cid)

    for i, shot in enumerate(shots, start=1):
        shot["shot_index"] = i
        from jiuwenswarm.server.runtime.designer.node_labels import derive_shot_name

        shot["title"] = derive_shot_name(shot, fallback_index=i)
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
            # else leave empty — fail closed; Supervisor/Manager must fill on_screen
    return _fold_shots_to_budget(shots, max(1, min(_MAX_SHOTS, shot_ceiling)))


def _heuristic_scenes(prompt: str) -> list[dict[str, str]]:
    """Infer a primary setting from place nouns in the prompt (domain-agnostic)."""
    lower = prompt.lower()
    # Capture the place word itself — no genre templates.
    place_re = re.compile(
        r"\b(?:in|at|inside|outside|near|from)\s+(?:a|an|the|his|her|their)?\s*"
        r"([a-z][a-z\-]*(?:\s+[a-z][a-z\-]*){0,2})\b",
        flags=re.I,
    )
    stop = {
        "the",
        "a",
        "an",
        "his",
        "her",
        "their",
        "this",
        "that",
        "moment",
        "time",
        "day",
        "night",
        "way",
        "while",
        "front",
        "back",
    }
    scenes: list[dict[str, str]] = []
    for match in place_re.finditer(lower):
        phrase = re.sub(r"\s+", " ", match.group(1).strip())
        words = [w for w in phrase.split() if w not in stop]
        if not words:
            continue
        name = " ".join(words)[:48]
        if name.lower() in {str(s.get("name") or "").lower() for s in scenes}:
            continue
        scenes.append(
            {
                "id": f"scene_{len(scenes) + 1}",
                "name": name.title(),
                "description": f"setting mentioned in the prompt: {name}",
            }
        )
        if len(scenes) >= 2:
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
    """Keep up to budget shots while preserving cast coverage (do not drop uncovered cast)."""
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

    # Uncovered cast stays uncovered (Manager/Supervisor must list them).
    # Never fold into on_screen/character_ids via action-text scoring — that bleeds
    # later-meet people into early beats.
    for i, shot in enumerate(selected, start=1):
        shot["shot_index"] = i
    return selected


def _fold_shots_to_budget(
    shots: list[dict[str, Any]], budget: int
) -> list[dict[str, Any]]:
    """Fit shots into budget by folding trailing beats into the last kept shot.

    Dropping beats would silently lose story the user wrote, so the overflow is
    appended instead of discarded.
    """
    if budget < 1 or len(shots) <= budget:
        return list(shots)
    kept = [dict(s) for s in shots[:budget]]
    tail = shots[budget:]
    last = kept[-1]
    extra = " ".join(str(s.get("action") or "") for s in tail).strip()
    if extra:
        last["action"] = f"{str(last.get('action') or '')} {extra}".strip()[:800]
        last["keyframe_prompt"] = str(last["action"])[:1200]
    ids = list(last.get("character_ids") or [])
    for shot in tail:
        for cid in shot.get("character_ids") or []:
            if cid not in ids:
                ids.append(cid)
    last["character_ids"] = ids
    for i, shot in enumerate(kept, start=1):
        shot["shot_index"] = i
    return kept


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
    # Multi-cast stories need enough beats so later subjects survive budget trim.
    if n_chars >= 3:
        budget = max(budget, min(4, n_shots, max(3, n_chars - 1)))
    # Explicit N-shot / N分镜 language is a HARD ceiling (and floor when larger).
    try:
        from jiuwenswarm.server.runtime.designer.experiments.director_contract import (
            _explicit_shot_count_from_prompt,
        )

        explicit = _explicit_shot_count_from_prompt(prompt or "")
        if explicit >= 1:
            budget = explicit
    except Exception:  # noqa: BLE001
        pass
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


def _heuristic_lean_shot(
    prompt: str,
    characters: list[dict[str, Any]],
    *,
    story_name: str = "",
) -> dict[str, Any]:
    """One full-narrative beat: no LLM → prefer a single KF/clip over naive multi-beat split."""
    from jiuwenswarm.server.runtime.designer.node_labels import derive_shot_name

    all_ids = [str(c.get("id")) for c in characters if str(c.get("id") or "").strip()]
    title = (story_name or derive_shot_name({"title": "", "action": prompt}, fallback_index=1))[:80]
    body = _strip_prompt_filler(prompt)[:1200] or prompt[:1200]
    return {
        "shot_index": 1,
        "title": title or "Full narrative",
        "action": body[:800],
        "camera": "medium / eye-level",
        "character_ids": list(all_ids),
        "on_screen": list(all_ids),
        "featured_cast_ids": list(all_ids[:1]),
        "ensemble_cast_ids": list(all_ids),
        "offscreen": [],
        "keyframe_prompt": body[:1200],
        "setting_id": "set_1",
        "timeline": "0-8s",
        "occupancy": {
            "must_appear": list(all_ids),
            "offscreen": [],
            "cast_actions": {},
        },
    }


def heuristic_analysis(prompt: str) -> dict[str, Any]:
    from jiuwenswarm.server.runtime.designer.audio_locks import ensure_audio_locks_on_analysis
    from jiuwenswarm.server.runtime.designer.node_labels import derive_story_name
    from jiuwenswarm.server.runtime.designer.skills_loader import detect_audio_intent

    prompt = _strip_reference_appendix(prompt)
    characters = _heuristic_characters(prompt)
    scenes = _heuristic_scenes(prompt)
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

    story_name = derive_story_name(prompt=prompt)
    explicit = 0
    beats = 1
    try:
        from jiuwenswarm.server.runtime.designer.experiments.director_contract import (
            _explicit_shot_count_from_prompt,
            count_narrative_beats,
        )

        explicit = int(_explicit_shot_count_from_prompt(prompt) or 0)
        beats = int(count_narrative_beats(prompt) or 1)
    except Exception:  # noqa: BLE001
        explicit = 0
        beats = 1

    # No LLM: explicit N-shot / N分镜 wins, else follow the beats the prompt itself
    # describes. A multi-beat story must not collapse into one keyframe just
    # because the opening LLM call failed.
    budget = explicit if explicit >= 2 else beats
    if budget >= 2:
        shots = _fold_shots_to_budget(
            _heuristic_shots(prompt, characters), max(1, min(_MAX_SHOTS, budget))
        )
        decisions = _supervisor_pipeline_decisions(prompt, characters, shots)
        decisions["target_shot_count"] = len(shots)
    else:
        shots = [_heuristic_lean_shot(prompt, characters, story_name=story_name)]
        n_chars = len(characters)
        decisions = {
            "target_shot_count": 1,
            "cast_layout": "combined" if n_chars > 1 else "single",
            "prefer_combined_cast": n_chars > 1,
            "prefer_split_cast": False,
        }

    payload = {
        "schema_version": "designer-script-analysis.v1",
        "source": "heuristic",
        "user_prompt": prompt,
        "story_name": story_name,
        "characters": characters,
        "scenes": scenes,
        "shots": shots,
        "audio": audio,
        "summary": (
            f"{len(characters)} characters, {len(scenes)} scenes, {len(shots)} shots, "
            f"cast={decisions['cast_layout']}, lean={budget < 2}"
        ),
        **decisions,
    }
    return ensure_audio_locks_on_analysis(payload, prompt)


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
    valid_ids, by_name = _cast_id_maps(norm_chars)
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
        cids = resolve_cast_token_list(
            sh.get("character_ids") or [],
            valid_ids=valid_ids,
            by_name=by_name,
        )
        # Fail closed: never invent round-robin cast when the shot omitted ids.
        ensemble = resolve_cast_token_list(
            sh.get("ensemble_cast_ids") or sh.get("character_ids") or [],
            valid_ids=valid_ids,
            by_name=by_name,
        ) or list(cids)
        featured = resolve_cast_token_list(
            sh.get("featured_cast_ids") or [],
            valid_ids=valid_ids,
            by_name=by_name,
        ) or list(cids[:1])
        exiting = resolve_cast_token_list(
            sh.get("exiting_character_ids") or [],
            valid_ids=valid_ids,
            by_name=by_name,
        )
        on_screen = resolve_cast_token_list(
            sh.get("on_screen")
            or sh.get("visible_cast_ids")
            or sh.get("featured_cast_ids")
            or [],
            valid_ids=valid_ids,
            by_name=by_name,
        )
        # Fail closed: do not promote full character_ids / ensemble when on_screen absent.
        if not on_screen:
            on_screen = resolve_cast_token_list(
                (sh.get("character_ids") or [])[:1],
                valid_ids=valid_ids,
                by_name=by_name,
            )
        offscreen = [
            cid
            for cid in resolve_cast_token_list(
                sh.get("offscreen") or sh.get("off_screen_cast_ids") or [],
                valid_ids=valid_ids,
                by_name=by_name,
            )
            if cid not in on_screen
        ]
        cast_actions: dict[str, str] = {}
        raw_actions = sh.get("cast_actions") or sh.get("doing")
        if isinstance(raw_actions, dict):
            for k, v in raw_actions.items():
                cid = resolve_cast_token(k, valid_ids=valid_ids, by_name=by_name)
                if cid and str(v or "").strip():
                    cast_actions[cid] = str(v).strip()[:240]
        strategy = str(sh.get("keyframe_strategy") or "").strip()
        setting_id = str(sh.get("setting_id") or sh.get("scene_id") or f"set_{i}").strip()
        # Prefer on_screen as the drawn cast for this beat.
        cids = list(on_screen) or cids
        entry: dict[str, Any] = {
            "shot_index": i,
            "title": str(sh.get("title") or ""),
            "action": str(sh.get("action") or "")[:500],
            "camera": str(sh.get("camera") or "medium / eye-level"),
            "character_ids": cids,
            "on_screen": on_screen or list(cids),
            "visible_cast_ids": on_screen or list(cids),
            "offscreen": offscreen,
            "off_screen_cast_ids": offscreen,
            "ensemble_cast_ids": ensemble,
            "featured_cast_ids": featured,
            "exiting_character_ids": exiting,
            "setting_id": setting_id or f"set_{i}",
            "keyframe_prompt": str(sh.get("keyframe_prompt") or sh.get("action") or "")[:600],
            "timeline": str(sh.get("timeline") or f"{(i - 1) * 2:.1f}-{i * 2:.1f}s"),
        }
        if cast_actions:
            entry["cast_actions"] = cast_actions
        if strategy in {"compose_from_solo_refs", "edit_prior_keyframe"}:
            entry["keyframe_strategy"] = strategy
        from jiuwenswarm.server.runtime.designer.node_labels import derive_shot_name

        entry["title"] = derive_shot_name(entry, fallback_index=i)
        norm_shots.append(entry)
    if not norm_shots:
        return None
    audio = parsed.get("audio") if isinstance(parsed.get("audio"), dict) else base.get("audio")
    try:
        tsc = int(parsed.get("target_shot_count") or 0)
    except (TypeError, ValueError):
        tsc = 0
    from jiuwenswarm.server.runtime.designer.node_labels import derive_story_name

    story_name = derive_story_name(
        analysis={
            "story_name": parsed.get("story_name") or parsed.get("film_title") or parsed.get("title"),
            "title": parsed.get("title"),
        },
        prompt=str(base.get("user_prompt") or base.get("summary") or ""),
        graph_title=str(parsed.get("story_name") or ""),
    )
    user_prompt = str(base.get("user_prompt") or base.get("summary") or "")
    decisions = _supervisor_pipeline_decisions(user_prompt, norm_chars, norm_shots)
    heuristic_budget = int(decisions["target_shot_count"])
    explicit = 0
    try:
        from jiuwenswarm.server.runtime.designer.experiments.director_contract import (
            _explicit_shot_count_from_prompt,
        )

        explicit = int(_explicit_shot_count_from_prompt(user_prompt) or 0)
    except Exception:  # noqa: BLE001
        explicit = 0
    # LLM owns N via shots[] / target_shot_count. Explicit user N-shot is a hard ceiling.
    # Soft safety only: never exceed _MAX_SHOTS. Heuristic budget is fallback when LLM omits N.
    if explicit >= 1:
        decisions["target_shot_count"] = max(1, min(_MAX_SHOTS, explicit))
    elif 1 <= tsc <= _MAX_SHOTS:
        decisions["target_shot_count"] = tsc
    elif norm_shots:
        decisions["target_shot_count"] = max(1, min(_MAX_SHOTS, len(norm_shots)))
    else:
        decisions["target_shot_count"] = heuristic_budget
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
    # Prefer coverage-preserving selection over naive first-N truncate.
    ceiling = max(1, min(int(decisions["target_shot_count"]), _MAX_SHOTS))
    if explicit < 1 and len(norm_shots) > ceiling and 1 <= tsc <= _MAX_SHOTS:
        # shots[] longer than declared target_shot_count → trust the longer list (soft max).
        ceiling = max(1, min(len(norm_shots), _MAX_SHOTS))
    decisions["target_shot_count"] = ceiling
    norm_shots = _select_shots_for_budget(norm_shots, ceiling, norm_chars)
    layout_decisions = _supervisor_pipeline_decisions(user_prompt, norm_chars, norm_shots)
    for key in ("cast_layout", "prefer_combined_cast", "prefer_split_cast"):
        if key in layout_decisions:
            decisions[key] = layout_decisions[key]
    # Keep LLM-owned N after layout refresh (do not re-clamp to heuristic 2–4).
    if explicit >= 1:
        decisions["target_shot_count"] = max(1, min(explicit, _MAX_SHOTS))
    else:
        decisions["target_shot_count"] = max(1, min(len(norm_shots) or ceiling, _MAX_SHOTS))
    if len(norm_shots) > int(decisions["target_shot_count"]):
        norm_shots = norm_shots[: int(decisions["target_shot_count"])]
    out: dict[str, Any] = {
        "schema_version": "designer-script-analysis.v1",
        "source": "llm",
        "story_name": story_name,
        "characters": norm_chars,
        "scenes": norm_scenes,
        "shots": norm_shots,
        "audio": audio,
        "skip_scene_plate": bool(parsed.get("skip_scene_plate", True)),
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
    reference_images: list[str] | None = None,
) -> dict[str, Any]:
    """LLM cast/shot analysis when models are available; else general heuristics."""
    base = heuristic_analysis(prompt)
    if not use_llm or not _llm_configured():
        return base

    short_clip, target_duration_sec = _prompt_mentions_duration(prompt)
    duration_sec = target_duration_sec or 6
    try:
        from jiuwenswarm.server.runtime.designer.model_tools import call_model_tool

        shot_count_rule = (
            "YOU decide target_shot_count (soft prefer ≤8, hard max 16; typical 1–6). "
            "One clip = one continuous beat — no rapid scene changes inside a Wan I2V clip. "
            "Reuse one KF when location/wardrobe/lighting/identity hold and only local "
            "subject motion or ONE camera move (pan/dolly/push/orbit/static) changes. "
            "New KF when: hard cut, new setting_id, wardrobe/prop set change, large "
            "pose/framing jump, or on-screen cast set changes materially. "
            "Qwen KF: lock identity+wardrobe in the still (entity+scene+light); change "
            "only pose/action or one camera variable; first setting KF = "
            "compose_from_solo_refs, later same setting = edit_prior_keyframe; prefer "
            "≤2–3 people with refs. "
            "Wan I2V prompt = motion+camera only (image already fixes look); aim ~3–5s "
            "per clip for stability (up to ~10–15s if motion stays simple). "
            "Explicit user N-shot / N分镜 is a HARD ceiling. "
        )
        if short_clip:
            duration_rule = (
                f"Film ~{duration_sec}s total: set target_duration_sec={duration_sec}. "
            )
        else:
            duration_rule = ""
        # Compact schema — long prompts make deepseek-flash return prose/empty.
        system = (
            "You are the Designer Supervisor. Domain-agnostic: use only places/people from the prompt. "
            "Extract EVERY named human into characters[]. Anonymous crowd is not a character. "
            "Each character description MUST lock wardrobe garments: shirt/top style+color, "
            "trousers/skirt/bottom style+color, footwear, outerwear/accessories if any "
            "(example: 'light blue short-sleeve shirt; dark charcoal trousers; black sneakers'). "
            "Per shot also lock staging: cast_actions (posture/doing), blocking positions "
            "(zone/facing), who looks_at whom, who talks_to whom, adjacency (next_to). "
            "Shots grouped by setting_id (different places = different setting_id). "
            "NOT every character in every scene. Per shot: on_screen (visible), offscreen "
            "(in scene, not in frame), cast_actions {id: doing-what}. "
            + shot_count_rule
            + duration_rule
            + " Output ONLY one JSON object (no markdown). "
            '{"story_name":"short film title any language",'
            '"characters":[{"id":"char_1","name":"...","description":"..."}],'
            '"shots":[{"shot_index":1,"title":"2-4 word beat name NEVER Shot N",'
            '"action":"...","camera":"...","on_screen":["char_1"],'
            '"offscreen":[],"cast_actions":{"char_1":"..."},"featured_cast_ids":["char_1"],'
            '"ensemble_cast_ids":["char_1"],"setting_id":"set_1","keyframe_prompt":"...","timeline":"0-5s"}],'
            '"skip_scene_plate":true,"target_shot_count":N'
            + (f',"target_duration_sec":{duration_sec}' if short_clip else "")
            + "}"
        )
        user_payload = {
            "user_prompt": prompt[:3000],
            "instructions": (
                "JSON only. Every named human must appear in characters[]. "
                "Decide shot count wisely (prefer fewer; merge pans/same-cast continuous "
                "action into one beat). Set target_shot_count = len(shots). "
                "Each shot title MUST be a 2–4 word description of the beat "
                "(any language; e.g. 'Open Door', 'Quiet Glance', '离开房间') — never 'Shot 1'."
            ),
        }

        async def _call(*, reinforce_json: bool = False) -> dict[str, Any] | None:
            """Return normalized LLM analysis, or None on soft failure (caller retries)."""
            sys_msg = system
            payload: dict[str, Any] = {
                "user_prompt": prompt[:3000] if not reinforce_json else prompt[:2000],
                "instructions": "JSON only. Every named human in characters[].",
            }
            if reinforce_json:
                sys_msg = (
                    "Output ONLY one JSON object starting with '{'. "
                    '{"characters":[{"id":"char_1","name":"...","description":"..."}],'
                    '"shots":[{"shot_index":1,"action":"...","camera":"...",'
                    '"character_ids":["char_1"],"ensemble_cast_ids":["char_1"],'
                    '"featured_cast_ids":["char_1"],"setting_id":"set_1",'
                    '"keyframe_prompt":"...","timeline":"0-5s"}],"skip_scene_plate":true}'
                )
                payload["retry"] = True
            result = await call_model_tool(
                prompt=json.dumps(payload, ensure_ascii=False),
                system=sys_msg,
                optimize_for="quality",
                max_tokens=32768,
                images=list(reference_images or []) or None,
            )
            if result.get("fallback"):
                logger.info("LLM script analysis used local fallback")
                return None
            if not result.get("ok"):
                logger.info(
                    "LLM script analysis tool err=%s; soft-fail",
                    result.get("error"),
                )
                return None
            text = str(result.get("text") or "")
            parsed = _extract_json_object(text)
            if not parsed:
                logger.info(
                    "LLM script analysis returned non-JSON (len=%s); soft-fail",
                    len(text),
                )
                return None
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
                logger.info("LLM script analysis looked like schema echo; soft-fail")
                return None
            if short_clip:
                parsed.setdefault("target_duration_sec", duration_sec)
            normalized = _normalize_llm_analysis(parsed, base)
            if not normalized:
                return None
            if short_clip and normalized.get("shots"):
                shots_n = list(normalized["shots"])
                n = max(1, len(shots_n))
                for i, shot in enumerate(shots_n, start=1):
                    shot["shot_index"] = i
                    if not str(shot.get("timeline") or "").strip():
                        half = float(duration_sec) / n
                        shot["timeline"] = f"{(i - 1) * half:.1f}-{i * half:.1f}s"
                normalized["shots"] = shots_n
                normalized["target_duration_sec"] = duration_sec
                normalized["target_shot_count"] = len(shots_n)
            normalized["source"] = "llm"
            return normalized

        # Prefer LLM; one reinforce if first reply empty/non-JSON (max 2 attempts).
        first = await asyncio.wait_for(_call(), timeout=max(3.0, float(timeout_sec)))
        if isinstance(first, dict) and first.get("source") == "llm":
            return first
        remaining = max(8.0, float(timeout_sec) * 0.4)
        second = await asyncio.wait_for(_call(reinforce_json=True), timeout=remaining)
        if isinstance(second, dict) and second.get("source") == "llm":
            return second
        # LLM configured but both attempts failed — mark pending so Supervisor re-authors.
        pending = dict(base)
        pending["source"] = "heuristic_pending_llm"
        pending["llm_pending"] = True
        logger.info("LLM script analysis exhausted retries; marking heuristic_pending_llm")
        return pending
    except asyncio.TimeoutError:
        logger.info("LLM script analysis timed out after %.1fs; marking pending", timeout_sec)
        pending = dict(base)
        pending["source"] = "heuristic_pending_llm"
        pending["llm_pending"] = True
        return pending
    except Exception as exc:  # noqa: BLE001
        logger.info("LLM script analysis failed, marking pending: %s", exc)
        pending = dict(base)
        pending["source"] = "heuristic_pending_llm"
        pending["llm_pending"] = True
        return pending


def analyze_creative_brief_sync(
    prompt: str,
    *,
    use_llm: bool = True,
    timeout_sec: float = _DEFAULT_LLM_TIMEOUT_SEC,
    reference_images: list[str] | None = None,
) -> dict[str, Any]:
    """Sync wrapper — always uses a dedicated event loop (never skips LLM on nest)."""

    def _run() -> dict[str, Any]:
        return asyncio.run(
            analyze_creative_brief(
                prompt,
                use_llm=use_llm,
                timeout_sec=timeout_sec,
                reference_images=reference_images,
            )
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()
    # Already on a loop: run in a worker thread with its own loop.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_run)
        return fut.result(timeout=max(30.0, float(timeout_sec) + 30.0))
