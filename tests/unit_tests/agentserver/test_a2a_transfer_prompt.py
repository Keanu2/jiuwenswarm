# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.tool import ToolExposure

from jiuwenswarm.agents.harness.common.prompt.prompt_builder import (
    LocalSectionName,
    _a2a_transfer_prompt,
    _output_prompt,
)
from jiuwenswarm.agents.harness.common.rails.response_prompt_rail import ResponsePromptRail
from jiuwenswarm.agents.harness.common.tools.a2a_registry_tools import A2ARegistryToolkit
from jiuwenswarm.agents.harness.common.tools.send_file_to_user import SendFileToolkit
from jiuwenswarm.common.file_sandbox import describe_file_sandbox_dir


def test_output_prompt_scopes_send_file_to_user_away_from_a2a():
    cn = _output_prompt("cn").content["cn"]
    en = _output_prompt("en").content["en"]
    assert "a2a_send_message" in cn
    assert "禁止 `send_file_to_user`" in cn
    assert "a2a_send_message" in en
    assert "Never use `send_file_to_user` for A2A peers" in en


def test_a2a_transfer_prompt_includes_self_id_and_examples():
    section = _a2a_transfer_prompt("cn", "JiuwenSwarm-Windows")
    text = section.content["cn"]
    assert section.name == LocalSectionName.A2A
    assert "JiuwenSwarm-Windows" in text
    assert "不要根据注册中心名单猜测" in text
    assert "禁止只写「发给我」" in text
    assert "当前渠道已经是 `a2a`" in text
    assert "禁止再调 `a2a_send_message`" in text
    assert "direct 工具" in text
    assert "第 2 条" in text
    assert "第 3 条" in text
    assert describe_file_sandbox_dir() in text
    assert "不要 glob" in text
    assert "先用 `glob` 只看" in text
    assert "不要猜文件名" in text
    assert "file_paths" in text
    assert "不要 `tool_search`，不要去搜" not in text
    assert "a2a_send_local_file" in text
    assert "等同 a2a_send_file" in text
    assert "请用 a2a_send_local_file" in text


def test_a2a_send_message_tool_card_forbids_send_file_to_user():
    tools = A2ARegistryToolkit.get_tools()
    send = next(tool for tool in tools if tool.card.name == "a2a_send_message")
    listed = next(tool for tool in tools if tool.card.name == "a2a_registry_list")
    assert "send_file_to_user" in send.card.description
    assert "empty query" in send.card.description
    assert "发给我" in send.card.description
    assert "禁止 tool_search" in send.card.description
    assert "a2a_send_local_file" in send.card.description
    sandbox = describe_file_sandbox_dir()
    assert sandbox in send.card.description
    assert sandbox in send.card.input_params["properties"]["file_paths"]["description"]
    assert send.card.exposure == ToolExposure.DIRECT
    assert listed.card.exposure == ToolExposure.DIRECT
    assert "exposure" in send.card.model_fields_set
    delivered = SendFileToolkit("request", "session", "web").get_tools()[0]
    summary = delivered.card.description[:120]
    assert "a2a_send_message" in summary
    assert "不要 tool_search" not in delivered.card.description


class _FakePromptBuilder:
    def __init__(self) -> None:
        self.language = "cn"
        self.sections = {}

    def add_section(self, section) -> None:
        self.sections[section.name] = section

    def remove_section(self, name: str) -> None:
        self.sections.pop(name, None)


@pytest.mark.asyncio
async def test_response_prompt_rail_injects_a2a_transfer_with_env_id(monkeypatch):
    monkeypatch.setenv("JIUWENSWARM_A2A_SERVICE_ID", "JiuwenSwarm-HW-PC1")
    rail = ResponsePromptRail()
    rail.system_prompt_builder = _FakePromptBuilder()

    await rail.before_model_call(SimpleNamespace(inputs={"channel": "a2a"}))

    assert LocalSectionName.A2A in rail.system_prompt_builder.sections
    text = rail.system_prompt_builder.sections[LocalSectionName.A2A].content["cn"]
    assert "JiuwenSwarm-HW-PC1" in text
