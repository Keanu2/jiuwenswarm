# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exec containment, disconnect propagation and request isolation."""

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenbox.supervisor import sandbox_daemon, win_exec, win_job


@pytest.mark.skipif(sys.platform != "win32" or os.environ.get("JIUWENBOX_NATIVE_RUNNER_TEST") != "1",
                    reason="opt-in: requires an existing jbx-sandbox account and accessible Python")
@pytest.mark.parametrize("close_job", [False, True])
def test_native_sandbox_account_runner_exec_and_cleanup(monkeypatch, tmp_path, close_job):
    from jiuwenbox.server.runtime.process import ProcessRuntime
    from jiuwenbox.supervisor import win_setup
    from jiuwenbox.supervisor.daemon_ipc import (
        REQUEST_TYPE_BG_KILL, REQUEST_TYPE_BG_STATUS, REQUEST_TYPE_EXEC_BACKGROUND,
        REQUEST_TYPE_EXEC, REQUEST_TYPE_SUBSCRIBE_LOG, LOG_FIELD_MESSAGE,
        encode_request, recv_frame, send_frame,
    )

    password = win_setup.get_sandbox_user_password()
    if not password:
        pytest.skip("existing sandbox credentials unavailable")
    monkeypatch.setenv("JIUWENBOX_SOFT_DELETE", "0")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = uuid.uuid4().hex
    env = {key: os.environ[key] for key in (
        "SystemRoot", "windir", "COMSPEC", "PATH", "PYTHONPATH", "JIUWENBOX_RUNNER_PYTHON",
    ) if key in os.environ}
    job = win_job.create_job()
    handle = None
    try:
        pid, handle = win_exec.two_hop_spawn_and_authorize(
            f"lifecycle-test-{uuid.uuid4().hex}", sandbox_user="jbx-sandbox", sandbox_password=password,
            workspace=str(tmp_path), proxy_port_start=60080, proxy_port_end=60089,
            control_port=port, env=env, control_token=token, job_handle=job,
        )
        deadline = time.monotonic() + 15
        while not win_exec.probe_runner_listen(port):
            assert win_exec.is_runner_alive(handle), "native sandbox runner exited before readiness"
            assert time.monotonic() < deadline, "native runner readiness timed out"
            time.sleep(0.05)
        response, _ = ProcessRuntime._win_roundtrip_blocking(
            port, REQUEST_TYPE_EXEC,
            {"command": [os.environ["COMSPEC"], "/d", "/c", "echo lifecycle-ok"],
             "workdir": os.path.join(os.environ["SystemRoot"], "System32")},
            None, False, control_token=token,
        )
        assert response["exit_code"] == 0
        assert "lifecycle-ok" in response["stdout"]
        if not close_job:
            # Abort one authenticated exec and observe its real child PID.
            # The shared runner must remain usable by another request.
            with socket.create_connection(("127.0.0.1", port), timeout=5) as logs:
                send_frame(logs, encode_request(request_type=REQUEST_TYPE_SUBSCRIBE_LOG,
                                               payload={"token": token}))
                with socket.create_connection(("127.0.0.1", port), timeout=5) as execution:
                    send_frame(execution, encode_request(request_type=REQUEST_TYPE_EXEC, payload={
                        "token": token,
                        "command": [os.environ["COMSPEC"], "/d", "/c", "ping -n 30 127.0.0.1 >nul"],
                        "workdir": os.path.join(os.environ["SystemRoot"], "System32"),
                    }))
                    while True:
                        message = json.loads(recv_frame(logs, 1024 * 1024))[LOG_FIELD_MESSAGE]
                        if "exec child 已启动" in message:
                            child_pid = int(re.search(r"pid=(\d+)", message).group(1))
                            break
                    child = win_job.get_kernel32().OpenProcess(0x100000, False, child_pid)
                    assert child
                    try:
                        execution.shutdown(socket.SHUT_RDWR)
                        assert win_exec.get_kernel32().WaitForSingleObject(child, 5000) == 0
                    finally:
                        win_exec.get_kernel32().CloseHandle(child)
            response, _ = ProcessRuntime._win_roundtrip_blocking(
                port, REQUEST_TYPE_EXEC,
                {"command": [os.environ["COMSPEC"], "/d", "/c", "echo sibling-ok"],
                 "workdir": os.path.join(os.environ["SystemRoot"], "System32")},
                None, False, control_token=token,
            )
            assert response["exit_code"] == 0
            assert "sibling-ok" in response["stdout"]
            # Background requests must release their IPC connection while the
            # real process remains queryable, and retain a terminal job record.
            for job_id, timeout in (("native-timeout", 1), ("native-kill", 30)):
                started_at = time.monotonic()
                response, _ = ProcessRuntime._win_roundtrip_blocking(
                    port, REQUEST_TYPE_EXEC_BACKGROUND,
                    {"job_id": job_id,
                     "command": [os.environ["COMSPEC"], "/d", "/c", "ping -n 30 127.0.0.1 >nul"],
                     "workdir": os.path.join(os.environ["SystemRoot"], "System32"), "timeout": timeout},
                    None, False, control_token=token,
                )
                assert time.monotonic() - started_at < 2
                assert response["started"] and response["running"] and response["pid"]
                if job_id == "native-kill":
                    response, _ = ProcessRuntime._win_roundtrip_blocking(
                        port, REQUEST_TYPE_BG_KILL, {"job_id": job_id, "signum": 9},
                        None, False, control_token=token,
                    )
                    assert response["killed"] and response["reason"] == "ok"
                deadline = time.monotonic() + 5
                while True:
                    response, _ = ProcessRuntime._win_roundtrip_blocking(
                        port, REQUEST_TYPE_BG_STATUS, {"job_id": job_id},
                        None, False, control_token=token,
                    )
                    assert response["ok"]
                    if not response["running"]:
                        break
                    assert time.monotonic() < deadline, "background process was not terminated"
                    time.sleep(0.05)
                assert response["exit_code"] == (-15 if job_id == "native-timeout" else -9)
        if close_job:
            win_job.close_job(job)
            job = None
            kernel = win_exec.get_kernel32()
            assert kernel.WaitForSingleObject(win_exec.wintypes.HANDLE(handle), 5000) == 0
        win_exec.stop_runner(pid, handle)
        handle = None
        assert not win_exec.probe_runner_listen(port)
    finally:
        if job is not None:
            win_job.close_job(job)
        if handle is not None:
            win_exec.stop_runner(pid, handle)


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows Jobs")
@pytest.mark.parametrize("kill_owner", [False, True])
@pytest.mark.parametrize("release_exec", [False, True])
def test_native_owner_exit_reaps_runner_and_nested_exec(kill_owner, release_exec):
    """Exercise real kernel Jobs without changing sandbox-account ACLs."""
    import ctypes
    from ctypes import wintypes

    runner_code = """
import sys, subprocess, time
from jiuwenbox.supervisor import win_job
sys.stdin.readline()
job = win_job.create_exec_job()
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
win_job.assign_process(job, int(child._handle))
RELEASE_EXEC
print(child.pid, flush=True)
time.sleep(120)
"""
    runner_code = runner_code.replace("RELEASE_EXEC", "win_job.release_exec_job(job)" if release_exec else "")
    owner_code = f"""
import sys, subprocess, json
from jiuwenbox.supervisor import win_job
job = win_job.create_job()
runner = subprocess.Popen([sys.executable, '-u', '-c', {runner_code!r}], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True)
win_job.assign_process(job, int(runner._handle))
runner.stdin.write('go\\n'); runner.stdin.flush()
child_pid = int(runner.stdout.readline())
print(json.dumps([runner.pid, child_pid]), flush=True)
sys.stdin.readline()
win_job.close_job(job)
"""
    owner = subprocess.Popen([sys.executable, "-u", "-c", owner_code], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handles = []
    try:
        pids = json.loads(owner.stdout.readline())
        for pid in pids:
            handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
            assert handle, f"cannot observe test process {pid}"
            handles.append(handle)
            assert kernel.WaitForSingleObject(handle, 0) == 0x102, "released exec killed a descendant"
        if kill_owner:
            owner.kill()
        else:
            owner.stdin.write("stop\n")
            owner.stdin.flush()
        owner.wait(timeout=10)
        for handle in handles:
            assert kernel.WaitForSingleObject(handle, 5000) == 0
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=10)
        for handle in handles:
            kernel.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows Jobs")
