# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the relay personal task-continuation client.

Pinned here for the same reason the group chat client tests pin their shapes:

* the **two** error envelopes — ``{"error": {...}}`` for business rejections and
  ``{"detail": ...}`` for the disabled module / auth layers — plus the special
  ``task_fence_stale`` code that must surface as ``TaskFenceStaleError`` (it is
  a takeover signal, not a retryable failure);
* the **fence** carried in every lease write, and the client-side validation of
  title / kind / fence / next_state, which keeps the relay from answering 400/500
  on something the caller could reject locally.
"""

from __future__ import annotations

import httpx
import pytest

from jiuwenswarm.agents.harness.team.tasks import (
    AsyncTasksClient,
    TaskError,
    TaskFenceStaleError,
    TaskNotConfiguredError,
    TaskRequestInvalidError,
    TasksConfig,
)
from jiuwenswarm.agents.harness.team.tasks.client import (
    _build_headers,
    _normalize_base_url,
)

BASE_URL = "http://relay.test:8000"


def _config(**overrides: object) -> TasksConfig:
    values: dict[str, object] = {
        "enabled": True,
        "base_url": BASE_URL,
        "api_key": "a2x_pat_test",
        "timeout": 5.0,
    }
    values.update(overrides)
    return TasksConfig(**values)  # type: ignore[arg-type]


def _client(handler, config: TasksConfig | None = None) -> AsyncTasksClient:
    """Build a client whose transport is mocked but whose headers are real."""
    config = config or _config()
    client = AsyncTasksClient(config)
    client._client = httpx.AsyncClient(
        base_url=_normalize_base_url(config.base_url),
        timeout=config.timeout,
        headers=_build_headers(config.api_key),
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── request construction ─────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_task_sends_expected_body_and_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"task_id": 7, "title": "t", "state": "pending", "seq_counter": 0})

    client = _client(handler)
    try:
        result = await client.create_task(title="  t  ")
    finally:
        await client.aclose()

    assert result["task_id"] == 7
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/api/tasks"
    assert request.headers["authorization"] == "Bearer a2x_pat_test"
    assert request.content.decode() == '{"title":"t"}'


@pytest.mark.anyio
async def test_list_tasks_normalizes_non_dict_entries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tasks"
        assert request.url.params["limit"] == "10"
        return httpx.Response(200, json={"tasks": [{"task_id": 1}, "junk", None]})

    client = _client(handler)
    try:
        tasks = await client.list_tasks(limit=10)
    finally:
        await client.aclose()

    assert tasks == [{"task_id": 1}]


@pytest.mark.anyio
async def test_acquire_uses_query_mode_and_omits_empty_ttl() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"task_id": 7, "fence": 3, "holder": "k", "state": "working"})

    client = _client(handler)
    try:
        result = await client.acquire(task_id=7)
    finally:
        await client.aclose()

    assert result["fence"] == 3
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/api/tasks/7/acquire"
    assert seen[0].url.params["mode"] == "force"
    assert seen[0].content.decode() == "{}"


@pytest.mark.anyio
async def test_renew_sends_the_fence_in_the_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"task_id": 7, "fence": 3, "expires_at": 1.0})

    client = _client(handler)
    try:
        await client.renew(task_id=7, fence=3)
    finally:
        await client.aclose()

    assert seen[0].url.path == "/api/tasks/7/renew"
    assert seen[0].content.decode() == '{"fence":3}'


@pytest.mark.anyio
async def test_append_message_sends_fence_kind_and_payload() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"task_id": 7, "seq": 1, "message_id": "m1", "kind": "tool_result"})

    client = _client(handler)
    try:
        await client.append_message(
            task_id=7, fence=4, payload={"out": "ok"}, kind="tool_result", client_msg_id="c1"
        )
    finally:
        await client.aclose()

    assert seen[0].url.path == "/api/tasks/7/messages"
    body = seen[0].content.decode()
    assert '"fence":4' in body
    assert '"kind":"tool_result"' in body
    assert '"payload":{"out":"ok"}' in body
    assert '"client_msg_id":"c1"' in body


# ── error envelopes ──────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_business_error_envelope_is_unwrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": "task_title_invalid", "message": "bad"}})

    client = _client(handler)
    try:
        with pytest.raises(TaskError) as excinfo:
            await client.create_task(title="n")
    finally:
        await client.aclose()

    assert excinfo.value.code == "task_title_invalid"
    assert excinfo.value.status_code == 400


@pytest.mark.anyio
async def test_fence_stale_raises_the_dedicated_error() -> None:
    """A stale fence is a takeover signal, not a run-of-the-mill rejection."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": {"code": "task_fence_stale", "message": "fenced"}})

    client = _client(handler)
    try:
        with pytest.raises(TaskFenceStaleError) as excinfo:
            await client.renew(task_id=7, fence=1)
    finally:
        await client.aclose()

    assert excinfo.value.code == "task_fence_stale"
    # It is still a TaskError, so generic handlers catch it too.
    assert isinstance(excinfo.value, TaskError)


