"""query_time_list 日期分组的时区回归测试.

背景：queryTimeList 曾用 UTC 给执行记录按日期分组，设备（UTC+8）本地
00:00~08:00 的执行会被归进前一天（真机实锤：10-21 07:00 北京时间的执行
显示在 10-20 分组里）。分组时区必须与任务 timezone 取齐，缺任务时退化
系统本地时区，最终兜底 UTC。
"""
from __future__ import annotations

from datetime import datetime, timezone

from jiuwenswarm.gateway.cron.controller import CronController
from jiuwenswarm.gateway.cron.models import CronJob

# 北京时间 2026-10-21 07:00 == UTC 2026-10-20 23:00
_TS_MS = int(datetime(2026, 10, 21, 7, 0, tzinfo=timezone.utc).timestamp() * 1000) - 8 * 3600 * 1000


def _entry(job_id: str, ts_ms: int) -> dict:
    return {"jobId": job_id, "ts": ts_ms, "status": "ok", "action": "finished"}


def _job(job_id: str, timezone_name: str) -> CronJob:
    return CronJob(
        id=job_id,
        name="t",
        cron_expr="0 9 * * *",
        timezone=timezone_name,
        description="",
        targets="web",
        enabled=True,
    )


def test_group_uses_job_timezone_shanghai() -> None:
    jobs_map = {"job1": _job("job1", "Asia/Shanghai")}
    by_date = CronController._group_entries_by_date([_entry("job1", _TS_MS)], jobs_map)
    assert list(by_date) == ["2026-10-21"], by_date


def test_group_utc_job_stays_on_utc_date() -> None:
    jobs_map = {"job1": _job("job1", "UTC")}
    by_date = CronController._group_entries_by_date([_entry("job1", _TS_MS)], jobs_map)
    assert list(by_date) == ["2026-10-20"], by_date


def test_deleted_job_falls_back_to_system_local_tz() -> None:
    # 任务已删除（jobs_map 查不到）→ 系统本地时区分组，不炸、不落 unknown
    by_date = CronController._group_entries_by_date([_entry("gone", _TS_MS)], {})
    dates = [d for d in by_date if d != "unknown"]
    expect = datetime.now().astimezone().strftime("%Y-%m-%d")
    assert dates == [expect], by_date


def test_zero_ts_lands_in_unknown_bucket() -> None:
    # ts 缺失/为 0 → 归 unknown（手机上"unknown 分组"的服务端机制）
    by_date = CronController._group_entries_by_date([_entry("job1", 0)], {"job1": _job("job1", "Asia/Shanghai")})
    assert list(by_date) == ["unknown"], by_date
