"""Generic core lifecycle hooks adapted to desktop behavior reporting."""
import asyncio
import uuid

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.session import InteractiveInput
from openjiuwen.core.single_agent.interrupt import InterruptRequest
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.rails.interrupt.confirm_rail import ConfirmPayload
from openjiuwen.harness.rails.interrupt.interrupt_base import BaseInterruptRail

from jiuwenswarm.common.behavior_security import (
    ACTIVE_BEHAVIOR,
    PENDING_OUTPUT,
    SECURITY_NOTICE_TOOL,
    OutputStashEntry,
    _event_id,
    _risk_category,
    raw_event,
    tool_output_clarify_approved_note,
    tool_output_clarify_confirm_message,
    tool_output_clarify_rejected_instruction,
    tool_output_reject_instruction,
)

# 仅 tool_output_force_finish: true（回退配置）时作为强制收尾文案使用；
# 默认路径（2026-10-18 三态 UI）改用指令模板让模型重新规划
OUTPUT_BLOCK_MESSAGE = (
    "当前工具输出未通过安全检测，已终止本轮操作。"
    "请确认内容来源可信后清空上下文并重试。"
)


class BehaviorSecurityRail(DeepAgentRail):
    priority = 100

    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge

    async def before_invoke(self, ctx):
        if ctx.session is None:
            return
        key = "behavior_security.turn_id"
        turn = ctx.session.get_state(key)
        if not isinstance(getattr(ctx.inputs, "query", None), InteractiveInput) or not turn:
            turn = uuid.uuid4().hex
            ctx.session.update_state({key: turn})
        ctx.extra["behavior.turn_id"] = turn

    async def before_model_call(self, ctx):
        pairs = []
        for message in getattr(ctx.inputs, "messages", None) or []:
            role = message.get("role") if isinstance(message, dict) else getattr(message, "role", "")
            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
            if not isinstance(content, str):
                continue
            if role == "user":
                pairs.append({"text": content, "response": ""})
            elif role == "assistant" and pairs:
                pairs[-1]["response"] += content
        ctx.extra["behavior.dialogue_context"] = pairs

    def _resume_pending_output(self, ctx) -> bool:
        """toolOutput CLARIFY 确认卡的续跑短路（2026-10-18 二次迭代）。

        返回 True = 本次 before_tool_call 已被确认卡恢复语义接管（调用方直接 return）。
        输出在 clarify 当下已暂存（bridge.output_stash）且未进模型上下文；此处按用户答复：
        批准 → _skip_tool 原样拼回暂存输出（工具不重执行，防副作用双跑）+ 采纳尾注；
        拒绝 → _skip_tool 替换为屏蔽指令（输出永不进上下文）。
        标记存在但没有用户答复 = 陈旧标记（中断后未恢复直接开了新轮），清掉走正常流程。
        """
        tool_call_id = str(getattr(ctx.inputs.tool_call, "id", "") or "")
        if not tool_call_id or ctx.session is None:
            return False
        state_key = f"{PENDING_OUTPUT}:{tool_call_id}"
        pending_event_id = ctx.session.get_state(state_key)
        if not pending_event_id:
            return False
        ctx.session.update_state({state_key: None})
        user_input = BaseInterruptRail()._get_user_input(ctx, tool_call_id)
        if user_input is None:
            return False
        stashed = self.bridge.pop_output_stash(str(pending_event_id))
        from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
        answer = PermissionInterruptRail.parse_confirm_payload(user_input)
        if answer is not None and answer.approved and stashed is not None:
            note = tool_output_clarify_approved_note()
            result = stashed.tool_result
            if isinstance(result, str):
                result = result + note
            tool_msg = stashed.tool_msg
            if tool_msg is not None and hasattr(tool_msg, "content") and isinstance(tool_msg.content, str):
                tool_msg.content = tool_msg.content + note
            ctx.extra["_skip_tool"] = True
            ctx.inputs.tool_result = result
            ctx.inputs.tool_msg = tool_msg if tool_msg is not None else ToolMessage(
                content=str(result), tool_call_id=tool_call_id)
            return True
        tool_name = stashed.tool_name if stashed is not None else str(getattr(ctx.inputs, "tool_name", "") or "")
        category = stashed.category if stashed is not None else "安全风险"
        instruction = tool_output_clarify_rejected_instruction(tool_name, category)
        feedback = str(getattr(answer, "feedback", "") or "").strip() if answer is not None else ""
        if feedback:
            instruction += f"\n用户反馈：{feedback}"
        ctx.extra["_skip_tool"] = True
        ctx.inputs.tool_result = instruction
        ctx.inputs.tool_msg = ToolMessage(content=instruction, tool_call_id=tool_call_id)
        return True

    async def before_tool_call(self, ctx):
        if self._resume_pending_output(ctx):
            return
        ctx.extra["behavior.finished"] = False
        ctx.extra["behavior.policy_revision"] = self.bridge.policy_revision()
        task = asyncio.current_task()
        if task is not None:
            task.add_done_callback(lambda _: ctx.extra.__setitem__("behavior.finished", True))
        local = self.bridge.local_decisions.get(_event_id(ctx, "tool.before"))
        if local is not None:
            ctx.extra["behavior.local_permission"] = local
        ctx.extra["behavior.context_token"] = ACTIVE_BEHAVIOR.set((self.bridge, ctx, asyncio.get_running_loop()))
        # Desktop full-access mode still uses PermissionInterruptRail. Its
        # evaluated result drives report/authorize; never synthesize an ALLOW.

    async def after_tool_call(self, ctx):
        ctx.extra["behavior.finished"] = True
        token = ctx.extra.pop("behavior.context_token", None)
        if token is not None:
            ACTIVE_BEHAVIOR.reset(token)
        confirmation = ctx.extra.pop("behavior.install_confirmation", None)
        if confirmation:
            BaseInterruptRail()._raise_interrupt(ctx.inputs.tool_name, ctx.inputs.tool_call,
                InterruptRequest(message=confirmation, payload_schema=ConfirmPayload.to_schema(),
                                 metadata={"source": "permission_interrupt"}))
        # No output exists for a denied or interrupted tool.
        decision = ctx.extra.get("_interrupt_decision")
        if decision is not None and type(decision).__name__ != "ApproveResult":
            return
        if getattr(ctx.inputs, "execution_started", False):
            tool_name = str(getattr(ctx.inputs, "tool_name", "") or "")
            if tool_name == SECURITY_NOTICE_TOOL:
                # 安全提示工具自身不送审（防递归；其价值即通知宿主，送审只会白花时延）
                return
            event = raw_event(ctx, "tool.after")
            outcome = await self.bridge.inspect(event)
            if outcome.result.is_denied:
                # 三态 UI（2026-10-18）：输出屏蔽 + 指令模板（原输出不进模型上下文/历史，
                # 引导模型调 security_notice 补写横幅第二行并重新规划）；默认不再
                # request_force_finish——轮次继续，模型重规划（tool_output_force_finish
                # 可回退旧的强制收尾语义）。指令只带风险类别、不带 remind 原文
                #（防 answer 滑窗对宿主指令二次送审自我命中清屏）
                instruction = tool_output_reject_instruction(
                    tool_name, _risk_category(outcome.labels), event["eventId"])
                ctx.inputs.tool_result = instruction
                tool_msg = getattr(ctx.inputs, "tool_msg", None)
                if tool_msg is not None and hasattr(tool_msg, "content"):
                    tool_msg.content = instruction
                if self.bridge.output_force_finish_enabled():
                    ctx.request_force_finish({
                        "output": OUTPUT_BLOCK_MESSAGE,
                        "result_type": "answer",
                    })
            elif outcome.verdict == "clarify":
                # 疑似风险（2026-10-18 二次迭代，确定性确认卡替代模型自觉 ask_user）：
                # 输出暂存 + 打断点弹确认卡——用户确认前输出不进模型上下文；
                # resume 由 before_tool_call 的 _resume_pending_output 接管
                #（批准原样拼回、拒绝屏蔽，工具均不重执行）。
                # 横幅本体由宿主裁决当下直发（risk-notice），不依赖模型依从。
                category = _risk_category(outcome.labels)
                tool_call_id = str(getattr(ctx.inputs.tool_call, "id", "") or "")
                self.bridge.stash_output(event["eventId"], OutputStashEntry(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    category=category,
                    tool_result=ctx.inputs.tool_result,
                    tool_msg=getattr(ctx.inputs, "tool_msg", None),
                ))
                if ctx.session is not None and tool_call_id:
                    ctx.session.update_state({f"{PENDING_OUTPUT}:{tool_call_id}": event["eventId"]})
                BaseInterruptRail()._raise_interrupt(
                    tool_name, ctx.inputs.tool_call,
                    InterruptRequest(
                        message=tool_output_clarify_confirm_message(tool_name, category),
                        payload_schema=ConfirmPayload.to_schema(),
                        metadata={"source": "permission_interrupt"},
                    ))

    def uninit(self, agent):
        for task in self.bridge.tasks:
            task.cancel()
