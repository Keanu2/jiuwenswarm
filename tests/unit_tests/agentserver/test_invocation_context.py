from __future__ import annotations

import asyncio
from dataclasses import asdict, fields
from types import SimpleNamespace

import pytest
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.single_agent.schema.agent_card import AgentCard
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.schema.config import DeepAgentConfig
from openjiuwen.harness.schema.interaction import SendInputRequest

from jiuwenswarm.agents.harness.common.rails.invocation_context_rail import (
    InvocationContextRail,
    _extract_invocation_context,
)
from jiuwenswarm.common.invocation_context import (
    INVOCATION_CONTEXT_EXTRA_KEY,
    INVOCATION_CONTEXT_VERSION,
    TRACE_HEADER_EXPORTER_METADATA_KEY,
    InvocationContext,
    TraceContext,
    attach_invocation_context,
    get_current_invocation_context,
    invocation_context_from_dict,
    invocation_context_to_dict,
    trace_context_from_dict,
    trace_context_to_dict,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.invocation_context_builder import build_invocation_context
from jiuwenswarm.server.request_context import build_device_context_from_request
from jiuwenswarm.server.gui_rpc.client import build_gui_rpc_request
from jiuwenswarm.server.xiaoyi_invocation import (
    XIAOYI_INVOCATION_EXTENSION_KEY,
    XiaoyiInvocationExtension,
    build_xiaoyi_device_command_context,
)
from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools import utils as device_utils
from jiuwenswarm.agents.harness.common.tools.xiaoyi_phone_tools import (
    xiaoyi_gui_tool as gui_tool,
)


def _context(
    *,
    xiaoyi: XiaoyiInvocationExtension | None = None,
    trace: TraceContext | None = None,
) -> InvocationContext:
    return InvocationContext(
        version=INVOCATION_CONTEXT_VERSION,
        invocation_id="invocation-1",
        request_id="request-1",
        session_id="session-1",
        channel_id="xiaoyi" if xiaoyi is not None else "web",
        chat_id="chat-1",
        trace=trace,
        metadata=(
            {XIAOYI_INVOCATION_EXTENSION_KEY: asdict(xiaoyi)}
            if xiaoyi is not None
            else {}
        ),
    )


def test_invocation_context_round_trip_without_xiaoyi() -> None:
    context = _context()
    assert invocation_context_from_dict(invocation_context_to_dict(context)) == context


def test_public_invocation_context_does_not_define_xiaoyi_extension() -> None:
    context = _context()

    assert "xiaoyi" not in {field.name for field in fields(InvocationContext)}
    assert "xiaoyi" not in invocation_context_to_dict(context)


def test_invocation_context_round_trip_with_xiaoyi_and_unknown_optional_field() -> None:
    context = _context(
        xiaoyi=XiaoyiInvocationExtension(
            root_session_id="root",
            params_session_id="params",
            task_id="task",
            message_id="message",
            device_id="device",
            scheduled_device={"required_intents": ["CreateNote"]},
            cron={"job_id": "job", "run_id": "run"},
        )
    )
    payload = invocation_context_to_dict(context)
    payload["unknown_optional"] = {"ignored": True}
    assert invocation_context_from_dict(payload) == context


def test_invocation_context_round_trip_with_platform_neutral_trace() -> None:
    context = _context(
        trace=TraceContext(
            version=1,
            trace_id="root&19&abc&0",
            conversation_id="root",
            interaction_id="19",
        )
    )
    assert invocation_context_from_dict(invocation_context_to_dict(context)) == context


def test_trace_context_public_codec_round_trip() -> None:
    trace = TraceContext(
        version=1,
        trace_id="root&19&abc&0",
        conversation_id="root",
        interaction_id="19",
    )

    assert trace_context_from_dict(trace_context_to_dict(trace)) == trace


def test_builder_extracts_trace_from_xiaoyi_task_id() -> None:
    invocation = build_invocation_context(
        AgentRequest(
            request_id="request-1",
            channel_id="xiaoyi",
            metadata={"xiaoyi_task_id": "root&19&abc&0"},
        )
    )
    assert invocation.trace == TraceContext(
        version=1,
        trace_id="root&19&abc&0",
        conversation_id="root",
        interaction_id="19",
    )


def test_builder_prefers_cron_identity_for_trace() -> None:
    invocation = build_invocation_context(
        AgentRequest(
            request_id="cron-request-1",
            channel_id="__cron__",
            metadata={
                "xiaoyi_task_id": "stale-task",
                "cron": {"job_id": "job-1", "run_id": "run/1"},
            },
        )
    )
    assert invocation.trace == TraceContext(
        version=1,
        trace_id="cron_run%2F1",
        conversation_id="job-1",
        interaction_id="run%2F1",
    )


def test_builder_names_desktop_exporter_for_desktop_trace() -> None:
    """桌面渠道带 metadata.interaction_id：trace 建立且导出器名补登记为 desktop。

    回归守卫（2026-09-20 实测事故）：导出器名缺位时 Team 边界
    team_manager._apply_trace_context 查不到注册表项提前返回空，团队各成员
    模型调用丢 x-hag-trace-id，服务端按 trace 归集计费时整轮仅首调归账
    （16 次调用 15 次漏计，FINISH consumedPoints=0.01）。
    """
    invocation = build_invocation_context(
        AgentRequest(
            request_id="request-1",
            channel_id="desktop",
            session_id="session-1",
            metadata={"interaction_id": "8b9f57fc-1cdb-4625-97bf-9c0bf85e531c"},
        )
    )
    assert invocation.trace == TraceContext(
        version=1,
        trace_id="session-1&8b9f57fc",
        conversation_id="session-1",
        interaction_id="8b9f57fc-1cdb-4625-97bf-9c0bf85e531c",
    )
    assert invocation.metadata[TRACE_HEADER_EXPORTER_METADATA_KEY] == "desktop"


def test_builder_keeps_xiaoyi_exporter_name_for_xiaoyi_channel() -> None:
    invocation = build_invocation_context(
        AgentRequest(
            request_id="request-1",
            channel_id="xiaoyi",
            metadata={"xiaoyi_task_id": "root&19&abc&0"},
        )
    )
    assert invocation.metadata[TRACE_HEADER_EXPORTER_METADATA_KEY] == "xiaoyi"


def test_builder_skips_exporter_name_when_trace_absent() -> None:
    """无 trace 的桌面请求（客户端未下发 interaction_id）不补导出器名。"""
    invocation = build_invocation_context(
        AgentRequest(
            request_id="request-1",
            channel_id="desktop",
            session_id="session-1",
        )
    )
    assert invocation.trace is None
    assert TRACE_HEADER_EXPORTER_METADATA_KEY not in invocation.metadata


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"invocation_id": "i"}, "version"),
        ({"version": 2, "invocation_id": "i", "request_id": "r", "channel_id": "c"}, "unsupported"),
        ({"version": 1, "request_id": "r", "channel_id": "c"}, "invocation_id"),
        ({"version": 1, "invocation_id": "i", "channel_id": "c"}, "request_id"),
    ],
)
def test_invocation_context_codec_rejects_invalid_identity(payload, message) -> None:
    with pytest.raises(ValueError, match=message):
        invocation_context_from_dict(payload)


