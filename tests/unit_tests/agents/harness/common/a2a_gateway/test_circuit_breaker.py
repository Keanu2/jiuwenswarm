# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the receptor-desensitization circuit breaker."""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.a2a_gateway.circuit_breaker import (
    CircuitState,
    PeerCircuitBreaker,
)
from jiuwenswarm.agents.harness.common.a2a_gateway.config import (
    CircuitBreakerConfig,
)


class TestPeerCircuitBreaker:
    async def test_disabled_always_allows(self) -> None:
        cb = PeerCircuitBreaker(CircuitBreakerConfig(enabled=False))
        for _ in range(100):
            await cb.acquire("p")
            await cb.release("p", success=False)
        assert cb.state("p") is CircuitState.CLOSED

    async def test_closed_then_desensitized_on_failure_rate(self) -> None:
        cb = PeerCircuitBreaker(CircuitBreakerConfig(
            enabled=True, failure_threshold=10, soft_threshold=0.5,
            desensitized_capacity=8,
        ))
        # 4 failures out of 4 → rate 1.0 >= 0.5 → desensitized
        for _ in range(4):
            await cb.acquire("p")
            await cb.release("p", success=False)
        assert cb.state("p") is CircuitState.DESENSITIZED

    async def test_open_after_failure_threshold(self) -> None:
        cb = PeerCircuitBreaker(CircuitBreakerConfig(
            enabled=True, failure_threshold=5, soft_threshold=2.0,
            desensitized_capacity=2,
        ))
        # soft_threshold=2.0 (impossible rate) keeps us out of desensitized;
        # accumulate 5 failures to trip the hard threshold → open.
        for _ in range(5):
            await cb.acquire("p")
            await cb.release("p", success=False)
        assert cb.state("p") is CircuitState.OPEN

    async def test_open_blocks_acquisition(self) -> None:
        cb = PeerCircuitBreaker(CircuitBreakerConfig(
            enabled=True, failure_threshold=2, soft_threshold=2.0,
        ))
        for _ in range(2):
            await cb.acquire("p")
            await cb.release("p", success=False)
        assert cb.state("p") is CircuitState.OPEN
        assert await cb.acquire("p") is False

    async def test_success_recovers_from_desensitized(self) -> None:
        cb = PeerCircuitBreaker(CircuitBreakerConfig(
            enabled=True, failure_threshold=10, soft_threshold=0.5,
            desensitized_capacity=8,
        ))
        for _ in range(4):
            await cb.acquire("p")
            await cb.release("p", success=False)
        assert cb.state("p") is CircuitState.DESENSITIZED
        await cb.acquire("p")
        await cb.release("p", success=True)
        assert cb.state("p") is CircuitState.CLOSED

    async def test_unknown_peer_starts_closed(self) -> None:
        cb = PeerCircuitBreaker(CircuitBreakerConfig(enabled=True))
        assert cb.state("never-seen") is CircuitState.CLOSED

    def test_reset_clears_peer(self) -> None:
        cb = PeerCircuitBreaker(CircuitBreakerConfig(enabled=True))
        # Touch the peer by inspecting state (creates entry).
        cb.state("p")
        cb.reset("p")
        # After reset, state should be CLOSED again.
        assert cb.state("p") is CircuitState.CLOSED
