# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the proactive personal-task bridge.

The lease lifecycle is the security- and correctness-relevant part: the bridge
must keep the lease alive while running, react to a takeover promptly, and — on
the graceful ``lease_preempt`` nudge — flush a checkpoint and release rather
than hang on. All IO seams (hub, relay calls, halt hook) are injected, so nothing
here touches a network or an AgentServer.

The turn-execution tests pin the loop-prevention contract: a user log entry
runs exactly one agent turn, the answer is posted under the held fence with
``reply_to`` marking it as ours, and the reply coming back through the
subscription must not start another turn.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from jiuwenswarm.agents.harness.team.tasks.bridge import (
    BRIDGE_SUBSCRIBER_ID,
    CHANNEL_ID,
    TaskBridge,
    build_reply_client_msg_id,
    build_tool_client_msg_id,
    consume_turn_stream,
    message_text,
    render_task_prompt,
    session_id_for_task,
    should_trigger_turn,
)
from jiuwenswarm.agents.harness.team.tasks.errors import (
    TaskError,
    TaskFenceStaleError,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _StubHub:
    def __init__(self) -> None:
        self.attached: list[tuple[int, str]] = []
        self.detached: list[tuple[int, str]] = []
        self.consumers: list = []

    def add_frame_consumer(self, callback):  # noqa: ANN001
        self.consumers.append(callback)

        def _remove() -> None:
            if callback in self.consumers:
                self.consumers.remove(callback)

        return _remove

    async def attach(self, *, task_id, subscriber_id):  # noqa: ANN001
        self.attached.append((task_id, subscriber_id))

    async def detach(self, *, task_id, subscriber_id):  # noqa: ANN001
        self.detached.append((task_id, subscriber_id))


def _async_return(value):  # noqa: ANN001
    async def _inner(*args, **kwargs):  # noqa: ANN002, ANN003
        return value

    return _inner


def _make_bridge(**kwargs):  # noqa: ANN003
    defaults = {
        "config_base": {
            "tasks": {
                "enabled": True,
                "base_url": "http://relay.test",
                "api_key": "test-key",
            }
        },
        "hub": _StubHub(),
        "acquirer": _async_return(
            {"task_id": 7, "fence": 3, "holder": "k", "expires_at": 1.0, "state": "working"}
        ),
        "renewer": _async_return({"task_id": 7, "fence": 3, "expires_at": 2.0}),
        "releaser": _async_return({"task_id": 7, "state": "input_required", "released": True}),
        "checkpointer": _async_return({"task_id": 7, "checkpoint_seq": 5}),
        "fetcher": _async_return({"messages": [], "latest_seq": 5}),
        "halt_execution": _async_return(None),
    }
    defaults.update(kwargs)
    return TaskBridge(**defaults)


async def _take(bridge: TaskBridge) -> None:
    assert await bridge.start() is True
    await bridge.take_task(7)


# ── ids / hub identity ───────────────────────────────────────────────────────


def test_session_and_channel_ids_are_stable() -> None:
    assert session_id_for_task(7) == "task_7"
    assert session_id_for_task(7) == session_id_for_task(7)
    assert CHANNEL_ID == "tasks"


def test_subscription_hub_is_separate_per_credential() -> None:
    """Two devices are two keys, so their hubs (and takeovers) must not share."""
    from jiuwenswarm.agents.harness.team.tasks.subscription import (
        get_subscription_hub,
        reset_subscription_hub_for_tests,
    )

    def base(api_key: str) -> dict:
        return {"tasks": {"enabled": True, "base_url": "http://relay.test", "api_key": api_key}}

    reset_subscription_hub_for_tests()
    try:
        phone = get_subscription_hub(base("phone-key"))
        pc = get_subscription_hub(base("pc-key"))
        assert phone is not pc
        assert get_subscription_hub(base("phone-key")) is phone
    finally:
        reset_subscription_hub_for_tests()


# ── lifecycle ────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_start_is_a_noop_when_not_configured() -> None:
    bridge = _make_bridge(config_base={})
    assert await bridge.start() is False
    assert bridge.held_tasks == []


@pytest.mark.anyio
async def test_start_registers_a_frame_consumer() -> None:
    bridge = _make_bridge()
    try:
        assert await bridge.start() is True
        assert bridge._hub.consumers, "bridge must register a frame consumer"
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_take_task_acquires_attaches_and_starts_keepalive() -> None:
    acquired: list[dict] = []

    async def acquirer(**kwargs):  # noqa: ANN003
        acquired.append(kwargs)
        return {"task_id": kwargs["task_id"], "fence": 9, "holder": "k", "state": "working"}

    bridge = _make_bridge(acquirer=acquirer)
    try:
        await bridge.start()
        result = await bridge.take_task(7, mode="force")
        assert result["fence"] == 9
        assert acquired == [{"task_id": 7, "ttl_seconds": None, "mode": "force"}]
        assert bridge._hub.attached == [(7, BRIDGE_SUBSCRIBER_ID)]
        run = bridge.run_for(7)
        assert run is not None
        assert run.fence == 9
        assert run.keepalive_task is not None
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_lease_lost_halts_without_releasing() -> None:
    released: list[dict] = []
    halted: list[str] = []

    async def releaser(**kwargs):  # noqa: ANN003
        released.append(kwargs)
        return {}

    async def halt(session_id: str) -> None:
        halted.append(session_id)

    bridge = _make_bridge(releaser=releaser, halt_execution=halt)
    try:
        await _take(bridge)
        await bridge._on_frame({"task_id": 7, "frame": "lease_lost", "fence": 4})
        assert released == [], "a fenced device must not try to release (it would 409)"
        assert halted == ["task_7"]
        assert bridge.run_for(7).halted is True
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_lease_preempt_flushes_checkpoint_and_releases() -> None:
    released: list[dict] = []

    async def releaser(**kwargs):  # noqa: ANN003
        released.append(kwargs)
        return {"state": "input_required", "released": True}

    bridge = _make_bridge(releaser=releaser)
    try:
        await _take(bridge)
        await bridge._on_frame({"task_id": 7, "frame": "lease_preempt"})

        assert len(released) == 1
        assert released[0]["task_id"] == 7
        assert released[0]["next_state"] == "input_required"
        assert bridge.run_for(7).halted is True
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_finish_task_releases_and_detaches() -> None:
    released: list[dict] = []

    async def releaser(**kwargs):  # noqa: ANN003
        released.append(kwargs)
        return {"state": "completed", "released": True}

    bridge = _make_bridge(releaser=releaser)
    try:
        await _take(bridge)
        await bridge.finish_task(7, next_state="completed", checkpoint={"done": True}, checkpoint_seq=5)

        assert released[0]["next_state"] == "completed"
        assert released[0]["checkpoint"] == {"done": True}
        assert released[0]["checkpoint_seq"] == 5
        assert (7, BRIDGE_SUBSCRIBER_ID) in bridge._hub.detached
        assert bridge.run_for(7) is None
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_finish_task_on_unheld_task_raises() -> None:
    bridge = _make_bridge()
    try:
        await bridge.start()
        with pytest.raises(TaskError) as excinfo:
            await bridge.finish_task(999)
        assert excinfo.value.code == "not_held"
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_renew_fence_stale_halts() -> None:
    async def renewer(**kwargs):  # noqa: ANN003
        raise TaskFenceStaleError("fenced")

    bridge = _make_bridge(renewer=renewer)
    try:
        await _take(bridge)
        run = bridge.run_for(7)
        await bridge._renew_once(run)
        assert run.halted is True
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_renew_passess_the_held_fence() -> None:
    renewed: list[dict] = []

    async def renewer(**kwargs):  # noqa: ANN003
        renewed.append(kwargs)
        return {}

    bridge = _make_bridge(renewer=renewer)
    try:
        await _take(bridge)
        await bridge._renew_once(bridge.run_for(7))
        assert renewed == [{"task_id": 7, "fence": 3}]
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_write_checkpoint_uses_the_held_fence() -> None:
    checkpoints: list[dict] = []

    async def checkpointer(**kwargs):  # noqa: ANN003
        checkpoints.append(kwargs)
        return {"checkpoint_seq": kwargs["checkpoint_seq"]}

    bridge = _make_bridge(checkpointer=checkpointer)
    try:
        await _take(bridge)
        await bridge.write_checkpoint(7, {"stage": "done"}, checkpoint_seq=5)
        assert checkpoints == [
            {"task_id": 7, "fence": 3, "checkpoint": {"stage": "done"}, "checkpoint_seq": 5}
        ]
    finally:
        await bridge.stop()


# ── turn execution ───────────────────────────────────────────────────────────


def _user_message(seq: int, text: str = "继续") -> dict:
    return {"seq": seq, "kind": "text", "reply_to": None, "payload": {"text": text}}


def test_should_trigger_turn_pure_cases() -> None:
    """The reply_to discriminator: user entries run, bridge replies never do."""
    assert should_trigger_turn(_user_message(41)) is True
    # 本 bridge 的回答（带 reply_to）绝不能再次触发 turn。
    assert (
        should_trigger_turn({"seq": 42, "kind": "text", "reply_to": 41, "payload": {"text": "答案"}})
        is False
    )
    # 非 text（event/system/工具结果）不触发。
    assert should_trigger_turn({"seq": 43, "kind": "event", "reply_to": None, "payload": {}}) is False
    # 空文本不触发。
    assert should_trigger_turn(_user_message(44, "   ")) is False


def test_message_text_shapes() -> None:
    assert message_text("直接字符串") == "直接字符串"
    assert message_text({"text": " 带空白 "}) == "带空白"
    assert message_text({"content": "内容字段"}) == "内容字段"
    assert message_text({"role": "user", "text": "导入消息"}) == "导入消息"
    assert message_text(None) == ""


def test_render_task_prompt_frames_trigger_and_history() -> None:
    """触发消息是任务，日志是背景；导入条目带角色标注。"""
    prompt = render_task_prompt(
        trigger=_user_message(41, "帮我查一下"),
        history=[
            {"seq": 20, "kind": "text", "payload": {"role": "user", "text": "旧问题"}},
            {"seq": 10, "kind": "text", "payload": {"role": "assistant", "text": "旧回答"}},
        ],
    )
    # 顺序按 seq 排，乱序进也要按序出。
    assert prompt.index("[10] [assistant] 旧回答") < prompt.index("[20] [user] 旧问题")
    assert '<task_message seq="41">' in prompt
    assert "帮我查一下" in prompt
    assert "<task_history>" in prompt
    # 「数据而非指令」框架必须出现在最前面。
    assert prompt.startswith("以下内容来自跨设备任务日志")


def test_render_task_prompt_lists_materialized_artifacts() -> None:
    """已物化到本机的交接文件以 <task_artifacts> 段告知 agent 本地路径。"""
    prompt = render_task_prompt(
        trigger=_user_message(41, "改一下五子棋"),
        history=[{"seq": 20, "kind": "tool_result", "payload": {"text": "[工具 write_file] …"}}],
        artifacts={"gomoku.html": "C:\\proj\\task_7\\gomoku.html"},
    )
    assert "<task_artifacts>" in prompt
    assert "gomoku.html: C:\\proj\\task_7\\gomoku.html" in prompt
    # 无文件时不输出空段。
    bare = render_task_prompt(trigger=_user_message(41), history=[])
    assert "<task_artifacts>" not in bare


def test_build_reply_client_msg_id_is_stable() -> None:
    assert build_reply_client_msg_id(7, 41) == "task-7-41"
    assert build_reply_client_msg_id(7, 41) == build_reply_client_msg_id(7, 41)


@pytest.mark.anyio
async def test_fence_for_three_states() -> None:
    """未持有 → None；持有 → fence；被击落（halted）→ None。"""
    bridge = _make_bridge()
    try:
        assert bridge.fence_for(7) is None
        await _take(bridge)
        assert bridge.fence_for(7) == 3
        bridge.run_for(7).halted = True
        assert bridge.fence_for(7) is None
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_message_frame_runs_one_turn_and_posts_fenced_reply() -> None:
    runs: list[dict] = []
    posted: list[dict] = []

    async def runner(**kwargs):  # noqa: ANN003
        runs.append(kwargs)
        return "答案正文"

    async def appender(**kwargs):  # noqa: ANN003
        posted.append(kwargs)
        return {"task_id": 7, "seq": 42}

    bridge = _make_bridge(runner=runner, appender=appender)
    try:
        await _take(bridge)
        await bridge._on_frame({"task_id": 7, "frame": "message", "message": _user_message(41)})

        assert bridge._turn_tasks, "a user text frame must start a turn"
        await asyncio.gather(*list(bridge._turn_tasks))

        assert len(runs) == 1
        assert runs[0]["session_id"] == "task_7"
        assert runs[0]["request_id"] == "task-7-41"
        # 注入渠道教训：metadata 只带自家标记，不带帧里任何对端字段。
        assert runs[0]["metadata"] == {"source": "tasks", "task_id": 7}

        assert len(posted) == 1
        assert posted[0]["fence"] == 3
        assert posted[0]["payload"] == {"text": "答案正文"}
        assert posted[0]["kind"] == "text"
        assert posted[0]["reply_to"] == 41
        assert posted[0]["client_msg_id"] == "task-7-41"
        # 回复落库后游标推进到回复的 seq。
        assert bridge.run_for(7).last_seq == 42
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_turn_materializes_artifacts_and_passes_paths_to_prompt() -> None:
    """接手设备的回合：先从 relay 拉回交接文件，提示词里给出本机路径。"""
    prompts: list[str] = []
    materialized: list[tuple] = []

    async def runner(**kwargs):  # noqa: ANN003
        prompts.append(kwargs["prompt"])
        return "已基于同步来的文件继续"

    async def materializer(history, session_id):  # noqa: ANN001
        materialized.append((history, session_id))
        return {"gomoku.html": "C:\\proj\\task_7\\gomoku.html"}

    async def fetcher(**kwargs):  # noqa: ANN003
        return {
            "messages": [
                {
                    "seq": 20,
                    "kind": "tool_result",
                    "payload": {
                        "tool_name": "write_file",
                        "text": "[工具 write_file] success=True",
                        "artifacts": [
                            {
                                "name": "gomoku.html",
                                "size": 23753,
                                "download_url": "http://relay.test/api/artifact-relay/uri/x/y",
                            }
                        ],
                    },
                }
            ],
            "latest_seq": 20,
        }

    bridge = _make_bridge(
        runner=runner,
        fetcher=fetcher,
        artifact_materializer=materializer,
        appender=_async_return({"task_id": 7, "seq": 42}),
    )
    try:
        await _take(bridge)
        await bridge._on_frame({"task_id": 7, "frame": "message", "message": _user_message(41)})
        await asyncio.gather(*list(bridge._turn_tasks))

        # 物化拿到的就是 _run_turn 取到的日志，落到任务会话的工作区。
        assert materialized[0][1] == "task_7"
        assert materialized[0][0][0]["payload"]["artifacts"]
        assert "<task_artifacts>" in prompts[0]
        assert "gomoku.html: C:\\proj\\task_7\\gomoku.html" in prompts[0]
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_turn_survives_artifact_materialization_failure() -> None:
    """物化失败只是少了文件清单，回合照常进行。"""
    prompts: list[str] = []

    async def runner(**kwargs):  # noqa: ANN003
        prompts.append(kwargs["prompt"])
        return "答案"

    async def materializer(history, session_id):  # noqa: ANN001
        raise RuntimeError("artifact relay down")

    bridge = _make_bridge(
        runner=runner,
        artifact_materializer=materializer,
        appender=_async_return({"task_id": 7, "seq": 42}),
    )
    try:
        await _take(bridge)
        await bridge._on_frame({"task_id": 7, "frame": "message", "message": _user_message(41)})
        await asyncio.gather(*list(bridge._turn_tasks))

        assert len(prompts) == 1
        assert "<task_artifacts>" not in prompts[0]
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_reply_frame_does_not_retrigger() -> None:
    """自己（或对端）发出的回复帧回环回来不能再起 turn。"""
    runs: list[dict] = []

    async def runner(**kwargs):  # noqa: ANN003
        runs.append(kwargs)
        return "不应到达"

    bridge = _make_bridge(runner=runner)
    try:
        await _take(bridge)
        await bridge._on_frame(
            {
                "task_id": 7,
                "frame": "message",
                "message": {"seq": 42, "kind": "text", "reply_to": 41, "payload": {"text": "答案"}},
            }
        )
        # 给事件循环一轮机会，确认确实没有创建 turn task。
        await asyncio.sleep(0)
        assert not bridge._turn_tasks
        assert runs == []
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_inflight_second_message_is_skipped() -> None:
    """One turn at a time：turn 进行中到达的第二条消息被丢弃，不排队。"""
    started = asyncio.Event()
    may_finish = asyncio.Event()
    runs: list[dict] = []

    async def runner(**kwargs):  # noqa: ANN003
        runs.append(kwargs)
        started.set()
        await may_finish.wait()
        return "答案"

    bridge = _make_bridge(runner=runner)
    try:
        await _take(bridge)
        await bridge._on_frame({"task_id": 7, "frame": "message", "message": _user_message(41)})
        first = next(iter(bridge._turn_tasks))
        await started.wait()

        await bridge._on_frame({"task_id": 7, "frame": "message", "message": _user_message(43)})
        assert len(bridge._turn_tasks) == 1, "a second message while inflight must be skipped"

        may_finish.set()
        await first
        assert len(runs) == 1
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_backlog_frame_never_triggers() -> None:
    """重放的历史（重新接管时的 backlog）绝不能逐条跑 turn。"""
    runs: list[dict] = []

    async def runner(**kwargs):  # noqa: ANN003
        runs.append(kwargs)
        return "不应到达"

    bridge = _make_bridge(runner=runner)
    try:
        await _take(bridge)
        await bridge._on_frame(
            {
                "task_id": 7,
                "frame": "backlog",
                "messages": [_user_message(41), _user_message(43)],
                "next_seq": 44,
                "has_more": False,
            }
        )
        await asyncio.sleep(0)
        assert runs == []
        assert not bridge._turn_tasks
        # 游标仍然推进（供 checkpoint 使用）。
        assert bridge.run_for(7).last_seq >= 43
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_stop_cancels_inflight_turn() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def runner(**kwargs):  # noqa: ANN003
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "不会到达"

    bridge = _make_bridge(runner=runner)
    try:
        await _take(bridge)
        await bridge._on_frame({"task_id": 7, "frame": "message", "message": _user_message(41)})
        await started.wait()
    finally:
        await bridge.stop()

    assert cancelled.is_set(), "stop() must cancel a running turn, not wait it out"


@pytest.mark.anyio
async def test_fenced_mid_turn_does_not_post_and_does_not_raise() -> None:
    """Turn 进行中被击落：回复不落库、异常不外泄、inflight 复位。"""
    async def runner(**kwargs):  # noqa: ANN003
        raise TaskFenceStaleError("fenced mid-turn")

    posted: list[dict] = []

    async def appender(**kwargs):  # noqa: ANN003
        posted.append(kwargs)
        return {"task_id": 7, "seq": 42}

    bridge = _make_bridge(runner=runner, appender=appender)
    try:
        await _take(bridge)
        await bridge._on_frame({"task_id": 7, "frame": "message", "message": _user_message(41)})
        await asyncio.gather(*list(bridge._turn_tasks))
        assert posted == []
        assert bridge.run_for(7).inflight is False
    finally:
        await bridge.stop()


# ── live tool events（接管后回合的中间结果） ──────────────────────────────────


async def _chunk_stream(payloads):  # noqa: ANN001
    """Feed plain payload dicts: ``consume_turn_stream`` treats dicts as chunks."""
    for payload in payloads:
        yield payload


@pytest.mark.anyio
async def test_consume_turn_stream_pairs_and_forwards_tool_events() -> None:
    """流解析：配对 tool_call/tool_result、孤儿结果单独成条、reasoning 归位。"""
    events: list[dict] = []
    payloads = [
        {"event_type": "chat.reasoning", "content": "先查一下"},
        {"event_type": "chat.reasoning", "content": "工具"},
        {
            "event_type": "chat.tool_call",
            "tool_call": {
                "name": "search_tools",
                "arguments": '{"query": "write file"}',
                "tool_call_id": "c1",
            },
        },
        {
            "event_type": "chat.tool_result",
            "tool_name": "search_tools",
            "tool_call_id": "c1",
            "result": "找到 write_file",
        },
        {
            "event_type": "chat.tool_result",
            "tool_name": "load_tools",
            "result": "loaded",
            "status": "error",
        },
        {"event_type": "chat.final", "content": "最终答案"},
    ]

    async def on_tool_event(entry):  # noqa: ANN001
        events.append(dict(entry))

    text = await consume_turn_stream(_chunk_stream(payloads), on_tool_event)

    assert text == "最终答案"
    assert len(events) == 2
    # 配对条目：调用侧的名字/参数 + 结果侧的结果，reasoning 摘要挂在调用上。
    assert events[0] == {
        "tool_name": "search_tools",
        "arguments": '{"query": "write file"}',
        "thinking": "先查一下工具",
        "result": "找到 write_file",
        "status": None,
    }
    # 孤儿结果（无对应调用）：arguments/thinking 留空，条目不丢。
    assert events[1] == {
        "tool_name": "load_tools",
        "arguments": None,
        "thinking": None,
        "result": "loaded",
        "status": "error",
    }


@pytest.mark.anyio
async def test_consume_turn_stream_error_without_final_raises() -> None:
    """没有 final 只有 error：等价于 unary 的 ok=False，上抛 turn_failed。"""
    payloads = [
        {"event_type": "chat.tool_result", "tool_name": "t", "result": "r"},
        {"event_type": "chat.error", "error": "模型超时"},
    ]

    with pytest.raises(TaskError) as excinfo:
        await consume_turn_stream(_chunk_stream(payloads), None)

    assert excinfo.value.code == "turn_failed"
    assert "模型超时" in str(excinfo.value)


@pytest.mark.anyio
async def test_turn_posts_tool_events_live_before_reply() -> None:
    """接管后的回合：工具事件实时落库（带 fence、幂等键），回复最后落库。"""
    posted: list[dict] = []

    async def runner(**kwargs):  # noqa: ANN003
        on_tool_event = kwargs["on_tool_event"]
        assert callable(on_tool_event)
        await on_tool_event(
            {
                "tool_name": "search_tools",
                "arguments": '{"query": "x"}',
                "thinking": "想",
                "result": "结果",
                "status": None,
            }
        )
        await on_tool_event(
            {
                "tool_name": "write_file",
                "arguments": None,
                "thinking": None,
                "result": "written",
                "status": None,
            }
        )
        return "答案正文"

    async def appender(**kwargs):  # noqa: ANN003
        posted.append(kwargs)
        return {"task_id": 7, "seq": 49 + len(posted)}

    bridge = _make_bridge(runner=runner, appender=appender)
    try:
        await _take(bridge)
        await bridge._on_frame({"task_id": 7, "frame": "message", "message": _user_message(41)})
        await asyncio.gather(*list(bridge._turn_tasks))

        # 顺序：两条工具事件在前，最终回复在后——面板按 seq 渲染出「先调
        # 工具、后给答案」的过程。
        assert [call["kind"] for call in posted] == ["tool_result", "tool_result", "text"]
        assert all(call["fence"] == 3 for call in posted)
        # 工具条目不是回答：reply_to 留 None，awaitingReply 只被真正的回复
        # （带 reply_to 的 text）熄灭，中间结果在转圈期间持续插入。
        assert all(call["reply_to"] is None for call in posted[:2])
        assert posted[2]["reply_to"] == 41

        first = posted[0]
        assert first["payload"]["tool_name"] == "search_tools"
        assert first["payload"]["role"] == "assistant"
        assert first["payload"]["text"].startswith("[工具 search_tools]")
        assert first["client_msg_id"] == build_tool_client_msg_id(7, 41, 0)
        assert posted[1]["client_msg_id"] == build_tool_client_msg_id(7, 41, 1)
        # 非文件类工具：不挂 artifacts。
        assert "artifacts" not in first["payload"]
        # 游标推进到最后一条（回复）的 seq。
        assert bridge.run_for(7).last_seq == 52
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_tool_event_archives_files_and_attaches_refs() -> None:
    """write_file 的工具事件：产物经 artifact-relay 上传，引用挂进 payload。"""
    posted: list[dict] = []
    uploads: list[list[str]] = []

    async def runner(**kwargs):  # noqa: ANN003
        await kwargs["on_tool_event"](
            {
                "tool_name": "write_file",
                "arguments": json.dumps(
                    {"file_path": "C:\\proj\\gomoku.html", "content": "x"}
                ),
                "thinking": None,
                "result": "ok",
                "status": None,
            }
        )
        return "已写好"

    async def uploader(paths, **kwargs):  # noqa: ANN001, ANN003
        uploads.append(list(paths))
        return {
            "C:\\proj\\gomoku.html": {
                "name": "gomoku.html",
                "size": 23753,
                "download_url": "http://relay.test/uri/a/b",
            }
        }

    async def appender(**kwargs):  # noqa: ANN003
        posted.append(kwargs)
        return {"task_id": 7, "seq": 50}

    bridge = _make_bridge(runner=runner, appender=appender, artifact_uploader=uploader)
    try:
        await _take(bridge)
        await bridge._on_frame({"task_id": 7, "frame": "message", "message": _user_message(41)})
        await asyncio.gather(*list(bridge._turn_tasks))

        assert uploads == [["C:\\proj\\gomoku.html"]]
        tool_payload = posted[0]["payload"]
        assert tool_payload["artifacts"] == [
            {
                "name": "gomoku.html",
                "size": 23753,
                "download_url": "http://relay.test/uri/a/b",
            }
        ]
        # 📎 行进 text：只读 text 的旧读者也拿得到下载链接。
        assert "📎" in tool_payload["text"]
        assert "http://relay.test/uri/a/b" in tool_payload["text"]
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_tool_event_append_failure_continues_turn() -> None:
    """工具事件落库失败（非 fence）：记日志继续回合，最终回复照常落库。"""
    posted: list[dict] = []

    async def runner(**kwargs):  # noqa: ANN003
        await kwargs["on_tool_event"](
            {
                "tool_name": "search_tools",
                "arguments": None,
                "thinking": None,
                "result": "r",
                "status": None,
            }
        )
        return "答案正文"

    async def appender(**kwargs):  # noqa: ANN003
        if kwargs["kind"] == "tool_result":
            raise TaskError("relay 暂时不可达", code="unavailable")
        posted.append(kwargs)
        return {"task_id": 7, "seq": 42}

    bridge = _make_bridge(runner=runner, appender=appender)
    try:
        await _take(bridge)
        await bridge._on_frame({"task_id": 7, "frame": "message", "message": _user_message(41)})
        await asyncio.gather(*list(bridge._turn_tasks))

        assert len(posted) == 1
        assert posted[0]["kind"] == "text"
        assert bridge.run_for(7).inflight is False
    finally:
        await bridge.stop()


@pytest.mark.anyio
async def test_tool_event_fence_stale_aborts_turn_without_reply() -> None:
    """工具事件落库被 fence 拒绝：回合立即停，回复不落库，异常不外泄。"""
    posted: list[dict] = []

    async def runner(**kwargs):  # noqa: ANN003
        await kwargs["on_tool_event"](
            {
                "tool_name": "search_tools",
                "arguments": None,
                "thinking": None,
                "result": "r",
                "status": None,
            }
        )
        return "不应到达"

    async def appender(**kwargs):  # noqa: ANN003
        posted.append(kwargs)
        if kwargs["kind"] == "tool_result":
            raise TaskFenceStaleError("被接管")
        return {"task_id": 7, "seq": 42}

    bridge = _make_bridge(runner=runner, appender=appender)
    try:
        await _take(bridge)
        await bridge._on_frame({"task_id": 7, "frame": "message", "message": _user_message(41)})
        await asyncio.gather(*list(bridge._turn_tasks))

        # 只有那条被拒的工具事件；最终回复没有落库（回合被 fence 中止）。
        assert [call["kind"] for call in posted] == ["tool_result"]
        assert bridge.run_for(7).inflight is False
    finally:
        await bridge.stop()