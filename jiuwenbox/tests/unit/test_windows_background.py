# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Background launch acknowledgements, observable jobs, and cancellation."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from tests.unit.test_exec_workdir_policy import _manager

from jiuwenbox.server.limits import SandboxLimitError
from jiuwenbox.server.runtime.base import RuntimeBackgroundExecRequest
from jiuwenbox.server.runtime import process
from jiuwenbox.server.runtime.process import BackgroundJob, ProcessRuntime
from jiuwenbox.server.sandbox_manager import SandboxBackgroundExecRequest
from jiuwenbox.supervisor import win_exec
from jiuwenbox.supervisor import win_background
from jiuwenbox.supervisor.win_background import WindowsBackgroundJob, WindowsBackgroundJobs


def test_background_launch_returns_while_command_is_running_and_can_be_killed(monkeypatch):
    def execute(_stream, _header, _token, _workspace, _stdin, *, background_job):
        background_job.started(42)
        assert background_job.stop.wait(5)
        background_job.respond({"ok": True, "exit_code": -15})

    monkeypatch.setattr(win_exec, "_handle_exec_request", execute)
    jobs = WindowsBackgroundJobs()
    started = jobs.start({"job_id": "test-job", "command": ["worker"]}, None, "workspace", b"")
    assert started["started"] and started["running"] and started["pid"] == 42
    assert jobs.status("test-job")["stdout"] == ""
    assert jobs.start({"job_id": "test-job", "command": ["other"]}, None, "workspace", b"")["started"] is False
    killed = jobs.kill("test-job")
    assert killed["killed"] is True
    assert jobs.status("test-job")["running"] is False
    assert jobs.status("test-job")["exit_code"] == -15
    assert jobs.kill("test-job")["reason"] == "already_exited"


def test_failed_spawn_never_reports_started_and_allows_retry(monkeypatch):
    def execute(_stream, _header, _token, _workspace, _stdin, *, background_job):
        background_job.respond({"ok": False, "error": "exec_failed", "stderr": "cannot spawn"})

    monkeypatch.setattr(win_exec, "_handle_exec_request", execute)
    jobs = WindowsBackgroundJobs()
    for _ in range(2):
        response = jobs.start({"job_id": "test-job", "command": ["missing"]}, None, "workspace", b"")
        assert response["started"] is False
        assert response["stderr"] == "cannot spawn"
    assert jobs.status("test-job")["error"] == "not_found"


def test_worker_exception_reports_failure_and_releases_job_id(monkeypatch):
    def execute(*_args, **_kwargs):
        raise OSError("pipe creation failed")

    monkeypatch.setattr(win_exec, "_handle_exec_request", execute)
    jobs = WindowsBackgroundJobs()
    response = jobs.start({"job_id": "test-job", "command": ["worker"]}, None, "workspace", b"")
    assert response["started"] is False
    assert response["stderr"] == "pipe creation failed"
    assert jobs.status("test-job")["error"] == "not_found"


def test_thread_start_failure_releases_job_id(monkeypatch):
    def fail_start(_thread):
        raise RuntimeError("cannot start thread")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    jobs = WindowsBackgroundJobs()
    for _ in range(2):
        response = jobs.start({"job_id": "test-job", "command": ["worker"]}, None, "workspace", b"")
        assert response["started"] is False
        assert response["stderr"] == "cannot start thread"
    assert jobs.status("test-job")["error"] == "not_found"