def test_native_completed_exec_jobs_do_not_leak_handles():
    import ctypes
    from ctypes import wintypes

    kernel = win_job.get_kernel32()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessHandleCount.restype = wintypes.BOOL
    process = kernel.GetCurrentProcess()

    def handle_count():
        count = wintypes.DWORD()
        assert kernel.GetProcessHandleCount(process, ctypes.byref(count))
        return count.value

    win_job.release_exec_job(win_job.create_exec_job())
    before = handle_count()
    for _ in range(100):
        win_job.release_exec_job(win_job.create_exec_job())
    assert handle_count() == before


def test_exec_job_release_failure_still_closes_kill_on_close_job(monkeypatch):
    kernel = MagicMock()
    kernel.SetInformationJobObject.return_value = False
    monkeypatch.setattr(win_job, "_require_windows", lambda: None)
    monkeypatch.setattr(win_job, "get_kernel32", lambda: kernel)
    # ctypes.WinError is unavailable on Linux, where this fault check also runs.
    monkeypatch.setattr(win_job.ctypes, "WinError", lambda *_: OSError("set limits failed"), raising=False)
    monkeypatch.setattr(win_job.ctypes, "get_last_error", lambda: 5, raising=False)
    with pytest.raises(OSError, match="set limits failed"):
        win_job.release_exec_job(20)
    kernel.CloseHandle.assert_called_once()
    assert kernel.CloseHandle.call_args.args[0].value == 20


