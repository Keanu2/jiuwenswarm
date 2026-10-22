from __future__ import annotations

from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    ModelClientConfig,
    ModelRequestConfig,
)

from jiuwenswarm.server.runtime.codex_cli_model_client import CodexCLIModelClient


class _FakeParser:
    async def parse(self, text: str) -> dict[str, str]:
        return {"parsed": text.upper()}


class _FakeAsyncCodex:
    result = None
    thread_start_kwargs = None
    run_kwargs = None
    run_input = None
    init_config = None

    def __init__(self, config):
        type(self).init_config = config

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def thread_start(self, **kwargs):
        type(self).thread_start_kwargs = kwargs
        return _FakeThread()


class _FakeThread:
    async def run(self, input, **kwargs):
        _FakeAsyncCodex.run_input = input
        _FakeAsyncCodex.run_kwargs = kwargs
        return _FakeAsyncCodex.result


def _build_client() -> CodexCLIModelClient:
    return CodexCLIModelClient(
        model_config=ModelRequestConfig(model="configured-model"),
        model_client_config=ModelClientConfig(
            client_provider="CodexCLI",
            api_base="codex://local",
            api_key="codex-cli-auth",
            timeout=321.0,
        ),
    )


@pytest.fixture(autouse=True)
def _isolated_codex_paths(monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    codex_home = tmp_path / "codex-home"
    sandbox.mkdir()
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'default_permissions = "jiuwen-file-sandbox"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("JIUWENSWARM_FILE_SANDBOX_ROOT", str(sandbox))
    monkeypatch.setenv("CODEX_CLI_HOME", str(codex_home))
    return sandbox, codex_home


def _fake_turn_result(final_response: str, *, status: str = "completed"):
    totals = SimpleNamespace(
        input_tokens=12,
        output_tokens=7,
        total_tokens=19,
        cached_input_tokens=3,
        reasoning_output_tokens=2,
    )
    return SimpleNamespace(
        final_response=final_response,
        duration_ms=2500,
        usage=SimpleNamespace(total=totals),
        status=SimpleNamespace(value=status),
    )


@pytest.mark.asyncio
async def test_invoke_uses_codex_cli_defaults_and_maps_usage(monkeypatch, _isolated_codex_paths):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.codex_cli_model_client.AsyncCodex",
        _FakeAsyncCodex,
    )
    sandbox, codex_home = _isolated_codex_paths
    monkeypatch.setenv("CODEX_CLI_CWD", "/tmp/ignored-project")
    monkeypatch.setenv("CODEX_CLI_REASONING_EFFORT", "medium")
    _FakeAsyncCodex.result = _fake_turn_result("plain answer")

    client = _build_client()
    result = await client.invoke(
        [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": [{"type": "text", "text": "Summarize this repo."}]},
        ],
        timeout=12.5,
        output_parser=_FakeParser(),
    )

    assert isinstance(result, AssistantMessage)
    assert result.content == "plain answer"
    assert result.finish_reason == "stop"
    assert result.parser_content == {"parsed": "PLAIN ANSWER"}
    assert result.usage_metadata is not None
    assert result.usage_metadata.model_name == "configured-model"
    assert result.usage_metadata.input_tokens == 12
    assert result.usage_metadata.output_tokens == 7
    assert result.usage_metadata.total_tokens == 19
    assert result.usage_metadata.cache_tokens == 3
    assert result.usage_metadata.reasoning_tokens == 2
    assert result.usage_metadata.total_latency == 2.5
    assert _FakeAsyncCodex.thread_start_kwargs["approval_mode"].value == "deny_all"
    assert _FakeAsyncCodex.thread_start_kwargs["ephemeral"] is True
    assert _FakeAsyncCodex.thread_start_kwargs["model"] == "configured-model"
    assert _FakeAsyncCodex.thread_start_kwargs["cwd"] == str(sandbox)
    assert "sandbox" not in _FakeAsyncCodex.thread_start_kwargs
    assert _FakeAsyncCodex.run_kwargs["approval_mode"].value == "deny_all"
    assert _FakeAsyncCodex.run_kwargs["cwd"] == str(sandbox)
    assert "sandbox" not in _FakeAsyncCodex.run_kwargs
    assert _FakeAsyncCodex.run_kwargs["effort"].value == "medium"
    assert _FakeAsyncCodex.init_config.env["CODEX_HOME"] == str(codex_home)
    assert _FakeAsyncCodex.init_config.config_overrides == ()
    assert "SYSTEM:\nYou are helpful." in _FakeAsyncCodex.run_input
    assert "USER:\nSummarize this repo." in _FakeAsyncCodex.run_input


@pytest.mark.asyncio
async def test_invoke_parses_tool_call_json(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.codex_cli_model_client.AsyncCodex",
        _FakeAsyncCodex,
    )
    _FakeAsyncCodex.result = _fake_turn_result(
        '{"tool_calls":[{"name":"search_docs","arguments":{"query":"A2A"}}]}'
    )

    client = _build_client()
    result = await client.invoke(
        "Use a tool if needed.",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "search_docs",
                    "description": "Search local docs",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            }
        ],
    )

    assert result.content == ""
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls is not None
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id.startswith("codexcli-tool-")
    assert result.tool_calls[0].id.endswith("-0")
    assert result.tool_calls[0].type == "function"
    assert result.tool_calls[0].name == "search_docs"
    assert result.tool_calls[0].arguments == '{"query": "A2A"}'
    assert result.tool_calls[0].index == 0
    assert "AVAILABLE_TOOLS:" in _FakeAsyncCodex.run_input


@pytest.mark.asyncio
async def test_stream_returns_single_final_chunk(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.codex_cli_model_client.AsyncCodex",
        _FakeAsyncCodex,
    )
    _FakeAsyncCodex.result = _fake_turn_result("streamed answer")

    client = _build_client()
    chunks = [chunk async for chunk in client.stream("hello")]

    assert len(chunks) == 1
    assert chunks[0].content == "streamed answer"
    assert chunks[0].finish_reason == "stop"
    assert chunks[0].usage_metadata is not None
    assert chunks[0].usage_metadata.total_tokens == 19


@pytest.mark.asyncio
async def test_invoke_rejects_multimodal_content(monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.codex_cli_model_client.AsyncCodex",
        _FakeAsyncCodex,
    )
    _FakeAsyncCodex.result = _fake_turn_result("unused")

    client = _build_client()
    with pytest.raises(Exception, match="does not support multimodal"):
        await client.invoke(
            [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "file:///tmp/a.png"}}]}]
        )
