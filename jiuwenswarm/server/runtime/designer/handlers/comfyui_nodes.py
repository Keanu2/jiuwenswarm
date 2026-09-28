# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Handlers for image / video nodes imported from a ComfyUI vLLM-Omni workflow.

``config.comfyui`` holds what the ``VLLMOmniGenerateImage`` /
``VLLMOmniGenerateVideo`` node and its sampling / model-params nodes carried.
Those values reach vLLM-Omni the same way the ComfyUI plugin sends them:
node fields and sampling params as top-level request fields, model params as
top-level fields except MiniMax-H3 ``audio_flow_shift``, which rides
``extra_params``. The canvas-wide aspect / style locks never apply here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    CONFIG_KEY_COMFYUI,
    EDGE_KIND_DATA,
    NODE_TYPE_AUDIO,
    NODE_TYPE_IMAGE,
    NODE_TYPE_VIDEO,
    DesignerGraphNode,
    edge_kind,
    node_config,
)
from jiuwenswarm.server.runtime.designer.handlers.common import (
    file_output_ref,
    graph_prompt,
    graph_workspace_dir,
    node_generate_prompt,
    predecessor_outputs,
    uploaded_material_image_paths,
)
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext, NodeResult

logger = logging.getLogger(__name__)

COMFYUI_CLASS_GENERATE_IMAGE = "VLLMOmniGenerateImage"
COMFYUI_CLASS_GENERATE_VIDEO = "VLLMOmniGenerateVideo"
MODEL_PARAMS_MINIMAX_H3 = "minimax_h3"

_SAMPLING_KEYS = (
    "num_inference_steps",
    "guidance_scale",
    "true_cfg_scale",
    "vae_use_slicing",
    "vae_use_tiling",
    "seed",
)
# MiniMax-H3 reads these from the ``extra_params`` JSON, not the form.
_H3_EXTRA_PARAM_KEYS = frozenset({"audio_flow_shift"})


