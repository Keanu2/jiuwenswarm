# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from openjiuwen.core.session.stream import OutputSchema

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.runtime.agent_adapter.todo_close_text_hold import (
    TodoCloseTextHold,
)


def test_hold_drops_text_and_release_returns_it() -> None:
    hold = TodoCloseTextHold()
    dropped = OutputSchema(type="todo_close_drop", index=0, payload={})
    released = OutputSchema(type="todo_close_release", index=0, payload={})

    assert hold.on_control("todo_close_hold") == "arm"
    assert hold.should_buffer(False) is True
    assert hold.should_buffer(True) is False
    hold.push({"event_type": "chat.delta", "content": "草稿"})
    assert hold.on_control(dropped.type) == "drop"
    assert hold.take() == []
    assert hold.should_buffer(False) is False

    assert hold.on_control("todo_close_hold") == "arm"
    hold.push({"event_type": "chat.delta", "content": "留下"})
    assert hold.on_control(released.type) == "release"
    assert hold.take() == [{"event_type": "chat.delta", "content": "留下"}]


def test_commit_frame_flushes_when_release_was_missed() -> None:
    hold = TodoCloseTextHold()
    hold.on_control("todo_close_hold")
    hold.push({"event_type": "chat.delta", "content": "进度"})
    assert hold.should_flush_for("llm_usage") is False
    assert hold.should_flush_for("tool_call") is True
    hold.disarm()
    assert hold.take() == [{"event_type": "chat.delta", "content": "进度"}]
    assert hold.should_buffer(False) is False


def test_second_hold_discards_unreleased_text() -> None:
    hold = TodoCloseTextHold()
    hold.on_control("todo_close_hold")
    hold.push({"event_type": "chat.delta", "content": "作废"})
    assert hold.on_control("todo_close_hold") == "arm"
    assert hold.take() == []
    assert hold.armed is True


def test_parse_stream_chunk_drops_todo_close_control() -> None:
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(
        OutputSchema(type="todo_close_drop", index=0, payload={"result_type": "control"}),
    )
    assert parsed is None
