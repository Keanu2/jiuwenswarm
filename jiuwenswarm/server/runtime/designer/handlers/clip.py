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
    node_role,
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
        if node_role(node) == NODE_ROLE_FRAME
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
    """Per-shot I2V refs: focus cast sheet(s), scene, this keyframe, optional prior keyframe."""
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
        continuity_id = str(cfg.get("continuity_frame_node_id") or "").strip()
        if continuity_id:
            for path in node_ids_output_image_paths(ctx, [continuity_id])[:1]:
                add(path)
    add(collect_clip_first_frame(ctx, shot_index))
    return paths


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
        override = node_generate_prompt(node)
        if override:
            shot["comment"] = override
        return index, shot
    return index, None


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
    return (
        f"Create shot {shot_index} as a {duration}-second video — unique action for THIS shot only."
        f"{extras}{focus} "
        "Keep identity and location consistent, but the camera and action must match this shot "
        "and must not repeat a previous shot. "
        "Start from this shot's keyframe. No subtitles, no cutaways.\n\n"
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
    action = str(
        cfg.get("shot_action")
        or (shot or {}).get("character_action")
        or (shot or {}).get("comment")
        or ""
    ).strip()
    camera = str(cfg.get("camera") or (shot or {}).get("camera") or "").strip()
    parts: list[str] = [
        _clip_prompt_lead(
            shot_index,
            duration,
            has_character=has_character,
            has_scene=has_scene,
            has_frame=has_frame,
            focus_names=focus_names,
            continuity=continuity,
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
    if action:
        parts.append(f"Primary action for shot {shot_index}: {action}")
    if camera:
        parts.append(f"Camera for shot {shot_index}: {camera}")
    identity = cfg.get("identity_refs") if isinstance(cfg.get("identity_refs"), dict) else {}
    costume_lock = str(identity.get("costume_lock") or cfg.get("costume_lock") or "").strip()
    if costume_lock:
        parts.append(f"Costume / identity lock (do not redesign): {costume_lock}")
    char_nodes = [
        str(x)
        for x in (identity.get("character_node_ids") or cfg.get("character_node_ids") or [])
        if str(x).strip()
    ]
    if char_nodes:
        parts.append(
            f"Use character reference sheets from nodes: {', '.join(char_nodes)}. "
            "Match faces and wardrobe exactly."
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
    if shot is not None:
        parts.append(_format_shot_block(shot, shot_index))
    else:
        storyboard = role_output_text(ctx, NODE_ROLE_STORYBOARD) if ctx is not None else ""
        parts.append(storyboard or graph_prompt(graph, node))
    override = str((cfg.get("generate") or {}).get("prompt") or "").strip() if isinstance(cfg.get("generate"), dict) else ""
    if override:
        parts.append(f"Supervisor shot brief: {override}")
    return "\n\n".join(part.strip() for part in parts if part.strip())[:6000]


async def generate_clip_video(
    prompt: str,
    save_dir: str | None = None,
    first_frame: str | None = None,
    reference_images: list[str] | None = None,
    duration: int = 5,
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

    result = await _invoke_model_video_generation(
        prompt,
        # wan3.0: 480P for I2V; matching WxH for T2V/R2V
        size="854*480",
        resolution="480P",
        first_frame=first_frame,
        reference_images=reference_images,
        duration=max(2, min(10, int(duration or 5))),
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
            node_role(item) == NODE_ROLE_FRAME for item in (ctx.graph.get("nodes") or [])
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
        prompt = build_clip_prompt(ctx.graph, node, ctx)
        try:
            result = await generate_clip_video(
                prompt,
                first_frame=str(first_frame) if first_frame is not None else None,
                reference_images=[str(path) for path in refs] or None,
                duration=duration,
            )
            path = Path(str(result["video_path"]))
            message = f"clip {shot_index} generated"
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