@dataclass(frozen=True)
class ComfyuiRequest:
    """vLLM-Omni call arguments rebuilt from ``config.comfyui``."""

    api_base: str
    model: str
    size: str | None
    negative_prompt: str | None
    fps: int | None = None
    duration: float | None = None
    extra_fields: dict[str, Any] = field(default_factory=dict)
    extra_params: dict[str, Any] = field(default_factory=dict)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _positive_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _sampling_fields(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _SAMPLING_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        if key == "seed":
            seed = value if isinstance(value, int) and not isinstance(value, bool) else None
            # ComfyUI uses -1 for "let the server pick".
            if seed is None or seed < 0:
                continue
        out[key] = value
    return out


def _model_param_fields(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split model params into (top-level form fields, ``extra_params`` entries)."""
    params = dict(raw)
    kind = str(params.pop("type", "") or "").strip()
    fields: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if kind == MODEL_PARAMS_MINIMAX_H3 and key in _H3_EXTRA_PARAM_KEYS:
            extra[key] = value
        else:
            fields[key] = value
    return fields, extra


def comfyui_request(node: DesignerGraphNode) -> ComfyuiRequest:
    comfy = _dict(node_config(node).get(CONFIG_KEY_COMFYUI))
    fields = _dict(comfy.get("fields"))
    width = _positive_int(fields.get("width"))
    height = _positive_int(fields.get("height"))
    extra_fields = _sampling_fields(_dict(comfy.get("sampling_params")))
    model_fields, extra_params = _model_param_fields(_dict(comfy.get("model_params")))
    extra_fields.update(model_fields)
    negative = str(fields.get("negative_prompt") or "").strip()
    return ComfyuiRequest(
        api_base=str(fields.get("url") or "").strip(),
        model=str(fields.get("model") or "").strip(),
        size=f"{width}x{height}" if width and height else None,
        negative_prompt=negative or None,
        fps=_positive_int(fields.get("fps")),
        duration=_positive_float(fields.get("duration")),
        extra_fields=extra_fields,
        extra_params=extra_params,
    )


@dataclass
class _References:
    images: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    audios: list[str] = field(default_factory=list)


def _wired_media_sources(ctx: NodeExecutionContext, node_id: str) -> list[dict]:
    by_id = {
        str(other.get("id") or ""): other
        for other in ctx.graph.get("nodes") or []
        if isinstance(other, dict)
    }
    sources: list[dict] = []
    for edge in ctx.graph.get("edges") or []:
        if not isinstance(edge, dict) or edge_kind(edge) != EDGE_KIND_DATA:
            continue
        if str(edge.get("target") or "") != node_id:
            continue
        source = by_id.get(str(edge.get("source") or ""))
        if source is not None and source.get("type") in {
            NODE_TYPE_IMAGE,
            NODE_TYPE_VIDEO,
            NODE_TYPE_AUDIO,
        }:
            sources.append(source)
    return sources


def _collect_references(ctx: NodeExecutionContext, node: DesignerGraphNode) -> _References:
    """Attached stills, then wired media outputs in edge order.

    A wired reference without a file fails the node: sending the request
    without it would silently change what the workflow generates.
    """
    node_id = str(node.get("id") or ctx.node_id)
    refs = _References(images=[str(path) for path in uploaded_material_image_paths(node)])
    outputs = predecessor_outputs(ctx, node) or []
    resolved_sources = {item.node_id for item in outputs if item.path is not None}
    missing = [
        str(source.get("label") or source.get("id"))
        for source in _wired_media_sources(ctx, node_id)
        if str(source.get("id") or "") not in resolved_sources
    ]
    if missing:
        raise RuntimeError(
            "ComfyUI reference has no file yet; upload or generate it first: " + ", ".join(missing)
        )
    buckets = {"image": refs.images, "video": refs.videos, "audio": refs.audios}
    for item in outputs:
        bucket = buckets.get(item.kind)
        if bucket is None or item.path is None:
            continue
        path = str(item.path)
        if path not in bucket:
            bucket.append(path)
    return refs


def _prompt(ctx: NodeExecutionContext, node: DesignerGraphNode) -> str:
    prompt = node_generate_prompt(node) or graph_prompt(ctx.graph, node)
    if not prompt.strip():
        raise RuntimeError("ComfyUI node has no prompt")
    return prompt


def _into_workspace(ctx: NodeExecutionContext, produced: str) -> Path:
    path = Path(produced)
    if not path.is_file():
        raise RuntimeError("vLLM-Omni returned no file")
    dest_dir = graph_workspace_dir(ctx.graph)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    if dest.resolve() != path.resolve():
        path.replace(dest)
    return dest.resolve()


def _stage(ctx: NodeExecutionContext, text: str) -> None:
    if callable(ctx.emit_activity):
        ctx.emit_activity("stage", text, "vllm_omni")


class ComfyuiImageNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        from jiuwenswarm.agents.harness.common.tools.image_tools import (
            _invoke_model_image_generation,
        )
        from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
            apply_image_gen_model_config_from_yaml,
        )
        from jiuwenswarm.common.config import get_config

        try:
            apply_image_gen_model_config_from_yaml(get_config())
        except Exception:  # noqa: BLE001
            logger.debug("Failed to apply image_gen model config from yaml", exc_info=True)

        request = comfyui_request(node)
        refs = _collect_references(ctx, node)
        _stage(ctx, "calling vLLM-Omni image model")
        result = await _invoke_model_image_generation(
            _prompt(ctx, node),
            size=request.size or "1024x1024",
            reference_images=refs.images or None,
            max_tries=1,
            model_override=request.model or None,
            provider_override="vllm-omni",
            api_base_override=request.api_base or None,
            negative_prompt=request.negative_prompt,
            **request.extra_fields,
        )
        if not isinstance(result, dict) or not result.get("image_path"):
            error = str((result or {}).get("error") or "no image_path")
            raise RuntimeError(f"vLLM-Omni image generation failed: {error}")
        path = _into_workspace(ctx, str(result["image_path"]))
        return NodeResult(
            output_ref=file_output_ref(path, kind=NODE_TYPE_IMAGE, mime_type="image/png"),
            message="image generated (vLLM-Omni, ComfyUI)",
        )


class ComfyuiVideoNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
            apply_video_gen_model_config_from_yaml,
        )
        from jiuwenswarm.agents.harness.common.tools.video_tools import (
            _invoke_model_video_generation,
        )
        from jiuwenswarm.common.config import get_config

        try:
            apply_video_gen_model_config_from_yaml(get_config())
        except Exception:  # noqa: BLE001
            logger.debug("Failed to apply video_gen model config from yaml", exc_info=True)

        request = comfyui_request(node)
        refs = _collect_references(ctx, node)
        options: dict[str, Any] = {
            "negative_prompt": request.negative_prompt,
            "fps": request.fps,
            **request.extra_fields,
        }
        if refs.videos:
            options["reference_videos"] = refs.videos
        if refs.audios:
            options["reference_audios"] = refs.audios
        if request.extra_params:
            options["extra_params"] = request.extra_params
        _stage(ctx, "calling vLLM-Omni video model")
        result = await _invoke_model_video_generation(
            _prompt(ctx, node),
            size=request.size or "1280*720",
            duration=request.duration or 5,
            reference_images=refs.images or None,
            model=request.model or None,
            provider_override="vllm-omni",
            api_base_override=request.api_base or None,
            **options,
        )
        if "error" in result:
            raise RuntimeError(str(result["error"]))
        path = _into_workspace(ctx, str(result.get("video_path") or ""))
        return NodeResult(
            output_ref=file_output_ref(path, kind=NODE_TYPE_VIDEO, mime_type="video/mp4"),
            message="video generated (vLLM-Omni, ComfyUI)",
        )


COMFYUI_IMAGE_HANDLER = ComfyuiImageNodeHandler()
COMFYUI_VIDEO_HANDLER = ComfyuiVideoNodeHandler()
