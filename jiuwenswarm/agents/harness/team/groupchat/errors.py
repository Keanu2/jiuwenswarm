# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exceptions raised by the group chat client.

Every error carries a ``code`` the UI can branch on. Codes fall into three
groups:

* ``not_configured`` / ``invalid_request`` — raised locally, before any
  request is sent (the section is disabled, or the caller passed something the
  relay would 500 on rather than reject cleanly).
* ``unavailable`` — the relay could not be reached or did not answer with
  parseable JSON.
* anything else — the relay's own ``groupchat_*`` code, passed through verbatim
  so callers can surface the server's precise reason (``groupchat_group_quota_exceeded``,
  ``groupchat_name_invalid``, ...).
"""

from __future__ import annotations

from typing import Any


class GroupChatError(Exception):
    """Base class for group chat client errors."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "groupchat_error",
        status_code: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.payload = payload


class GroupChatNotConfiguredError(GroupChatError):
    """The ``groupchat`` section is absent, disabled, or lacks a base URL."""

    def __init__(self, message: str = "group chat is not configured") -> None:
        super().__init__(message, code="not_configured")


class GroupChatUnavailableError(GroupChatError):
    """Transport failure, timeout, or a non-JSON response body."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message, code="unavailable", status_code=status_code)


class GroupChatRequestInvalidError(GroupChatError):
    """A locally-detected invalid argument; no request was sent."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="invalid_request")
