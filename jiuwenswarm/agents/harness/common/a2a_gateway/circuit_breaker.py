# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Receptor-desensitization circuit breaker for outbound A2A peers.

Bio-inspired by cellular receptor desensitization: under sustained stimulus
(failure), receptors lose sensitivity (``desensitized``) before fully
shutting down (``open``), and recover gradually (``recovering``) toward
baseline (``closed``) once the stimulus abates. This avoids both
thundering-herd retries on a sick peer and permanent disconnection.

States::

    closed ──(failure_threshold reached)──> desensitized ──(more failures)──> open
      ▲                                          │
      └──────────(recovery_rate_constant)────────┘
                          │
                         open ──(open_timeout_ms)──> recovering ──(probe success)──> closed
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

from .config import CircuitBreakerConfig


class CircuitState(str, Enum):
    CLOSED = "closed"
    DESENSITIZED = "desensitized"
    OPEN = "open"
    RECOVERING = "recovering"


@dataclass
class _PeerState:
    failures: int = 0
    successes: int = 0
    window_total: int = 0
    window_failures: int = 0
    state: CircuitState = CircuitState.CLOSED
    opened_at: float = 0.0
    in_flight: int = 0
    last_transition: float = 0.0


class PeerCircuitBreaker:
    """Per-peer circuit breaker.

    Call :meth:`allow` before a send to check admission; :meth:`record_success`
    / :meth:`record_failure` after. The breaker is best-effort and never raises
    — admission decisions degrade gracefully to ``CLOSED`` on misconfiguration.
    """

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self._config = config or CircuitBreakerConfig()
        self._peers: dict[str, _PeerState] = {}
        self._lock = asyncio.Lock()

    def _state(self, peer: str) -> _PeerState:
        s = self._peers.get(peer)
        if s is None:
            s = _PeerState(last_transition=time.monotonic())
            self._peers[peer] = s
        return s

    async def allow(self, peer: str) -> bool:
        """Return whether a call to *peer* may proceed right now."""
        if not self._config.enabled:
            return True
        async with self._lock:
            s = self._state(peer)
            now = time.monotonic()
            self._maybe_recover(s, now)
            if s.state is CircuitState.OPEN:
                return False
            if s.state is CircuitState.DESENSITIZED:
                return s.in_flight < self._config.desensitized_capacity
            return True

    async def acquire(self, peer: str) -> bool:
        """Admission control + in-flight accounting. Returns False if rejected."""
        if not self._config.enabled:
            return True
        async with self._lock:
            s = self._state(peer)
            now = time.monotonic()
            self._maybe_recover(s, now)
            if s.state is CircuitState.OPEN:
                return False
            if s.state is CircuitState.DESENSITIZED:
                if s.in_flight >= self._config.desensitized_capacity:
                    return False
            s.in_flight += 1
            return True

    async def release(self, peer: str, *, success: bool) -> None:
        if not self._config.enabled:
            return
        async with self._lock:
            s = self._state(peer)
            s.in_flight = max(0, s.in_flight - 1)
            s.window_total += 1
            if success:
                s.successes += 1
                self._record_success(s)
            else:
                s.failures += 1
                s.window_failures += 1
                self._record_failure(s)

    def _maybe_recover(self, s: _PeerState, now: float) -> None:
        """Advance state machine based on elapsed time (gradual recovery)."""
        elapsed = now - s.last_transition
        if s.state is CircuitState.OPEN:
            if elapsed * 1000 >= self._config.open_timeout_ms:
                s.state = CircuitState.RECOVERING
                s.last_transition = now
        elif s.state is CircuitState.RECOVERING:
            # Recovering decays toward closed at recovery_rate_constant per second.
            if elapsed * self._config.recovery_rate_constant >= 1.0:
                s.state = CircuitState.CLOSED
                s.failures = 0
                s.window_failures = 0
                s.window_total = 0
                s.last_transition = now
        elif s.state is CircuitState.DESENSITIZED:
            # Gradual recovery while no new failures accumulate.
            if elapsed > 0 and s.failures == 0:
                s.state = CircuitState.CLOSED
                s.last_transition = now

    def _record_success(self, s: _PeerState) -> None:
        now = time.monotonic()
        if s.state in (CircuitState.RECOVERING, CircuitState.DESENSITIZED):
            s.state = CircuitState.CLOSED
            s.failures = 0
            s.window_failures = 0
            s.window_total = 0
            s.last_transition = now

    def _record_failure(self, s: _PeerState) -> None:
        now = time.monotonic()
        # Soft threshold: high failure rate over a small window → desensitize.
        if s.window_total >= 4 and s.state is CircuitState.CLOSED:
            rate = s.window_failures / s.window_total
            if rate >= self._config.soft_threshold:
                s.state = CircuitState.DESENSITIZED
                s.last_transition = now
                return
        if s.failures >= self._config.failure_threshold:
            if s.state is not CircuitState.OPEN:
                s.state = CircuitState.OPEN
                s.opened_at = now
                s.last_transition = now

    def state(self, peer: str) -> CircuitState:
        if not self._config.enabled:
            return CircuitState.CLOSED
        return self._state(peer).state

    def reset(self, peer: str | None = None) -> None:
        """Reset one or all peers (e.g. on manual override)."""
        if peer is None:
            self._peers.clear()
        else:
            self._peers.pop(peer, None)
