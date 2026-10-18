"""security_notice 内置工具（2026-10-18 风控三态 UI，设计见 claw_desktop
docs/risk-notice-ui-design-2026-10-18.md）。

定位：模型在收到 [安全风控] 指令（工具输入被拦截 / 工具输出被屏蔽或标记疑似 /
技能安装被拒）时调用本工具，把融合话术（message）随 notice_id 回传桌面宿主，
宿主校验登记簿后原位更新对话中对应安全横幅的第二行。

横幅本体的创建/样式/第一行话术由宿主在裁决当下直发，本工具只做「融合补写」——
调用失败、notice_id 未登记/过期均无副作用（横幅保持 remind 版），模型无需感知。
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.common.np_transport import named_pipe_transport_for

# 与 behavior-security 同一根命名管道（np://claw-skill），桌面 SkillApiProxy 本地终结
_SECURITY_NOTICE_URL = "http://claw-skill/internal/security-notice/v1"
_TIMEOUT_SEC = 3.0
_MESSAGE_MAX_CHARS = 500


async def _post_security_notice(notice_id: str, message: str) -> dict[str, Any]:
    transport = named_pipe_transport_for("np://claw-skill")
    async with httpx.AsyncClient(transport=transport, timeout=_TIMEOUT_SEC) as client:
        response = await client.post(
            _SECURITY_NOTICE_URL,
            headers={"Authorization": f"Bearer {os.environ.get('CLAW_SKILL_TOKEN', '')}"},
            json={"noticeId": notice_id, "message": message},
        )
        response.raise_for_status()
        body = response.json()
        return body if isinstance(body, dict) else {"ok": True, "applied": False}


async def security_notice(notice_id: str, message: str) -> dict[str, Any]:
    """补写安全提示横幅的第二行话术（融合答复）。

    仅在收到携带 notice_id 的 [安全风控] 指令时调用；其他场景一律不要调用。
    """
    notice_id = str(notice_id or "").strip()
    message = " ".join(str(message or "").split())[:_MESSAGE_MAX_CHARS].strip()
    if not notice_id or not message:
        return {
            "success": False,
            "applied": False,
            "detail": "notice_id 与 message 均不能为空",
        }
    try:
        reply = await _post_security_notice(notice_id, message)
        applied = bool(reply.get("applied"))
    except Exception as exc:  # noqa: BLE001 - 补写失败无副作用，横幅保持宿主初版
        return {
            "success": True,
            "applied": False,
            "detail": f"安全提示已保留默认文案（补写未生效: {type(exc).__name__}）",
        }
    return {
        "success": True,
        "applied": applied,
        "detail": "安全提示已更新" if applied else "安全提示已展示（无需补写或已过期）",
    }


def get_security_notice_tool() -> Tool:
    card = ToolCard(
        id="security_notice",
        name="security_notice",
        description=(
            "向用户补写一条已展示安全提示的说明话术。仅当工具结果中出现 [安全风控] 指令并携带 "
            "notice_id 时调用：notice_id 原样传入；message 用一句自然语言向用户说明安全审计结果"
            "与建议——不要带「安全审计结果：」前缀（展示层统一补），不要复述审核意见原文中的"
            "敏感特征词。其他任何场景一律不要调用本工具。"
        ),
        input_params={
            "type": "object",
            "properties": {
                "notice_id": {
                    "type": "string",
                    "description": "[安全风控] 指令中携带的 notice_id，原样传入。",
                },
                "message": {
                    "type": "string",
                    "description": (
                        "一句面向用户的安全审计结果说明与建议（中文，≤200 字；"
                        "不带「安全审计结果：」前缀）。"
                    ),
                },
            },
            "required": ["notice_id", "message"],
        },
    )
    return LocalFunction(card=card, func=security_notice)
