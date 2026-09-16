# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Film-wide language / speech / BGM locks and clip-embedded video audio."""

from __future__ import annotations

import os
import re
from typing import Any


_WAN3_AUDIO_MODEL = "wan3.0-video"


def infer_language_lock(prompt: str, *, hint: str | None = None) -> str:
    """Return a short language code/name for spoken dialogue (locked film-wide)."""
    explicit = str(hint or "").strip().lower()
    if explicit:
        return _normalize_language(explicit)
    text = str(prompt or "")
    lower = text.lower()
    markers = (
        ("zh", ("中文", "普通话", "国语", "chinese", "mandarin", "speak chinese", "in chinese")),
        ("en", ("english", "in english", "speak english")),
        ("ja", ("japanese", "日本語", "にほんご")),
        ("ko", ("korean", "한국어", "조선말")),
        ("fr", ("french", "français", "francais")),
        ("es", ("spanish", "español", "espanol")),
        ("de", ("german", "deutsch")),
    )
    for code, kws in markers:
        if any(kw in lower or kw in text for kw in kws):
            return code
    # Script detection: CJK density → zh; Hangul → ko; Hiragana/Katakana → ja.
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", text):
        return "ko"
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk >= 8 and cjk >= latin:
        return "zh"
    return "en"


def _normalize_language(value: str) -> str:
    v = (value or "").strip().lower()
    aliases = {
        "chinese": "zh",
        "mandarin": "zh",
        "cn": "zh",
        "zh-cn": "zh",
        "zh-tw": "zh",
        "english": "en",
        "en-us": "en",
        "en-gb": "en",
        "japanese": "ja",
        "korean": "ko",
        "french": "fr",
        "spanish": "es",
        "german": "de",
    }
    return aliases.get(v, v[:16] or "en")


def language_display_name(code: str) -> str:
    names = {
        "zh": "Chinese (Mandarin)",
        "en": "English",
        "ja": "Japanese",
        "ko": "Korean",
        "fr": "French",
        "es": "Spanish",
        "de": "German",
    }
    c = _normalize_language(code)
    return names.get(c, c or "English")


def infer_bgm_lock(prompt: str, audio: dict[str, Any] | None = None) -> dict[str, Any]:
    """Film-wide BGM lock (mood/style). Empty instruments ok — model fills tastefully."""
    audio = audio if isinstance(audio, dict) else {}
    existing = audio.get("bgm_lock") if isinstance(audio.get("bgm_lock"), dict) else {}
    if existing.get("mood") or existing.get("style") or existing.get("rule"):
        out = {
            "mood": str(existing.get("mood") or "")[:120],
            "style": str(existing.get("style") or "")[:120],
            "instruments": str(existing.get("instruments") or "")[:160],
            "continuity": str(existing.get("continuity") or "same bed across clips; no sudden genre jumps")[
                :200
            ],
            "rule": str(
                existing.get("rule")
                or "Non-vocal underscore only; never drown dialogue; keep one film score identity."
            )[:280],
        }
        return out
    text = (prompt or "").lower()
    mood = "cinematic warm"
    style = "soft orchestral / ambient underscore"
    if any(w in text for w in ("horror", "thriller", "tense", "恐怖", "惊悚")):
        mood, style = "tense / uneasy", "sparse strings + low pulse"
    elif any(w in text for w in ("romance", "love", "valentine", "浪漫", "爱情")):
        mood, style = "tender romantic", "gentle piano + soft strings"
    elif any(w in text for w in ("church", "faith", "sermon", "教堂", "信仰")):
        mood, style = "reverent hopeful", "soft choir pad + organ-like tones (non-vocal bed)"
    elif any(w in text for w in ("action", "chase", "race", "追逐", "动作")):
        mood, style = "driving energetic", "rhythmic percussion + light synth"
    elif any(w in text for w in ("night", "city", "neon", "夜", "城市")):
        mood, style = "nocturnal urban", "subtle electronic ambient"
    return {
        "mood": mood,
        "style": style,
        "instruments": "",
        "continuity": "same bed across clips; no sudden genre jumps",
        "rule": "Non-vocal underscore only; never drown dialogue; keep one film score identity.",
    }


