# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Product copy for permission prompts; command splitting is display-only."""

from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import Any

from openjiuwen.harness.security.permission_engine.approve.ask_presentation import (
    build_permission_ask_presentation,
    permission_intent_first_line,
    render_ask_presentation_message,
)
from openjiuwen.harness.security.permission_engine.models import (
    PermissionLevel,
    PermissionResult,
)
from openjiuwen.harness.security.permission_engine.toolguard.tool_categories import (
    is_shell_tool,
    shell_tools_from_config,
)

_CONTROL_FLOW_RE = re.compile(
    r"(?:^|[;&|\n])\s*(?:if|for|while|until|case|function|switch|try|do|else|elif|fi|done)\b",
    re.IGNORECASE,
)
_ACTION_LABELS = {
    "read": "读取",
    "write": "写入",
    "edit": "修改",
    "exec": "执行",
    "list": "列出",
    "access": "访问",
}
_ACTION_ALIASES = {"edit": "write", "list": "read"}


def _is_shell(tool_name: str, permission_config: dict | None = None) -> bool:
    return tool_name == "exec_command" or is_shell_tool(
        tool_name,
        shell_tools_from_config(permission_config),
    )


def permission_intent(tool_name: str, permission_config: dict | None = None) -> str:
    """Describe the requested action without coupling the title to a tool name."""
    return permission_intent_first_line(tool_name, permission_config)


def _command_lines(command: str, shell_type: str) -> list[str]:
    """Break top-level chains into display lines, retaining their operators.

    Quoted/escaped operators and redirections stay intact. Compound scripts,
    nesting, heredocs and unbalanced quotes retain the complete original text.
    This output must never be used for execution or authorization matching.
    """
    if _CONTROL_FLOW_RE.search(command) or "<<" in command or "<#" in command:
        return [command]
    escapes = {
        "bash": "\\",
        "sh": "\\",
        "cmd": "^",
        "powershell": "`",
        "pwsh": "`",
    }.get(
        shell_type,
        "^`" if os.name == "nt" else "\\",
    )
    quotes = '"' if shell_type == "cmd" else "\"'"
    if shell_type in {"bash", "sh"} or (shell_type == "auto" and os.name != "nt"):
        quotes += "`"
    quote = ""
    lines: list[str] = []
    start = 0
    i = 0
    while i < len(command):
        char = command[i]
        if char in escapes and (not quote or quote != "'"):
            i += 3 if command[i + 1 : i + 3] == "\r\n" else 2
            continue
        if quote:
            if char == quote:
                if (
                    quote == "'"
                    and shell_type in {"powershell", "pwsh"}
                    and command[i : i + 2] == "''"
                ):
                    i += 2
                    continue
                quote = ""
            i += 1
            continue
        if char in quotes:
            quote = char
            i += 1
            continue
        if char in "(){}[]" or (char == "#" and (i == 0 or command[i - 1].isspace())):
            return [command]
        if char not in "&|;\r\n":
            i += 1
            continue
        if char == ";" and shell_type == "cmd":
            i += 1
            continue
        # File descriptor duplication (2>&1) and &> are not chain operators.
        if char == "&" and (
            (i > 0 and command[i - 1] in "<>") or command[i + 1 : i + 2] == ">"
        ):
            i += 1
            continue
        if not command[start:i].strip():
            # PowerShell's leading invocation operator (& program) stays attached.
            i += 1
            continue
        width = 2 if command[i : i + 2] in {"&&", "||", "|&", "\r\n"} else 1
        end = i if char in "\r\n" else i + width
        lines.append(command[start:end].strip())
        start = i + width
        i = start
    if quote:
        return [command]
    if command[start:].strip():
        lines.append(command[start:].strip())
    return lines or [command]