@pytest.mark.parametrize("failure", [None, "assign", "token", "resume"])
def test_runner_job_binding_precedes_resume_and_failures_are_reaped(monkeypatch, failure):
    from jiuwenbox.supervisor import win_softdelete

    events = []
    kernel = MagicMock()
    monkeypatch.setattr(win_exec, "_require_windows", lambda: None)
    monkeypatch.setattr(win_exec, "get_kernel32", lambda: kernel)
    monkeypatch.setattr(win_exec, "two_hop_spawn", lambda *a, **kw: (42, 30, 31, 32))
    monkeypatch.setattr(win_softdelete, "enabled_from_environ", lambda: False)

    def action(name):
        def call(*args):
            events.append(name)
            if name == failure:
                raise RuntimeError(name)
        return call

    assign = MagicMock(side_effect=action("assign"))
    monkeypatch.setattr(win_job, "assign_process", assign)
    monkeypatch.setattr(win_exec, "_write_token_to_pipe", action("token"))
    monkeypatch.setattr(win_job, "resume_process", action("resume"))
    stop = MagicMock()
    monkeypatch.setattr(win_exec, "stop_runner", stop)
    kwargs = dict(sandbox_user="test", sandbox_password="test", workspace="workspace",
                  proxy_port_start=1, proxy_port_end=2, control_port=3,
                  control_token="token", job_handle=20)
    if failure:
        with pytest.raises(RuntimeError, match=failure if failure != "token" else "control_token"):
            win_exec.two_hop_spawn_and_authorize("sb", **kwargs)
        stop.assert_called_once_with(42, 30)
        assert events == ["assign", "token", "resume"][:["assign", "token", "resume"].index(failure) + 1]
    else:
        assert win_exec.two_hop_spawn_and_authorize("sb", **kwargs) == (42, 30)
        assert events == ["assign", "token", "resume"]
        stop.assert_not_called()
    assign.assert_called_once_with(20, 30)
    closed = [call.args[0].value for call in kernel.CloseHandle.call_args_list]
    assert sorted(closed) == [31, 32]


@pytest.mark.parametrize("waits,terminate_ok,success", [
    ([0], True, True),
    ([0x102, 0], True, True),
    ([0x102, 0x102], True, False),
    ([0xFFFFFFFF], True, False),
    ([0x102, 0xFFFFFFFF], True, False),
    ([0x102, 0x102], False, False),
    ([0x102, 0], False, True),
])
def test_stop_runner_retains_handle_until_confirmed(monkeypatch, waits, terminate_ok, success):
    kernel = MagicMock()
    kernel.WaitForSingleObject.side_effect = waits
    kernel.TerminateProcess.return_value = terminate_ok
    monkeypatch.setattr(win_exec, "_require_windows", lambda: None)
    monkeypatch.setattr(win_exec, "get_kernel32", lambda: kernel)
    monkeypatch.setattr(win_exec.ctypes, "get_last_error", lambda: 5, raising=False)
    if success:
        win_exec.stop_runner(42, 30, timeout_ms=123)
        kernel.CloseHandle.assert_called_once()
    else:
        with pytest.raises(RuntimeError, match="pid=42"):
            win_exec.stop_runner(42, 30, timeout_ms=123)
        kernel.CloseHandle.assert_not_called()
    if len(waits) > 1:
        assert kernel.WaitForSingleObject.call_args.args[1] == 123


