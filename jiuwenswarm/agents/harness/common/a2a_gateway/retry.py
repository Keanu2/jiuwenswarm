# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exponential-backoff retry helper for outbound A2A sends.

A minimal, jitter-aware retry wrapper. We avoid pulling in a retry library
to keep the optional ``a2a`` dependency surface small.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .config import RetryConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _is_retryable_exception(exc: BaseException) -> bool:
    """Classify whether *exc* is worth retrying.

    Connection errors, timeouts, and transport-level 5xx are retryable.
    Auth (401/403) and A2A protocol errors are not — they would fail
    identically on the next attempt.
    """
    name = type(exc).__name__.lower()
    if any(k in name for k in ("timeout", "connect", "connection", "reset", "refused")):
        return True
    # httpx transport errors carry a status_code attr on some wrappers.
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and 500 <= status < 600:
        return True
    return False


async def with_retry(
    func: Callable[[], Awaitable[T]],
    config: RetryConfig,
    *,
    peer_name: str = "",
    log: bool = True,
) -> T:
    """Call *func* with exponential backoff up to ``config.max_retries`` times.

    Only retryable exceptions (see :func:`_is_retryable_exception`) trigger a
    retry; others propagate immediately.
    """
    attempt = 0
    delay_ms = config.initial_delay_ms
    while True:
        try:
            return await func()
        except Exception as exc:  # noqa: BLE001
            if not _is_retryable_exception(exc) or attempt >= config.max_retries:
                raise
            attempt += 1
            jitter = random.uniform(0, config.jitter_ms) if config.jitter_ms else 0
            sleep_s = (delay_ms + jitter) / 1000.0
            if log:
                logger.info(
                    "[a2a.retry] peer=%s attempt=%d/%d retry in %.2fs: %s",
                    peer_name, attempt, config.max_retries, sleep_s, exc,
                )
            await asyncio.sleep(sleep_s)
            delay_ms = min(int(delay_ms * config.backoff_factor), config.max_delay_ms)
