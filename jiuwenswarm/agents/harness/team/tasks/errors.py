# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exceptions raised by the personal task-continuation client.

Every error carries a ``code`` the UI/bridge can branch on. Codes fall into the
same three groups as the group chat client:

* ``not_configured`` / ``invalid_request`` — raised locally, before any request
  is sent (the section is disabled, or the caller passed something the relay
  would 500 on rather than reject cleanly).
* ``unavailable`` — the relay could not be reached or did not answer with
  parseable JSON.
* anything else — the relay's own ``task_*`` code, passed through verbatim so
  callers can surface the server's precise reason (``task_fence_stale``,
  ``task_terminal``, ``task_quota_exceeded``, ...).

One code gets its own subclass: ``task_fence_stale`` is not an ordinary
rejection but the relay telling this device it has been fenced off by a
takeover. The bridge catches ``TaskFenceStaleError`` specifically to halt and
release, rather than treating it as a transient failure worth retrying.
"""

from __future__ import annotations

from typing import Any


class TaskError(Exception):
    """Base class for task client errors."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "task_error",
        status_code: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.payload = payload


class TaskNotConfiguredError(TaskError):
    """The ``tasks`` section is absent, disabled, or lacks a base URL."""

    def __init__(self, message: str = "task continuation is not configured") -> None:
        super().__init__(message, code="not_configured")


class TaskUnavailableError(TaskError):
    """Transport failure, timeout, or a non-JSON response body."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message, code="unavailable", status_code=status_code)


class TaskRequestInvalidError(TaskError):
    """A locally-detected invalid argument; no request was sent."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="invalid_request")


class TaskFenceStaleError(TaskError):
    """The relay rejected a write because this device's fence is stale.

    Signifies a takeover: another device has acquired the lease, so the local
    execution must halt and release rather than keep writing.
    """

    def __init__(self, message: str, *, status_code: int | None = 409) -> None:
        super().__init__(message, code="task_fence_stale", status_code=status_code)