@pytest.mark.asyncio
async def test_failed_stop_preserves_runner_and_acl_for_retry(monkeypatch):
    from jiuwenbox.server.runtime.process import ProcessRuntime
    from jiuwenbox.supervisor import win_acl, win_recycle_broker

    runtime = ProcessRuntime.__new__(ProcessRuntime)
    runner = {"pid": 42, "process_handle": 30, "control_port": 123}
    runtime._win_runners = {"sb": runner}
    runtime._win_stop_locks = {}
    runtime._win_stop_users = {}
    runtime._win_job_handles = {}
    runtime._win_log_stops = {}
    runtime._win_log_threads = {}
    runtime._win_acl_paths = {"sb": ["workspace"]}
    runtime._win_sandbox_sids = {"sb": "sid"}
    runtime._win_pipe_locks = {}
    runtime._win_acl_lock = asyncio.Lock()
    runtime._send_runner_shutdown = AsyncMock()
    stop = MagicMock(side_effect=[RuntimeError("exit timeout"), None])
    monkeypatch.setattr(win_exec, "stop_runner", stop)
    revoke = MagicMock()
    monkeypatch.setattr(win_acl, "revoke_sandbox_acl", revoke)
    monkeypatch.setattr(win_recycle_broker, "detach_runner_recycle", MagicMock())
    with pytest.raises(RuntimeError, match="exit timeout"):
        await runtime._stop_windows("sb")
    assert runtime._win_runners["sb"] is runner
    assert runtime._win_acl_paths["sb"] == ["workspace"]
    assert not runtime._win_stop_locks
    assert not runtime._win_stop_users
    revoke.assert_not_called()
    await runtime._stop_windows("sb")
    assert not runtime._win_runners
    assert not runtime._win_acl_paths
    revoke.assert_called_once()
    assert not runtime._win_stop_locks
    assert not runtime._win_stop_users


@pytest.mark.asyncio
async def test_cancelled_stop_finishes_cleanup_before_propagating(monkeypatch):
    import threading

    from jiuwenbox.server.runtime.process import ProcessRuntime
    from jiuwenbox.supervisor import win_acl, win_recycle_broker

    runtime = ProcessRuntime.__new__(ProcessRuntime)
    runtime._win_runners = {"sb": {"pid": 42, "process_handle": 30, "control_port": 123, "recycle_token": "tok"}}
    runtime._win_stop_locks = {}
    runtime._win_stop_users = {}
    runtime._win_job_handles = {"sb": 999}
    runtime._win_log_stops = {}
    runtime._win_log_threads = {}
    runtime._win_acl_paths = {"sb": ["workspace"]}
    runtime._win_sandbox_sids = {"sb": "sid"}
    runtime._win_pipe_locks = {}
    runtime._win_acl_lock = asyncio.Lock()
    runtime._send_runner_shutdown = AsyncMock()
    entered, release = threading.Event(), threading.Event()

    def stop(pid, handle):
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(win_exec, "stop_runner", stop)
    revoke = MagicMock()
    monkeypatch.setattr(win_acl, "revoke_sandbox_acl", revoke)
    detach = MagicMock()
    monkeypatch.setattr(win_recycle_broker, "detach_runner_recycle", detach)
    teardown = MagicMock()
    monkeypatch.setattr(win_job, "teardown", teardown)
    task = asyncio.create_task(runtime._stop_windows("sb"))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    detach.assert_called_once_with("tok")
    teardown.assert_called_once_with(999)
    revoke.assert_called_once()
    assert not runtime._win_runners
    assert not runtime._win_acl_paths
    assert not runtime._win_stop_locks
    assert not runtime._win_stop_users


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["start_sandbox", "stop_sandbox", "restart_sandbox"])
async def test_lifecycle_entrypoints_reject_deleting_sandbox(action):
    from jiuwenbox.server.sandbox_manager import SandboxManager, SandboxPhase, SandboxStateError

    manager = SandboxManager.__new__(SandboxManager)
    manager._lock = asyncio.Lock()
    manager._sandboxes = {"sb": SimpleNamespace(phase=SandboxPhase.DELETING)}
    manager.runtime = SimpleNamespace(create=AsyncMock(), stop=AsyncMock())
    with pytest.raises(SandboxStateError, match="deleting"):
        await getattr(manager, action)("sb")
    manager.runtime.create.assert_not_awaited()
    manager.runtime.stop.assert_not_awaited()


@pytest.fixture
def windows_cleanup_runtime(monkeypatch):
    from jiuwenbox.server.runtime.process import ProcessRuntime
    from jiuwenbox.supervisor import win_acl, win_recycle_broker

    runtime = ProcessRuntime()
    monkeypatch.setattr(sys, "platform", "win32")
    runtime._win_runners["sb-1"] = {"pid": 42, "process_handle": 30, "control_port": 123}
    runtime._win_job_handles["sb-1"] = 999
    runtime._win_acl_paths["sb-1"] = ["workspace"]
    runtime._win_sandbox_sids["sb-1"] = "sid"
    runtime._send_runner_shutdown = AsyncMock()
    monkeypatch.setattr(win_exec, "stop_runner", MagicMock())
    monkeypatch.setattr(win_job, "teardown", MagicMock())
    monkeypatch.setattr(win_acl, "revoke_sandbox_acl", MagicMock())
    monkeypatch.setattr(win_recycle_broker, "detach_runner_recycle", MagicMock())
    return runtime