def normalize_speech_by_character(
    shot: dict[str, Any],
    characters: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Per-character spoken lines for one shot. Empty dict = intentional silence."""
    if not isinstance(shot, dict):
        return {}
    raw = shot.get("speech_by_character")
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for cid, line in raw.items():
            text = str(line or "").strip()
            if text:
                out[str(cid).strip()] = text[:280]
    if out:
        return out
    # Legacy single line → assign to first on-screen / featured character.
    line = str(
        shot.get("speech_line") or shot.get("dialogue") or shot.get("speech") or ""
    ).strip()
    if not line:
        return {}
    cast = [
        str(x)
        for x in (
            shot.get("on_screen")
            or shot.get("featured_cast_ids")
            or shot.get("character_ids")
            or []
        )
        if str(x).strip()
    ]
    if not cast and characters:
        for c in characters:
            if isinstance(c, dict) and c.get("id"):
                cast.append(str(c["id"]))
                break
    if cast:
        out[cast[0]] = line[:280]
    else:
        out["narrator"] = line[:280]
    return out


def speech_line_from_by_character(by_char: dict[str, str]) -> str:
    parts = [f"{cid}: {line}" for cid, line in by_char.items() if str(line).strip()]
    return " | ".join(parts)[:500]


def ensure_audio_locks_on_analysis(
    analysis: dict[str, Any],
    prompt: str = "",
) -> dict[str, Any]:
    """Stamp language_lock + bgm_lock + per-shot speech onto script_analysis."""
    out = dict(analysis or {})
    audio = dict(out.get("audio") or {}) if isinstance(out.get("audio"), dict) else {}
    user = str(prompt or out.get("user_prompt") or "")
    lang = infer_language_lock(
        user,
        hint=str(out.get("language_lock") or audio.get("language_lock") or ""),
    )
    out["language_lock"] = lang
    audio["language_lock"] = lang
    bgm = infer_bgm_lock(user, {**audio, "bgm_lock": out.get("bgm_lock") or audio.get("bgm_lock")})
    out["bgm_lock"] = bgm
    audio["bgm_lock"] = bgm
    if audio.get("policy") != "silent":
        # Narrative default: keep music bed; speech when any dialogue field exists later.
        if "include_music" not in audio:
            audio["include_music"] = True
        if audio.get("include_speech") and audio.get("include_music"):
            audio["policy"] = "speech_and_music"
        elif audio.get("include_speech"):
            audio["policy"] = "speech"
        elif audio.get("include_music") and audio.get("policy") not in {
            "music",
            "optional_music",
            "speech_and_music",
        }:
            audio["policy"] = "optional_music"
    characters = [c for c in (out.get("characters") or []) if isinstance(c, dict)]
    shots = []
    any_speech = False
    for shot in out.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        s = dict(shot)
        by_char = normalize_speech_by_character(s, characters)
        s["speech_by_character"] = by_char
        joined = speech_line_from_by_character(by_char)
        if joined:
            s["speech_line"] = joined
            any_speech = True
        elif "speech_line" not in s:
            s["speech_line"] = ""
        s["language_lock"] = str(s.get("language_lock") or lang)
        shots.append(s)
    if shots:
        out["shots"] = shots
    if any_speech and audio.get("policy") != "silent":
        audio["include_speech"] = True
        if audio.get("include_music"):
            audio["policy"] = "speech_and_music"
        else:
            audio["policy"] = "speech"
    out["audio"] = audio
    return out


def audio_lock_prompt_block(
    *,
    language_lock: str = "",
    speech_by_character: dict[str, str] | None = None,
    speech_line: str = "",
    bgm_lock: dict[str, Any] | None = None,
    include_speech: bool = False,
    include_music: bool = False,
    clip_embedded: bool = False,
) -> str:
    """Manager / clip prompt clause for locked speech + BGM (+ video-model audio)."""
    lines: list[str] = []
    lang = _normalize_language(language_lock) if language_lock else ""
    if lang:
        lines.append(
            f"LANGUAGE LOCK: all spoken dialogue in this film must be "
            f"{language_display_name(lang)} ({lang}). Do not switch languages."
        )
    by_char = speech_by_character if isinstance(speech_by_character, dict) else {}
    if include_speech or by_char or speech_line:
        if by_char:
            bits = "; ".join(f"{cid} says exactly: \"{line}\"" for cid, line in by_char.items())
            lines.append(
                f"SPEECH LOCK (per character this clip): {bits}. "
                "Lip-sync timing; do not invent extra lines or off-cast speakers."
            )
        elif speech_line:
            lines.append(
                f"SPEECH LOCK (this clip): speak exactly: \"{speech_line[:280]}\". "
                "No extra dialogue."
            )
        elif include_speech:
            lines.append(
                "SPEECH LOCK: include natural diegetic speech for on-screen cast this beat "
                f"in {language_display_name(lang or 'en')}; keep lines short."
            )
    else:
        lines.append("SPEECH LOCK: intentional silence this beat — no invented dialogue.")
    bgm = bgm_lock if isinstance(bgm_lock, dict) else {}
    if include_music or bgm:
        lines.append(
            "BGM LOCK (film-wide): "
            f"mood={bgm.get('mood') or 'cinematic'}; "
            f"style={bgm.get('style') or 'soft underscore'}; "
            f"instruments={bgm.get('instruments') or 'tasteful non-vocal'}; "
            f"continuity={bgm.get('continuity') or 'same bed across clips'}. "
            f"{str(bgm.get('rule') or 'Never drown dialogue; one score identity.')}"
        )
    elif not include_speech and not by_char and not speech_line:
        lines.append("BGM LOCK: no underscore this beat unless storyboard requires it.")
    if clip_embedded and (include_speech or include_music or by_char or speech_line or bgm):
        lines.append(
            "AUDIO ROUTE: no separate TTS/BGM backend — generate native speech + BGM "
            "inside this video clip (video-model audio=true). Keep speech intelligible; "
            "BGM under dialogue."
        )
    return "\n".join(lines).strip()


def resolve_audio_intent_flags(meta: dict[str, Any] | None, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve include_speech / include_music / language / bgm from graph meta + node cfg."""
    meta = meta if isinstance(meta, dict) else {}
    cfg = cfg if isinstance(cfg, dict) else {}
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    audio = {}
    if isinstance(meta.get("audio_intent"), dict):
        audio = dict(meta["audio_intent"])
    if isinstance(analysis.get("audio"), dict):
        audio = {**audio, **analysis["audio"]}
    routing = meta.get("audio_routing") if isinstance(meta.get("audio_routing"), dict) else {}
    lang = str(
        cfg.get("language_lock")
        or analysis.get("language_lock")
        or audio.get("language_lock")
        or meta.get("language_lock")
        or "en"
    )
    bgm = cfg.get("bgm_lock") if isinstance(cfg.get("bgm_lock"), dict) else None
    if not bgm:
        bgm = analysis.get("bgm_lock") if isinstance(analysis.get("bgm_lock"), dict) else None
    if not bgm:
        bgm = audio.get("bgm_lock") if isinstance(audio.get("bgm_lock"), dict) else None
    by_char = cfg.get("speech_by_character") if isinstance(cfg.get("speech_by_character"), dict) else {}
    speech_line = str(cfg.get("speech_line") or "").strip()
    include_speech = bool(
        audio.get("include_speech")
        or by_char
        or speech_line
        or cfg.get("include_speech")
    )
    include_music = bool(
        audio.get("include_music")
        or audio.get("policy") in {"optional_music", "music", "speech_and_music"}
        or cfg.get("include_music")
        or bgm
    )
    if str(audio.get("policy") or "") == "silent":
        include_speech = False
        include_music = False
    clip_embedded = bool(
        cfg.get("clip_embedded_audio")
        or routing.get("clip_embedded")
        or meta.get("prefer_wan3_clip_audio")
        or cfg.get("prefer_wan3_clip_audio")
    )
    return {
        "language_lock": _normalize_language(lang),
        "bgm_lock": bgm or {},
        "speech_by_character": {
            str(k): str(v)[:280] for k, v in by_char.items() if str(v).strip()
        },
        "speech_line": speech_line,
        "include_speech": include_speech,
        "include_music": include_music,
        "clip_embedded": clip_embedded,
        "policy": str(audio.get("policy") or ""),
    }


def should_request_video_audio(cfg: dict[str, Any] | None, meta: dict[str, Any] | None) -> bool:
    """Whether the video model should synthesize native audio for this clip."""
    flags = resolve_audio_intent_flags(meta, cfg)
    if flags["policy"] == "silent":
        return False
    if not (flags["include_speech"] or flags["include_music"] or flags["speech_by_character"] or flags["speech_line"]):
        # Still request audio when clip-embedded routing is on (soft bed).
        return bool(flags["clip_embedded"])
    if flags["clip_embedded"]:
        return True
    cfg = cfg if isinstance(cfg, dict) else {}
    if cfg.get("prefer_wan3_clip_audio") or cfg.get("video_audio"):
        return True
    meta = meta if isinstance(meta, dict) else {}
    return bool(meta.get("prefer_wan3_clip_audio") or meta.get("video_audio"))


def video_model_supports_native_audio(model: str) -> bool:
    return "wan3" in (model or "").strip().lower()


def wan3_audio_model_name() -> str:
    return (
        os.environ.get("WAN3_AUDIO_MODEL")
        or os.environ.get("VIDEO_GEN_WAN3_MODEL")
        or _WAN3_AUDIO_MODEL
    ).strip() or _WAN3_AUDIO_MODEL


def resolve_video_audio_request(
    cfg: dict[str, Any] | None,
    meta: dict[str, Any] | None,
    *,
    current_model: str | None = None,
) -> tuple[bool, str | None]:
    """Return (audio_flag, model_override). Override to wan3 when audio needed but model lacks it."""
    want = should_request_video_audio(cfg, meta)
    if not want:
        return False, None
    model = (current_model or os.environ.get("VIDEO_GEN_MODEL_NAME") or "wan2.6-t2v").strip()
    if video_model_supports_native_audio(model):
        return True, None
    return True, wan3_audio_model_name()


def stamp_audio_fields_on_clip_config(
    cfg: dict[str, Any],
    *,
    shot: dict[str, Any] | None = None,
    analysis: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    clip_embedded: bool = False,
) -> dict[str, Any]:
    """Mutate clip config with locked speech/BGM/language + video-audio flags."""
    analysis = analysis if isinstance(analysis, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    shot = shot if isinstance(shot, dict) else {}
    audio = analysis.get("audio") if isinstance(analysis.get("audio"), dict) else {}
    if not audio and isinstance(meta.get("audio_intent"), dict):
        audio = meta["audio_intent"]
    lang = str(
        shot.get("language_lock")
        or analysis.get("language_lock")
        or audio.get("language_lock")
        or "en"
    )
    by_char = normalize_speech_by_character(shot, analysis.get("characters") or [])
    if not by_char and isinstance(cfg.get("speech_by_character"), dict):
        by_char = normalize_speech_by_character(
            {"speech_by_character": cfg["speech_by_character"], "speech_line": cfg.get("speech_line")},
            analysis.get("characters") or [],
        )
    speech_line = speech_line_from_by_character(by_char) or str(
        shot.get("speech_line") or cfg.get("speech_line") or ""
    ).strip()
    bgm = shot.get("bgm_lock") if isinstance(shot.get("bgm_lock"), dict) else None
    if not bgm:
        bgm = analysis.get("bgm_lock") if isinstance(analysis.get("bgm_lock"), dict) else None
    if not bgm:
        bgm = audio.get("bgm_lock") if isinstance(audio.get("bgm_lock"), dict) else infer_bgm_lock("")
    cfg["language_lock"] = _normalize_language(lang)
    cfg["speech_by_character"] = by_char
    cfg["speech_line"] = speech_line
    cfg["bgm_lock"] = bgm
    include_speech = bool(audio.get("include_speech") or by_char or speech_line)
    include_music = bool(
        audio.get("include_music")
        or audio.get("policy") in {"optional_music", "music", "speech_and_music"}
        or bgm
    )
    if str(audio.get("policy") or "") == "silent":
        include_speech = False
        include_music = False
    cfg["include_speech"] = include_speech
    cfg["include_music"] = include_music
    if clip_embedded or (
        (include_speech or include_music)
        and not bool((meta.get("audio_routing") or {}).get("can_speech"))
        and not bool((meta.get("audio_routing") or {}).get("can_music"))
    ):
        cfg["clip_embedded_audio"] = True
        cfg["prefer_wan3_clip_audio"] = True
        cfg["video_audio"] = True
    block = audio_lock_prompt_block(
        language_lock=cfg["language_lock"],
        speech_by_character=by_char,
        speech_line=speech_line,
        bgm_lock=bgm,
        include_speech=include_speech,
        include_music=include_music,
        clip_embedded=bool(cfg.get("clip_embedded_audio")),
    )
    gen = dict(cfg.get("generate") or {})
    prompt = str(gen.get("prompt") or "")
    if block and "LANGUAGE LOCK" not in prompt and "SPEECH LOCK" not in prompt:
        gen["prompt"] = (prompt + "\n" + block).strip()
        cfg["generate"] = gen
    return cfg
