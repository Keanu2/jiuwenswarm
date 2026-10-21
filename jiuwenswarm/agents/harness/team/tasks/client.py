# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Async HTTP client for the relay's personal task-continuation API.

Mirrors the group chat client's contract discipline, verified against the
relay's ``a2x_registry/tasks/router.py``:

* Every route requires ``Authorization: Bearer a2x_pat_…``. There is no
  anonymous mode, so an empty ``api_key`` is a configuration error, not a
  degraded mode — and every mutate additionally needs the key to carry a
  ``key_id`` (the relay derives the *device* from the token).
* The relay reports errors in **two** shapes. Business rejections come back as
  ``{"error": {"code", "message"}}``; the disabled-module and auth-dependency
  paths use FastAPI's ``{"detail": ...}``, where ``detail`` is a *string* for
  auth failures but a *dict* when the module is off. ``_extract_error`` handles
  all three.
* Every write that touches the lease (renew/release/checkpoint/append) carries
  a ``fence`` and the relay checks it inside the same atomic ``UPDATE`` that
  advances the task. A stale fence comes back as ``task_fence_stale``, which the
  client raises as :class:`TaskFenceStaleError` rather than a generic failure —
  it means "you were fenced off by a takeover", not "try again".
"""

from __future__ import annotations

import unicodedata
from typing import Any, Literal
from urllib.parse import quote

import httpx

from .config import (
    CLIENT_WRITABLE_KINDS,
    MAX_MESSAGE_BYTES,
    MAX_TITLE_CHARS,
    MESSAGE_KINDS,
    TASK_STATES,
    TasksConfig,
)
from .errors import (
    TaskError,
    TaskFenceStaleError,
    TaskNotConfiguredError,
    TaskRequestInvalidError,
    TaskUnavailableError,
)

HTTPMethod = Literal["GET", "POST", "PUT", "DELETE", "PATCH"]

TASKS_PATH = "api/tasks"


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/"


def _encode(segment: str) -> str:
    """Percent-encode one path segment (principal ids and tokens are opaque)."""
    return quote(str(segment), safe="")


def _task_path(task_id: Any) -> str:
    return f"{TASKS_PATH}/{_coerce_positive_int(task_id, 'task_id')}"


def _coerce_positive_int(value: Any, field: str) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise TaskRequestInvalidError(f"{field} 必须是正整数") from exc
    if parsed < 1:
        raise TaskRequestInvalidError(f"{field} 必须大于 0")
    return parsed


def _coerce_non_negative_int(value: Any, field: str) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise TaskRequestInvalidError(f"{field} 必须是非负整数") from exc
    if parsed < 0:
        raise TaskRequestInvalidError(f"{field} 不能为负")
    return parsed


def _build_headers(api_key: str | None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _extract_error(resp: httpx.Response) -> tuple[str, str]:
    """Return ``(code, message)`` from either error envelope.

    Falls back to a synthesized code when the body is unparseable or empty, so
    callers always get something branchable.
    """
    try:
        payload = resp.json()
    except ValueError:
        return f"http_{resp.status_code}", resp.reason_phrase or "request failed"
    if not isinstance(payload, dict):
        return f"http_{resp.status_code}", resp.reason_phrase or "request failed"

    # Business errors: {"error": {"code", "message"}}
    error = payload.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or f"http_{resp.status_code}")
        message = str(error.get("message") or error.get("code") or "request failed")
        return code, message
    if isinstance(error, str):
        return str(payload.get("code") or f"http_{resp.status_code}"), error

    # Dependency errors: {"detail": {...}} for the disabled module,
    # {"detail": "..."} for auth. The nested form carries the useful code.
    detail = payload.get("detail")
    if isinstance(detail, dict):
        code = str(detail.get("code") or f"http_{resp.status_code}")
        message = str(detail.get("message") or detail.get("code") or "request failed")
        return code, message
    if isinstance(detail, str) and detail:
        return f"http_{resp.status_code}", detail

    return f"http_{resp.status_code}", resp.reason_phrase or "request failed"


def validate_title(title: Any) -> str:
    """Normalize a task title; raise if the relay would reject it."""
    cleaned = str(title or "").strip()
    if len(cleaned) > MAX_TITLE_CHARS:
        raise TaskRequestInvalidError(
            f"任务标题不能超过 {MAX_TITLE_CHARS} 个字符"
        )
    # Mirrors the relay's rejection of Cc/Cf/Cs categories (it would 400, but
    # catching it here gives a specific message instead of a generic one).
    if any(unicodedata.category(ch) in {"Cc", "Cf", "Cs"} for ch in cleaned):
        raise TaskRequestInvalidError("任务标题不能包含控制字符")
    return cleaned


def validate_kind(kind: Any) -> str:
    cleaned = str(kind or "text").strip()
    if cleaned not in MESSAGE_KINDS:
        raise TaskRequestInvalidError(f"kind 必须是 {'、'.join(MESSAGE_KINDS)} 之一")
    if cleaned not in CLIENT_WRITABLE_KINDS:
        raise TaskRequestInvalidError(
            "event 和 system 消息只能由服务端写入"
        )
    return cleaned


def validate_fence(fence: Any) -> int:
    """Validate a ``fence`` before it leaves the process.

    The relay would 400 a negative or non-integer fence; the bool guard matters
    because Python's ``True == 1`` would otherwise let a boolean through.
    """
    if isinstance(fence, bool) or not isinstance(fence, int) or fence < 0:
        raise TaskRequestInvalidError("fence 必须是非负整数")
    return fence


def validate_next_state(state: Any) -> str:
    cleaned = str(state or "input_required").strip()
    if cleaned not in TASK_STATES or cleaned == "working":
        allowed = "、".join(s for s in TASK_STATES if s != "working")
        raise TaskRequestInvalidError(f"next_state 必须是 {allowed} 之一")
    return cleaned


def _check_payload_size(payload: Any) -> None:
    import json

    try:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TaskRequestInvalidError("payload 必须可 JSON 序列化") from exc
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise TaskRequestInvalidError(
            f"payload 超过 {MAX_MESSAGE_BYTES} 字节上限"
        )


class AsyncTasksClient:
    """Async client for the relay personal task-continuation API."""

    def __init__(self, config: TasksConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    @property
    def config(self) -> TasksConfig:
        return self._config

    def _get_client(self) -> httpx.AsyncClient:
        if not self._config.configured:
            raise TaskNotConfiguredError(
                "任务接续服务未配置（tasks.enabled / tasks.base_url）"
            )
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=_normalize_base_url(self._config.base_url),
                timeout=self._config.timeout,
                headers=_build_headers(self._config.api_key),
            )
        return self._client

    async def _request(
        self,
        method: HTTPMethod,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        client = self._get_client()
        try:
            resp = await client.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise TaskUnavailableError(f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            code, message = _extract_error(resp)
            if code == "task_fence_stale":
                raise TaskFenceStaleError(
                    message, status_code=resp.status_code
                )
            raise TaskError(message, code=code, status_code=resp.status_code)
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise TaskUnavailableError(
                "任务接续服务返回了非 JSON 响应", status_code=resp.status_code
            ) from exc

    # ── Tasks ──────────────────────────────────────────────────────────────

    async def create_task(
        self,
        *,
        title: str = "",
        owner_dataset: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a task. Returns ``{task_id, title, owner_dataset, state, seq_counter}``.

        The relay derives the owner from the authenticated principal, so no
        owner field is sent. There is no idempotency key: a retry creates a
        second task.
        """
        cleaned_title = validate_title(title)
        if payload is not None:
            _check_payload_size(payload)

        body: dict[str, Any] = {"title": cleaned_title}
        if owner_dataset:
            body["owner_dataset"] = str(owner_dataset).strip()
        if payload is not None:
            body["payload"] = payload

        result = await self._request("POST", TASKS_PATH, json=body)
        return result if isinstance(result, dict) else {}

    async def list_tasks(
        self, *, state: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List the configured principal's tasks, optionally filtered by state."""
        if state is not None and str(state).strip() not in TASK_STATES:
            raise TaskRequestInvalidError(f"state 必须是 {'、'.join(TASK_STATES)} 之一")
        try:
            safe_limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            safe_limit = 50

        params: dict[str, Any] = {"limit": safe_limit}
        if state is not None and str(state).strip():
            params["state"] = str(state).strip()

        result = await self._request("GET", TASKS_PATH, params=params)
        if not isinstance(result, dict):
            return []
        tasks = result.get("tasks")
        return [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []

    async def get_task(self, *, task_id: int) -> dict[str, Any]:
        """Task detail plus its most recent messages."""
        result = await self._request("GET", _task_path(task_id))
        return result if isinstance(result, dict) else {}

    async def update_task(
        self,
        *,
        task_id: int,
        title: str | None = None,
        archived: bool | None = None,
    ) -> dict[str, Any]:
        """Rename or archive a task. Returns ``{task_id, title, archived}``."""
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = validate_title(title)
        if archived is not None:
            body["archived"] = bool(archived)
        result = await self._request("PATCH", _task_path(task_id), json=body)
        return result if isinstance(result, dict) else {}

    async def retire(self, *, task_id: int) -> dict[str, Any]:
        """Retire a completed/failed/canceled task. Irreversible."""
        result = await self._request("POST", f"{_task_path(task_id)}/retire")
        return result if isinstance(result, dict) else {}

    # ── Lease ─────────────────────────────────────────────────────────────

    async def acquire(
        self,
        *,
        task_id: int,
        ttl_seconds: int | None = None,
        mode: str = "force",
    ) -> dict[str, Any]:
        """Take the lease on a task. Returns ``{task_id, fence, holder, expires_at, state}``.

        ``mode`` is ``"force"`` (default, atomic takeover) or ``"graceful"``
        (nudges the current holder to release first). Either way the returned
        ``fence`` is the token every subsequent write must present.
        """
        if mode not in ("force", "graceful"):
            raise TaskRequestInvalidError("mode 必须是 'force' 或 'graceful'")

        body: dict[str, Any] = {}
        if ttl_seconds is not None and str(ttl_seconds).strip() != "":
            body["ttl_seconds"] = _coerce_positive_int(ttl_seconds, "ttl_seconds")

        result = await self._request(
            "POST", f"{_task_path(task_id)}/acquire", params={"mode": mode}, json=body
        )
        return result if isinstance(result, dict) else {}

    async def renew(
        self, *, task_id: int, fence: int, ttl_seconds: int | None = None
    ) -> dict[str, Any]:
        """Extend the lease. Returns ``{task_id, fence, expires_at}``."""
        safe_fence = validate_fence(fence)
        body: dict[str, Any] = {"fence": safe_fence}
        if ttl_seconds is not None and str(ttl_seconds).strip() != "":
            body["ttl_seconds"] = _coerce_positive_int(ttl_seconds, "ttl_seconds")

        result = await self._request(
            "POST", f"{_task_path(task_id)}/renew", json=body
        )
        return result if isinstance(result, dict) else {}

    async def release(
        self,
        *,
        task_id: int,
        fence: int,
        next_state: str | None = None,
        checkpoint: dict[str, Any] | None = None,
        checkpoint_seq: int | None = None,
    ) -> dict[str, Any]:
        """Drop the lease, writing the resume point atomically with the release.

        ``next_state`` defaults to ``"input_required"`` (a human must decide);
        ``checkpoint``/``checkpoint_seq`` are the cross-device resume fuel.
        Returns ``{task_id, state, released}``.
        """
        safe_fence = validate_fence(fence)
        body: dict[str, Any] = {"fence": safe_fence}
        if next_state is not None:
            body["next_state"] = validate_next_state(next_state)
        if checkpoint is not None:
            if not isinstance(checkpoint, dict):
                raise TaskRequestInvalidError("checkpoint 必须是 JSON 对象")
            body["checkpoint"] = checkpoint
        if checkpoint_seq is not None and str(checkpoint_seq).strip() != "":
            body["checkpoint_seq"] = _coerce_non_negative_int(
                checkpoint_seq, "checkpoint_seq"
            )

        result = await self._request(
            "POST", f"{_task_path(task_id)}/release", json=body
        )
        return result if isinstance(result, dict) else {}

    async def checkpoint(
        self,
        *,
        task_id: int,
        fence: int,
        checkpoint: dict[str, Any],
        checkpoint_seq: int,
    ) -> dict[str, Any]:
        """Write a resume checkpoint while still holding the lease."""
        safe_fence = validate_fence(fence)
        if not isinstance(checkpoint, dict):
            raise TaskRequestInvalidError("checkpoint 必须是 JSON 对象")
        safe_seq = _coerce_non_negative_int(checkpoint_seq, "checkpoint_seq")

        body = {"fence": safe_fence, "checkpoint": checkpoint, "checkpoint_seq": safe_seq}
        result = await self._request(
            "POST", f"{_task_path(task_id)}/checkpoint", json=body
        )
        return result if isinstance(result, dict) else {}

    # ── Messages ────────────────────────────────────────────────────────────

    async def append_message(
        self,
        *,
        task_id: int,
        fence: int,
        payload: Any,
        kind: str = "text",
        client_msg_id: str | None = None,
        reply_to: int | None = None,
    ) -> dict[str, Any]:
        """Write one entry into the task log (requires the lease).

        ``client_msg_id`` is the relay's idempotency key; a retry returns the
        original entry rather than duplicating it.
        """
        safe_fence = validate_fence(fence)
        cleaned_kind = validate_kind(kind)
        if payload is None:
            raise TaskRequestInvalidError("payload 不能为空")
        _check_payload_size(payload)

        body: dict[str, Any] = {
            "fence": safe_fence,
            "kind": cleaned_kind,
            "payload": payload,
        }
        if client_msg_id:
            body["client_msg_id"] = str(client_msg_id)[:128]
        if reply_to is not None and str(reply_to).strip() != "":
            body["reply_to"] = _coerce_non_negative_int(reply_to, "reply_to")

        result = await self._request(
            "POST", f"{_task_path(task_id)}/messages", json=body
        )
        return result if isinstance(result, dict) else {}

    async def fetch_messages(
        self,
        *,
        task_id: int,
        after_seq: int | None = None,
        before_seq: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Pull a page of the task log.

        Omitting ``after_seq`` lets the relay use its recorded per-device
        cursor; a brand-new device falls back to the authoritative checkpoint.
        """
        params: dict[str, Any] = {}
        if after_seq is not None and str(after_seq).strip() != "":
            params["after_seq"] = _coerce_non_negative_int(after_seq, "after_seq")
        if before_seq is not None and str(before_seq).strip() != "":
            params["before_seq"] = _coerce_non_negative_int(before_seq, "before_seq")
        if params.get("after_seq") is not None and params.get("before_seq") is not None:
            raise TaskRequestInvalidError("after_seq 与 before_seq 不能同时指定")
        if limit is not None and str(limit).strip() != "":
            params["limit"] = _coerce_positive_int(limit, "limit")

        result = await self._request(
            "GET", f"{_task_path(task_id)}/messages", params=params
        )
        return result if isinstance(result, dict) else {}

    async def ack(self, *, task_id: int, seq: int) -> dict[str, Any]:
        """Advance the device's read cursor. It only ever moves forward."""
        safe_seq = _coerce_non_negative_int(seq, "seq")
        result = await self._request(
            "POST", f"{_task_path(task_id)}/messages/{safe_seq}/ack"
        )
        return result if isinstance(result, dict) else {}

    # ── Status / identity ───────────────────────────────────────────────────

    async def status(self) -> dict[str, Any]:
        """Relay-side module status (enabled, backend, quotas, lease timing)."""
        result = await self._request("GET", f"{TASKS_PATH}/status")
        return result if isinstance(result, dict) else {}

    async def whoami(self) -> dict[str, Any]:
        """Return the identity the configured API key authenticates as."""
        result = await self._request("GET", "api/auth/whoami")
        return result if isinstance(result, dict) else {}

    # ── Subscription ────────────────────────────────────────────────────────

    def subscribe_url(self, *, task_id: int, after_seq: int = 0) -> tuple[str, list[str]]:
        """Build the relay WebSocket URL and the subprotocol list for ``websockets``.

        The credential goes in the handshake headers, not ``?token=``.
        """
        config = self._config
        if not config.configured:
            raise TaskNotConfiguredError(
                "任务接续服务未配置（tasks.enabled / tasks.base_url）"
            )
        base = _normalize_base_url(config.base_url)
        ws_base = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        url = f"{ws_base}api/tasks/{_coerce_positive_int(task_id, 'task_id')}/subscribe?after_seq={_coerce_non_negative_int(after_seq, 'after_seq')}"
        return url, []

    def subscribe_headers(self) -> dict[str, str]:
        """Bearer header for the WebSocket handshake."""
        return _build_headers(self._config.api_key)

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def __aenter__(self) -> "AsyncTasksClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()