@pytest.mark.asyncio
async def test_delete_and_cleanup_after_create_stop_runner_once(monkeypatch, windows_cleanup_runtime):
    import threading

    from jiuwenbox.models.sandbox import SandboxSpec
    from jiuwenbox.server.sandbox_manager import SandboxManager

    runtime = windows_cleanup_runtime
    create_entered, finish_create = asyncio.Event(), asyncio.Event()
    stop_entered, finish_stop = threading.Event(), threading.Event()

    async def create(**kwargs):
        create_entered.set()
        await finish_create.wait()
        return 42

    def stop(pid, handle):
        stop_entered.set()
        assert finish_stop.wait(5)

    runtime.create = AsyncMock(side_effect=create)
    stop_mock = MagicMock(side_effect=stop)
    monkeypatch.setattr(win_exec, "stop_runner", stop_mock)
    manager = SandboxManager.__new__(SandboxManager)
    manager._lock = asyncio.Lock()
    manager._delete_tasks = {}
    manager._sandboxes, manager._policies = {}, {}
    manager.runtime = runtime
    manager.policy_engine, manager.audit = MagicMock(), MagicMock()
    manager._resolve_effective_policy = MagicMock(return_value=SimpleNamespace(name="test"))
    manager._save_state, manager._delete_state = MagicMock(), MagicMock()
    creation = asyncio.create_task(manager.create_sandbox(SandboxSpec(sandbox_id="sb-1")))
    await create_entered.wait()
    deletion = asyncio.create_task(manager.delete_sandbox("sb-1"))
    try:
        assert await asyncio.to_thread(stop_entered.wait, 5)
        finish_create.set()
        await asyncio.sleep(0)
        assert not creation.done() and not deletion.done()
        stop_mock.assert_called_once_with(42, 30)
    finally:
        finish_create.set()
        finish_stop.set()
    await asyncio.wait_for(asyncio.gather(creation, deletion), 5)
    stop_mock.assert_called_once_with(42, 30)
    win_job.teardown.assert_called_once_with(999)
    assert not manager._sandboxes
    assert not runtime._win_runners
    assert not runtime._win_acl_paths
    assert not runtime._win_stop_locks
    assert not runtime._win_stop_users


@pytest.mark.asyncio
async def test_cancelled_cleanup_waiter_does_not_split_lock(windows_cleanup_runtime):
    runtime = windows_cleanup_runtime
    entered, release = asyncio.Event(), asyncio.Event()

    async def shutdown(*args):
        entered.set()
        await release.wait()

    runtime._send_runner_shutdown = AsyncMock(side_effect=shutdown)
    first = asyncio.create_task(runtime._stop_windows("sb-1"))
    await entered.wait()
    second = asyncio.create_task(runtime._stop_windows("sb-1"))
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    third = asyncio.create_task(runtime._stop_windows("sb-1"))
    try:
        await asyncio.sleep(0)
        assert not third.done()
        runtime._send_runner_shutdown.assert_awaited_once()
        win_exec.stop_runner.assert_not_called()
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(first, third), 5)
    win_exec.stop_runner.assert_called_once_with(42, 30)
    assert not runtime._win_stop_locks
    assert not runtime._win_stop_users


@pytest.mark.asyncio
async def test_windows_cleanup_does_not_block_other_sandboxes(windows_cleanup_runtime):
    runtime = windows_cleanup_runtime
    runtime._win_runners["sb-2"] = {"pid": 43, "process_handle": 31, "control_port": 124}
    entered, release = asyncio.Event(), asyncio.Event()

    async def shutdown(sandbox_id, runner):
        if sandbox_id == "sb-1":
            entered.set()
            await release.wait()

    runtime._send_runner_shutdown = AsyncMock(side_effect=shutdown)
    first = asyncio.create_task(runtime._stop_windows("sb-1"))
    await entered.wait()
    try:
        await asyncio.wait_for(runtime._stop_windows("sb-2"), 5)
        win_exec.stop_runner.assert_called_once_with(43, 31)
        assert not first.done()
    finally:
        release.set()
    await asyncio.wait_for(first, 5)
    assert win_exec.stop_runner.call_count == 2
    assert not runtime._win_stop_locks
    assert not runtime._win_stop_users


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_duplicate_delete_waits_for_original_cleanup(failure):
    from jiuwenbox.server.sandbox_manager import SandboxManager, SandboxNotFoundError, SandboxPhase

    entered, release = asyncio.Event(), asyncio.Event()

    async def cleanup(sandbox_id):
        entered.set()
        await release.wait()
        if failure:
            raise RuntimeError("ACL cleanup failed")

    manager = SandboxManager.__new__(SandboxManager)
    manager._lock = asyncio.Lock()
    manager._delete_tasks = {}
    manager._sandboxes = {"sb": SimpleNamespace(phase=SandboxPhase.READY)}
    manager._policies = {"sb": object()}
    manager.runtime = SimpleNamespace(cleanup=AsyncMock(side_effect=cleanup))
    manager.policy_engine = MagicMock()
    manager.audit = MagicMock()
    manager._save_state = MagicMock()
    manager._delete_state = MagicMock()
    first = asyncio.create_task(manager.delete_sandbox("sb"))
    await entered.wait()
    second = asyncio.create_task(manager.delete_sandbox("sb"))
    await asyncio.sleep(0)
    assert not second.done()
    assert manager._sandboxes["sb"].phase == SandboxPhase.DELETING
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    manager.runtime.cleanup.assert_awaited_once()
    assert not manager._delete_tasks
    if failure:
        assert all(isinstance(result, RuntimeError) for result in results)
        assert "sb" in manager._sandboxes
        manager.runtime.cleanup.side_effect = None
        await manager.delete_sandbox("sb")
    else:
        assert results == [None, None]
    assert "sb" not in manager._sandboxes
    with pytest.raises(SandboxNotFoundError):
        await manager.delete_sandbox("sb")


