# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Clip node handler: generate one video per storyboard shot."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_ROLE_FRAME,
    NODE_ROLE_SCENE,
    NODE_ROLE_STORYBOARD,
    NODE_TYPE_VIDEO,
    AssetRef,
    DesignerExecutionGraph,
    DesignerGraphNode,
    node_pipeline,
    node_shot_index,
)
from jiuwenswarm.server.runtime.designer.handlers.common import (
    graph_prompt,
    node_generate_prompt,
    node_output_image_paths,
    role_output_image_path,
    role_output_text,
)
from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
    StoryboardShot,
    parse_storyboard_shots,
)
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext, NodeResult

logger = logging.getLogger(__name__)


def _find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        exe = str(imageio_ffmpeg.get_ffmpeg_exe() or "").strip()
        return exe or None
    except Exception:
        logger.debug("imageio_ffmpeg unavailable for still→mp4", exc_info=True)
        return None


def still_image_to_mp4(
    image: Path,
    *,
    duration: int = 5,
    dest: Path | None = None,
) -> Path:
    """Local fallback: hold a still as a real mp4 when remote I2V returns no URL."""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg unavailable for still→mp4 fallback")
    if not image.is_file():
        raise RuntimeError(f"still image missing: {image}")
    out = dest or (image.parent / f"{image.stem}_still_{max(2, min(10, int(duration)))}s.mp4")
    sec = max(2, min(10, int(duration or 5)))
    # -loop 1 + -t produces a valid H.264 mp4 compose can concatenate.
    proc = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loop",
            "1",
            "-i",
            str(image.resolve()),
            "-t",
            str(sec),
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "24",
            str(out.resolve()),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size <= 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise RuntimeError(f"still→mp4 failed: {detail or 'ffmpeg error'}")
    return out.resolve()

_TIMELINE_NUM = re.compile(r"\d+(?:\.\d+)?")


def parse_shot_duration_seconds(timeline: str, default: int = 5) -> int:
    nums = [float(item) for item in _TIMELINE_NUM.findall(timeline or "")]
    if len(nums) >= 2 and nums[1] > nums[0]:
        span = int(round(nums[1] - nums[0]))
        return max(2, min(10, span if span > 0 else default))
    return max(2, min(10, int(default)))


def collect_clip_first_frame(
    ctx: NodeExecutionContext | None,
    shot_index: int = 1,
) -> Path | None:
    """Use the keyframe node that matches this shot."""
    if ctx is None:
        return None
    frames = [
        node
        for node in (ctx.graph.get("nodes") or [])
        if node_pipeline(node) == NODE_ROLE_FRAME
    ]
    matched = next(
        (node for node in frames if node_shot_index(node) == shot_index),
        frames[0] if len(frames) == 1 else None,
    )
    if matched is not None:
        paths = node_output_image_paths(ctx, str(matched.get("id") or ""))
        if paths:
            if node_shot_index(matched) == shot_index or len(paths) == 1:
                return paths[0]
            index = max(1, int(shot_index)) - 1
            if index < len(paths):
                return paths[index]
            return None
    return role_output_image_path(ctx, NODE_ROLE_STORYBOARD)


def collect_clip_reference_images(
    ctx: NodeExecutionContext | None,
    shot_index: int = 1,
    node: DesignerGraphNode | None = None,
) -> list[Path]:
    """I2V refs: prefer keyframe only.

    Passing solo cast sheets *plus* a keyframe that already contains those faces
    commonly clones a person (one moving, one frozen in the previous pose). When a
    keyframe exists, use it alone as the first-frame / identity source.
    """
    from jiuwenswarm.server.runtime.designer.handlers.common import (
        node_ids_output_image_paths,
        role_output_image_paths,
    )

    paths: list[Path] = []
    seen: set[str] = set()

    def add(path: Path | None) -> None:
        if path is None:
            return
        resolved = path.resolve()
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        paths.append(resolved)

    first = collect_clip_first_frame(ctx, shot_index)
    if first is not None:
        add(first)
        return paths

    # No keyframe image yet — fall back to cast + scene stills.
    if ctx is not None:
        cfg = node.get("config") if isinstance(node, dict) and isinstance(node.get("config"), dict) else {}
        preferred = [str(x) for x in (cfg.get("character_node_ids") or []) if str(x).strip()]
        if preferred:
            for path in node_ids_output_image_paths(ctx, preferred):
                add(path)
        else:
            for path in role_output_image_paths(ctx, NODE_ROLE_CHARACTER_DESIGN)[:2]:
                add(path)
        scene_paths = role_output_image_paths(ctx, NODE_ROLE_SCENE)
        if scene_paths:
            add(scene_paths[0])
    return paths