def test_attach_invocation_context_merges_existing_run_context() -> None:
    context = _context()
    inputs = {
        "query": "hello",
        "run": {
            "kind": "cron",
            "context": {"extra": {"raw_query": "x", "foo": "bar"}},
        },
    }
    attached = attach_invocation_context(inputs, context)
    assert attached["run"]["kind"] == "cron"
    assert attached["run"]["context"]["extra"]["raw_query"] == "x"
    payload = attached["run"]["context"]["extra"][INVOCATION_CONTEXT_EXTRA_KEY]
    assert invocation_context_from_dict(payload) == context
    assert INVOCATION_CONTEXT_EXTRA_KEY not in inputs["run"]["context"]["extra"]


def test_builder_and_device_adapter_preserve_routing_fields() -> None:
    request = AgentRequest(
        request_id="request-1",
        channel_id="xiaoyi",
        session_id="jiuwen-1",
        chat_id="root-1",
        params={"session_id": "params-1", "task_id": "task-1"},
        metadata={
            "xiaoyi_root_session_id": "root-override",
            "xiaoyi_params_session_id": "params-override",
            "xiaoyi_task_id": "task-override",
            "xiaoyi_rpc_id": "message-1",
            "xiaoyi_device_id": "device-1",
            "scheduled_device": {"required_intents": ["CreateNote"]},
            "cron": {"job_id": "job-1"},
            "app_id": "app-1",
            "binding_id": "binding-1",
            "ignored": "not copied",
        },
    )
    invocation = build_invocation_context(request)
    legacy = build_device_context_from_request(request)
    device = build_xiaoyi_device_command_context(invocation)
    assert device.source_request_id == legacy.source_request_id
    assert device.channel_id == legacy.channel_id
    assert device.jiuwen_session_id == legacy.jiuwen_session_id
    assert device.xiaoyi_root_session_id == legacy.xiaoyi_root_session_id
    assert device.xiaoyi_params_session_id == legacy.xiaoyi_params_session_id
    assert device.xiaoyi_task_id == legacy.xiaoyi_task_id
    assert device.xiaoyi_rpc_id == legacy.xiaoyi_rpc_id
    assert device.metadata == {
        "invocation_id": invocation.invocation_id,
        "app_id": "app-1",
        "binding_id": "binding-1",
        "scheduled_device": legacy.metadata["scheduled_device"],
        "cron": legacy.metadata["cron"],
    }