@pytest.mark.asyncio
async def test_disconnected_delete_caller_does_not_cancel_cleanup():
    from jiuwenbox.server.sandbox_manager import SandboxManager, SandboxPhase

    entered, release = asyncio.Event(), asyncio.Event()

    async def cleanup(sandbox_id):
        entered.set()
        await release.wait()

    manager = SandboxManager.__new__(SandboxManager)
    manager._lock = asyncio.Lock()
    manager._delete_tasks = {}
    manager._sandboxes = {"sb": SimpleNamespace(phase=SandboxPhase.READY)}
    manager._policies = {}
    manager.runtime = SimpleNamespace(cleanup=AsyncMock(side_effect=cleanup))
    manager.policy_engine = manager.audit = MagicMock()
    manager._save_state = manager._delete_state = MagicMock()
    caller = asyncio.create_task(manager.delete_sandbox("sb"))
    await entered.wait()
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    assert not manager._delete_tasks["sb"].cancelled()
    release.set()
    await manager.delete_sandbox("sb")
    manager.runtime.cleanup.assert_awaited_once()


@pytest.mark.parametrize("waits,failed,retries", [
    ([0], False, 0),
    ([0x102, 0], False, 1),
    ([0x102, 0x102], True, 1),
    ([0xFFFFFFFF], True, 0),
    ([0x102, 0xFFFFFFFF], True, 1),
])
def test_confirm_exit_checks_wait_result(monkeypatch, waits, failed, retries):
    kernel = MagicMock()
    kernel.WaitForSingleObject.side_effect = waits
    monkeypatch.setattr(win_exec, "_push_log", MagicMock())
    if failed:
        with pytest.raises(RuntimeError, match="cannot confirm exec process exit"):
            win_exec._confirm_process_exit(kernel, 30)
    else:
        win_exec._confirm_process_exit(kernel, 30)
    assert kernel.TerminateProcess.call_count == retries
    assert kernel.WaitForSingleObject.call_count == len(waits)


@pytest.mark.parametrize("waits,exit_query_ok", [
    ([0, 0x102, 0x102, 0], True),
    ([0, 0xFFFFFFFF, 0], True),
    ([0xFFFFFFFF, 0], True),
    ([0, 0, 0], False),
])
def test_exec_wait_failure_sends_error_not_completion(monkeypatch, waits, exit_query_ok):
    kernel = MagicMock()
    kernel.WaitForSingleObject.side_effect = waits
    kernel.GetExitCodeProcess.return_value = exit_query_ok
    monkeypatch.setattr(win_exec, "get_kernel32", lambda: kernel)
    monkeypatch.setattr(win_exec, "_clear_inherit", lambda *_: None)
    monkeypatch.setattr(win_exec, "_exec_peer_disconnected", lambda *_: False)
    monkeypatch.setattr(win_exec, "_bash_unavailable", lambda: False)
    monkeypatch.setattr(win_exec, "_get_runner_primary_token", lambda: 10)
    monkeypatch.setattr(win_exec, "_push_log", MagicMock())
    monkeypatch.setattr(win_exec, "_create_process_as_user", lambda *a, **k: (30, 31, 32))
    for name in ("assign_process", "resume_process", "close_job"):
        monkeypatch.setattr(win_job, name, MagicMock())
    monkeypatch.setattr(win_job, "create_exec_job", lambda: 20)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=lambda *a: 200))
    monkeypatch.setattr(win_exec._threading, "Thread", MagicMock())
    close_fd = MagicMock()
    monkeypatch.setattr(win_exec.os, "close", close_fd)
    counter = iter(range(100, 104))

    def pipe(read, write, *_):
        read._obj.value, write._obj.value = next(counter), next(counter)
        return True

    kernel.CreatePipe.side_effect = pipe
    response, error = MagicMock(), MagicMock()
    monkeypatch.setattr(win_exec, "_send_response", response)
    monkeypatch.setattr(win_exec, "_send_error_response", error)
    win_exec._handle_exec_request(None, {"command": ["cmd", "/c", "echo hello"]}, None, None, b"")
    error.assert_called_once()
    response.assert_not_called()
    close_fd.assert_called_once_with(200)