def _code_block(text: str) -> str:
    # Commands containing Markdown fences must remain literal code.
    longest = max((len(m.group()) for m in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def _plain_text(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]<>#])", r"\\\1", " ".join(text.splitlines()))


def build_permission_prompt(
    tool_name: str,
    tool_args: dict[str, Any] | None,
    message: str,
    metadata: dict[str, Any] | None = None,
    permission_config: dict | None = None,
) -> tuple[str, str]:
    """Return a title/body while keeping the original approval request intact."""
    args = tool_args or {}
    metadata = metadata or {}
    title = permission_intent(tool_name, permission_config)
    first = message.lstrip("\ufeff").split("\n", 1)[0].strip()
    if first.startswith("**助手想要") and first.endswith("**"):
        title = first[2:-2].strip()

    # Core owns file-operation extraction and risk classification. On new
    # interrupts prefer its captured data so later conversion cannot change CWD.
    matched_rule = metadata.get("matched_rule")
    presentation = build_permission_ask_presentation(
        tool_name,
        args,
        PermissionResult(
            PermissionLevel.ASK,
            matched_rule=matched_rule if isinstance(matched_rule, str) else None,
        ),
        permission_config=permission_config,
    )
    risk_title = metadata.get("ask_title")
    risk_level = metadata.get("ask_risk_level")
    old_risk = re.search(r"风险等级[：:]\s*([高中低])风险", message)
    if not isinstance(risk_level, str) or risk_level not in {"高", "中", "低"}:
        risk_level = old_risk.group(1) if old_risk else presentation.risk_level
        if isinstance(risk_title, str) and "受保护的文件路径" in risk_title:
            risk_level = "高"
    operations = presentation.file_operations
    captured = metadata.get("file_operations")
    if isinstance(captured, list):
        operations = tuple(
            (op["path"], op["action"], str(op.get("source") or "tool_arg"))
            for op in captured
            if isinstance(op, dict)
            and isinstance(op.get("path"), str)
            and isinstance(op.get("action"), str)
        )
    # Use the same action contract for captured metadata and fresh extraction.
    # Older action names map to the canonical types rendered by core.
    operations = tuple(
        (path, _ACTION_ALIASES.get(action, action), source)
        for path, action, source in operations
        if action in _ACTION_LABELS
    )
    presentation = replace(
        presentation,
        intent=title,
        file_operations=operations,
        risk_level=risk_level,
        title=risk_title.strip()
        if isinstance(risk_title, str) and risk_title.strip()
        else presentation.title,
    )

    parts: list[str] = []
    command = args.get("command") or args.get("cmd")
    if (
        _is_shell(tool_name, permission_config)
        and isinstance(command, str)
        and command.strip()
    ):
        shell_type = str(args.get("shell_type") or "auto").lower()
        if tool_name in {"powershell", "core.powershell"}:
            shell_type = "powershell"
        lines = _command_lines(command, shell_type)
        parts.append(f"将运行以下 {len(lines)} 条指令，本次授权适用于整组指令。")
        workdir = metadata.get("workdir") or args.get("workdir")
        if isinstance(workdir, str) and workdir.strip():
            parts.append(f"工作目录：\n\n{_code_block(workdir)}")
        parts.append(
            f"待执行指令（保留原始连接关系）：\n\n{_code_block(chr(10).join(lines))}"
        )

    if (
        isinstance(metadata.get("ask_risk_level"), str)
        and metadata["ask_risk_level"] in {"高", "中", "低"}
        and first.startswith("**助手想要")
    ):
        # The complete core body is rendered once below; preserve its remember
        # hint without repeating the intent/file list/risk sections.
        hint_marker = "\n\n> 默认仅授权当前对象"
        _, marker, hint = message.partition(hint_marker)
        message = (marker + hint).strip() if marker else ""
    elif message.strip() == presentation.summary:
        message = ""
    elif message.strip().startswith(presentation.summary + "\n"):
        message = message.strip()[len(presentation.summary) :].strip()

    if message.strip():
        message = re.sub(
            rf"^({'|'.join(_ACTION_LABELS)}) (?=\S)",
            lambda match: _ACTION_LABELS[match.group(1)] + " ",
            message.strip(),
        )
    reason = metadata.get("ask_reason")
    if isinstance(reason, str) and reason.strip() and reason.strip() not in message:
        message = "\n\n".join(
            filter(None, [message, f"授权说明：{_plain_text(reason.strip())}"])
        )
    return title, render_ask_presentation_message(
        presentation,
        command_details="\n\n".join(parts),
        supplemental_text=message,
    ).strip()
