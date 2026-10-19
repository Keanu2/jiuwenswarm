"""run-now 的 run_id 形态回归测试.

背景：run-now 曾在 run_id 中嵌入 uuid（job:uuid:ts），模型网关拒绝多段冒号 id
并返回 {"error": {}} 空信封，导致定时任务"立即执行"100% 报
[181001] 模型返回空响应。run_id 必须与到点触发同形（单冒号+秒级时间戳），
且最后一段可被 int(run_id.split(":")[-1]) 解析为 push_ts。
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import Mock

import pytest

from jiuwenswarm.gateway.cron.scheduler import CronSchedulerService
from jiuwenswarm.gateway.cron.store import CronJobStore

RUN_ID_SHAPE = re.compile(r"^[0-9a-f]{32}:\d{10}$")


def _make_scheduler(tmp_path: Path) -> CronSchedulerService:
    return CronSchedulerService(
        store=CronJobStore(path=tmp_path / "cron_jobs.json"),
        agent_client=Mock(),
        message_handler=Mock(),
    )


async def _seed_job(scheduler: CronSchedulerService) -> str:
    job = await scheduler._store.create_job(
        name="t",
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="d",
        targets="web",
    )
    return job.id


@pytest.mark.asyncio
async def test_run_now_run_id_shape(tmp_path: Path) -> None:
    scheduler = _make_scheduler(tmp_path)
    job_id = await _seed_job(scheduler)
    info = await scheduler.trigger_run_now_info(job_id)
    assert RUN_ID_SHAPE.match(info["run_id"]), info["run_id"]
    ts = int(info["run_id"].split(":")[-1])
    assert 1_700_000_000 < ts < 4_000_000_000


@pytest.mark.asyncio
async def test_run_now_same_second_reuses_active_run(tmp_path: Path) -> None:
    scheduler = _make_scheduler(tmp_path)
    job_id = await _seed_job(scheduler)
    a = await scheduler.trigger_run_now_info(job_id)
    b = await scheduler.trigger_run_now_info(job_id)
    assert a["run_id"] == b["run_id"]
    assert b["session_id"] == a["session_id"]
    assert len(scheduler._runs) == 1
