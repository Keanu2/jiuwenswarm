# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Settings-backed model tools for Designer node agents."""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.config import get_config, get_model_names, resolve_env_vars

logger = logging.getLogger(__name__)


def llm_available() -> bool:
    """True when Settings has a usable chat model with credentials for Designer agents."""
    try:
        from jiuwenswarm.common.utils import get_env_file
        from jiuwenswarm.dotenv_early import load_dotenv_runtime

        load_dotenv_runtime(dotenv_path=get_env_file(), override=False)
    except Exception:  # noqa: BLE001
        pass
    try:
        models = list_configured_models()
    except Exception:  # noqa: BLE001
        return False
    import os

    env_key = (os.environ.get("API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    env_base = (os.environ.get("API_BASE") or os.environ.get("OPENAI_API_BASE") or "").strip()
    for m in models:
        if not isinstance(m, dict):
            continue
        if not str(m.get("id") or m.get("model_name") or "").strip():
            continue
        base = resolve_env_vars(str(m.get("api_base") or env_base or "")).strip()
        # Key may live only in env; list_configured_models does not always expose it.
        key = env_key
        if key and base and not base.startswith("https://example.com"):
            return True
        if base and not base.startswith("https://example.com") and env_key:
            return True
    return bool(env_key and env_base)


def list_configured_models() -> list[dict[str, Any]]:
    """Return models from Settings / config.yaml for agent tool use."""
    cfg = get_config() or {}
    models = cfg.get("models") or {}
    defaults = models.get("defaults")
    out: list[dict[str, Any]] = []
    if isinstance(defaults, list):
        for idx, entry in enumerate(defaults):
            if not isinstance(entry, dict):
                continue
            mcc = entry.get("model_client_config") or {}
            if not isinstance(mcc, dict):
                continue
            name = resolve_env_vars(str(mcc.get("model_name") or "")).strip()
            alias = resolve_env_vars(str(entry.get("alias") or "")).strip()
            if not name and not alias:
                continue
            out.append(
                {
                    "id": alias or name,
                    "model_name": name,
                    "alias": alias,
                    "api_base": resolve_env_vars(str(mcc.get("api_base") or "")),
                    "client_provider": str(mcc.get("client_provider") or "OpenAI"),
                    "is_default": bool(entry.get("is_default")),
                    "index": idx,
                }
            )
    if not out:
        # Fallback to env-backed default entry names.
        for name in get_model_names():
            out.append(
                {
                    "id": name,
                    "model_name": name,
                    "alias": "",
                    "api_base": "",
                    "client_provider": "OpenAI",
                    "is_default": False,
                    "index": 0,
                }
            )
    return out


def pick_model_for_optimize(optimize_for: str) -> dict[str, Any] | None:
    models = list_configured_models()
    if not models:
        return None
    defaults = [m for m in models if m.get("is_default")]
    pool = defaults or models
    if optimize_for == "cost":
        # Prefer flash / mini style names when present.
        for m in pool:
            nid = str(m.get("id") or "").lower()
            if "flash" in nid or "mini" in nid or "fast" in nid:
                return m
        return pool[-1]
    for m in pool:
        nid = str(m.get("id") or "").lower()
        if "pro" in nid or "reason" in nid:
            return m
    return pool[0]


async def call_model_tool(
    *,
    prompt: str,
    system: str,
    optimize_for: str,
    preferred_model: str | None = None,
    max_tokens: int = 800,
) -> dict[str, Any]:
    """Call a configured chat model (OpenAI-compatible) as an agent tool."""
    # Ensure ~/.jiuwenswarm/config/.env is loaded (API_KEY / API_BASE).
    try:
        from jiuwenswarm.common.utils import get_env_file
        from jiuwenswarm.dotenv_early import load_dotenv_runtime

        load_dotenv_runtime(dotenv_path=get_env_file(), override=False)
    except Exception:  # noqa: BLE001
        pass

    models = list_configured_models()
    chosen: dict[str, Any] | None = None
    if preferred_model:
        for m in models:
            if m.get("id") == preferred_model or m.get("model_name") == preferred_model:
                chosen = m
                break
    if chosen is None:
        chosen = pick_model_for_optimize(optimize_for)
    if chosen is None:
        return {
            "ok": False,
            "error": "No models configured in Settings",
            "model": None,
            "text": "",
        }

    # Resolve credentials from defaults entry / env
    cfg = get_config() or {}
    defaults = (cfg.get("models") or {}).get("defaults") or []
    api_key = ""
    api_base = str(chosen.get("api_base") or "")
    model_name = str(chosen.get("model_name") or chosen.get("id") or "")
    if isinstance(defaults, list) and defaults:
        idx = int(chosen.get("index") or 0)
        if 0 <= idx < len(defaults) and isinstance(defaults[idx], dict):
            mcc = defaults[idx].get("model_client_config") or {}
            api_key = resolve_env_vars(str(mcc.get("api_key") or ""))
            if not api_base:
                api_base = resolve_env_vars(str(mcc.get("api_base") or ""))
            if not model_name:
                model_name = resolve_env_vars(str(mcc.get("model_name") or ""))
    if not api_key:
        import os

        api_key = (os.environ.get("API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_base:
        import os

        api_base = (os.environ.get("API_BASE") or os.environ.get("OPENAI_API_BASE") or "").strip()
    api_base = resolve_env_vars(api_base)
    api_key = resolve_env_vars(api_key)
    model_name = resolve_env_vars(model_name)

    if not api_key or not api_base or api_base.startswith("https://example.com"):
        # Soft-fail with a deterministic local plan so the pipeline remains usable.
        text = (
            f"[local-tool-fallback] model={model_name} optimize={optimize_for}\n"
            f"{system.strip()}\n---\n{prompt.strip()[:1200]}"
        )
        return {
            "ok": True,
            "fallback": True,
            "model": chosen.get("id"),
            "model_name": model_name,
            "text": text,
        }

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, base_url=api_base)
        resp = await client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.4 if optimize_for == "quality" else 0.7,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            logger.warning("call_model_tool empty response model=%s", model_name)
            return {
                "ok": False,
                "fallback": False,
                "error": "empty_model_response",
                "model": chosen.get("id"),
                "model_name": model_name,
                "text": "",
            }
        return {
            "ok": True,
            "fallback": False,
            "model": chosen.get("id"),
            "model_name": model_name,
            "text": text,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("call_model_tool failed: %s", exc)
        return {
            "ok": False,
            "error": str(exc),
            "model": chosen.get("id"),
            "model_name": model_name,
            "text": "",
        }
