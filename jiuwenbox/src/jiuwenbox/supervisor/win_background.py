# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Runner-owned Windows background jobs, independent of request connections."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class WindowsBackgroundJob:
    job_id: str
    pid: int | None = None
    result: dict[str, Any] | None = None
    signum: int = 15
    lock: Any = field(default_factory=threading.Lock)
    ready: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)

    def started(self, pid: int) -> None:
        with self.lock:
            self.pid = pid
        self.ready.set()

    def respond(self, result: dict[str, Any]) -> None:
        with self.lock:
            self.result = result

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            result = dict(self.result or {})
            return {
                "ok": self.pid is not None,
                "started": self.pid is not None,
                "job_id": self.job_id,
                "pid": self.pid,
                "running": self.pid is not None and not self.done.is_set(),
                "exit_code": result.get("exit_code") if self.done.is_set() else None,
                "stdout": "",
                "stderr": result.get("stderr", ""),
                "error": result.get("error"),
            }


class WindowsBackgroundJobs:
    """One registry per runner; worker threads own all native process handles."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, WindowsBackgroundJob] = {}
        self._closing = False

    def start(self, header: dict, token: int | None, workspace: str, stdin: bytes) -> dict:
        job_id = header.get("job_id")
        if not isinstance(job_id, str) or not job_id.strip() or not header.get("command"):
            return {"ok": False, "started": False, "error": "bad_request",
                    "stderr": "exec_background requires job_id and command"}
        job_id = job_id.strip()
        with self._lock:
            if self._closing or job_id in self._jobs:
                return {"ok": False, "started": False, "error": "conflict",
                        "stderr": "runner is stopping or background job already exists"}
            job = WindowsBackgroundJob(job_id)
            self._jobs[job_id] = job
            worker = threading.Thread(target=self._run, args=(job, dict(header), token, workspace, stdin),
                                      name=f"win-background-{job_id}", daemon=True)
            try:
                worker.start()
            except RuntimeError as exc:
                self._jobs.pop(job_id, None)
                return {"ok": False, "started": False, "error": "exec_failed", "stderr": str(exc)}
        if not job.ready.wait(30):
            job.stop.set()
            with self._lock:
                if self._jobs.get(job_id) is job:
                    self._jobs.pop(job_id)
            return {"ok": False, "started": False, "error": "start_timeout",
                    "stderr": "background process did not start within 30 seconds"}
        response = job.snapshot()
        if not response["started"]:
            with self._lock:
                self._jobs.pop(job_id, None)
        return response

    @staticmethod
    def _run(job: WindowsBackgroundJob, header: dict, token: int | None,
             workspace: str, stdin: bytes) -> None:
        from jiuwenbox.supervisor.win_exec import _handle_exec_request

        try:
            _handle_exec_request(None, header, token, workspace, stdin, background_job=job)
        except Exception as exc:  # noqa: BLE001
            job.respond({"ok": False, "error": "exec_failed", "exit_code": 1, "stderr": str(exc)})
        finally:
            with job.lock:
                if job.result is None:
                    job.result = {"error": "exec_failed", "exit_code": 1,
                                  "stderr": "background process did not complete successfully"}
            job.done.set()
            job.ready.set()

    def status(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": "not_found", "stderr": "background job not found"}
        return job.snapshot()

    def kill(self, job_id: str, signum: int = 15) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "killed": False, "reason": "not_found"}
        with job.lock:
            if job.done.is_set():
                return {"ok": True, "killed": False, "reason": "already_exited",
                        "exit_code": (job.result or {}).get("exit_code")}
            job.signum = signum
            job.stop.set()
        finished = job.done.wait(15)
        status = job.snapshot()
        return {"ok": finished, "killed": finished, "job_id": job_id,
                "reason": "ok" if finished else "permission_denied",
                "exit_code": status["exit_code"]}

    def shutdown(self, timeout: float) -> None:
        with self._lock:
            self._closing = True
            jobs = list(self._jobs.values())
            for job in jobs:
                job.stop.set()
        deadline = time.monotonic() + timeout
        for job in jobs:
            job.done.wait(max(0, deadline - time.monotonic()))