def test_gui_builder_uses_explicit_invocation() -> None:
    context = _context(
        xiaoyi=XiaoyiInvocationExtension(
            root_session_id="root",
            params_session_id="params",
            task_id="task",
            message_id="message",
            device_id="device",
        )
    )
    request = build_gui_rpc_request(query="open settings", invocation=context, timeout=30)
    assert request.source_request_id == "request-1"
    assert request.jiuwen_session_id == "session-1"
    assert request.xiaoyi_session_id == "root"
    assert request.xiaoyi_task_id == "task"
    assert request.xiaoyi_message_id == "message"
    assert request.device_id == "device"


@pytest.mark.asyncio
async def test_invocation_context_rail_nested_task_reset() -> None:
    outer = _context()
    inner = _context(
        xiaoyi=XiaoyiInvocationExtension(
            root_session_id="root",
            task_id="task",
            message_id="message",
        )
    )
    rail = InvocationContextRail()
    invoke_ctx = SimpleNamespace(
        inputs=SimpleNamespace(
            run_context=SimpleNamespace(
                extra={INVOCATION_CONTEXT_EXTRA_KEY: invocation_context_to_dict(outer)}
            )
        ),
        extra={},
    )
    task_ctx = SimpleNamespace(
        inputs={
            "run_context": {
                "extra": {INVOCATION_CONTEXT_EXTRA_KEY: invocation_context_to_dict(inner)}
            }
        },
        extra={},
    )
    await rail.before_invoke(invoke_ctx)
    assert get_current_invocation_context() == outer
    await rail.before_task_iteration(task_ctx)
    assert get_current_invocation_context() == inner
    await rail.after_task_iteration(task_ctx)
    assert get_current_invocation_context() == outer
    await rail.after_invoke(invoke_ctx)
    assert get_current_invocation_context() is None


