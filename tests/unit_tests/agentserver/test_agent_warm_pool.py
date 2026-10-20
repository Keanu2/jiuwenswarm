from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.agent_warm_pool import AgentWarmPool


class _FakeRootAgent:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.prepared: list[str] = []
        self.cleaned: list[str] = []

    async def prepare_session(self, *, session_id: str, **_kwargs) -> None:
        await asyncio.sleep(0)
        if self.fail:
            raise RuntimeError("prepare failed")
        self.prepared.append(session_id)

    async def cleanup_session_runtime(self, session_id: str) -> bool:
        self.cleaned.append(session_id)
        return True


class _ControlledRootAgent(_FakeRootAgent):
    def __init__(self) -> None:
        super().__init__()
        self.started: list[str] = []
        self.cancelled: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}

    async def prepare_session(self, *, session_id: str, **_kwargs) -> None:
        gate = asyncio.Event()
        self.gates[session_id] = gate
        self.started.append(session_id)
        try:
            await gate.wait()
        except asyncio.CancelledError:
            self.cancelled.append(session_id)
            raise
        self.prepared.append(session_id)


class _FakeManager:
    def __init__(self, agent: _FakeRootAgent) -> None:
        self.agent = agent
        self.pins = 0
        self.get_agent_calls: list[dict] = []

    async def get_agent(self, **kwargs):
        self.get_agent_calls.append(kwargs)
        return self.agent

    def pin_agent(self, _agent) -> None:
        self.pins += 1

    def unpin_agent(self, _agent) -> None:
        self.pins -= 1


