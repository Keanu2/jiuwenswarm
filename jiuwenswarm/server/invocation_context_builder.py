"""Build and adapt explicit invocation context at the AgentServer boundary."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from jiuwenswarm.common.invocation_context import TRACE_HEADER_EXPORTER_METADATA_KEY
from jiuwenswarm.common.invocation_context.codec import attach_invocation_context
from jiuwenswarm.common.invocation_context.models import (
    INVOCATION_CONTEXT_VERSION,
    InvocationContext,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.xiaoyi_invocation import (
    DESKTOP_TRACE_HEADER_EXPORTER_NAME,
    build_xiaoyi_invocation_extension,
    build_xiaoyi_trace_context,
)

logger = logging.getLogger(__name__)


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def build_invocation_context(request: AgentRequest) -> InvocationContext:
    """Construct one invocation identity from an incoming ``AgentRequest``.

    Platform-specific request parsing is delegated to its private adapter.
    """

    if not isinstance(request, AgentRequest):
        raise TypeError("request must be an AgentRequest")

    request_id = _first_text(request.request_id)
    if request_id is None:
        raise ValueError("request_id is required to build InvocationContext")
    channel_id = _first_text(request.channel_id)
    if channel_id is None:
        raise ValueError("channel_id is required to build InvocationContext")

    trace = build_xiaoyi_trace_context(request)
    metadata = build_xiaoyi_invocation_extension(request)
    if channel_id.lower() in {"xiaoyi", "a2a"}:
        from jiuwenswarm.common.permission_profile import normalize_permission_profile

        profile = normalize_permission_profile((request.metadata or {}).get("xiaoyi_permission_profile"))
        if profile is not None:
            metadata["xiaoyi_permission_profile"] = profile
    if trace is not None and TRACE_HEADER_EXPORTER_METADATA_KEY not in metadata:
        # 桌面/通用渠道：trace 已建立（客户端经 metadata.interaction_id 显式下发），
        # 但无 Xiaoyi 扩展、导出器名缺位——补登记 desktop 导出器。Team 边界
        # （team_manager._apply_trace_context）依该名把 trace 头注入各成员模型配置；
        # 缺位会让团队内模型调用丢 x-hag-trace-id，服务端按 trace 归集时整轮漏计。
        metadata = {
            **metadata,
            TRACE_HEADER_EXPORTER_METADATA_KEY: DESKTOP_TRACE_HEADER_EXPORTER_NAME,
        }
    context = InvocationContext(
        version=INVOCATION_CONTEXT_VERSION,
        invocation_id=f"inv_{uuid.uuid4().hex}",
        request_id=request_id,
        session_id=_first_text(request.session_id),
        channel_id=channel_id,
        chat_id=_first_text(request.chat_id),
        trace=trace,
        metadata=metadata,
    )
    logger.info(
        "[INVOCATION_CTX] BUILT invocation_id=%s request_id=%s session_id=%s channel_id=%s",
        context.invocation_id,
        context.request_id,
        context.session_id,
        context.channel_id,
    )
    return context


__all__ = [
    "attach_invocation_context",
    "build_invocation_context",
]