@pytest.mark.asyncio
async def test_invocation_context_rail_isolated_for_concurrent_sessions() -> None:
    """ContextVar bindings must not bleed between concurrent persistent tasks."""

    async def _turn(request_id: str, task_id: str) -> tuple[str, str | None]:
        context = InvocationContext(
            version=INVOCATION_CONTEXT_VERSION,
            invocation_id=f"inv-{request_id}",
            request_id=request_id,
            session_id=f"session-{request_id}",
            channel_id="xiaoyi",
            chat_id=f"chat-{request_id}",
            metadata={
                XIAOYI_INVOCATION_EXTENSION_KEY: asdict(
                    XiaoyiInvocationExtension(
                        root_session_id=f"root-{request_id}",
                        params_session_id=f"params-{request_id}",
                        task_id=task_id,
                        message_id=f"message-{request_id}",
                        device_id=f"device-{request_id}",
                    )
                )
            },
        )
        rail = InvocationContextRail()
        callback = SimpleNamespace(
            inputs={
                "run_context": {
                    "extra": {
                        INVOCATION_CONTEXT_EXTRA_KEY: invocation_context_to_dict(context)
                    }
                }
            },
            extra={},
        )
        await rail.before_invoke(callback)
        # Yield while the sibling session binds its own context.  ContextVar
        # state remains local to each asyncio task.
        await asyncio.sleep(0)
        seen = get_current_invocation_context()
        await rail.after_invoke(callback)
        return request_id, seen.request_id if seen is not None else None

    results = await asyncio.gather(_turn("request-a", "task-a"), _turn("request-b", "task-b"))
    assert sorted(results) == [("request-a", "request-a"), ("request-b", "request-b")]
    assert get_current_invocation_context() is None


@pytest.mark.asyncio
async def test_invocation_context_rail_sequential_turns_leave_no_session_residue() -> None:
    rail = InvocationContextRail()

    async def _turn(request_id: str) -> None:
        context = InvocationContext(
            version=INVOCATION_CONTEXT_VERSION,
            invocation_id=f"inv-{request_id}",
            request_id=request_id,
            # Deliberately reuse one persistent session while changing the
            # invocation/request identity on each turn.
            session_id="session-shared",
            channel_id="web",
            chat_id=f"chat-{request_id}",
        )
        callback = SimpleNamespace(
            inputs=SimpleNamespace(
                run_context=SimpleNamespace(
                    extra={INVOCATION_CONTEXT_EXTRA_KEY: invocation_context_to_dict(context)}
                )
            ),
            extra={},
        )
        await rail.before_invoke(callback)
        assert get_current_invocation_context() == context
        await rail.after_invoke(callback)
        assert get_current_invocation_context() is None

    await _turn("request-a")
    await _turn("request-b")


@pytest.mark.asyncio
async def test_persistent_lifecycle_probe_rail_and_tool_binding(monkeypatch) -> None:
    """Probe rail/tool binding across a persistent-style attach/send sequence.

    This is intentionally a lightweight lifecycle probe; constructing a real
    openjiuwen ``start_interaction`` runner requires model/provider fixtures
    and is left to the integration suite.
    """

    context = _context(
        xiaoyi=XiaoyiInvocationExtension(
            root_session_id="root",
            params_session_id="params",
            task_id="task",
            message_id="message",
            device_id="device",
        )
    )
    attached = attach_invocation_context({"query": "device"}, context)
    run_context = attached["run"]["context"]
    rail = InvocationContextRail()
    invoke_callback = SimpleNamespace(
        inputs=SimpleNamespace(run_context=SimpleNamespace(extra=run_context["extra"])),
        extra={},
    )
    task_callback = SimpleNamespace(
        inputs={"run_context": {"extra": run_context["extra"]}},
        extra={},
    )
    calls: list[dict] = []

    class _Manager:
        async def call(self, *, intent_name, command, context, timeout):
            calls.append({
                "intent_name": intent_name,
                "command": command,
                "context": context,
                "timeout": timeout,
            })
            return SimpleNamespace(ok=True, result={"ok": True})

    monkeypatch.setattr(
        device_utils,
        "get_xiaoyi_device_reverse_rpc_client",
        lambda: _Manager(),
    )

    # Exercise the same hook order used by a persistent runner: the outer
    # invocation is bound once, attach_output starts a task iteration, and
    # send_input executes the Device Tool while that task-local binding is
    # active.
    await rail.before_invoke(invoke_callback)
    assert get_current_invocation_context() == context
    await rail.before_task_iteration(task_callback)
    assert get_current_invocation_context() == context
    result = await device_utils.execute_device_command("CreateNote", {"title": "hello"})
    assert result == {"ok": True}
    assert calls and calls[0]["context"].source_request_id == context.request_id
    assert calls[0]["context"].xiaoyi_task_id == "task"
    await rail.after_task_iteration(task_callback)
    assert get_current_invocation_context() == context
    await rail.after_invoke(invoke_callback)
    assert get_current_invocation_context() is None