@pytest.mark.anyio
async def test_disabled_module_detail_dict_is_unwrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": {"code": "tasks_disabled", "message": "off"}})

    client = _client(handler)
    try:
        with pytest.raises(TaskError) as excinfo:
            await client.list_tasks()
    finally:
        await client.aclose()

    assert excinfo.value.code == "tasks_disabled"
    assert excinfo.value.status_code == 404


@pytest.mark.anyio
async def test_auth_failure_detail_string_gets_synthetic_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Authentication required"})

    client = _client(handler)
    try:
        with pytest.raises(TaskError) as excinfo:
            await client.list_tasks()
    finally:
        await client.aclose()

    assert excinfo.value.code == "http_401"


@pytest.mark.anyio
async def test_non_json_error_body_falls_back_to_status_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>boom</html>")

    client = _client(handler)
    try:
        with pytest.raises(TaskError) as excinfo:
            await client.create_task(title="n")
    finally:
        await client.aclose()

    assert excinfo.value.code == "http_500"


@pytest.mark.anyio
async def test_unconfigured_client_refuses_to_send() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be sent")

    client = _client(handler, config=_config(enabled=False))
    try:
        with pytest.raises(TaskNotConfiguredError):
            await client.create_task(title="n")
    finally:
        await client.aclose()


# ── client-side validation ───────────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"title": "x" * 201}, id="title-too-long"),
        pytest.param({"title": "bad\x01title"}, id="title-control-char"),
    ],
)
async def test_invalid_create_title_never_reaches_the_network(kwargs: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("an invalid argument must not issue a request")

    client = _client(handler)
    try:
        with pytest.raises(TaskRequestInvalidError):
            await client.create_task(**kwargs)
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_reserved_message_kind_is_rejected_locally() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("a reserved kind must not issue a request")

    client = _client(handler)
    try:
        with pytest.raises(TaskRequestInvalidError):
            await client.append_message(task_id=7, fence=1, payload="x", kind="event")
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_bool_fence_is_rejected_locally() -> None:
    """``True == 1``, so a bool fence must be caught before it leaves."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    client = _client(handler)
    try:
        with pytest.raises(TaskRequestInvalidError):
            await client.renew(task_id=7, fence=True)  # type: ignore[arg-type]
    finally:
        await client.aclose()

    assert seen == []


@pytest.mark.anyio
async def test_working_next_state_on_release_is_rejected():
    """``working`` is not a valid release target (the lease is being dropped)."""
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("an invalid next_state must not issue a request")

    client = _client(handler)
    try:
        with pytest.raises(TaskRequestInvalidError):
            await client.release(task_id=7, fence=1, next_state="working")
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_conflicting_cursors_are_rejected_locally() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("conflicting cursors must not issue a request")

    client = _client(handler)
    try:
        with pytest.raises(TaskRequestInvalidError):
            await client.fetch_messages(task_id=7, after_seq=1, before_seq=5)
    finally:
        await client.aclose()