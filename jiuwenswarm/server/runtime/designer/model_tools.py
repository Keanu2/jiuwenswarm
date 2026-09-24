# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Settings-backed model tools for Designer node agents."""

from __future__ import annotations

import base64
import logging
import mimetypes
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from jiuwenswarm.common.config import get_config, get_model_names, resolve_env_vars

logger = logging.getLogger(__name__)

# DeepSeek/Ark allow very large completions; keep a high practical ceiling so
# storyboard/plan JSON is not truncated mid-object (truncation → parse retry → slow).
# Thinking mode shares this budget with final content — we disable thinking below.
_DESIGNER_MAX_TOKENS_CAP = 65536
_DESIGNER_DEFAULT_MAX_TOKENS = 16384
_preferred_designer_model: ContextVar[str | None] = ContextVar(
    "preferred_designer_model",
    default=None,
)


@contextmanager
def use_preferred_designer_model(model_name: str | None) -> Iterator[None]:
    """Apply one UI-selected model to all Designer planning calls in this context."""
    normalized = str(model_name or "").strip() or None
    token = _preferred_designer_model.set(normalized)
    try:
        yield
    finally:
        _preferred_designer_model.reset(token)


def _clamp_max_tokens(value: int | None) -> int:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        n = _DESIGNER_DEFAULT_MAX_TOKENS
    return max(256, min(_DESIGNER_MAX_TOKENS_CAP, n))