@pytest.mark.asyncio
async def test_real_deep_agent_persistent_lifecycle_binds_tool_task_context() -> None:
    """Carry invocation data through the real persistent supervisor/task loop."""

    context = _context(
        xiaoyi=XiaoyiInvocationExtension(
            root_session_id="root",
            params_session_id="params",
            task_id="task",
            message_id="message",
            device_id="device",
        )
    )
    inputs = attach_invocation_context({"query": "probe context"}, context)
    card = AgentCard(id="invocation-context-probe", name="invocation-context-probe")
    rail = InvocationContextRail()
    observed: asyncio.Future[tuple[InvocationContext | None, int | None]] = (
        asyncio.get_running_loop().create_future()
    )

    class _ProbeReactAgent:
        async def register_callback(self, *args, **kwargs) -> None:
            return None

        async def invoke(self, effective, session, _streaming=False):
            if not observed.done():
                task = asyncio.current_task()
                observed.set_result(
                    (get_current_invocation_context(), id(task) if task else None)
                )
            return {"output": "context observed", "result_type": "answer"}

        async def write_invoke_result_to_stream(self, result, session) -> None:
            return None

    agent = DeepAgent(card).configure(
        DeepAgentConfig(
            card=card,
            enable_task_loop=True,
            completion_timeout=5.0,
            auto_create_workspace=False,
            rails=[rail],
        )
    )
    agent.set_react_agent(_ProbeReactAgent())
    await agent.ensure_initialized()
    session = create_agent_session(session_id="persistent-session", card=card)
    await session.pre_run(inputs={})

    request_task = asyncio.current_task()
    try:
        await agent.start(session=session)
        stream = await agent.attach_output()
        assert stream is not None
        await agent.send_input(
            SendInputRequest(request_id=context.request_id, inputs=inputs)
        )
        seen, tool_task_id = await asyncio.wait_for(observed, timeout=5.0)
        assert seen == context
        assert request_task is not None
        assert tool_task_id != id(request_task)
        await asyncio.wait_for(
            _drain_interaction_output(stream),
            timeout=5.0,
        )
    finally:
        await agent.stop()
        completion_rail = agent.find_rail_by_name("TaskCompletionRail")
        if completion_rail is not None:
            completion_rail.uninit(agent)
        await session.post_run()

    assert get_current_invocation_context() is None


async def _drain_interaction_output(stream) -> list[object]:
    return [item async for item in stream]


