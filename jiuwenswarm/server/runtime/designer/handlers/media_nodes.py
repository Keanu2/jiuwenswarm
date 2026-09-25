# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Generic image / video / audio handlers for canvas media nodes.

Pipeline recipes (character_design, clip, …) keep specialized handlers via
``config.pipeline``. These run when the node is a plain image / video / audio.
"""

from __future__ import annotations

from pathlib import Path

from jiuwenswarm.common.schema.designer_graph import (
    NODE_TYPE_IMAGE,
    NODE_TYPE_VIDEO,
    DesignerGraphNode,
    data_predecessors,
    video_concat_source_ids,
)
from jiuwenswarm.server.runtime.designer.handlers.audio_nodes import MusicNodeHandler
from jiuwenswarm.server.runtime.designer.handlers.clip import generate_clip_video
from jiuwenswarm.server.runtime.designer.handlers.common import (
    file_output_ref,
    graph_prompt,
    graph_workspace_dir,
    node_generate_prompt,
    node_output_image_paths,
    path_from_uri,
    uploaded_material_image_paths,
)
from jiuwenswarm.server.runtime.designer.handlers.compose import ComposeNodeHandler
from jiuwenswarm.server.runtime.designer.handlers.image_nodes import _image_or_notes
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext, NodeResult
from jiuwenswarm.server.runtime.designer.user_references import (
    user_reference_image_paths,
    user_reference_node_file,
)

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _predecessor_ids(ctx: NodeExecutionContext, node: DesignerGraphNode) -> list[str]:
    node_id = str(node.get("id") or ctx.node_id)
    config = node.get("config") if isinstance(node.get("config"), dict) else {}
    declared = [str(item).strip() for item in (config.get("inputs") or []) if str(item).strip()]
    incoming = data_predecessors(ctx.graph).get(node_id, [])
    ordered: list[str] = []
    for item in [*declared, *incoming]:
        if item and item not in ordered:
            ordered.append(item)
    return ordered


def _graph_output_image_paths(ctx: NodeExecutionContext, node_id: str) -> list[Path]:
    paths: list[Path] = []
    for other in ctx.graph.get("nodes") or []:
        if not isinstance(other, dict) or str(other.get("id") or "") != node_id:
            continue
        ref = other.get("output_ref")
        if not isinstance(ref, dict):
            continue
        path = path_from_uri(str(ref.get("uri") or ""))
        if path is None or not path.is_file():
            continue
        kind = str(ref.get("kind") or "").lower()
        mime = str(ref.get("mime_type") or "").lower()
        if kind != "image" and not mime.startswith("image/") and path.suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        paths.append(path.resolve())
    return paths


def _upstream_images(ctx: NodeExecutionContext, node: DesignerGraphNode) -> list[Path]:
    """User stills plus every upstream image. Attachment count is not the input count."""
    seen: set[str] = set()
    paths: list[Path] = []

    def add(path: Path | None) -> None:
        if path is None or not path.is_file():
            return
        resolved = path.resolve()
        key = str(resolved)
        if key in seen:
            return
        seen.add(key)
        paths.append(resolved)

    for path in uploaded_material_image_paths(node):
        add(path)
    for node_id in _predecessor_ids(ctx, node):
        for path in [*node_output_image_paths(ctx, node_id), *_graph_output_image_paths(ctx, node_id)]:
            add(path)
    for path in user_reference_image_paths(ctx.graph) or []:
        add(Path(path) if path else None)
    return paths


def _image_size_from_ctx(ctx: NodeExecutionContext, node: DesignerGraphNode) -> str:
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    aspect = cfg.get("aspect_lock") if isinstance(cfg.get("aspect_lock"), dict) else {}
    if not aspect:
        meta = (ctx.graph.get("metadata") or {}) if isinstance(ctx.graph, dict) else {}
        aspect = meta.get("aspect_lock") if isinstance(meta.get("aspect_lock"), dict) else {}
    return str(
        (aspect or {}).get("image_size") or cfg.get("image_size") or "1024x1024"
    ).strip() or "1024x1024"


class ImageNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        prompt = node_generate_prompt(node) or graph_prompt(ctx.graph, node)
        refs = _upstream_images(ctx, node)
        return await _image_or_notes(
            prompt=prompt,
            notes=prompt,
            stem=f"designer_image_{ctx.run_id}_{ctx.node_id}",
            kind_if_text=NODE_TYPE_IMAGE,
            size=_image_size_from_ctx(ctx, node),
            max_tries=4,
            require_image=True,
            reference_images=[str(path) for path in refs] or None,
            ctx=ctx,
        )


class VideoNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        sources = video_concat_source_ids(ctx.graph, str(node.get("id") or ctx.node_id))
        if sources:
            return await ComposeNodeHandler().execute(node, ctx)
        prompt = node_generate_prompt(node) or graph_prompt(ctx.graph, node)
        refs = [str(path) for path in _upstream_images(ctx, node)]
        generated = await generate_clip_video(
            prompt,
            save_dir=str(graph_workspace_dir(ctx.graph)),
            reference_images=refs or None,
            force_reference_mode=bool(refs),
        )
        path = Path(str(generated.get("video_path") or ""))
        if not path.is_file():
            raise RuntimeError("video generation returned no file")
        return NodeResult(
            output_ref=file_output_ref(path, kind=NODE_TYPE_VIDEO, mime_type="video/mp4"),
            message="video generated",
        )


class AudioNodeHandler(MusicNodeHandler):
    """Generic audio uses the same bed/music path as a music node."""


class UserReferenceNodeHandler:
    """Passthrough for an attached upload — the original file is the output."""

    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        del ctx
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        upload = cfg.get("upload") if isinstance(cfg.get("upload"), dict) else {}
        kind = str(node.get("type") or NODE_TYPE_IMAGE)
        mime = str(upload.get("mime_type") or "") or f"{kind}/*"
        path = user_reference_node_file(node)
        if path is None:
            # A moved upload must not block the film; downstream skips dead refs.
            declared = node.get("output_ref") if isinstance(node.get("output_ref"), dict) else {}
            uri = str(declared.get("uri") or "")
            if not uri:
                raise RuntimeError("user reference file is missing")
            return NodeResult(
                output_ref={"kind": kind, "uri": uri, "mime_type": mime},
                message="user reference file is no longer on disk",
            )
        return NodeResult(
            output_ref=file_output_ref(path, kind=kind, mime_type=mime),
            message=f"user reference {path.name}",
        )


GENERIC_IMAGE_HANDLER = ImageNodeHandler()
GENERIC_VIDEO_HANDLER = VideoNodeHandler()
GENERIC_AUDIO_HANDLER = AudioNodeHandler()
USER_REFERENCE_HANDLER = UserReferenceNodeHandler()