@pytest.mark.parametrize("failure", ["job", "create", "assign", "resume"])
def test_exec_start_failure_never_runs_uncontained(monkeypatch, failure):
    kernel = MagicMock()
    monkeypatch.setattr(win_exec, "get_kernel32", lambda: kernel)
    monkeypatch.setattr(win_exec, "_clear_inherit", lambda *_: None)
    monkeypatch.setattr(win_exec, "_exec_peer_disconnected", lambda *_: False)
    monkeypatch.setattr(win_exec, "_bash_unavailable", lambda: False)
    monkeypatch.setattr(win_exec, "_get_runner_primary_token", lambda: 10)
    monkeypatch.setattr(win_exec, "_push_log", lambda *a, **k: None)
    error = MagicMock()
    monkeypatch.setattr(win_exec, "_send_error_response", error)
    counter = iter(range(100, 120))

    def pipe(read, write, *_):
        read._obj.value, write._obj.value = next(counter), next(counter)
        return True

    kernel.CreatePipe.side_effect = pipe
    events = []

    def action(name, value=None):
        def call(*a, **k):
            events.append(name)
            if failure == name:
                raise OSError(name)
            return value
        return call

    monkeypatch.setattr(win_job, "create_exec_job", action("job", 20))
    monkeypatch.setattr(win_exec, "_create_process_as_user", action("create", (30, 31, 32)))
    monkeypatch.setattr(win_job, "assign_process", action("assign"))
    monkeypatch.setattr(win_job, "resume_process", action("resume"))
    close_job = MagicMock()
    monkeypatch.setattr(win_job, "close_job", close_job)
    win_exec._handle_exec_request(None, {"command": ["cmd", "/c", "echo hello"]}, None, None, b"")
    assert events == ["job", "create", "assign", "resume"][:["job", "create", "assign", "resume"].index(failure) + 1]
    error.assert_called_once()
    assert kernel.TerminateProcess.call_count == int(failure in {"assign", "resume"})
    assert close_job.call_count == int(failure != "job")
    closed = [call.args[0].value for call in kernel.CloseHandle.call_args_list]
    # HANDLE objects are cleared only on the success path in this test.
    assert set(range(100, 104)).issubset(closed)


@pytest.mark.parametrize("detector", [
    pytest.param(win_exec._exec_peer_disconnected, id="windows"),
    pytest.param(sandbox_daemon._exec_disconnected, id="linux"),
])
def test_peer_disconnect_is_request_local(detector):
    left, right = socket.socketpair()
    other_left, other_right = socket.socketpair()
    try:
        assert not detector(left)
        right.close()
        assert detector(left)
        assert not detector(other_left)
    finally:
        for sock in (left, right, other_left, other_right):
            sock.close()


