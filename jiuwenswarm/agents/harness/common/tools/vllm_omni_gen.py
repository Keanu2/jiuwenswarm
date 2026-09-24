# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""vLLM-Omni (self-deployed) image/video generation backend.

Every generation is a two-request flow: first ``GET {api_base}/models`` to
discover the actually served model id, then the generation request itself.
The served id matters twice:

- it is sent as the request ``model`` field (the server rejects a mismatching
  model name with a 400), and
- it drives the model-spec registry below, which decides model-specific
  request shaping — hardcoded sampler fields and the ``extra_params`` JSON.

Only MiniMax-H3 is registered for now. When the served model is not in the
registry, no ``extra_params`` is built and a plain generic request is sent.

References: vLLM-Omni ``docs/serving/videos_api.md``,
``docs/serving/image_generation_api.md``, ``docs/serving/image_edit_api.md``
and ``recipes/MiniMaxAI/MiniMax-H3.md``.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import requests

from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.agents.harness.common.tools.ssl_config import get_requests_verify

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# A 50-step H3 generation takes ~9 min on 2xRTX5090 and much longer on
# capacity-oriented single-GPU profiles, so poll generously.
_POLL_INTERVAL_SECONDS = 5.0
_POLL_TIMEOUT_SECONDS = 7200.0
_MODELS_TIMEOUT_SECONDS = 15.0
_CREATE_TIMEOUT_SECONDS = 120.0
_DOWNLOAD_TIMEOUT_SECONDS = 300.0
_REFERENCE_DOWNLOAD_TIMEOUT_SECONDS = 60.0

# Provider-level default: every vLLM-Omni video request pins fps=24. This is
# deliberately not user-facing; H3 output is fixed at 24 FPS anyway.
_VIDEO_FPS = 24

# H3 reference images accept up to 30 MiB each (recipe limit).
_MAX_REFERENCE_BYTES = 30 * 1024 * 1024


# ---------------------------------------------------------------------------
# HTTP helpers (self-contained so video_tools/image_tools stay import-cycle free)
# ---------------------------------------------------------------------------


