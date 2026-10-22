# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime enforcement for model-visible file and shell tools."""

from __future__ import annotations

import json
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.file_sandbox import (
    FileSandboxViolation,
    get_file_sandbox_root,
    resolve_file_sandbox_path,
)


_BLOCKED_HOST_EXECUTION_TOOLS = frozenset(
    {
        "bash",
        "code",
        "create_terminal",
        "execute_cmd",
        "execute_cmd_background",
        "execute_cmd_stream",
        "mcp_exec_command",
        "powershell",
    }
)
_EXACT_PATH_KEYS = frozenset(
    {
        "abs_file_path_list",
        "cwd",
        "directory",
        "dir",
        "file",
        "file_path",
        "file_paths",
        "local_path",
        "output_path",
        "path",
        "paths",
        "project_dir",
        "save_path",
        "source_path",
        "target_path",
        "workspace",
        "workspace_dir",
    }
)


class FileSandboxRail(DeepAgentRail):
    """Deny host shell access and confine path-bearing tool arguments."""

    priority = 95

    @staticmethod
    def _looks_like_path_key(key: str) -> bool:
        normalized = key.strip().lower()
        return (
            normalized in _EXACT_PATH_KEYS
            or normalized.endswith("_path")
            or normalized.endswith("_paths")
            or normalized.endswith("_dir")
            or normalized.endswith("_directory")
        )

    @staticmethod
    def _is_non_file_reference(value: str) -> bool:
        lowered = value.strip().lower()
        return lowered.startswith(("http://", "https://", "data:", "ws://", "wss://"))

    @classmethod
    def _resolve_path_value(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped or cls._is_non_file_reference(stripped):
                return value
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    resolved = [cls._resolve_path_value(item) for item in parsed]
                    return json.dumps(resolved, ensure_ascii=False)
            return str(resolve_file_sandbox_path(stripped))
        if isinstance(value, list):
            return [cls._resolve_path_value(item) for item in value]
        if isinstance(value, tuple):
            return [cls._resolve_path_value(item) for item in value]
        return value

    @classmethod
    def _rewrite_path_arguments(cls, value: Any) -> Any:
        if isinstance(value, dict):
            rewritten: dict[str, Any] = {}
            for key, item in value.items():
                rewritten[key] = (
                    cls._resolve_path_value(item)
                    if cls._looks_like_path_key(str(key))
                    else cls._rewrite_path_arguments(item)
                )
            return rewritten
        if isinstance(value, list):
            return [cls._rewrite_path_arguments(item) for item in value]
        return value

    @staticmethod
    def _reject(ctx: AgentCallbackContext, message: str) -> None:
        tool_call = getattr(ctx.inputs, "tool_call", None)
        tool_call_id = getattr(tool_call, "id", "") or ""
        ctx.extra["_skip_tool"] = True
        ctx.inputs.tool_result = {"success": False, "error": message}
        ctx.inputs.tool_msg = ToolMessage(content=message, tool_call_id=tool_call_id)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        tool_name = str(getattr(ctx.inputs, "tool_name", "") or "")
        if tool_name in _BLOCKED_HOST_EXECUTION_TOOLS:
            self._reject(
                ctx,
                f"Tool '{tool_name}' is disabled. File operations are limited to "
                f"the JiuwenSwarm sandbox: {get_file_sandbox_root()}",
            )
            return

        tool_args = getattr(ctx.inputs, "tool_args", None)
        if not isinstance(tool_args, dict):
            return
        try:
            ctx.inputs.tool_args = self._rewrite_path_arguments(tool_args)
        except (FileSandboxViolation, OSError, ValueError) as exc:
            self._reject(ctx, f"File sandbox rejected tool '{tool_name}': {exc}")


__all__ = ["FileSandboxRail"]
