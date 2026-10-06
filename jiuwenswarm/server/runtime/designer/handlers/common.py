# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared helpers for Designer node handlers."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_ROLE_SCENE,
    AssetRef,
    DesignerExecutionGraph,
    DesignerGraphNode,
    node_role,
)
from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

logger = logging.getLogger(__name__)


def graph_prompt(graph: DesignerExecutionGraph, node: DesignerGraphNode | None = None) -> str:
    if node is not None:
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        node_prompt = str(config.get("prompt") or "").strip()
        if node_prompt:
            return node_prompt
        generate = config.get("generate")
        if isinstance(generate, dict):
            generate_prompt = str(generate.get("prompt") or "").strip()
            if generate_prompt:
                return generate_prompt
        if node_role(node) == NODE_ROLE_BRIEF:
            pass
    for candidate in graph.get("nodes") or []:
        if node_role(candidate) != NODE_ROLE_BRIEF:
            continue
        brief_config = candidate.get("config") if isinstance(candidate.get("config"), dict) else {}
        brief_prompt = str(brief_config.get("prompt") or "").strip()
        if brief_prompt:
            return brief_prompt
    description = str(graph.get("description") or "").strip()
    if description:
        return description
    title = str(graph.get("title") or "").strip()
    return title or "短视频"


def node_generate_prompt(node: DesignerGraphNode | None) -> str:
    if node is None:
        return ""
    config = node.get("config") if isinstance(node.get("config"), dict) else {}
    generate = config.get("generate") if isinstance(config, dict) else None
    if isinstance(generate, dict):
        text = str(generate.get("prompt") or "").strip()
        if text:
            return text
    return str((config or {}).get("prompt") or "").strip()


def path_from_uri(uri: str) -> Path | None:
    value = (uri or "").strip()
    if not value or value.startswith("designer://"):
        return None
    if value.startswith("file:"):
        parsed = urlparse(value)
        path = unquote(parsed.path)
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return Path(path)
    candidate = Path(value)
    return candidate if candidate.exists() else None


def write_workspace_text(stem: str, content: str) -> Path:
    directory = get_agent_workspace_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.md"
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path.resolve()


def file_output_ref(path: Path, *, kind: str, mime_type: str) -> AssetRef:
    resolved = path.resolve()
    return {
        "kind": kind,
        "uri": resolved.as_uri(),
        "mime_type": mime_type,
        "label": resolved.name,
    }


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".jfif"}


def node_output_refs(ctx: NodeExecutionContext, node_id: str) -> list[dict]:
    if ctx.run is None or not node_id:
        return []
    state = (ctx.run.get("node_states") or {}).get(node_id) or {}
    raw_refs = state.get("output_refs")
    collected: list[dict] = []
    if isinstance(raw_refs, list):
        collected = [
            item for item in raw_refs if isinstance(item, dict) and str(item.get("uri") or "").strip()
        ]
    if not collected:
        single = state.get("output_ref") or {}
        if isinstance(single, dict) and str(single.get("uri") or "").strip():
            collected = [single]
    return collected


def node_output_image_paths(ctx: NodeExecutionContext, node_id: str) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for ref in node_output_refs(ctx, node_id):
        path = path_from_uri(str(ref.get("uri") or ""))
        if path is None or not path.is_file():
            continue
        kind = str(ref.get("kind") or "").lower()
        mime = str(ref.get("mime_type") or "").lower()
        if kind != "image" and not mime.startswith("image/") and path.suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        resolved = path.resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        paths.append(resolved)
    return paths


def role_output_refs(ctx: NodeExecutionContext, role: str) -> list[dict]:
    """Collect output refs from every node with the given role (multi-character/scene)."""
    if ctx.run is None:
        return []
    collected: list[dict] = []
    seen_uri: set[str] = set()
    for node in ctx.graph.get("nodes") or []:
        if node_role(node) != role:
            continue
        for ref in node_output_refs(ctx, str(node.get("id") or "")):
            uri = str(ref.get("uri") or "").strip()
            if not uri or uri in seen_uri:
                continue
            seen_uri.add(uri)
            collected.append(ref)
    return collected