async def _wait_until(predicate, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not become true")


@pytest.fixture
def isolated_pool(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.project_store.list_projects",
        lambda **_kwargs: [],
    )

    def factory(agent: _FakeRootAgent) -> AgentWarmPool:
        # Prewarming is on by default; stay explicit so a developer environment
        # that opts out cannot silently turn these cases into no-ops.
        return AgentWarmPool(_FakeManager(agent), max_concurrency=4, enabled=True)

    yield factory


def test_prewarm_is_enabled_unless_the_environment_opts_out(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.get_agent_sessions_dir",
        lambda: tmp_path,
    )

    def build() -> AgentWarmPool:
        return AgentWarmPool(_FakeManager(_FakeRootAgent()))

    monkeypatch.delenv("JIUWENSWARM_AGENT_PREWARM", raising=False)
    assert build()._enabled is True

    monkeypatch.setenv("JIUWENSWARM_AGENT_PREWARM", " OFF ")
    assert build()._enabled is False

    monkeypatch.setenv("JIUWENSWARM_AGENT_PREWARM", "1")
    assert build()._enabled is True


@pytest.mark.asyncio
async def test_disabled_pool_never_warms_and_always_bypasses(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    monkeypatch.setenv("JIUWENSWARM_AGENT_PREWARM", "0")
    agent = _FakeRootAgent()
    pool = AgentWarmPool(_FakeManager(agent))

    stats = await pool.sync(["web"], config={"model": "a"})
    assert stats == {"target": 0, "ready": 0, "warming": 0, "failed": 0, "stale": 0}

    key = pool.make_key(
        channel_id="web",
        project_id="default",
        project_dir="",
        work_mode="work",
    )
    claim = await pool.claim(key)
    assert claim.prewarm_hit is False
    assert claim.prewarm_status == "bypassed"
    assert claim.session_id.startswith("web_")
    assert not pool._slots
    assert not pool._tasks
    assert agent.prepared == []
    await pool.wait_for_session(claim.session_id)
    await pool.close()


def test_warm_key_normalizes_project_directory(tmp_path: Path) -> None:
    key = AgentWarmPool.make_key(
        channel_id=" web ",
        project_id="project-a",
        project_dir=str(tmp_path / ".." / tmp_path.name),
        work_mode="CODE",
    )
    assert key.channel_id == "web"
    assert key.project_dir == str(tmp_path.resolve()).lower()
    assert key.agent_mode == "code"
    assert key.agent_sub_mode == "normal"


@pytest.mark.asyncio
async def test_code_prewarm_uses_same_manager_cache_identity_as_code_chat(
    isolated_pool,
) -> None:
    pool = isolated_pool(_FakeRootAgent())
    key = pool.make_key(
        channel_id="web",
        project_id="project-code",
        project_dir="/tmp/code-project",
        work_mode="code",
    )

    claim = await pool.claim(key)
    await pool.wait_for_session(claim.session_id)

    assert pool._manager.get_agent_calls == [
        {
            "channel_id": "web",
            "mode": "code",
            "project_dir": key.project_dir,
            "sub_mode": "normal",
        }
    ]
    await pool.end_foreground()
    await pool.close()


@pytest.mark.asyncio
async def test_pool_keeps_one_ready_slot_total_and_claim_is_atomic(isolated_pool) -> None:
    agent = _FakeRootAgent()
    pool = isolated_pool(agent)
    stats = await pool.sync(["web"], config={"model": "a"})
    assert stats["target"] == 2
    await _wait_until(lambda: len(pool._slots) == 1)

    work_key = pool.make_key(
        channel_id="web",
        project_id="default",
        project_dir="",
        work_mode="work",
    )
    claims = await asyncio.gather(pool.claim(work_key), pool.claim(work_key))
    assert len({claim.session_id for claim in claims}) == 2
    assert sum(claim.prewarm_hit for claim in claims) == 1
    assert {claim.prewarm_status for claim in claims} == {"ready", "warming"}
    await pool.end_foreground()
    await _wait_until(lambda: work_key in pool._slots)
    assert len([key for key in pool._slots if key == work_key]) == 1
    assert len(pool._slots) == 1
    await pool.close()


@pytest.mark.asyncio
async def test_sync_filters_non_user_protocol_channels(isolated_pool) -> None:
    pool = isolated_pool(_FakeRootAgent())
    stats = await pool.sync(["web", "acp", "a2a"], config={"model": "a"})
    assert stats["target"] == 2
    assert pool._enabled_channels == {"web"}
    await pool.close()


@pytest.mark.asyncio
async def test_foreground_bypasses_background_and_pauses_lazy_dispatch(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.project_store.list_projects",
        lambda **_kwargs: [],
    )
    agent = _ControlledRootAgent()
    pool = AgentWarmPool(_FakeManager(agent), max_concurrency=1, enabled=True)
    await pool.sync(["web"], config={"model": "a"})
    await _wait_until(lambda: len(agent.started) == 1)
    assert len(pool._tasks) == 1
    assert len(pool._pending) == 1

    await pool.begin_foreground()
    key = pool.make_key(
        channel_id="web",
        project_id="default",
        project_dir="",
        work_mode="work",
    )
    claim = await pool.claim(key)
    assert claim.prewarm_hit is False
    await _wait_until(lambda: len(agent.started) == 2)

    background_id, foreground_id = agent.started
    await _wait_until(lambda: background_id in agent.cancelled)
    assert len(agent.started) == 2

    agent.gates[foreground_id].set()
    await pool.wait_for_session(foreground_id)
    assert len(agent.started) == 2

    await pool.end_foreground()
    await _wait_until(lambda: len(agent.started) == 3)
    for gate in agent.gates.values():
        gate.set()
    await pool.close()


@pytest.mark.asyncio
async def test_claim_promotes_matching_background_task_without_duplicate_prepare(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_warm_pool.project_store.list_projects",
        lambda **_kwargs: [],
    )
    agent = _ControlledRootAgent()
    pool = AgentWarmPool(_FakeManager(agent), max_concurrency=1, enabled=True)
    await pool.sync(["web"], config={"model": "a"})
    await _wait_until(lambda: len(agent.started) == 1)
    background_id = agent.started[0]
    key = pool.make_key(
        channel_id="web",
        project_id="default",
        project_dir="",
        work_mode="work",
    )

    claim = await pool.claim(key)

    assert claim.session_id == background_id
    assert claim.prewarm_hit is False
    assert claim.prewarm_status == "warming"
    assert len(agent.started) == 1
    agent.gates[background_id].set()
    await pool.wait_for_session(background_id)
    assert len(agent.started) == 1
    await pool.close()


@pytest.mark.asyncio
async def test_config_revision_replaces_unclaimed_slots(isolated_pool) -> None:
    agent = _FakeRootAgent()
    pool = isolated_pool(agent)
    await pool.sync(["web"], config={"model": "old"})
    await _wait_until(lambda: len(pool._slots) == 1)
    old_ids = {slot.session_id for slot in pool._slots.values()}

    await pool.sync(["web"], config={"model": "new"})
    await _wait_until(
        lambda: len(pool._slots) == 1
        and not old_ids.intersection(slot.session_id for slot in pool._slots.values())
    )
    assert old_ids.issubset(set(agent.cleaned))
    await pool.close()


@pytest.mark.asyncio
async def test_slow_older_sync_cannot_overwrite_newer_revision(
    isolated_pool, monkeypatch
) -> None:
    pool = isolated_pool(_FakeRootAgent())
    original_desired_keys = pool._desired_keys
    first_started = threading.Event()
    release_first = threading.Event()
    call_count = 0
    call_lock = threading.Lock()

    def _controlled_desired_keys(channels):
        nonlocal call_count
        with call_lock:
            call_count += 1
            call_number = call_count
        if call_number == 1:
            first_started.set()
            assert release_first.wait(timeout=5)
        return original_desired_keys(channels)

    monkeypatch.setattr(pool, "_desired_keys", _controlled_desired_keys)
    older = asyncio.create_task(pool.sync(["web"], config={"model": "old"}))
    await asyncio.wait_for(asyncio.to_thread(first_started.wait), timeout=1)
    await pool.sync(["web"], config={"model": "new"})
    release_first.set()
    await older

    assert pool._revision.config_fingerprint == pool.config_fingerprint(
        {"model": "new"}
    )
    await pool.close()


@pytest.mark.asyncio
async def test_failed_prepare_never_becomes_ready(isolated_pool) -> None:
    pool = isolated_pool(_FakeRootAgent(fail=True))
    await pool.sync(["web"], config={"model": "broken"})
    await _wait_until(lambda: not pool._tasks)
    stats = await pool.stats()
    assert stats["ready"] == 0
    assert stats["failed"] == 1
    assert stats["warming"] == 1
    await pool.close()


@pytest.mark.asyncio
async def test_cold_start_retains_budget_beyond_mcp_discovery(isolated_pool, monkeypatch):
    from jiuwenswarm.common import mcp_call_timeout_patch as mcp_timeouts
    from jiuwenswarm.server.runtime import agent_warm_pool as module

    # Scale seconds to milliseconds while preserving the production budgets.
    scale = 0.001
    monkeypatch.setattr(module, "_PREPARE_TIMEOUT_SECONDS", module._PREPARE_TIMEOUT_SECONDS * scale)
    discovery_budget = mcp_timeouts.DISCOVERY_TIMEOUT * scale

    class ColdAgent(_FakeRootAgent):
        async def prepare_session(self, *, session_id, **_kwargs):
            await asyncio.sleep(discovery_budget * 2)
            self.prepared.append(session_id)

    agent = ColdAgent()
    pool = isolated_pool(agent)

    async def cold_get_agent(**_kwargs):
        await asyncio.sleep(discovery_budget * 2)
        return agent

    monkeypatch.setattr(pool._manager, "get_agent", cold_get_agent)
    key = pool.make_key(channel_id="web", project_id="default", project_dir="", work_mode="work")
    try:
        claim = await pool.claim(key)
        await pool.wait_for_session(claim.session_id)
        assert agent.prepared == [claim.session_id]
        assert not agent.cleaned
        assert not pool._failed
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["get_agent", "prepare_session", "cleanup"])
async def test_prepare_deadline_releases_initialization_lock(isolated_pool, monkeypatch, stage):
    from jiuwenswarm.server.runtime import agent_warm_pool as module

    monkeypatch.setattr(module, "_PREPARE_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(module, "_PREPARE_CLEANUP_TIMEOUT_SECONDS", 0.03)
    agent = _ControlledRootAgent()
    pool = isolated_pool(agent)
    if stage == "get_agent":
        async def blocked_get_agent(**_kwargs):
            await asyncio.Event().wait()
        monkeypatch.setattr(pool._manager, "get_agent", blocked_get_agent)
    elif stage == "cleanup":
        async def blocked_cleanup(_sid):
            await asyncio.Event().wait()
        monkeypatch.setattr(agent, "cleanup_session_runtime", blocked_cleanup)
    key = pool.make_key(channel_id="web", project_id="default", project_dir="", work_mode="work")
    try:
        with pytest.raises(TimeoutError, match="preparation timed out"):
            await asyncio.wait_for(pool._prepare(key, "deadline-session", pool._revision, keep_as_slot=False), 1.0)
        assert key in pool._failed
        assert "timed out" in pool._failed[key]
        assert not pool._slots
        assert not pool._initialization_lock.locked()
        assert pool._foreground_count == 0
        if stage == "prepare_session":
            assert agent.cancelled == ["deadline-session"]
            assert agent.cleaned == ["deadline-session"]
        # A subsequent healthy preparation must get past the shared lock.
        healthy = _FakeRootAgent()
        pool._manager.agent = healthy
        monkeypatch.setattr(pool._manager, "get_agent", _FakeManager(healthy).get_agent)
        await asyncio.wait_for(pool._prepare(key, "healthy-session", pool._revision, keep_as_slot=False), 1.0)
        assert healthy.prepared == ["healthy-session"]
    finally:
        await pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_failed_prepare_stops_real_manager_dispatch(isolated_pool, monkeypatch, late, stream):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from jiuwenswarm.server.runtime import agent_warm_pool as module
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

    monkeypatch.setattr(module, "_PREPARE_TIMEOUT_SECONDS", 0.03)
    pool = isolated_pool(_ControlledRootAgent())
    key = pool.make_key(channel_id="web", project_id="default", project_dir="", work_mode="work")
    claim = await pool.claim(key)
    manager = AgentManager.__new__(AgentManager)
    manager.warm_pool = pool
    manager.get_agent = AsyncMock()
    try:
        if late:
            await _wait_until(lambda: pool._session_tasks[claim.session_id].done())
        request = SimpleNamespace(session_id=claim.session_id, channel_id="web", params={})
        with pytest.raises(TimeoutError, match="preparation timed out"):
            if stream:
                async for _ in manager.process_message_stream(request):
                    pytest.fail("A failed preparation must not produce an agent result")
            else:
                await manager.process_message(request)
        manager.get_agent.assert_not_awaited()
        assert claim.session_id not in pool._session_tasks
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_timed_out_background_slot_recovers_without_sync(isolated_pool, monkeypatch):
    from jiuwenswarm.server.runtime import agent_warm_pool as module

    monkeypatch.setattr(module, "_PREPARE_TIMEOUT_SECONDS", 0.03)

    class FailOnce(_FakeRootAgent):
        calls = 0

        async def prepare_session(self, *, session_id, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                await asyncio.Event().wait()
            self.prepared.append(session_id)

    agent = FailOnce()
    pool = isolated_pool(agent)
    try:
        await pool.sync(["web"], config={"model": "recovery"})
        await _wait_until(lambda: bool(pool._slots))
        assert agent.calls == 2
        assert len(agent.cleaned) == 1
        assert agent.prepared[0] != agent.cleaned[0]
        assert not pool._failed
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_prepare_deadline_includes_shared_lock_wait(isolated_pool, monkeypatch):
    from jiuwenswarm.server.runtime import agent_warm_pool as module

    monkeypatch.setattr(module, "_PREPARE_TIMEOUT_SECONDS", 0.03)
    pool = isolated_pool(_FakeRootAgent())
    key = pool.make_key(channel_id="web", project_id="default", project_dir="", work_mode="work")
    await pool._initialization_lock.acquire()
    try:
        claim = await pool.claim(key)
        with pytest.raises(TimeoutError, match="preparation timed out"):
            await asyncio.wait_for(pool.wait_for_session(claim.session_id), 1.0)
        assert pool._manager.get_agent_calls == []
        # The timed-out waiter must not release another task's lock.
        assert pool._initialization_lock.locked()
    finally:
        pool._initialization_lock.release()
        await pool.close()


@pytest.mark.asyncio
async def test_claim_marker_survives_until_metadata_activation(isolated_pool) -> None:
    pool = isolated_pool(_FakeRootAgent())
    await pool.sync(["web"], config={"model": "ready"})
    await _wait_until(lambda: len(pool._slots) == 1)
    key = pool.make_key(
        channel_id="web",
        project_id="default",
        project_dir="",
        work_mode="work",
    )

    claim = await pool.claim(key)
    marker = pool._marker_path(claim.session_id)
    assert claim.prewarm_hit is True
    assert marker.is_file()

    pool.clear_marker(claim.session_id)
    assert not marker.exists()
    await pool.close()


def test_new_boot_cleans_only_metadata_less_marked_workspace(
    isolated_pool, tmp_path: Path
) -> None:
    old_pool = isolated_pool(_FakeRootAgent())
    key = old_pool.make_key(
        channel_id="web",
        project_id="default",
        project_dir="",
        work_mode="work",
    )
    stale_id = "web_stale"
    persisted_id = "web_persisted"
    for session_id in (stale_id, persisted_id):
        (tmp_path / session_id).mkdir()
        old_pool._write_marker(session_id, key)
    (tmp_path / persisted_id / "metadata.json").write_text("{}", encoding="utf-8")

    isolated_pool(_FakeRootAgent())

    assert not (tmp_path / stale_id).exists()
    assert (tmp_path / persisted_id / "metadata.json").is_file()
