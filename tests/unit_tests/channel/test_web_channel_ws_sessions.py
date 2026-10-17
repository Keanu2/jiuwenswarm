# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebChannel `_ws_sessions` 仅追踪显式 session（Issue #2334）。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from jiuwenswarm.common.security.web_identity import PRINCIPAL_ATTR, WebPrincipal
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web.web_connect import (
    WebChannel,
    WebChannelConfig,
)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent_frames: list[dict[str, Any]] = []
        self.remote_address = ("127.0.0.1", 12345)
        setattr(self, PRINCIPAL_ATTR, WebPrincipal("local", workspace_owner=True))

    async def send(self, data: str) -> None:
        self.sent_frames.append(json.loads(data))


def _req(*, req_id: str, method: str, params: dict[str, Any] | None = None) -> str:
    return json.dumps(
        {
            "type": "req",
            "id": req_id,
            "method": method,
            "params": params or {},
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_ws_sessions_tracks_explicit_session_id():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    ws = _FakeWebSocket()
    channel.on_message(lambda msg: None)

    await channel._handle_raw_message(
        ws,
        _req(
            req_id="req-chat",
            method="chat.send",
            params={"session_id": "sess-real", "content": "hi"},
        ),
        {},
    )

    assert channel._ws_sessions.get(id(ws)) == {"sess-real"}


@pytest.mark.asyncio
async def test_ws_sessions_ignores_temporary_session_without_explicit_id():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    ws = _FakeWebSocket()
    seen = []

    channel.on_message(lambda msg: seen.append(msg))

    await channel._handle_raw_message(
        ws,
        _req(req_id="req-config", method="config.get", params={}),
        {},
    )

    assert id(ws) not in channel._ws_sessions
    assert len(seen) == 1
    assert seen[0].session_id.startswith("sess_")


@pytest.mark.asyncio
async def test_ws_sessions_does_not_accumulate_temp_ids_across_requests():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    ws = _FakeWebSocket()
    channel.on_message(lambda msg: None)

    await channel._handle_raw_message(
        ws,
        _req(
            req_id="req-1",
            method="chat.send",
            params={"session_id": "sess-a", "content": "a"},
        ),
        {},
    )
    for i in range(5):
        await channel._handle_raw_message(
            ws,
            _req(req_id=f"req-poll-{i}", method="skills.list", params={}),
            {},
        )
    await channel._handle_raw_message(
        ws,
        _req(
            req_id="req-2",
            method="chat.send",
            params={"session_id": "sess-b", "content": "b"},
        ),
        {},
    )
    await channel._handle_raw_message(
        ws,
        _req(req_id="req-history", method="history.get", params={}),
        {},
    )

    assert channel._ws_sessions.get(id(ws)) == {"sess-a", "sess-b"}


@pytest.mark.asyncio
async def test_ws_sessions_ignores_empty_string_session_id():
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    ws = _FakeWebSocket()
    channel.on_message(lambda msg: None)

    await channel._handle_raw_message(
        ws,
        _req(
            req_id="req-empty-sid",
            method="config.get",
            params={"session_id": ""},
        ),
        {},
    )

    assert id(ws) not in channel._ws_sessions


@pytest.mark.asyncio
async def test_web_channel_uses_guarded_attachment_download(monkeypatch):
    """Retain public attachments through the SSRF-safe downloader."""
    from unittest.mock import AsyncMock
    from jiuwenswarm.gateway.channel_manager.web import web_connect

    download = AsyncMock(return_value=b"safe-public-attachment")
    monkeypatch.setattr(web_connect, "download_attachment", download)
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    url = "https://attachments.example/public.txt"
    assert await channel._download_file(url) == b"safe-public-attachment"
    download.assert_awaited_once_with(url, channel._attachment_policy)


@pytest.mark.asyncio
async def test_ws_metadata_attachment_is_rejected_before_http_get(monkeypatch):
    from unittest.mock import AsyncMock

    import aiohttp

    def _boom(*_a, **_k):
        raise AssertionError("WebChannel must not GET client-supplied file URLs")

    monkeypatch.setattr(aiohttp, "ClientSession", _boom)
    channel = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    response = AsyncMock()
    monkeypatch.setattr(channel, "send_response", response)
    ws = _FakeWebSocket()
    received: list[Any] = []
    channel.on_message(lambda msg: received.append(msg))

    await channel._handle_raw_message(
        ws,
        _req(
            req_id="req-ssrf",
            method="chat.send",
            params={
                "session_id": "sess-ssrf",
                "content": "hi",
                "files": [{"url": "http://169.254.169.254/latest/meta-data/", "name": "x"}],
            },
        ),
        {},
    )
    assert not received
    response.assert_awaited_once()
    assert response.await_args.kwargs["ok"] is False
    assert response.await_args.kwargs["code"] == "ATTACHMENT_REJECTED"