@pytest.mark.asyncio
async def test_real_persistent_worker_rebinds_request_permission_profiles(monkeypatch):
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.harness.rails.interrupt.interrupt_base import ApproveResult, InterruptResult
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import build_permission_rail

    monkeypatch.setenv("CLAW_BEHAVIOR_SECURITY", "0")
    stored = {"permissions": {"enabled": True, "package_builtin_rules": False, "tools": {"bash": "ask"}}}
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: stored)
    permission = build_permission_rail(stored)
    observed = asyncio.Queue()
    request_task = asyncio.current_task()

    class ProbeReactAgent:
        async def register_callback(self, *args, **kwargs):
            return None

        async def invoke(self, effective, session, _streaming=False):
            invocation = get_current_invocation_context()
            call = ToolCall(id="profile-tool", type="function", name="bash",
                            arguments='{"command":"profile-operation"}')
            callback = SimpleNamespace(
                session=session, extra={}, inputs=SimpleNamespace(tool_call=call, tool_name="bash", tool_args={}),
            )
            decision = await permission.resolve_interrupt(callback, call, None)
            observed.put_nowait((decision, invocation, asyncio.current_task()))
            return {"output": "permission observed", "result_type": "answer"}

        async def write_invoke_result_to_stream(self, result, session):
            return None

    card = AgentCard(id="permission-profile-worker", name="permission-profile-worker")
    agent = DeepAgent(card).configure(DeepAgentConfig(
        card=card, enable_task_loop=True, completion_timeout=5.0,
        auto_create_workspace=False, rails=[InvocationContextRail()],
    ))
    agent.set_react_agent(ProbeReactAgent())
    await agent.ensure_initialized()
    session = create_agent_session(session_id="permission-profile-session", card=card)
    await session.pre_run(inputs={})
    try:
        # Start before any request/profile exists: worker context cannot rely
        # on the request task's ContextVar snapshot.
        await agent.start(session=session)
        for index, profile in enumerate(("full_access", None, "default", "auto_approve", None)):
            request = AgentRequest(
                request_id=f"profile-turn-{index}", channel_id="xiaoyi", session_id="permission-profile-session",
                metadata={"xiaoyi_permission_profile": profile} if profile else {}, params={"query": "check"},
            )
            invocation = build_invocation_context(request)
            stream = await agent.attach_output()
            await agent.send_input(SendInputRequest(
                request_id=request.request_id, inputs=attach_invocation_context({"query": "check"}, invocation),
            ))
            decision, seen, worker = await asyncio.wait_for(observed.get(), 5.0)
            assert seen == invocation and worker is not request_task
            assert isinstance(decision, ApproveResult if profile in {"full_access", "auto_approve"} else InterruptResult)
            await asyncio.wait_for(_drain_interaction_output(stream), 5.0)
            assert stored["permissions"]["enabled"] is True
            assert stored["permissions"]["tools"]["bash"] == "ask"
    finally:
        await agent.stop()
        completion = agent.find_rail_by_name("TaskCompletionRail")
        if completion is not None:
            completion.uninit(agent)
        await session.post_run()
    assert get_current_invocation_context() is None