def test_start_timeout_allows_retry_while_old_worker_finishes(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    timed_out_job = WindowsBackgroundJob("test-job")

    def timeout_wait(_timeout):
        assert entered.wait(5)
        return False

    def execute(_stream, header, _token, _workspace, _stdin, *, background_job):
        if header["command"] == ["slow-start"]:
            entered.set()
            assert release.wait(5)
            background_job.started(41)
        else:
            background_job.started(42)
            assert background_job.stop.wait(5)
        background_job.respond({"ok": True, "exit_code": -15})

    monkeypatch.setattr(timed_out_job.ready, "wait", timeout_wait)
    monkeypatch.setattr(win_background, "WindowsBackgroundJob", Mock(side_effect=[
        timed_out_job, WindowsBackgroundJob("test-job"),
    ]))
    monkeypatch.setattr(win_exec, "_handle_exec_request", execute)
    jobs = WindowsBackgroundJobs()
    try:
        response = jobs.start({"job_id": "test-job", "command": ["slow-start"]}, None, "workspace", b"")
        assert response["started"] is False and response["error"] == "start_timeout"
        assert timed_out_job.stop.is_set()
        assert jobs.status("test-job")["error"] == "not_found"

        response = jobs.start({"job_id": "test-job", "command": ["worker"]}, None, "workspace", b"")
        assert response["started"] and response["pid"] == 42
        release.set()
        assert timed_out_job.done.wait(5)
        assert jobs.status("test-job")["pid"] == 42
        assert jobs.status("test-job")["running"] is True
    finally:
        release.set()
        jobs.shutdown(5)
        assert timed_out_job.done.wait(5)


@pytest.fixture
def windows_kill_runtime(monkeypatch):
    monkeypatch.setattr(process, "sys", SimpleNamespace(platform="win32"))
    runtime = ProcessRuntime.__new__(ProcessRuntime)
    runtime._win_runners = {"sb": {"control_port": 1}}
    runtime._background_processes = {"sb": {"test-job": BackgroundJob(
        job_id="test-job", sandbox_id="sb", command=["worker"], pid=42, workdir=None,
    )}}
    runtime._win_roundtrip_blocking = Mock()
    runtime._win_runner_roundtrip = AsyncMock()
    return runtime


def test_kill_returns_not_found_if_runner_lost_job_before_initial_status(windows_kill_runtime):
    runtime = windows_kill_runtime
    runtime._win_roundtrip_blocking.return_value = ({"error": "not_found"}, b"")
    result = asyncio.run(runtime.kill_background_job("sb", "test-job"))
    assert result.killed is False and result.reason == "not_found"
    runtime._win_runner_roundtrip.assert_not_awaited()


def test_kill_skips_refresh_when_runner_reports_not_found(windows_kill_runtime):
    runtime = windows_kill_runtime
    runtime._win_roundtrip_blocking.side_effect = [
        ({"ok": True, "pid": 42, "running": True}, b""),
        ({"error": "not_found"}, b""),
    ]
    runtime._win_runner_roundtrip.return_value = {"killed": False, "reason": "not_found"}
    result = asyncio.run(runtime.kill_background_job("sb", "test-job"))
    assert result.killed is False and result.reason == "not_found"
    assert runtime._win_roundtrip_blocking.call_count == 1


def test_successful_kill_survives_job_disappearing_during_refresh(windows_kill_runtime):
    runtime = windows_kill_runtime
    runtime._win_roundtrip_blocking.side_effect = [
        ({"ok": True, "pid": 42, "running": True}, b""),
        ({"error": "not_found"}, b""),
    ]
    runtime._win_runner_roundtrip.return_value = {"killed": True, "reason": "ok", "exit_code": -9}
    result = asyncio.run(runtime.kill_background_job("sb", "test-job", signum=9))
    assert result.killed is True and result.reason == "ok" and result.exit_code == -9


def test_shutdown_stops_all_background_commands(monkeypatch):
    stopped = threading.Event()

    def execute(_stream, _header, _token, _workspace, _stdin, *, background_job):
        background_job.started(42)
        assert background_job.stop.wait(5)
        stopped.set()
        background_job.respond({"ok": True, "exit_code": -15})

    monkeypatch.setattr(win_exec, "_handle_exec_request", execute)
    jobs = WindowsBackgroundJobs()
    jobs.start({"job_id": "test-job", "command": ["worker"]}, None, "workspace", b"")
    jobs.shutdown(2)
    assert stopped.is_set()
    assert jobs.status("test-job")["running"] is False
    assert jobs.start({"job_id": "new-job", "command": ["worker"]}, None, "workspace", b"")["started"] is False


def test_windows_background_without_runner_does_not_report_started():
    runtime = ProcessRuntime.__new__(ProcessRuntime)
    runtime._win_runners = {}
    result = asyncio.run(runtime._exec_background_windows(
        "missing", RuntimeBackgroundExecRequest(command=["worker"], job_id="test-job")))
    assert result.started is False
    assert result.error_message


def test_windows_background_registers_returned_job_and_forwards_deadline():
    runtime = ProcessRuntime.__new__(ProcessRuntime)
    runtime._win_runners = {"sb": {"control_port": 1}}
    runtime._background_processes = {}
    runtime._win_runner_roundtrip = AsyncMock(return_value={
        "ok": True, "started": True, "pid": 42, "running": True,
    })
    result = asyncio.run(runtime._exec_background_windows(
        "sb", RuntimeBackgroundExecRequest(command=["worker"], job_id="test-job", timeout=2)))
    assert result.started and result.running
    assert runtime._background_processes["sb"]["test-job"].pid == 42
    assert runtime._win_runner_roundtrip.await_args.args[3]["timeout"] == 2


def test_invalid_background_timeout_is_rejected_before_execution():
    manager, _runtime = _manager()
    with pytest.raises(SandboxLimitError):
        asyncio.run(manager.exec_background_in_sandbox(
            "sb", SandboxBackgroundExecRequest(command=["worker"], timeout=0)))