def _http_request(method: str, url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("verify", get_requests_verify())
    try:
        return requests.request(method, url, **kwargs)
    except requests.exceptions.ProxyError:
        with requests.Session() as session:
            session.trust_env = False
            return session.request(method, url, **kwargs)


def _auth_headers(api_key: str) -> dict[str, str]:
    headers = {"User-Agent": _USER_AGENT}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _error_message(response: requests.Response) -> str:
    try:
        data = response.json()
    except Exception:
        return response.text[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("msg")
            if msg:
                return str(msg)
        for key in ("detail", "message", "msg"):
            if data.get(key):
                return str(data[key])
    return response.text[:300]


def fetch_served_model_id(api_base: str, api_key: str = "") -> str | None:
    """First model id from ``GET {api_base}/models``; None when undiscoverable.

    A vLLM-Omni server instance serves a single model, so the first entry is
    the served one. Failures are non-fatal: callers fall back to a configured
    model name or omit the field (the server then uses its own default).
    """
    base = (api_base or "").strip().rstrip("/")
    if not base:
        return None
    try:
        response = _http_request(
            "GET",
            f"{base}/models",
            headers=_auth_headers(api_key),
            timeout=_MODELS_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning("[vLLM-Omni] GET %s/models failed", base, exc_info=True)
        return None
    if not response.ok:
        logger.warning(
            "[vLLM-Omni] GET %s/models returned %s: %s",
            base,
            response.status_code,
            response.text[:200],
        )
        return None
    try:
        data = response.json().get("data")
    except Exception:
        logger.warning("[vLLM-Omni] GET %s/models returned non-JSON payload", base)
        return None
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    model_id = str(first.get("id") if isinstance(first, dict) else "").strip()
    return model_id or None


# ---------------------------------------------------------------------------
# Model-spec registry: per-model request shaping (extra_params & hardcoded fields)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VllmOmniVideoInputs:
    """User-perceivable inputs a video spec may weave into the request."""

    size: str | None
    duration: int | float | None
    resolution: str | None
    has_references: bool


@dataclass(frozen=True)
class VllmOmniVideoForm:
    """Shaped multipart form: top-level fields + the extra_params JSON object."""

    fields: dict[str, str]
    extra_params: dict[str, Any] | None


@dataclass(frozen=True)
class VllmOmniVideoSpec:
    """One registered model family: matcher + request builder."""

    key: str
    matches: Callable[[str], bool]
    build: Callable[[VllmOmniVideoInputs], VllmOmniVideoForm]


def _parse_size(size: str | None) -> tuple[int, int] | None:
    """Accept both ``1280*720`` (DashScope style) and ``1280x720``."""
    value = (size or "").strip().lower().replace("*", "x")
    if "x" not in value:
        return None
    try:
        width_s, height_s = value.split("x", 1)
        width, height = int(width_s), int(height_s)
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


# -- MiniMax-H3 (recipes/MiniMaxAI/MiniMax-H3.md) ----------------------------

# Recipe-pinned sampler values: not user-friendly, so they are hardcoded.
_MINIMAX_H3_NUM_INFERENCE_STEPS = 50  # matches the reference accuracy workloads
_MINIMAX_H3_FLOW_SHIFT = 12.0  # video sigma shift
_MINIMAX_H3_AUDIO_FLOW_SHIFT = 3.0  # audio sigma shift; H3 output always has audio
_MINIMAX_H3_MIN_DURATION_SECONDS = 4.0  # H3 output contract: 4-15 s at 24 FPS
_MINIMAX_H3_MAX_DURATION_SECONDS = 15.0
_MINIMAX_H3_SHORT_EDGE = 768  # H3 shape policy requires exactly 768 when used
_MINIMAX_H3_NAMED_RATIOS: tuple[tuple[int, int], ...] = (
    (21, 9),
    (16, 9),
    (4, 3),
    (1, 1),
    (3, 4),
    (9, 16),
)


def _matches_minimax_h3(served_model_id: str) -> bool:
    # Covers "MiniMaxAI/MiniMax-H3", local paths like "/models/MiniMax-H3" and
    # task-partition paths like "/models/MiniMax-H3/FL2VA".
    normalized = (served_model_id or "").strip().lower().replace("\\", "/")
    return "minimax-h3" in normalized


def _snap_to_canvas_multiple(value: int, multiple: int = 32) -> int:
    """H3 requires a 32-pixel canvas multiple."""
    return max(multiple, int(round(value / multiple)) * multiple)


def _nearest_named_ratio(width: int, height: int, *, default: str = "16:9") -> str:
    target = width / height
    best = default
    best_err = float("inf")
    for rw, rh in _MINIMAX_H3_NAMED_RATIOS:
        err = abs(target - rw / rh)
        if err < best_err:
            best = f"{rw}:{rh}"
            best_err = err
    return best


def _build_minimax_h3_video_form(inputs: VllmOmniVideoInputs) -> VllmOmniVideoForm:
    # Dynamic task: any visual reference rides ref2va, plain text rides t2va.
    task = "ref2va" if inputs.has_references else "t2va"
    fields: dict[str, str] = {
        "num_inference_steps": str(_MINIMAX_H3_NUM_INFERENCE_STEPS),
        "flow_shift": str(_MINIMAX_H3_FLOW_SHIFT),
    }
    parsed = _parse_size(inputs.size)
    if parsed is not None:
        fields["width"] = str(_snap_to_canvas_multiple(parsed[0]))
        fields["height"] = str(_snap_to_canvas_multiple(parsed[1]))
    if task == "t2va":
        # T2VA requires one named output ratio.
        fields["aspect_ratio"] = _nearest_named_ratio(*parsed) if parsed else "16:9"
    elif parsed is None:
        # Ref2VA without explicit dimensions: adaptive ratio on the 768 canvas.
        fields["aspect_ratio"] = "adaptive"
        fields["short_edge"] = str(_MINIMAX_H3_SHORT_EDGE)
    # H3 has no resolution knob beyond the fixed 768px canvas; ``resolution``
    # is intentionally not forwarded.
    duration = (
        _MINIMAX_H3_MIN_DURATION_SECONDS
        if inputs.duration is None
        else float(inputs.duration)
    )
    duration = max(
        _MINIMAX_H3_MIN_DURATION_SECONDS,
        min(_MINIMAX_H3_MAX_DURATION_SECONDS, duration),
    )
    extra_params = {
        "task": task,
        "duration": duration,
        "audio_flow_shift": _MINIMAX_H3_AUDIO_FLOW_SHIFT,
    }
    return VllmOmniVideoForm(fields=fields, extra_params=extra_params)


_VIDEO_SPECS: tuple[VllmOmniVideoSpec, ...] = (
    VllmOmniVideoSpec(
        key="minimax-h3",
        matches=_matches_minimax_h3,
        build=_build_minimax_h3_video_form,
    ),
)


def match_video_spec(served_model_id: str | None) -> VllmOmniVideoSpec | None:
    """Registry lookup keyed by the served model id from ``GET /models``."""
    if not served_model_id:
        return None
    for spec in _VIDEO_SPECS:
        if spec.matches(served_model_id):
            return spec
    return None


def _build_generic_video_form(inputs: VllmOmniVideoInputs) -> VllmOmniVideoForm:
    """Unregistered model: only OpenAI-style fields, never extra_params."""
    fields: dict[str, str] = {}
    parsed = _parse_size(inputs.size)
    if parsed is not None:
        fields["size"] = f"{parsed[0]}x{parsed[1]}"
    if inputs.duration:
        fields["seconds"] = str(max(1, int(inputs.duration)))
    return VllmOmniVideoForm(fields=fields, extra_params=None)


# ---------------------------------------------------------------------------
# Reference media loading (multipart uploads)
# ---------------------------------------------------------------------------


def _guess_image_mime(name: str) -> str:
    mime, _ = mimetypes.guess_type(name)
    if mime and mime.startswith("image/"):
        return mime
    return "image/png"


def _local_reference_path(value: str) -> Path | None:
    text = value.strip()
    if text.startswith("file:"):
        local = unquote(urlparse(text).path)
        if len(local) >= 3 and local[0] == "/" and local[2] == ":":
            local = local[1:]
        candidate = Path(local)
    else:
        candidate = Path(text).expanduser()
    if not candidate.is_file():
        return None
    return candidate.resolve()


def _read_reference_bytes(reference: str) -> tuple[str, bytes, str] | None:
    """Load one reference image as ``(filename, bytes, mime)``; None if unreadable."""
    value = (reference or "").strip()
    if not value:
        return None
    if value.startswith("data:"):
        try:
            header, payload = value.split(",", 1)
            mime = header[len("data:"):].split(";")[0] or "image/png"
            raw = base64.b64decode(payload)
        except Exception:
            logger.warning("[vLLM-Omni] undecodable data: reference skipped")
            return None
        ext = mimetypes.guess_extension(mime) or ".png"
        return (f"reference{ext}", raw, mime)
    if value.startswith(("http://", "https://")):
        try:
            response = _http_request(
                "GET",
                value,
                headers={"User-Agent": _USER_AGENT},
                timeout=_REFERENCE_DOWNLOAD_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except Exception:
            logger.warning("[vLLM-Omni] reference download failed: %s", value[:120])
            return None
        raw = response.content
        name = Path(unquote(urlparse(value).path)).name or "reference.png"
        return (name, raw, _guess_image_mime(name))
    local = _local_reference_path(value)
    if local is None:
        logger.warning("[vLLM-Omni] reference is not a readable local file: %s", value[:120])
        return None
    try:
        raw = local.read_bytes()
    except OSError:
        logger.warning("[vLLM-Omni] reference read failed: %s", local)
        return None
    return (local.name, raw, _guess_image_mime(str(local)))


def _load_video_references(
    first_frame: str | None,
    reference_images: list[str] | None,
) -> list[tuple[str, bytes, str]]:
    """First frame + reference images, deduped, capped at the H3 image limit."""
    ordered: list[str] = []
    seen: set[str] = set()
    for item in [first_frame, *(reference_images or [])]:
        value = (item or "").strip()
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    references: list[tuple[str, bytes, str]] = []
    for item in ordered:
        loaded = _read_reference_bytes(item)
        if loaded is None:
            continue
        if len(loaded[1]) > _MAX_REFERENCE_BYTES:
            logger.warning("[vLLM-Omni] reference exceeds 30 MiB, skipped: %s", loaded[0])
            continue
        references.append(loaded)
        if len(references) >= 9:  # H3 accepts at most 9 images.
            break
    return references


def _reference_files_payload(
    references: list[tuple[str, bytes, str]],
) -> list[tuple[str, tuple[str, bytes, str]]]:
    """One reference rides ``input_reference``; several ride repeated ``input_references``."""
    field = "input_reference" if len(references) == 1 else "input_references"
    return [(field, item) for item in references]


# ---------------------------------------------------------------------------
# Video generation (async /v1/videos + poll + content download)
# ---------------------------------------------------------------------------


def _download_generated_video(content_url: str, prompt: str, headers: dict[str, str]) -> dict[str, Any]:
    output_dir = get_agent_workspace_dir()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"generated_{timestamp}_{random.randint(1000, 9999)}.mp4"
    response = _http_request("GET", content_url, headers=headers, timeout=_DOWNLOAD_TIMEOUT_SECONDS)
    response.raise_for_status()
    with open(output_path, "wb") as f:
        f.write(response.content)
    return {
        "video_path": str(output_path.absolute()),
        "revised_prompt": prompt,
        "original_url": content_url,
    }


def invoke_vllm_omni_video_generation_sync(
    prompt: str,
    *,
    api_key: str,
    api_base: str,
    model: str,
    size: str | None,
    duration: int,
    resolution: str | None,
    first_frame: str | None = None,
    reference_images: list[str] | None = None,
) -> dict[str, Any]:
    """vLLM-Omni async video generation (``POST /v1/videos`` + poll + download)."""
    base = (api_base or "").strip().rstrip("/")
    if not base:
        raise ValueError("VIDEO_GEN_API_BASE is required for the vLLM-Omni backend.")
    headers = _auth_headers(api_key)

    # Request 1 of 2: discover the actually served model. It drives both the
    # ``model`` field (the server 400s on a mismatch) and the spec registry.
    served_model_id = fetch_served_model_id(base, api_key)
    model_to_send = served_model_id or (model or "").strip() or None
    spec = match_video_spec(served_model_id)
    if served_model_id and spec is None:
        logger.info(
            "[vLLM-Omni] served model %s is not registered; no extra_params built",
            served_model_id,
        )

    references = _load_video_references(first_frame, reference_images)
    if (first_frame or reference_images) and not references:
        raise ValueError(
            "reference images were provided but none could be read as local files or URLs."
        )
    inputs = VllmOmniVideoInputs(
        size=size,
        duration=duration,
        resolution=resolution,
        has_references=bool(references),
    )
    form = spec.build(inputs) if spec else _build_generic_video_form(inputs)

    fields: dict[str, str] = {
        "prompt": prompt,
        "fps": str(_VIDEO_FPS),
        **form.fields,
    }
    if model_to_send:
        fields["model"] = model_to_send
    if form.extra_params:
        # No seed / quality: reproducibility and cache policies stay server-side.
        fields["extra_params"] = json.dumps(form.extra_params, ensure_ascii=False)
    files = _reference_files_payload(references)

    logger.info(
        "[vLLM-Omni] video create model=%s spec=%s fields=%s refs=%d",
        model_to_send,
        spec.key if spec else None,
        sorted(fields),
        len(references),
    )
    response = _http_request(
        "POST",
        f"{base}/videos",
        headers=headers,
        data=fields,
        files=files or None,
        timeout=_CREATE_TIMEOUT_SECONDS,
    )
    if not response.ok:
        raise ValueError(
            f"vLLM-Omni video create failed {response.status_code}: {_error_message(response)}"
        )
    body = response.json()
    video_id = str(body.get("id") or "").strip()
    if not video_id:
        raise ValueError(f"vLLM-Omni video create response missing id: {body}")

    query_url = f"{base}/videos/{video_id}"
    deadline_ts = time.monotonic() + _POLL_TIMEOUT_SECONDS
    last_status = "unknown"
    while time.monotonic() < deadline_ts:
        poll = _http_request("GET", query_url, headers=headers, timeout=60)
        if not poll.ok:
            raise ValueError(
                f"vLLM-Omni poll failed {poll.status_code}: {_error_message(poll)}"
            )
        payload = poll.json()
        status = str(payload.get("status") or "").strip().lower()
        last_status = status or last_status
        if status == "completed":
            return _download_generated_video(f"{query_url}/content", prompt, headers)
        if status in {"failed", "cancelled", "canceled", "expired"}:
            err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            err_msg = str(err.get("message") or "").strip()
            raise ValueError(err_msg or f"vLLM-Omni video generation {status}")
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"vLLM-Omni video generation timed out (last status={last_status})")


# ---------------------------------------------------------------------------
# Image generation (OpenAI-compatible /v1/images/generations + /v1/images/edits)
# ---------------------------------------------------------------------------


def _load_image_references(
    reference_images: list[str] | None,
) -> list[tuple[str, bytes, str]]:
    """Deduped edit input images; unreadable or oversized ones are skipped."""
    ordered: list[str] = []
    seen: set[str] = set()
    for item in reference_images or []:
        value = (item or "").strip()
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    references: list[tuple[str, bytes, str]] = []
    for item in ordered:
        loaded = _read_reference_bytes(item)
        if loaded is None:
            continue
        if len(loaded[1]) > _MAX_REFERENCE_BYTES:
            logger.warning("[vLLM-Omni] reference exceeds 30 MiB, skipped: %s", loaded[0])
            continue
        references.append(loaded)
    return references


def _save_image_response_body(body: Any, prompt: str, api_key: str) -> dict[str, Any]:
    """Shared response tail for /images/generations and /images/edits."""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or not data:
        raise ValueError(f"vLLM-Omni image response missing data: {body}")
    first = data[0] if isinstance(data[0], dict) else {}
    image_b64 = str(first.get("b64_json") or "").strip() or None
    image_url = str(first.get("url") or "").strip() or None

    output_dir = get_agent_workspace_dir()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"generated_{timestamp}_{random.randint(1000, 9999)}.png"
    if image_b64:
        with open(output_path, "wb") as f:
            f.write(base64.b64decode(image_b64))
        return {"image_path": str(output_path.absolute()), "revised_prompt": prompt}
    if image_url:
        download = _http_request("GET", image_url, headers=_auth_headers(api_key), timeout=120)
        download.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(download.content)
        return {
            "image_path": str(output_path.absolute()),
            "revised_prompt": prompt,
            "original_url": image_url,
        }
    raise ValueError("vLLM-Omni image generation succeeded but returned no image data")


def invoke_vllm_omni_image_generation_sync(
    prompt: str,
    *,
    api_key: str,
    api_base: str,
    model: str,
    size: str | None,
    reference_images: list[str] | None = None,
) -> dict[str, Any]:
    """vLLM-Omni image generation.

    Plain prompt rides text-to-image (``POST /v1/images/generations``, JSON);
    with reference images it rides image-to-image (``POST /v1/images/edits``,
    multipart, repeated ``image`` fields). No seed / quality is ever sent.
    """
    base = (api_base or "").strip().rstrip("/")
    if not base:
        raise ValueError("IMAGE_GEN_API_BASE is required for the vLLM-Omni backend.")

    # The model field is optional server-side; prefer the configured name and
    # otherwise discover the served model (a mismatching name would be a 400).
    model_to_send = (model or "").strip() or fetch_served_model_id(base, api_key)
    parsed = _parse_size(size)

    references = _load_image_references(reference_images)
    if reference_images and not references:
        raise ValueError(
            "reference images were provided but none could be read as local files or URLs."
        )

    headers = _auth_headers(api_key)
    if references:
        # Image-to-image. Without an explicit size the server keeps "auto" and
        # infers dimensions from the first input image.
        fields: dict[str, str] = {"prompt": prompt}
        if model_to_send:
            fields["model"] = model_to_send
        if parsed is not None:
            fields["size"] = f"{parsed[0]}x{parsed[1]}"
        files = [("image", item) for item in references]
        logger.info(
            "[vLLM-Omni] image edit model=%s size=%s refs=%d",
            model_to_send,
            fields.get("size"),
            len(references),
        )
        response = _http_request(
            "POST",
            f"{base}/images/edits",
            headers=headers,
            data=fields,
            files=files,
            timeout=600,
        )
    else:
        payload: dict[str, Any] = {"prompt": prompt, "response_format": "b64_json"}
        if parsed is not None:
            payload["size"] = f"{parsed[0]}x{parsed[1]}"
        if model_to_send:
            payload["model"] = model_to_send
        logger.info("[vLLM-Omni] image create model=%s size=%s", model_to_send, payload.get("size"))
        response = _http_request(
            "POST",
            f"{base}/images/generations",
            headers={**headers, "Content-Type": "application/json"},
            json=payload,
            timeout=600,
        )
    if not response.ok:
        raise ValueError(
            f"vLLM-Omni image create failed {response.status_code}: {_error_message(response)}"
        )
    return _save_image_response_body(response.json(), prompt, api_key)
