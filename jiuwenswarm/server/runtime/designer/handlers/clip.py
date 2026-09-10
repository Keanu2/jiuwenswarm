# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Clip node handler: generate one video per storyboard shot."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    GENERATE_PROMPT_ORIGIN_STORYBOARD,
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
    node_generate_prompt_origin,
    node_output_image_paths,
    role_output_image_path,
    role_output_text,
    role_output_text_path,
)
from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
    StoryboardShot,
    parse_storyboard_shots,
)
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext, NodeResult

logger = logging.getLogger(__name__)

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
) -> list[Path]:
    """wan3 reference_image order: this shot's keyframe, then identity, then scene."""
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

    add(collect_clip_first_frame(ctx, shot_index))
    if ctx is not None:
        add(role_output_image_path(ctx, NODE_ROLE_CHARACTER_DESIGN))
        add(role_output_image_path(ctx, NODE_ROLE_SCENE))
    return paths


def collect_clip_storyboard_file(ctx: NodeExecutionContext | None) -> Path | None:
    if ctx is None:
        return None
    return role_output_text_path(ctx, NODE_ROLE_STORYBOARD)


def _graph_has_role(graph: DesignerExecutionGraph, role: str) -> bool:
    return any(node_role(node) == role for node in (graph.get("nodes") or []))


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
        origin = node_generate_prompt_origin(node)
        if override and origin != GENERATE_PROMPT_ORIGIN_STORYBOARD:
            shot["comment"] = override
        return index, shot
    return index, None


def _format_shot_block(shot: StoryboardShot, shot_index: int) -> str:
    lines = [
        f"Shot {shot_index} (shot no. {shot.get('shot_no') or shot_index})",
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
    has_storyboard: bool,
) -> str:
    image_n = 1
    lines = [
        f"Create shot {shot_index} as a {duration}-second video.",
        "Use the attached references. Image N matches the media array order.",
    ]
    if has_frame:
        lines.append(
            f"Image {image_n} is this shot's keyframe. Animate that composition; keep this camera and pose."
        )
        image_n += 1
    if has_character:
        lines.append(
            f"Image {image_n} is the character sheet for identity and costume only. "
            "Do not replace the keyframe framing with the character sheet."
        )
        image_n += 1
    if has_scene:
        lines.append(
            f"Image {image_n} is the scene. Keep location, lighting, and weather."
        )
    if has_storyboard:
        lines.append("The attached file is the storyboard table. Film only this shot's row.")
    lines.append("Picture and action only. No music, no soundtrack, no BGM, no score.")
    lines.append("No subtitles, no cutaways.")
    return "\n".join(lines) + "\n\n"


def build_clip_prompt(
    graph: DesignerExecutionGraph,
    node: DesignerGraphNode,
    ctx: NodeExecutionContext | None = None,
) -> str:
    """This shot's storyboard row, full table, then brief. Sibling clips submit other shots."""
    shot_index, shot = _shot_for_node(graph, node, ctx)
    duration = parse_shot_duration_seconds((shot or {}).get("timeline") or "", default=5)
    has_character = (
        ctx is not None and role_output_image_path(ctx, NODE_ROLE_CHARACTER_DESIGN) is not None
    )
    has_scene = ctx is not None and role_output_image_path(ctx, NODE_ROLE_SCENE) is not None
    has_frame = collect_clip_first_frame(ctx, shot_index) is not None
    storyboard = role_output_text(ctx, NODE_ROLE_STORYBOARD) if ctx is not None else ""
    parts: list[str] = [
        _clip_prompt_lead(
            shot_index,
            duration,
            has_character=has_character,
            has_scene=has_scene,
            has_frame=has_frame,
            has_storyboard=bool(storyboard or collect_clip_storyboard_file(ctx)),
        )
    ]
    if shot is not None:
        parts.append("This shot from the storyboard:\n" + _format_shot_block(shot, shot_index))
    if storyboard:
        parts.append("Full storyboard table:\n" + storyboard)
    elif shot is None:
        parts.append(graph_prompt(graph, node))
    if ctx is not None:
        brief = role_output_text(ctx, NODE_ROLE_BRIEF)
        if brief:
            parts.append(brief)
    return "\n\n".join(part.strip() for part in parts if part.strip())[:8000]


async def generate_clip_video(
    prompt: str,
    save_dir: str | None = None,
    first_frame: str | None = None,
    reference_images: list[str] | None = None,
    reference_file: str | None = None,
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
        first_frame=first_frame,
        reference_images=reference_images,
        reference_file=reference_file,
        duration=max(2, min(10, int(duration or 5))),
        audio=False,
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
    """Submit one reference-to-video job per storyboard shot."""

    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        shot_index = node_shot_index(node)
        has_frame_node = _graph_has_role(ctx.graph, NODE_ROLE_FRAME)
        first_frame = collect_clip_first_frame(ctx, shot_index)
        if has_frame_node and first_frame is None:
            raise RuntimeError(
                f"Shot {shot_index} has no matching keyframe. Regenerate the Keyframe node for this shot first."
            )
        missing: list[str] = []
        character = role_output_image_path(ctx, NODE_ROLE_CHARACTER_DESIGN)
        scene = role_output_image_path(ctx, NODE_ROLE_SCENE)
        if _graph_has_role(ctx.graph, NODE_ROLE_CHARACTER_DESIGN) and character is None:
            missing.append("Character")
        if _graph_has_role(ctx.graph, NODE_ROLE_SCENE) and scene is None:
            missing.append("Scene")
        if missing:
            raise RuntimeError(
                "Clip generation must send "
                + " and ".join(missing)
                + " as reference images. Finish those nodes first."
            )
        if _graph_has_role(ctx.graph, NODE_ROLE_STORYBOARD) and not role_output_text(
            ctx, NODE_ROLE_STORYBOARD
        ):
            raise RuntimeError(
                "Clip generation must send the Storyboard table. Finish the Storyboard node first."
            )
        _, shot = _shot_for_node(ctx.graph, node, ctx)
        duration = parse_shot_duration_seconds((shot or {}).get("timeline") or "", default=5)
        prompt = build_clip_prompt(ctx.graph, node, ctx)
        refs = collect_clip_reference_images(ctx, shot_index)
        storyboard_file = collect_clip_storyboard_file(ctx)
        identity = character is not None or scene is not None
        # wan3: img_url (I2V) cannot mix with reference_image or file.
        use_reference_mode = (
            identity or storyboard_file is not None or len(refs) > 1
        )
        result = await generate_clip_video(
            prompt,
            first_frame=(
                None
                if use_reference_mode
                else (str(first_frame) if first_frame is not None else None)
            ),
            reference_images=[str(path) for path in refs] or None,
            reference_file=str(storyboard_file) if storyboard_file is not None else None,
            duration=duration,
        )
        path = Path(str(result["video_path"]))
        output_ref: AssetRef = {
            "kind": NODE_TYPE_VIDEO,
            "uri": path.resolve().as_uri(),
            "mime_type": "video/mp4",
            "label": path.name,
        }
        return NodeResult(output_ref=output_ref, message=f"clip {shot_index} generated")