@pytest.mark.parametrize("backend", ["windows", "linux"])
@pytest.mark.asyncio
async def test_cancel_runtime_closes_only_current_exec(monkeypatch, backend):
    from jiuwenbox.server.runtime import process
    from jiuwenbox.supervisor.daemon_ipc import REQUEST_TYPE_EXEC

    pairs = [socket.socketpair(), socket.socketpair()]
    clients = iter(client for client, _ in pairs)
    started = [asyncio.Event(), asyncio.Event()]
    finished = [asyncio.Event(), asyncio.Event()]
    loop = asyncio.get_running_loop()
    # Replace only the runtime's socket factory, not asyncio's global socket module.
    monkeypatch.setattr(process, "socket", SimpleNamespace(
        socket=lambda *a: next(clients), AF_INET=socket.AF_INET,
        AF_UNIX=getattr(socket, "AF_UNIX", 1), SOCK_STREAM=socket.SOCK_STREAM,
        SHUT_RDWR=socket.SHUT_RDWR, timeout=socket.timeout,
    ))
    runtime = process.ProcessRuntime.__new__(process.ProcessRuntime)

    def receive(exec_socket):
        index = next(i for i, pair in enumerate(pairs) if pair[0] is exec_socket)
        loop.call_soon_threadsafe(started[index].set)
        try:
            return exec_socket.recv(1).decode()
        except OSError:
            return ""
        finally:
            loop.call_soon_threadsafe(finished[index].set)

    if backend == "windows":
        monkeypatch.setattr(runtime, "_win_roundtrip_blocking",
                            lambda *a, exec_socket, **k: (receive(exec_socket), b""))

        def execute():
            return runtime._win_runner_roundtrip(
                "sb", {"control_port": 1}, REQUEST_TYPE_EXEC, {}, None,
            )
    else:
        monkeypatch.setattr(runtime, "_control_socket_host_path", lambda _: "control.sock")
        monkeypatch.setattr(runtime, "_daemon_ipc_available", lambda _: True)
        monkeypatch.setattr(runtime, "_exec_via_daemon_blocking",
                            lambda call: receive(call.exec_socket))

        def execute():
            return runtime._exec_via_daemon("sb", SimpleNamespace(
                command=["command"], env=None, workdir=None, stdin_data=None, timeout=None,
            ))

    tasks = []
    try:
        for event in started:
            tasks.append(asyncio.create_task(execute()))
            await asyncio.wait_for(event.wait(), 2)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        await asyncio.wait_for(finished[0].wait(), 2)
        assert pairs[0][0].fileno() == -1
        pairs[0][1].settimeout(2)
        assert pairs[0][1].recv(1) == b""
        assert not tasks[1].done()
        assert pairs[1][0].fileno() != -1
        pairs[1][1].sendall(b"x")
        assert await asyncio.wait_for(tasks[1], 2) == "x"
        assert pairs[1][0].fileno() == -1
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for client, peer in pairs:
            peer.close()
            client.close()


@pytest.mark.asyncio
async def test_http_disconnect_cancels_only_current_exec(monkeypatch):
    from jiuwenbox.server.routes import sandbox

    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def execution(*, request, **kwargs):
        try:
            await release.wait()
            return {"stdout": request.command[0], "exit_code": 0}
        finally:
            if request.command[0] == "cancel":
                cancelled.set()

    monkeypatch.setattr(sandbox, "_mgr", lambda: SimpleNamespace(exec_in_sandbox=execution))
    other = asyncio.create_task(sandbox.exec_in_sandbox(
        "sb", sandbox.ExecRequest(command=["keep"]),
        SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
    ))
    try:
        response = await asyncio.wait_for(sandbox.exec_in_sandbox(
            "sb", sandbox.ExecRequest(command=["cancel"]),
            SimpleNamespace(is_disconnected=AsyncMock(return_value=True)),
        ), 2)
        assert response.status_code == 499
        assert cancelled.is_set()
        assert not other.done()
        release.set()
        assert await asyncio.wait_for(other, 2) == {"stdout": "keep", "exit_code": 0}
    finally:
        other.cancel()
        await asyncio.gather(other, return_exceptions=True)


@pytest.mark.parametrize("ending", ["normal", "timeout", "disconnect", "leader_exited"])
def test_daemon_only_reaps_exec_group_on_abort(monkeypatch, ending):
    daemon = sandbox_daemon
    proc = MagicMock(pid=123, returncode=0)
    timeout = daemon.subprocess.TimeoutExpired("command", 0.2)
    proc.communicate.side_effect = (
        [timeout, (b"output", b"")] if ending in {"timeout", "leader_exited"}
        else [(b"output", b"")]
    )
    proc.poll.return_value = 0 if ending == "leader_exited" else None
    spawn = MagicMock(return_value=proc)
    monkeypatch.setattr(daemon.subprocess, "Popen", spawn)
    monkeypatch.setattr(daemon, "_exec_disconnected", MagicMock(
        side_effect=[False, True] if ending == "disconnect" else None,
        return_value=False,
    ))
    killpg = MagicMock()
    monkeypatch.setattr(daemon.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(daemon.signal, "SIGKILL", 9, raising=False)
    response = MagicMock()
    monkeypatch.setattr(daemon, "_send_response", response)
    daemon._handle_exec(None, {
        "command": ["command"], "timeout": 0 if ending == "timeout" else 30,
    }, None)
    assert spawn.call_args.kwargs["start_new_session"] is True
    if ending in {"timeout", "disconnect"}:
        killpg.assert_called_with(123, 9)
    else:
        killpg.assert_not_called()
    proc.wait.assert_called_once()
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        pipe.close.assert_called_once()
    if ending == "timeout":
        assert response.call_args.args[1]["exit_code"] == 124
    elif ending in {"normal", "leader_exited"}:
        assert response.call_args.args[1]["stdout"] == "output"
