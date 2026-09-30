# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the relay group chat client backing the 项目 (projects) page.

Two things are pinned here because they are the parts most likely to break
silently:

* the **two** error envelopes the relay uses — ``{"error": {...}}`` for business
  rejections and ``{"detail": ...}`` for the disabled module / auth layers. A
  client that only understands one of them reports the wrong code to the UI.
* the **client-side validation** of ``join_policy`` and ``max_members``. The
  relay does not validate either, and an invalid value surfaces as an unhandled
  500 rather than a 400, so these checks are the only thing standing between a
  typo and an opaque failure.
"""

from __future__ import annotations

import httpx
import pytest

from jiuwenswarm.agents.harness.team.groupchat import (
    AsyncGroupChatClient,
    GroupChatConfig,
    GroupChatError,
    GroupChatNotConfiguredError,
    GroupChatRequestInvalidError,
)
from jiuwenswarm.agents.harness.team.groupchat.client import (
    _build_headers,
    _normalize_base_url,
)

BASE_URL = "http://relay.test:8000"


def _config(**overrides: object) -> GroupChatConfig:
    values: dict[str, object] = {
        "enabled": True,
        "base_url": BASE_URL,
        "api_key": "a2x_pat_test",
        "timeout": 5.0,
        "default_max_members": 50,
    }
    values.update(overrides)
    return GroupChatConfig(**values)  # type: ignore[arg-type]


def _client(handler, config: GroupChatConfig | None = None) -> AsyncGroupChatClient:
    """Build a client whose transport is mocked but whose headers are real."""
    config = config or _config()
    client = AsyncGroupChatClient(config)
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
async def test_create_group_sends_expected_body_and_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"group_id": 7, "name": "proj", "seq_counter": 1})

    client = _client(handler)
    try:
        result = await client.create_group(name="  proj  ")
    finally:
        await client.aclose()

    assert result["group_id"] == 7
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/api/groups"
    assert request.headers["authorization"] == "Bearer a2x_pat_test"
    # The relay assigns the owner from the principal, so no owner field is sent.
    assert request.content.decode() == (
        '{"name":"proj","join_policy":"invite_only","max_members":50}'
    )


@pytest.mark.anyio
async def test_create_group_passes_owner_dataset_when_given() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"group_id": 1, "name": "p"})

    client = _client(handler)
    try:
        await client.create_group(name="p", owner_dataset="research")
    finally:
        await client.aclose()

    assert '"owner_dataset":"research"' in seen[0].content.decode()


@pytest.mark.anyio
async def test_list_groups_normalizes_non_dict_entries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/groups"
        assert request.url.params["limit"] == "10"
        return httpx.Response(200, json={"groups": [{"group_id": 1}, "junk", None]})

    client = _client(handler)
    try:
        groups = await client.list_groups(limit=10)
    finally:
        await client.aclose()

    assert groups == [{"group_id": 1}]


# ── error envelopes ──────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_business_error_envelope_is_unwrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": "groupchat_name_invalid", "message": "bad name"}},
        )

    client = _client(handler)
    try:
        with pytest.raises(GroupChatError) as excinfo:
            await client.create_group(name="n")
    finally:
        await client.aclose()

    assert excinfo.value.code == "groupchat_name_invalid"
    assert excinfo.value.status_code == 400


@pytest.mark.anyio
async def test_disabled_module_detail_dict_is_unwrapped() -> None:
    """The disabled path uses ``detail`` as a *dict*, unlike auth failures."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"detail": {"code": "groupchat_disabled", "message": "off"}},
        )

    client = _client(handler)
    try:
        with pytest.raises(GroupChatError) as excinfo:
            await client.list_groups()
    finally:
        await client.aclose()

    assert excinfo.value.code == "groupchat_disabled"
    assert excinfo.value.status_code == 404


@pytest.mark.anyio
async def test_auth_failure_detail_string_gets_synthetic_code() -> None:
    """Auth failures carry a plain string detail with no code — synthesize one."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Authentication required"})

    client = _client(handler)
    try:
        with pytest.raises(GroupChatError) as excinfo:
            await client.list_groups()
    finally:
        await client.aclose()

    assert excinfo.value.code == "http_401"
    assert "Authentication required" in str(excinfo.value)


@pytest.mark.anyio
async def test_non_json_error_body_falls_back_to_status_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>boom</html>")

    client = _client(handler)
    try:
        with pytest.raises(GroupChatError) as excinfo:
            await client.create_group(name="n")
    finally:
        await client.aclose()

    assert excinfo.value.code == "http_500"
    assert excinfo.value.status_code == 500


@pytest.mark.anyio
async def test_unconfigured_client_refuses_to_send() -> None:
    """A disabled section must fail before any request leaves the process."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be sent")

    client = _client(handler, config=_config(enabled=False))
    try:
        with pytest.raises(GroupChatNotConfiguredError):
            await client.create_group(name="n")
    finally:
        await client.aclose()


# ── client-side validation (the relay would 500 on these) ────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"name": ""}, id="empty-name"),
        pytest.param({"name": "   "}, id="blank-name"),
        pytest.param({"name": "x" * 121}, id="name-too-long"),
        pytest.param({"name": "bad\x01name"}, id="control-char"),
        pytest.param({"name": "n", "join_policy": "nope"}, id="bad-join-policy"),
        pytest.param({"name": "n", "max_members": "abc"}, id="non-int-max"),
        pytest.param({"name": "n", "max_members": True}, id="bool-max"),
        pytest.param({"name": "n", "max_members": 0}, id="zero-max"),
        pytest.param({"name": "n", "max_members": 999}, id="max-over-ceiling"),
    ],
)
async def test_invalid_arguments_never_reach_the_network(kwargs: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("an invalid argument must not issue a request")

    client = _client(handler)
    try:
        with pytest.raises(GroupChatRequestInvalidError):
            await client.create_group(**kwargs)
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_max_members_accepts_numeric_string() -> None:
    """A form field arrives as a string; it must still be usable."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert '"max_members":20' in request.content.decode()
        return httpx.Response(201, json={"group_id": 1})

    client = _client(handler)
    try:
        await client.create_group(name="p", max_members="20")
    finally:
        await client.aclose()
