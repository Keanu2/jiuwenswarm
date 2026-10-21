# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime entry points for the personal task-continuation client.

Mirrors the group chat runtime: the client class is imported late, construction
never raises, and every call site closes the client in a ``finally``. Every
public function here is a thin "open a client, do one thing, close it" wrapper —
free functions rather than methods so the RPC handlers stay free of client
lifecycle code, and raw (possibly-unvalidated) params so the client's validators
remain the single place those are checked.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from .client import AsyncTasksClient
from .config import TasksConfig, resolve_tasks_config
from .errors import TaskUnavailableError

logger = logging.getLogger(__name__)

DEFAULT_CALL_TIMEOUT = 20.0
"""Wall-clock cap around a single task API call, independent of the httpx timeout.

The httpx timeout covers connect/read/write individually; this bounds the whole
operation so a wedged relay cannot hold an AgentServer request handler open.
"""


def build_tasks_client(
    config_base: Mapping[str, Any] | None = None,
) -> tuple[AsyncTasksClient, TasksConfig]:
    """Create a client from resolved config."""
    config = resolve_tasks_config(config_base)
    return AsyncTasksClient(config), config


async def _call(
    method: str,
    *,
    config_base: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Invoke one client method on a short-lived client, closing it either way."""
    client, config = build_tasks_client(config_base)
    try:
        return await asyncio.wait_for(
            getattr(client, method)(**kwargs), timeout=_call_timeout(config)
        )
    except asyncio.TimeoutError as exc:
        raise TaskUnavailableError("任务接续服务响应超时") from exc
    finally:
        await client.aclose()


def _call_timeout(config: TasksConfig) -> float:
    return max(config.timeout, DEFAULT_CALL_TIMEOUT)


# ── Tasks ───────────────────────────────────────────────────────────────────


async def create_task(
    *,
    title: str = "",
    owner_dataset: str | None = None,
    payload: dict[str, Any] | None = None,
    config_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return await _call(
        "create_task",
        config_base=config_base,
        title=title,
        owner_dataset=owner_dataset,
        payload=payload,
    )


async def list_tasks(
    *,
    state: str | None = None,
    limit: int = 50,
    config_base: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return await _call(
        "list_tasks", config_base=config_base, state=state, limit=limit
    )


async def get_task(
    *, task_id: Any, config_base: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return await _call("get_task", config_base=config_base, task_id=task_id)


async def update_task(
    *,
    task_id: Any,
    title: Any = None,
    archived: Any = None,
    config_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return await _call(
        "update_task",
        config_base=config_base,
        task_id=task_id,
        title=title,
        archived=archived,
    )


async def retire(
    *, task_id: Any, config_base: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return await _call("retire", config_base=config_base, task_id=task_id)


# ── Lease ───────────────────────────────────────────────────────────────────


async def acquire(
    *,
    task_id: Any,
    ttl_seconds: Any = None,
    mode: str = "force",
    config_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return await _call(
        "acquire",
        config_base=config_base,
        task_id=task_id,
        ttl_seconds=ttl_seconds,
        mode=mode,
    )


async def renew(
    *,
    task_id: Any,
    fence: Any,
    ttl_seconds: Any = None,
    config_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return await _call(
        "renew",
        config_base=config_base,
        task_id=task_id,
        fence=fence,
        ttl_seconds=ttl_seconds,
    )


async def release(
    *,
    task_id: Any,
    fence: Any,
    next_state: Any = None,
    checkpoint: Any = None,
    checkpoint_seq: Any = None,
    config_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return await _call(
        "release",
        config_base=config_base,
        task_id=task_id,
        fence=fence,
        next_state=next_state,
        checkpoint=checkpoint,
        checkpoint_seq=checkpoint_seq,
    )


async def checkpoint(
    *,
    task_id: Any,
    fence: Any,
    checkpoint: Any,
    checkpoint_seq: Any,
    config_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return await _call(
        "checkpoint",
        config_base=config_base,
        task_id=task_id,
        fence=fence,
        checkpoint=checkpoint,
        checkpoint_seq=checkpoint_seq,
    )


# ── Messages ────────────────────────────────────────────────────────────────


async def append_message(
    *,
    task_id: Any,
    fence: Any,
    payload: Any,
    kind: Any = "text",
    client_msg_id: Any = None,
    reply_to: Any = None,
    config_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return await _call(
        "append_message",
        config_base=config_base,
        task_id=task_id,
        fence=fence,
        payload=payload,
        kind=kind,
        client_msg_id=client_msg_id,
        reply_to=reply_to,
    )


async def fetch_messages(
    *,
    task_id: Any,
    after_seq: Any = None,
    before_seq: Any = None,
    limit: Any = None,
    config_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return await _call(
        "fetch_messages",
        config_base=config_base,
        task_id=task_id,
        after_seq=after_seq,
        before_seq=before_seq,
        limit=limit,
    )


async def ack_messages(
    *,
    task_id: Any,
    seq: Any,
    config_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return await _call("ack", config_base=config_base, task_id=task_id, seq=seq)


async def status(
    *, config_base: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Relay-side module status, for diagnostics."""
    return await _call("status", config_base=config_base)


async def whoami(
    *, config_base: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Resolve the identity behind the configured tasks credential."""
    return await _call("whoami", config_base=config_base)


__all__ = [
    "AsyncTasksClient",
    "TasksConfig",
    "TaskUnavailableError",
    "ack_messages",
    "acquire",
    "append_message",
    "build_tasks_client",
    "checkpoint",
    "create_task",
    "fetch_messages",
    "get_task",
    "list_tasks",
    "release",
    "renew",
    "resolve_tasks_config",
    "retire",
    "status",
    "update_task",
    "whoami",
]