@pytest.mark.asyncio
async def test_real_deep_agent_second_turn_rebinds_invocation_context() -> None:
    """同一常驻会话的第二轮必须改绑到第二轮的 InvocationContext.

    桌面计费事故回归（2026-09-20）：DeepAgent 常驻运行时的 supervisor/round
    任务树在首轮创建，asyncio.create_task 只拷贝创建时刻的 contextvar 快照；
    没有 rail 逐轮改绑时，次轮模型调用的 x-hag-trace-id 冻结在首轮，服务端按
    trace 归集计费恒为 0。本用例走真实 start/attach_output/send_input 链路，
    验证 InvocationContextRail 在次轮把执行上下文改绑到次轮。
    """

    def _turn_context(tag: str) -> InvocationContext:
        return InvocationContext(
            version=INVOCATION_CONTEXT_VERSION,
            invocation_id=f"inv-{tag}",
            request_id=f"request-{tag}",
            session_id="session-shared",
            channel_id="desktop",
            chat_id=f"chat-{tag}",
            trace=TraceContext(
                version=1,
                trace_id=f"session-shared&{tag}",
                conversation_id="session-shared",
                interaction_id=tag,
            ),
            metadata={},
        )

    context_1 = _turn_context("turn1")
    context_2 = _turn_context("turn2")
    inputs_1 = attach_invocation_context({"query": "first"}, context_1)
    inputs_2 = attach_invocation_context({"query": "second"}, context_2)

    card = AgentCard(id="invocation-context-rebind", name="invocation-context-rebind")
    rail = InvocationContextRail()
    observed: asyncio.Queue[tuple[str, InvocationContext | None]] = asyncio.Queue()

    class _ProbeReactAgent:
        async def register_callback(self, *args, **kwargs) -> None:
            return None

        async def invoke(self, effective, session, _streaming=False):
            query = effective.get("query") if isinstance(effective, dict) else ""
            observed.put_nowait((str(query), get_current_invocation_context()))
            return {"output": "context observed", "result_type": "answer"}

        async def write_invoke_result_to_stream(self, result, session) -> None:
            return None

    agent = DeepAgent(card).configure(
        DeepAgentConfig(
            card=card,
            enable_task_loop=True,
            completion_timeout=5.0,
            auto_create_workspace=False,
            rails=[rail],
        )
    )
    agent.set_react_agent(_ProbeReactAgent())
    await agent.ensure_initialized()
    session = create_agent_session(session_id="rebind-session", card=card)
    await session.pre_run(inputs={})

    try:
        await agent.start(session=session)
        stream = await agent.attach_output()
        assert stream is not None
        await agent.send_input(
            SendInputRequest(request_id=context_1.request_id, inputs=inputs_1)
        )
        query_1, seen_1 = await asyncio.wait_for(observed.get(), timeout=5.0)
        assert query_1 == "first"
        assert seen_1 is not None and seen_1.request_id == "request-turn1"
        await asyncio.wait_for(_drain_interaction_output(stream), timeout=5.0)

        # 与桌面端行为一致：每轮重新 attach_output 领取读流租约（上一轮读流
        # 排空后租约释放，不重新 attach 的话 supervisor 会停在等消费者）。
        stream_2 = await agent.attach_output()
        assert stream_2 is not None
        await agent.send_input(
            SendInputRequest(request_id=context_2.request_id, inputs=inputs_2)
        )
        query_2, seen_2 = await asyncio.wait_for(observed.get(), timeout=5.0)
        assert query_2 == "second"
        # 核心断言：次轮在执行任务内读到的是次轮上下文，不是冻结的首轮。
        assert seen_2 is not None and seen_2.request_id == "request-turn2"
        assert seen_2.trace is not None and seen_2.trace.trace_id == "session-shared&turn2"
        await asyncio.wait_for(_drain_interaction_output(stream_2), timeout=5.0)
    finally:
        await agent.stop()
        completion_rail = agent.find_rail_by_name("TaskCompletionRail")
        if completion_rail is not None:
            completion_rail.uninit(agent)
        await session.post_run()

    assert get_current_invocation_context() is None


def test_instantiate_rails_always_mounts_invocation_context_rail_first() -> None:
    """_instantiate_rails 是全模式共享装配点：必须恒挂 InvocationContextRail 于首位.

    回归守护：code 模式（JiuwenSwarmCodeAdapter._build_agent_rails）曾独立声明
    rail 列表且漏挂本 rail，导致桌面对话次轮起计费 trace 冻结（2026-09-20）。
    挂载点收口到 _instantiate_rails 后，本用例锁定「agent/code 两条声明路径
    都必然带上该 rail、且只挂一次」。
    """
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._parent_session_id = "session-test"
    rails = adapter._instantiate_rails([], {})
    invocation_rails = [r for r in rails if isinstance(r, InvocationContextRail)]
    assert len(invocation_rails) == 1
    assert rails[0] is invocation_rails[0]
    assert adapter._invocation_context_rail is invocation_rails[0]


def test_extract_invocation_context_accepts_run_context_mapping() -> None:
    context = _context()
    extracted = _extract_invocation_context(
        {"extra": {INVOCATION_CONTEXT_EXTRA_KEY: invocation_context_to_dict(context)}}
    )
    assert extracted == context


@pytest.mark.asyncio
async def test_device_tool_fails_closed_without_invocation(monkeypatch) -> None:
    monkeypatch.setattr(device_utils, "get_current_invocation_context", lambda: None)

    with pytest.raises(RuntimeError, match="No active Jiuwen invocation context"):
        await device_utils.execute_device_command("CreateNote", {})


@pytest.mark.asyncio
async def test_gui_tool_fails_closed_without_invocation(monkeypatch) -> None:
    monkeypatch.setattr(gui_tool, "get_current_invocation_context", lambda: None)

    with pytest.raises(RuntimeError, match="INVALID_CONTEXT"):
        await gui_tool.xiaoyi_gui_agent.invoke({"query": "open settings"})
