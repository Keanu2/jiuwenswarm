"""run-now 对关闭任务的处理回归测试（force 语义，对齐 openclaw）.

背景（真机 bug 实证）：开关关闭时点"立即执行"，旧代码创建无 started_at 的
CronRunState（ts=0）并排事件，事件被 _handle_event 的 enabled 拦截（静默
return），状态永不推进——ts=0、status=pending 的条目一直留在 _runs 被
queryTimeList 吐给设备，历史页出现 "unknown 日期 + 08:00(epoch 0) + 失败"
的坏条目，重启前每次刷新都复现。

修复（对齐 openclaw isJobDue 的 force 语义，xy_channel 对设备 run-now 硬编码
mode=force——开关只管到点自动触发，手动执行是用户显式覆盖）：
① trigger_run_now_info 创建 state 即补 started_at 并打 manual=True；
② _handle_event 的 enabled 拦截放行 manual run；
③ queryTimeList 防御性跳过无 started_at 的内存条目。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.gateway.cron.controller import CronController
from jiuwenswarm.gateway.cron.models import CronRunState
from jiuwenswarm.gateway.cron.scheduler import CronSchedulerService, _Event
from jiuwenswarm.gateway.cron.store import CronJobStore


def _make_scheduler(tmp_path: Path) -> CronSchedulerService:
    return CronSchedulerService(
        store=CronJobStore(path=tmp_path / "cron_jobs.json"),
        agent_client=Mock(),
        message_handler=Mock(),
    )


async def _create_job(store: CronJobStore, *, enabled: bool) -> object:
    return await store.create_job(
        name="t",
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="d",
        targets="web",
        enabled=enabled,
    )


@pytest.mark.asyncio
async def test_run_now_on_disabled_job_creates_manual_state(tmp_path: Path) -> None:
    """关闭开关的 run-now 放行（force 语义），state 带 manual 标记和 started_at."""
    scheduler = _make_scheduler(tmp_path)
    job = await _create_job(scheduler._store, enabled=False)

    info = await scheduler.trigger_run_now_info(str(job.id))
    state = scheduler._runs[info["run_id"]]
    assert state.manual is True
    assert state.started_at is not None and state.started_at > 0


@pytest.mark.asyncio
async def test_manual_run_bypasses_enabled_gate(tmp_path: Path) -> None:
    """关闭任务的 manual run：事件层放行进入 _on_wake；非 manual 照旧拦截."""
    scheduler = _make_scheduler(tmp_path)
    job = await _create_job(scheduler._store, enabled=False)
    await scheduler.reload()  # 填充 _jobs（生产上任务创建/变更会触发 reload）

    # manual run：事件放行
    info = await scheduler.trigger_run_now_info(str(job.id))
    scheduler._on_wake = AsyncMock()
    await scheduler._handle_event(
        _Event(at_ts=0.0, seq=1, kind="wake", job_id=str(job.id), run_id=info["run_id"])
    )
    assert scheduler._on_wake.await_count == 1

    # 非手动 state（如到点调度的残留）：照旧拦截
    scheduler2 = _make_scheduler(tmp_path / "b")
    job2 = await _create_job(scheduler2._store, enabled=False)
    await scheduler2.reload()
    scheduler2._runs["ghost"] = CronRunState(
        run_id="ghost",
        job_id=str(job2.id),
        wake_at_iso="2026-01-01T00:00:00+08:00",
        push_at_iso="2026-01-01T00:00:00+08:00",
    )  # manual 缺省 False
    scheduler2._on_wake = AsyncMock()
    await scheduler2._handle_event(
        _Event(at_ts=0.0, seq=2, kind="wake", job_id=str(job2.id), run_id="ghost")
    )
    assert scheduler2._on_wake.await_count == 0


def test_query_time_list_skips_states_without_started_at(tmp_path: Path) -> None:
    # 防御层：即使某处创建了无 started_at 的 state，也不得进入 queryTimeList
    scheduler = _make_scheduler(tmp_path)
    controller = CronController(store=scheduler._store, scheduler=scheduler)
    ghost = CronRunState(
        run_id="j1:0",
        job_id="j1",
        wake_at_iso="2026-01-01T00:00:00+08:00",
        push_at_iso="2026-01-01T00:00:00+08:00",
    )  # started_at 缺省 None → ts=0
    scheduler._runs["j1:0"] = ghost

    ans = asyncio.get_event_loop().run_until_complete(controller.query_time_list())
    assert ans == [] or all("unknown" not in group for group in ans), ans
