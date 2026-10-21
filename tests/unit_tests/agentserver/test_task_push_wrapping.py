# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the task hub → web push wiring.

``set_task_frame_callback`` wraps relay subscription frames as ``task.message``
/ ``task.state`` events and ships them through the gateway push transport to
the ``web`` channel, where the web gateway broadcasts them to every client and
the panels filter by ``task_id``. What these tests pin:

* the payload shape per frame kind (lease frames double as the panel's state
  signal — the relay's task channel has no task-state push of its own);
* binding is idempotent per hub, so a panel subscribing later cannot unregister
  the bridge's own consumer;
* nothing of the relay frame leaks into request metadata — the push carries a
  synthetic ``request_id`` only.
"""

from __future__ import annotations

import pytest

import jiuwenswarm.server.gateway_push.transport as transport_module
from jiuwenswarm.agents.harness.team.tasks.subscription import (
    TaskSubscriptionHub,
    set_task_frame_callback,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeTransport:
    sent: list[dict] = []

    async def send_push(self, request: dict) -> None:
        _FakeTransport.sent.append(request)


@pytest.fixture()
def fake_transport(monkeypatch):
    _FakeTransport.sent = []
    monkeypatch.setattr(
        transport_module, "WebSocketGatewayPushTransport", lambda: _FakeTransport()
    )
    return _FakeTransport


def _hub() -> TaskSubscriptionHub:
    return TaskSubscriptionHub(
        config_base={"tasks": {"enabled": True, "base_url": "http://relay.test", "api_key": "k"}}
    )


@pytest.mark.anyio
async def test_message_frame_wraps_as_task_message(fake_transport) -> None:
    hub = _hub()
    set_task_frame_callback(hub)

    message = {
        "seq": 41,
        "message_id": "m-41",
        "sender_id": "u_1",
        "kind": "text",
        "payload": {"text": "继续"},
        "reply_to": None,
        "created_at": 1.0,
    }
    await hub._consumers[-1]({"task_id": 7, "frame": "message", "message": message})

    assert len(fake_transport.sent) == 1
    push = fake_transport.sent[0]
    assert push["channel_id"] == "web"
    assert push["request_id"] == "task-7-41"
    assert push["payload"] == {
        "event_type": "task.message",
        "task_id": 7,
        "frame": "message",
        "message": message,
    }


@pytest.mark.anyio
async def test_lease_frames_double_as_task_state(fake_transport) -> None:
    hub = _hub()
    set_task_frame_callback(hub)
    consumer = hub._consumers[-1]

    await consumer({"task_id": 7, "frame": "lease_lost", "fence": 4})
    await consumer({"task_id": 7, "frame": "lease_preempt"})

    assert [push["payload"] for push in fake_transport.sent] == [
        {"event_type": "task.state", "task_id": 7, "state": "lease_lost", "fence": 4},
        {"event_type": "task.state", "task_id": 7, "state": "lease_preempt", "fence": None},
    ]
    # request_id 仅用于日志关联，不含帧内容。
    assert fake_transport.sent[0]["request_id"] == "task-7-lease_lost"
    assert fake_transport.sent[1]["request_id"] == "task-7-lease_preempt"


@pytest.mark.anyio
async def test_backlog_frame_passes_page_fields_through(fake_transport) -> None:
    hub = _hub()
    set_task_frame_callback(hub)

    messages = [{"seq": 1, "kind": "text", "payload": {"text": "a"}, "reply_to": None}]
    await hub._consumers[-1](
        {
            "task_id": 7,
            "frame": "backlog",
            "messages": messages,
            "next_seq": 2,
            "has_more": False,
        }
    )

    push = fake_transport.sent[0]
    assert push["payload"]["event_type"] == "task.message"
    assert push["payload"]["frame"] == "backlog"
    assert push["payload"]["messages"] == messages
    assert push["payload"]["next_seq"] == 2
    assert push["payload"]["has_more"] is False


def test_binding_is_idempotent_per_hub() -> None:
    """面板后订阅不能挤掉 bridge 的消费者：回调按消费者追加，且只绑一次。"""
    hub = _hub()
    set_task_frame_callback(hub)
    after_first = len(hub._consumers)
    set_task_frame_callback(hub)
    assert len(hub._consumers) == after_first

    # 与面板/bridge 自己的消费者共存：追加一个新消费者不会破坏已有回调。
    hub.add_frame_consumer(lambda frame: None)
    assert len(hub._consumers) == after_first + 1