def _storyboard_narrative_action(shot: dict[str, Any] | None) -> str:
    """Prefer Comment (keyframe/clip description), then Character action."""
    if not isinstance(shot, dict):
        return ""
    return str(shot.get("comment") or shot.get("character_action") or "").strip()


def _extract_action_from_generate_prompt(text: str) -> str:
    """Pull a clean Action: … beat from a lock-stuffed generate.prompt when present."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    match = re.search(
        r"(?i)(?:^|\n)\s*(?:Primary action for shot\s+\d+\s*:|Action)\s*:\s*(.+?)(?:\n|$)",
        raw,
    )
    if match:
        return match.group(1).strip()[:500]
    # Fallback: first non-lock sentence if prompt is short and narrative-only.
    if not _looks_like_contaminated_prompt(raw) and len(raw) <= 500:
        return raw[:500]
    return ""


def _shot_for_node(
    graph: DesignerExecutionGraph,
    node: DesignerGraphNode,
    ctx: NodeExecutionContext | None,
) -> tuple[int, StoryboardShot | None]:
    index = node_shot_index(node)
    text = role_output_text(ctx, NODE_ROLE_STORYBOARD) if ctx is not None else ""
    shots = parse_storyboard_shots(text)
    if shots and 1 <= index <= len(shots):
        shot = dict(shots[index - 1])
        # Keep live storyboard comment/character_action intact.
        # Do NOT overwrite comment with generate.prompt (often lock-stuffed / stale).
        sb_action = _storyboard_narrative_action(shot)
        if not str(shot.get("character_action") or "").strip() and sb_action:
            shot["character_action"] = sb_action
        if not str(shot.get("comment") or "").strip() and sb_action:
            shot["comment"] = sb_action
        return index, shot
    return index, None


def _looks_like_contaminated_prompt(text: str) -> bool:
    """True only for pasted handoff/assignment dumps — not normal SCENE BIBLE / staging."""
    raw = (text or "").upper()
    needles = (
        "PRIOR KEYFRAME PROMPT",
        "PREVIOUS KEYFRAME HAD",
        "PRIOR CLIP CONTINUITY",
        "PREVIOUS CLIP HAD",
        "YOUR ASSIGNMENT",
        "MASTER SCENE PROMPT",
        "CONTINUITY CARD (MANAGER)",
        "PRIOR CLIP WAN",
        "PREVIOUS WAN PROMPT",
    )
    return any(n in raw for n in needles)


def _format_shot_block(shot: StoryboardShot, shot_index: int) -> str:
    lines = [
        f"Shot {shot_index} ONLY (do not film other shots)",
        f"- Timeline: {shot.get('timeline') or ''}",
        f"- Camera: {shot.get('camera') or ''}",
        f"- Camera move: {shot.get('move') or ''}",
        f"- Character action: {shot.get('character_action') or ''}",
        f"- Scene change: {shot.get('scene_change') or ''}",
    ]
    comment = str(shot.get("comment") or "").strip()
    if comment:
        lines.append(f"- Shot description: {comment}")
    return "\n".join(lines)


def _clip_prompt_lead(
    shot_index: int,
    duration: int,
    *,
    has_character: bool,
    has_scene: bool,
    has_frame: bool,
    focus_names: str = "",
    continuity: bool = False,
    video_style: str = "",
) -> str:
    attached: list[str] = []
    if has_character:
        attached.append("character sheet for this shot's cast")
    if has_scene:
        attached.append("scene")
    if continuity:
        attached.append("previous shot keyframe for continuity")
    if has_frame:
        attached.append("this shot's keyframe as the first frame")
    extras = (
        " Explicit visual inputs are attached, in order: " + ", ".join(attached) + "."
        if attached
        else ""
    )
    focus = f" Feature only: {focus_names}." if focus_names else ""
    animate = (
        "Animate ONLY the attached first-frame keyframe. "
    )
    if str(video_style or "").strip() == "final_frame_reverse":
        animate = (
            "Animate ONLY the attached first-frame keyframe (this beat's START). "
            "The user reference / classic still is the FILM'S last-second ENDPOINT, "
            "not a turntable subject — motion must push the arc toward that final "
            "composition; if this is a late beat, decelerate and settle into it. "
        )
    return (
        f"Create shot {shot_index} as a {duration}-second video — unique action for THIS shot only."
        f"{extras}{focus} "
        f"{animate}"
        "ONE instance per person — never clone/duplicate a face in two places at once. "
        "Do not invent new people or a new crowd; keep the same extras layout as the keyframe. "
        "Keep identity and location consistent; camera/action must match this shot only. "
        "No subtitles, no cutaways.\n\n"
    )


def build_clip_prompt(
    graph: DesignerExecutionGraph,
    node: DesignerGraphNode,
    ctx: NodeExecutionContext | None = None,
) -> str:
    """Shot-specific clip prompt. Prefer shot row over full Brief to avoid identical clips."""
    shot_index, shot = _shot_for_node(graph, node, ctx)
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    duration = parse_shot_duration_seconds((shot or {}).get("timeline") or "", default=5)
    focus_names = ""
    cfg_names = [str(x).strip() for x in (cfg.get("cast_names") or []) if str(x).strip()]
    if cfg_names:
        focus_names = ", ".join(cfg_names)
    if ctx is not None:
        preferred = [str(x) for x in (cfg.get("character_node_ids") or []) if str(x).strip()]
        has_character = bool(preferred) or role_output_image_path(ctx, NODE_ROLE_CHARACTER_DESIGN) is not None
        has_scene = role_output_image_path(ctx, NODE_ROLE_SCENE) is not None
    else:
        has_character = False
        has_scene = False
    has_frame = collect_clip_first_frame(ctx, shot_index) is not None
    continuity = bool(str(cfg.get("continuity_frame_node_id") or "").strip())
    sb_action = _storyboard_narrative_action(shot if isinstance(shot, dict) else None)
    action = str(
        sb_action
        or cfg.get("shot_action")
        or (shot or {}).get("character_action")
        or (shot or {}).get("comment")
        or ""
    ).strip()
    camera = str(
        (shot or {}).get("camera") or cfg.get("camera") or ""
    ).strip()
    style_id = str(cfg.get("video_style") or "").strip()
    style_clause = ""
    try:
        from jiuwenswarm.server.runtime.designer.video_styles import (
            resolve_video_style,
            video_style_clause as _video_style_clause,
        )

        if not style_id:
            meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
            style_id = str(meta.get("video_style") or "").strip() or resolve_video_style(graph)
        style_clause = _video_style_clause(style_id, for_clip=True) if style_id else ""
    except Exception:  # noqa: BLE001
        pass
    parts: list[str] = [
        _clip_prompt_lead(
            shot_index,
            duration,
            has_character=has_character,
            has_scene=has_scene,
            has_frame=has_frame,
            focus_names=focus_names,
            continuity=continuity,
            video_style=style_id,
        )
    ]
    # One-line style from Brief only (not the full brief — that homogenizes all clips).
    if ctx is not None:
        brief = role_output_text(ctx, NODE_ROLE_BRIEF)
        if brief:
            for line in brief.splitlines():
                if "visual style" in line.lower() or line.lower().startswith("**visual"):
                    parts.append(line.strip())
                    break
    if style_clause:
        parts.append(style_clause)
    if action:
        parts.append(f"Primary action for shot {shot_index}: {action}")
    if camera:
        parts.append(f"Camera for shot {shot_index}: {camera}")
    identity = cfg.get("identity_refs") if isinstance(cfg.get("identity_refs"), dict) else {}
    setting_id = str(cfg.get("setting_id") or (shot or {}).get("setting_id") or "").strip()
    if setting_id:
        parts.append(f"Setting lock for this clip only: {setting_id} — do not borrow another scene.")
    bible = cfg.get("scene_bible") if isinstance(cfg.get("scene_bible"), dict) else None
    if not bible and isinstance(identity.get("scene_bible"), dict):
        bible = identity["scene_bible"]
    if bible:
        parts.append(
            "SCENE BIBLE (architecture/objects/light — keep; only animate this beat): "
            f"place={bible.get('place')}; lighting={bible.get('lighting')}; "
            f"objects={', '.join(str(x) for x in (bible.get('objects') or [])[:6])}; "
            f"crowd={bible.get('crowd')}."
        )
    view_key = str(cfg.get("view_key") or "").strip()
    if view_key:
        parts.append(f"Active view_key: {view_key}")
    costume_lock = str(identity.get("costume_lock") or cfg.get("costume_lock") or "").strip()
    if not costume_lock:
        try:
            from jiuwenswarm.server.runtime.designer.experiments.clothing_lock import (
                ensure_cfg_clothing_lock,
            )

            analysis_chars = list(
                ((graph.get("metadata") or {}).get("script_analysis") or {}).get("characters")
                or []
            )
            costume_lock = ensure_cfg_clothing_lock(cfg, characters=analysis_chars)
        except Exception:  # noqa: BLE001
            costume_lock = ""
    if costume_lock:
        from jiuwenswarm.server.runtime.designer.experiments.clothing_lock import (
            clothing_lock_clause,
        )

        cloth = clothing_lock_clause(costume_lock, for_clip=True)
        parts.append(cloth if cloth else f"Costume / identity lock (do not redesign): {costume_lock}")
    try:
        from jiuwenswarm.server.runtime.designer.experiments.shot_staging_lock import (
            ensure_cfg_staging_locks,
            staging_locks_from_cfg,
        )

        analysis_chars = list(
            ((graph.get("metadata") or {}).get("script_analysis") or {}).get("characters")
            or []
        )
        shot_row = shot if isinstance(shot, dict) else None
        # Staging must use the preferred narrative (storyboard), not stale cfg.shot_action.
        staging_cfg = dict(cfg)
        if action:
            staging_cfg["shot_action"] = action
        staging = ensure_cfg_staging_locks(
            staging_cfg, shot=shot_row, characters=analysis_chars
        ) or staging_locks_from_cfg(staging_cfg, for_clip=True)
        if staging and not any("STAGING LOCK" in p for p in parts):
            parts.append(staging)
    except Exception:  # noqa: BLE001
        pass
    char_nodes = [
        str(x)
        for x in (identity.get("character_node_ids") or cfg.get("character_node_ids") or [])
        if str(x).strip()
    ]
    if char_nodes and not has_frame:
        parts.append(
            f"Use character reference sheets from nodes: {', '.join(char_nodes)}. "
            "Match faces and wardrobe exactly. One instance per person — no clones."
        )
    elif has_frame:
        parts.append(
            "ANTI-CLONE: the keyframe already contains the cast — animate those bodies only; "
            "do not spawn a second copy of anyone."
        )
    lock = cfg.get("continuity_lock") if isinstance(cfg.get("continuity_lock"), dict) else None
    if not lock and action:
        from jiuwenswarm.server.runtime.designer.continuity import infer_continuity_lock

        lock = infer_continuity_lock(action)
    if lock:
        from jiuwenswarm.server.runtime.designer.continuity import continuity_prompt_clause

        clause = continuity_prompt_clause(lock)
        if clause:
            parts.append(clause.strip())
    # THIS shot only — never dump the full storyboard (homogenizes / mixes scenes).
    if shot is not None:
        parts.append(_format_shot_block(shot, shot_index))
    else:
        parts.append(
            f"Storyboard beat for shot {shot_index} only "
            f"(action={action or 'see keyframe'}; camera={camera or 'match keyframe'})."
        )
    override = str((cfg.get("generate") or {}).get("prompt") or "").strip() if isinstance(cfg.get("generate"), dict) else ""
    # When live storyboard already provided the beat, skip stale generate.prompt narratives.
    if override and not sb_action:
        extracted = _extract_action_from_generate_prompt(override)
        if extracted and extracted.casefold() not in (action or "").casefold():
            if not action:
                action = extracted
                parts.append(f"Primary action for shot {shot_index}: {action}")
            elif not _looks_like_contaminated_prompt(override):
                parts.append(f"Supervisor shot brief: {extracted[:400]}")
        elif (
            not extracted
            and not _looks_like_contaminated_prompt(override)
            and override.casefold() not in (action or "").casefold()
        ):
            parts.append(f"Supervisor shot brief: {override[:400]}")
    already_done = [str(x) for x in (cfg.get("already_done") or []) if str(x)]
    occupancy = cfg.get("occupancy") if isinstance(cfg.get("occupancy"), dict) else {}
    if occupancy:
        parts.append(
            f"OCCUPANCY: must_appear={occupancy.get('must_appear')}; "
            f"featured={occupancy.get('featured')}; "
            f"offscreen={occupancy.get('offscreen') or cfg.get('offscreen') or []}."
        )
    from jiuwenswarm.server.runtime.designer.experiments.clip_prompt_handoff import (
        collect_prior_clip_prompts,
        handoff_clause_for_prompt,
    )

    prior = collect_prior_clip_prompts(graph, shot_index=shot_index)
    if not prior and str(cfg.get("previous_clip_action") or "").strip():
        prior = [
            {
                "node_id": str(cfg.get("previous_clip_node_id") or ""),
                "shot_index": int(
                    cfg.get("previous_clip_shot_index") or max(1, shot_index - 1)
                ),
                "shot_action": str(cfg.get("previous_clip_action") or ""),
                "speech_line": str(cfg.get("previous_clip_speech") or ""),
            }
        ]
    clause = handoff_clause_for_prompt(
        prior,
        this_shot_index=shot_index,
        this_action=action,
        this_camera=camera,
        this_speech=str(cfg.get("speech_line") or ""),
        already_done=already_done,
    )
    joined = "\n".join(parts)
    if clause and "PREVIOUS CLIP HAD" not in joined and "PRIOR CLIP CONTINUITY" not in joined:
        parts.append(clause)
    from jiuwenswarm.server.runtime.designer.user_references import (
        graph_user_references,
        prompt_slot_roster,
        user_reference_video_path,
        user_reference_audio_path,
    )

    roster = prompt_slot_roster(graph_user_references(graph))
    if roster:
        parts.append(
            "User reference slots (original files are visual/audio authority). "
            "Video and audio are generic references — not the first frame or keyframe:\n"
            f"{roster}"
        )
    if user_reference_video_path(graph) is not None:
        parts.append(
            "Attached video 1 is a motion/style reference only. "
            "Do not treat it as this shot's first frame."
        )
    if user_reference_audio_path(graph) is not None:
        parts.append(
            "Attached audio 1 is a soundtrack/voice reference only; "
            "do not invent a conflicting score."
        )
    # Locked speech / language / BGM (Supervisor storyboard + Manager).
    from jiuwenswarm.server.runtime.designer.audio_locks import (
        audio_lock_prompt_block,
        resolve_audio_intent_flags,
    )

    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    flags = resolve_audio_intent_flags(meta, cfg if isinstance(cfg, dict) else {})
    block = audio_lock_prompt_block(
        language_lock=str(flags.get("language_lock") or ""),
        speech_by_character=flags.get("speech_by_character") or {},
        speech_line=str(flags.get("speech_line") or ""),
        bgm_lock=flags.get("bgm_lock") or {},
        include_speech=bool(flags.get("include_speech")),
        include_music=bool(flags.get("include_music")),
        clip_embedded=bool(flags.get("clip_embedded")),
    )
    if block and "LANGUAGE LOCK" not in "\n".join(parts) and "SPEECH LOCK" not in "\n".join(parts):
        parts.append(block)
    return "\n\n".join(part.strip() for part in parts if part.strip())[:6000]


async def generate_clip_video(
    prompt: str,
    save_dir: str | None = None,
    first_frame: str | None = None,
    reference_images: list[str] | None = None,
    reference_file: str | None = None,
    duration: int = 5,
    audio: bool | None = None,
    size: str | None = None,
    resolution: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Call the shared video-generation stack. Tests monkeypatch this function."""
    from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
        apply_video_gen_model_config_from_yaml,
    )
    from jiuwenswarm.agents.harness.common.tools.video_tools import (
        _invoke_model_video_generation,
    )
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.common.utils import get_env_file
    from jiuwenswarm.dotenv_early import load_dotenv_runtime

    try:
        load_dotenv_runtime(dotenv_path=get_env_file(), override=True)
    except Exception:
        logger.debug("Failed to reload video_gen env before generation", exc_info=True)

    try:
        apply_video_gen_model_config_from_yaml(get_config())
    except Exception:
        logger.debug("Failed to apply video_gen model config from yaml", exc_info=True)

    # Honor film aspect_lock size when stamped; otherwise let the model default.
    video_size = str(size or "").strip() or None
    video_res = str(resolution or "").strip() or None

    result = await _invoke_model_video_generation(
        prompt,
        size=video_size,
        resolution=video_res,
        first_frame=first_frame,
        reference_images=reference_images,
        reference_file=reference_file,
        duration=max(2, min(10, int(duration or 5))),
        audio=audio,
        model=(str(model).strip() or None) if model else None,
    )
    if "error" in result:
        raise RuntimeError(str(result["error"]))

    video_path = str(result.get("video_path") or "").strip()
    if not video_path:
        raise RuntimeError("video generation returned no video_path")

    if save_dir:
        dest_dir = Path(save_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / Path(video_path).name
        Path(video_path).replace(dest)
        result = {**result, "video_path": str(dest.resolve())}

    return result


class ClipNodeHandler:
    """Submit one I2V job per storyboard shot."""

    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        shot_index = node_shot_index(node)
        has_frame_node = any(
            node_pipeline(item) == NODE_ROLE_FRAME for item in (ctx.graph.get("nodes") or [])
        )
        first_frame = collect_clip_first_frame(ctx, shot_index)
        refs = collect_clip_reference_images(ctx, shot_index, node=node)
        _IMG = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
        # Keyframe may be notes-only after image rate limits; fall back to cast/scene stills.
        if first_frame is None:
            first_frame = next(
                (p for p in refs if p.is_file() and p.suffix.lower() in _IMG),
                None,
            )
        if has_frame_node and first_frame is None and not any(
            p.is_file() and p.suffix.lower() in _IMG for p in refs
        ):
            raise RuntimeError(
                f"Shot {shot_index} has no matching keyframe. Regenerate the Keyframe node for this shot first."
            )
        _, shot = _shot_for_node(ctx.graph, node, ctx)
        duration = parse_shot_duration_seconds((shot or {}).get("timeline") or "", default=5)
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        prompt = build_clip_prompt(ctx.graph, node, ctx)
        from jiuwenswarm.server.runtime.designer.experiments.wan_call_locks import (
            apply_wan_call_locks,
        )

        prompt = apply_wan_call_locks(
            prompt,
            cfg=cfg,
            graph=ctx.graph if isinstance(ctx.graph, dict) else {},
            shot_index=shot_index,
            has_first_frame=first_frame is not None,
        )
        if ctx is not None and callable(getattr(ctx, "on_prompt_artifact", None)):
            try:
                ctx.on_prompt_artifact(prompt)
            except Exception:  # noqa: BLE001
                logger.debug("clip early prompt handoff failed", exc_info=True)
        aspect = cfg.get("aspect_lock") if isinstance(cfg.get("aspect_lock"), dict) else {}
        if not aspect:
            meta = (ctx.graph.get("metadata") or {}) if isinstance(ctx.graph, dict) else {}
            aspect = meta.get("aspect_lock") if isinstance(meta.get("aspect_lock"), dict) else {}
        video_size = str(
            cfg.get("video_size") or (aspect or {}).get("video_size") or ""
        ).strip() or None
        video_res = str(
            cfg.get("video_resolution") or (aspect or {}).get("video_resolution") or ""
        ).strip() or None
        # Do not re-send the keyframe as a second identity sheet (avoids face clones).
        ff_key = str(first_frame.resolve()) if first_frame is not None else ""
        extra_refs = [
            str(path)
            for path in refs
            if path.is_file() and (not ff_key or str(path.resolve()) != ff_key)
        ]
        from jiuwenswarm.server.runtime.designer.user_references import (
            user_reference_video_path,
        )

        user_video = user_reference_video_path(ctx.graph)
        # User video is a generic reference, never the I2V first frame.
        reference_file = (
            str(user_video.resolve())
            if user_video is not None and user_video.is_file()
            else None
        )
        from jiuwenswarm.server.runtime.designer.audio_locks import resolve_video_audio_request

        meta = (ctx.graph.get("metadata") or {}) if isinstance(ctx.graph, dict) else {}
        want_audio, _model_override = resolve_video_audio_request(cfg, meta)
        try:
            result = await generate_clip_video(
                prompt,
                first_frame=str(first_frame) if first_frame is not None else None,
                reference_images=extra_refs or None,
                reference_file=reference_file,
                duration=duration,
                size=video_size,
                resolution=video_res,
                audio=True if want_audio else False,
                model=None,
            )
            path = Path(str(result["video_path"]))
            message = f"clip {shot_index} generated" + (" (with audio)" if want_audio else "")
        except Exception as exc:
            meta = (ctx.graph.get("metadata") or {}) if isinstance(ctx.graph, dict) else {}
            allow_still = bool(
                meta.get("allow_still_clip_fallback")
                or (node.get("config") or {}).get("allow_still_clip_fallback")
            )
            if not allow_still:
                # Still→mp4 is not a film beat — fail so the pipeline retries / surfaces error.
                raise RuntimeError(
                    f"I2V failed for shot {shot_index} and still→mp4 fallback is disabled: {exc}"
                ) from exc
            _IMG = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

            def _is_image(p: Path | None) -> bool:
                return bool(p and p.is_file() and p.suffix.lower() in _IMG)

            candidates = [first_frame, *refs]
            still = next((p for p in candidates if _is_image(p)), None)
            if still is None:
                raise
            logger.warning(
                "Remote video failed for shot %s (%s); using still→mp4 fallback from %s",
                shot_index,
                exc,
                still,
            )
            from jiuwenswarm.common.utils import get_agent_workspace_dir

            dest = (
                Path(get_agent_workspace_dir())
                / f"designer_clip_still_{ctx.run_id}_shot{shot_index}.mp4"
            )
            path = still_image_to_mp4(Path(still), duration=duration, dest=dest)
            message = f"clip {shot_index} still→mp4 fallback ({type(exc).__name__})"
        output_ref: AssetRef = {
            "kind": NODE_TYPE_VIDEO,
            "uri": path.resolve().as_uri(),
            "mime_type": "video/mp4",
            "label": path.name,
        }
        return NodeResult(output_ref=output_ref, message=message)