def node_ids_output_image_paths(ctx: NodeExecutionContext, node_ids: list[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for nid in node_ids:
        for path in node_output_image_paths(ctx, nid):
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
    return paths


def collect_frame_reference_images(ctx: NodeExecutionContext, node: dict) -> list[Path]:
    """Identity-first refs: optional prior keyframe, solo cast sheets, then scene.

    Order matters for I2I models: prior KF (edit strategy) → character solos → scene.
    """
    cfg = node.get("config") or {}
    identity = cfg.get("identity_refs") if isinstance(cfg.get("identity_refs"), dict) else {}
    preferred = [
        str(x)
        for x in (
            identity.get("character_node_ids")
            or cfg.get("character_node_ids")
            or []
        )
        if str(x).strip()
    ]
    paths = node_ids_output_image_paths(ctx, preferred) if preferred else []
    if not paths:
        # Prefer identity_source solos over combined compose aids.
        solo_ids: list[str] = []
        for other in ctx.graph.get("nodes") or []:
            if not isinstance(other, dict):
                continue
            oc = other.get("config") if isinstance(other.get("config"), dict) else {}
            if str(oc.get("role") or "") != NODE_ROLE_CHARACTER_DESIGN:
                continue
            if oc.get("combined_cast"):
                continue
            solo_ids.append(str(other.get("id") or ""))
        paths = node_ids_output_image_paths(ctx, [x for x in solo_ids if x])
    if not paths:
        paths = list(role_output_image_paths(ctx, NODE_ROLE_CHARACTER_DESIGN))

    scene_paths = list(role_output_image_paths(ctx, NODE_ROLE_SCENE))
    # Prefer explicit master + this shot's scene view from node inputs / identity_refs.
    preferred_scene_ids: list[str] = []
    master_id = str(
        identity.get("master_scene_node_id") or cfg.get("master_scene_node_id") or ""
    ).strip()
    shot_scene_id = str(
        identity.get("scene_node_id") or cfg.get("scene_node_id") or ""
    ).strip()
    if master_id:
        preferred_scene_ids.append(master_id)
    if shot_scene_id and shot_scene_id not in preferred_scene_ids:
        preferred_scene_ids.append(shot_scene_id)
    input_ids = [str(x) for x in (cfg.get("inputs") or []) if str(x).startswith("n_scene")]
    for iid in input_ids:
        if iid not in preferred_scene_ids:
            preferred_scene_ids.append(iid)
    if preferred_scene_ids:
        scene_paths = node_ids_output_image_paths(ctx, preferred_scene_ids) or scene_paths

    prior_id = str(
        identity.get("prior_keyframe_node_id")
        or cfg.get("prior_keyframe_node_id")
        or ""
    ).strip()
    prior_paths: list[Path] = []
    if prior_id:
        prior_paths = node_ids_output_image_paths(ctx, [prior_id])

    merged: list[Path] = []
    seen: set[str] = set()
    # Prior keyframe first when editing sequentially; else cast then scene.
    strategy = str(
        identity.get("keyframe_strategy") or cfg.get("keyframe_strategy") or ""
    )
    ordered = (
        [*prior_paths, *paths, *scene_paths]
        if strategy == "edit_prior_keyframe" and prior_paths
        else [*paths, *scene_paths, *prior_paths]
    )
    for path in ordered:
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        merged.append(path)
    # Cap refs — DashScope often rejects large multi-ref batches.
    return merged[:4]


def role_output_image_paths(ctx: NodeExecutionContext, role: str) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for ref in role_output_refs(ctx, role):
        path = path_from_uri(str(ref.get("uri") or ""))
        if path is None or not path.is_file():
            continue
        kind = str(ref.get("kind") or "").lower()
        mime = str(ref.get("mime_type") or "").lower()
        if kind != "image" and not mime.startswith("image/") and path.suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        resolved = path.resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        paths.append(resolved)
    return paths


def role_output_image_path(ctx: NodeExecutionContext, role: str) -> Path | None:
    paths = role_output_image_paths(ctx, role)
    return paths[0] if paths else None


def role_output_text(ctx: NodeExecutionContext, role: str) -> str:
    if ctx.run is None:
        return ""
    states = ctx.run.get("node_states") or {}
    for node in ctx.graph.get("nodes") or []:
        if node_role(node) != role:
            continue
        ref = (states.get(node["id"]) or {}).get("output_ref") or {}
        path = path_from_uri(str(ref.get("uri") or ""))
        if path is None or not path.is_file():
            continue
        text_suffixes = {".md", ".txt", ".markdown", ".csv"}
        candidates = [path] if path.suffix.lower() in text_suffixes else []
        sidecar = path.with_suffix(".md")
        if sidecar not in candidates:
            candidates.append(sidecar)
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                return candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
    return ""


async def complete_designer_text(prompt: str, *, max_tokens: int = 1200) -> str:
    """Call the default chat model. Tests monkeypatch this function."""
    from jiuwenswarm.common.config import get_config, get_default_models
    from openjiuwen.core.foundation.llm import Model
    from openjiuwen.core.foundation.llm.schema.config import (
        ModelClientConfig,
        ModelRequestConfig,
    )

    entries = get_default_models(get_config())
    entry = next((item for item in entries if item.get("is_default") is True), None)
    if entry is None and entries:
        entry = entries[0]
    client = (entry or {}).get("model_client_config") if isinstance(entry, dict) else {}
    mco = (entry or {}).get("model_config_obj") if isinstance(entry, dict) else {}
    if not isinstance(client, dict):
        return ""
    if not isinstance(mco, dict):
        mco = {}
    api_key = str(client.get("api_key") or "").strip()
    api_base = str(client.get("api_base") or "").strip()
    model_name = str(client.get("model_name") or "").strip()
    provider = str(client.get("client_provider") or "").strip()
    if not api_key or not model_name:
        return ""
    kwargs: dict[str, object] = {
        "api_key": api_key,
        "api_base": api_base,
        "client_provider": provider,
    }
    profile = str(client.get("endpoint_profile") or "").strip()
    if profile:
        kwargs["endpoint_profile"] = profile
    request = ModelRequestConfig(
        model_name=model_name,
        temperature=float(mco.get("temperature", 0.4) or 0.4),
        top_p=float(mco.get("top_p", 0.95) or 0.95),
        max_tokens=max_tokens,
    )
    model = Model(
        model_client_config=ModelClientConfig(**kwargs),
        model_config=request,
    )
    response = await model.invoke(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
        max_tokens=max_tokens,
        model=model_name,
    )
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("text"):
                parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts).strip()
    return str(content).strip()


def _image_gen_switch_enabled() -> bool:
    raw = (os.environ.get("IMAGE_GEN_ENABLED") or "").strip().lower()
    if not raw:
        return True
    return raw in {"true", "1", "yes", "on", "enabled"}


_IMAGE_GEN_SEM = None


def _image_gen_semaphore():
    """Limit concurrent DashScope image calls to avoid RateQuota 429s."""
    import asyncio

    global _IMAGE_GEN_SEM
    if _IMAGE_GEN_SEM is None:
        # Keep low: quality path fans out cast+scene+frames; API quotas are tight.
        _IMAGE_GEN_SEM = asyncio.Semaphore(2)
    return _IMAGE_GEN_SEM


def _is_rate_limit_error(text: str) -> bool:
    low = (text or "").lower()
    return any(
        token in low
        for token in (
            "ratequota",
            "rate limit",
            "throttling",
            "too many requests",
            "429",
        )
    )


async def generate_designer_image(
    prompt: str,
    size: str = "1024x1024",
    reference_image: str | None = None,
    reference_images: list[str] | None = None,
    max_tries: int = 4,
    timeout_sec: float = 120.0,
) -> dict[str, str] | None:
    """Call image_gen when configured. Tests monkeypatch this function.

    Retries transient RateQuota/429 with backoff under a global concurrency cap.
    """
    import asyncio

    from jiuwenswarm.agents.harness.common.tools.image_tools import _invoke_model_image_generation
    from jiuwenswarm.agents.harness.common.tools.multimodal_config import (
        apply_image_gen_model_config_from_yaml,
    )
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.common.utils import get_env_file
    from jiuwenswarm.dotenv_early import load_dotenv_runtime

    try:
        load_dotenv_runtime(dotenv_path=get_env_file(), override=True)
    except Exception:
        logger.debug("Failed to reload image_gen env before generation", exc_info=True)

    if not _image_gen_switch_enabled():
        logger.info("Designer image generation skipped: IMAGE_GEN_ENABLED is off")
        return None
    try:
        apply_image_gen_model_config_from_yaml(get_config())
    except Exception:
        logger.debug("Failed to apply image_gen model config from yaml", exc_info=True)
    refs = [str(item).strip() for item in (reference_images or []) if str(item).strip()]
    single = (reference_image or "").strip()
    if single and single not in refs:
        refs.insert(0, single)
    if refs:
        logger.info(
            "Designer image generation using reference_images=%s",
            ",".join(Path(item).name for item in refs),
        )

    attempts = max(1, min(6, int(max_tries or 1)))
    last_error = ""
    sem = _image_gen_semaphore()
    for attempt in range(1, attempts + 1):
        async with sem:
            try:
                result = await asyncio.wait_for(
                    _invoke_model_image_generation(
                        prompt,
                        size=size,
                        reference_images=refs or None,
                        max_tries=1,
                    ),
                    timeout=max(30.0, float(timeout_sec or 120.0)),
                )
            except asyncio.TimeoutError:
                last_error = f"image_gen timed out after {int(timeout_sec)}s"
                logger.info("Designer image generation timed out (attempt %s/%s)", attempt, attempts)
                result = {"error": last_error}
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                result = {"error": last_error}

        if isinstance(result, dict) and result.get("image_path"):
            return {"image_path": str(result["image_path"])}
        err = str((result or {}).get("error") or last_error or "image_gen failed")
        last_error = err
        if attempt < attempts and _is_rate_limit_error(err):
            delay = min(45.0, 4.0 * (2 ** (attempt - 1)))
            logger.info(
                "Designer image rate-limited; retry in %.1fs (attempt %s/%s)",
                delay,
                attempt,
                attempts,
            )
            await asyncio.sleep(delay)
            continue
        if attempt < attempts and "timed out" in err.lower():
            await asyncio.sleep(2.0)
            continue
        break

    logger.info("Designer image generation unavailable: %s", last_error)
    return {"error": last_error}