def _thinking_disabled_extra_body() -> dict[str, Any]:
    """Provider-neutral knobs so reasoning does not eat the output budget."""
    return {
        "thinking": {"type": "disabled"},
        "reasoning": {"enabled": False},
        "enable_thinking": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def _message_text(msg: Any) -> str:
    """Prefer visible content; fall back to reasoning_content if content is empty."""
    if msg is None:
        return ""
    text = str(getattr(msg, "content", None) or "").strip()
    if text:
        return text
    text = str(getattr(msg, "refusal", None) or "").strip()
    if text:
        return text
    # DeepSeek thinking models may put usable text only in reasoning_content
    # when the visible budget was exhausted — better than a total soft-fail.
    text = str(getattr(msg, "reasoning_content", None) or "").strip()
    if text:
        return text
    extra = getattr(msg, "model_extra", None)
    if isinstance(extra, dict):
        text = str(extra.get("reasoning_content") or "").strip()
        if text:
            return text
    return ""


# Process-wide: a 402 / insufficient-balance reply means Designer must not
# label nodes as live chat agents. Cleared only when this process restarts.
_chat_billing_block: str = ""
_chat_confirmed: bool = False
_chat_probe_done: bool = False


def is_chat_payment_block(detail: object) -> bool:
    """True for HTTP 402 / insufficient balance on the chat model (not image or video)."""
    code = getattr(detail, "status_code", None)
    if code is None:
        resp = getattr(detail, "response", None)
        code = getattr(resp, "status_code", None) if resp is not None else None
    try:
        if int(code) == 402:
            return True
    except (TypeError, ValueError):
        pass
    low = str(detail or "").lower()
    return (
        "insufficient balance" in low
        or "insufficient_quota" in low
        or "payment required" in low
        or "error code: 402" in low
    )


def chat_model_billing_block() -> str:
    """Non-empty when the chat account is known to be unpaid / 402."""
    return _chat_billing_block


def note_chat_model_unavailable(detail: object) -> bool:
    """Record a payment block. Returns True only for 402-style failures."""
    global _chat_billing_block
    if not is_chat_payment_block(detail):
        return False
    _chat_billing_block = str(detail or "402")[:300]
    return True


def demote_config_to_handler(cfg: dict[str, Any]) -> None:
    """Stop treating this node as a live chat agent; handlers keep prewritten text."""
    if not isinstance(cfg, dict):
        return
    cfg["delegate"] = "handler"
    draft = cfg.get("draft_prewritten")
    if draft and not str(cfg.get("prewritten") or "").strip():
        cfg["prewritten"] = draft
    cfg["skip_llm"] = True


def llm_available() -> bool:
    """True when Settings has a usable chat model with credentials for Designer agents.

    A recorded 402 / insufficient balance makes this False so graphs are not
    stamped ``delegate=agent``.
    """
    if _chat_billing_block:
        return False
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


def ensure_chat_model_reachable() -> bool:
    """Probe the chat model once. A 402 marks it unavailable for this process.

    Unit tests skip the network probe (``PYTEST_CURRENT_TEST``). Image and video
    vendors are not probed here.
    """
    global _chat_confirmed, _chat_probe_done
    import os

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return llm_available()
    if _chat_billing_block:
        return False
    if _chat_confirmed:
        return True
    if not llm_available():
        return False
    if _chat_probe_done:
        return llm_available()
    _chat_probe_done = True
    try:
        from openai import OpenAI

        models = list_configured_models()
        chosen = pick_model_for_optimize("cost") or (models[0] if models else None)
        if not isinstance(chosen, dict):
            return llm_available()
        api_key = (os.environ.get("API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
        api_base = resolve_env_vars(
            str(chosen.get("api_base") or os.environ.get("API_BASE") or os.environ.get("OPENAI_API_BASE") or "")
        ).strip()
        model_name = resolve_env_vars(
            str(chosen.get("model_name") or chosen.get("id") or os.environ.get("MODEL_NAME") or "")
        ).strip()
        if not api_key or not api_base or not model_name or api_base.startswith("https://example.com"):
            return llm_available()
        client = OpenAI(api_key=api_key, base_url=api_base, timeout=8.0)
        try:
            client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": "ok"}],
                max_tokens=1,
                temperature=0,
            )
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        _chat_confirmed = True
        return True
    except Exception as exc:  # noqa: BLE001
        if note_chat_model_unavailable(exc):
            logger.warning("chat model unavailable (billing): %s", exc)
            return False
        logger.info("chat model probe failed open: %s", exc)
        return llm_available()


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


_MAX_VISION_IMAGE_BYTES = 6 * 1024 * 1024


def vision_user_content(
    prompt: str,
    images: list[str] | None = None,
) -> str | list[dict[str, Any]]:
    """OpenAI-compatible user content: text plus original reference images.

    Original files stay the visual authority. Callers should describe slots in
    ``prompt``; this helper does not caption or summarize the pictures.
    """
    blocks: list[dict[str, Any]] = [{"type": "text", "text": str(prompt or "")}]
    for raw in (images or [])[:3]:
        url = _image_data_uri(raw)
        if not url:
            continue
        blocks.append({"type": "image_url", "image_url": {"url": url}})
    if len(blocks) == 1:
        return str(prompt or "")
    return blocks


def _image_data_uri(raw: str) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if value.startswith(("http://", "https://", "data:")):
        return value
    path = Path(value)
    if value.startswith("file:"):
        from urllib.parse import unquote, urlparse

        parsed = urlparse(value)
        pathname = unquote(parsed.path)
        if len(pathname) >= 3 and pathname[0] == "/" and pathname[2] == ":":
            pathname = pathname[1:]
        path = Path(pathname)
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size <= 0 or size > _MAX_VISION_IMAGE_BYTES:
            return None
        mime, _ = mimetypes.guess_type(str(path))
        if not mime or not mime.startswith("image/"):
            mime = "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None
    return f"data:{mime};base64,{encoded}"


async def call_model_tool(
    *,
    prompt: str,
    system: str,
    optimize_for: str,
    preferred_model: str | None = None,
    max_tokens: int = _DESIGNER_DEFAULT_MAX_TOKENS,
    images: list[str] | None = None,
) -> dict[str, Any]:
    """Call a configured chat model (OpenAI-compatible) as an agent tool."""
    global _chat_confirmed
    if _chat_billing_block:
        return {
            "ok": False,
            "unavailable": True,
            "error": _chat_billing_block,
            "model": None,
            "text": "",
        }
    # Ensure ~/.jiuwenswarm/config/.env is loaded (API_KEY / API_BASE).
    try:
        from jiuwenswarm.common.utils import get_env_file
        from jiuwenswarm.dotenv_early import load_dotenv_runtime

        load_dotenv_runtime(dotenv_path=get_env_file(), override=False)
    except Exception:  # noqa: BLE001
        pass

    max_tokens = _clamp_max_tokens(max_tokens)
    preferred_model = preferred_model or _preferred_designer_model.get()

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

        async def _once(*, temperature: float) -> dict[str, Any]:
            # Large plans need more wall time than the old 90s default.
            client = AsyncOpenAI(api_key=api_key, base_url=api_base, timeout=1200.0)
            try:
                user_content = vision_user_content(prompt, images)
                resp = await client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_content},
                    ],
                    max_tokens=max_tokens,
                    temperature=temperature,
                    extra_body=_thinking_disabled_extra_body(),
                )
                choice = resp.choices[0] if resp.choices else None
                msg = choice.message if choice is not None else None
                text = _message_text(msg)
                finish = str(getattr(choice, "finish_reason", None) or "")
                if not text:
                    return {
                        "ok": False,
                        "fallback": False,
                        "error": "empty_model_response",
                        "model": chosen.get("id"),
                        "model_name": model_name,
                        "text": "",
                        "finish_reason": finish or None,
                        "max_tokens": max_tokens,
                    }
                return {
                    "ok": True,
                    "fallback": False,
                    "model": chosen.get("id"),
                    "model_name": model_name,
                    "text": text,
                    "finish_reason": finish or None,
                    "max_tokens": max_tokens,
                }
            finally:
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass

        first = await _once(temperature=0.4 if optimize_for == "quality" else 0.7)
        if first.get("ok"):
            _chat_confirmed = True
            return first
        if first.get("error") == "empty_model_response":
            logger.warning(
                "call_model_tool empty response model=%s finish=%s max_tokens=%s; retrying once",
                model_name,
                first.get("finish_reason"),
                max_tokens,
            )
            return await _once(temperature=0.2)
        return first
    except Exception as exc:  # noqa: BLE001
        blocked = note_chat_model_unavailable(exc)
        logger.warning("call_model_tool failed: %s", exc)
        return {
            "ok": False,
            "unavailable": blocked,
            "error": str(exc),
            "model": chosen.get("id"),
            "model_name": model_name,
            "text": "",
        }
