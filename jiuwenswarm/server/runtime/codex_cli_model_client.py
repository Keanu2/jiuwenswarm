"""Codex CLI-backed ModelClient for local authenticated Codex sessions."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

from openjiuwen.core.common.exception.codes import StatusCode
from openjiuwen.core.common.exception.errors import build_error
from openjiuwen.core.foundation.llm import (
    AssistantMessage,
    AssistantMessageChunk,
    BaseMessage,
    BaseModelClient,
    ToolCall,
    UsageMetadata,
)
from openjiuwen.core.foundation.llm.output_parsers.output_parser import BaseOutputParser

from jiuwenswarm.common.file_sandbox import get_file_sandbox_root

try:
    from openai_codex import ApprovalMode, AsyncCodex, CodexConfig
    from openai_codex.generated.v2_all import ReasoningEffort
except ImportError as exc:  # pragma: no cover - exercised only when optional dep missing
    ApprovalMode = AsyncCodex = CodexConfig = ReasoningEffort = None  # type: ignore[assignment]
    _CODEX_IMPORT_ERROR: ImportError | None = exc
else:
    _CODEX_IMPORT_ERROR = None


_DEFAULT_CODEX_MODEL = "gpt-5.5"
_DEFAULT_TIMEOUT_S = 600.0
_TEXT_PART_TYPES = {"text", "input_text"}


class CodexCLIModelClient(BaseModelClient):
    """Route Jiuwen model calls through the local authenticated Codex CLI."""

    __client_name__ = ["CodexCLI"]

    def _get_client_name(self) -> str:
        return "Codex CLI client"

    def _validate_config(self) -> None:
        timeout = getattr(self.model_client_config, "timeout", None)
        if timeout is not None and float(timeout) <= 0:
            raise build_error(
                StatusCode.MODEL_SERVICE_CONFIG_ERROR,
                error_msg="model client config timeout must be greater than zero for CodexCLI.",
            )

    @staticmethod
    def _ensure_sdk_available() -> None:
        if _CODEX_IMPORT_ERROR is None:
            return
        raise build_error(
            StatusCode.MODEL_SERVICE_CONFIG_ERROR,
            error_msg=f"CodexCLI dependency is unavailable: {_CODEX_IMPORT_ERROR}",
        ) from _CODEX_IMPORT_ERROR

    def _resolve_model_name(self, explicit_model: str | None) -> str:
        return (
            explicit_model
            or getattr(self.model_config, "model_name", None)
            or os.getenv("CODEX_CLI_MODEL")
            or _DEFAULT_CODEX_MODEL
        )

    def _resolve_timeout(self, explicit_timeout: float | None) -> float:
        if explicit_timeout is not None:
            if explicit_timeout <= 0:
                raise build_error(
                    StatusCode.MODEL_INVOKE_PARAM_ERROR,
                    error_msg="CodexCLI timeout must be greater than zero.",
                )
            return explicit_timeout
        env_timeout = os.getenv("CODEX_CLI_TIMEOUT_S", "").strip()
        if env_timeout:
            try:
                timeout_value = float(env_timeout)
            except ValueError as exc:
                raise build_error(
                    StatusCode.MODEL_SERVICE_CONFIG_ERROR,
                    error_msg=f"invalid CODEX_CLI_TIMEOUT_S: {env_timeout!r}",
                ) from exc
            if timeout_value <= 0:
                raise build_error(
                    StatusCode.MODEL_SERVICE_CONFIG_ERROR,
                    error_msg="CODEX_CLI_TIMEOUT_S must be greater than zero.",
                )
            return timeout_value
        client_timeout = getattr(self.model_client_config, "timeout", None)
        if client_timeout is not None:
            return float(client_timeout)
        return _DEFAULT_TIMEOUT_S

    @staticmethod
    def _resolve_cwd() -> str:
        # Historical CODEX_CLI_CWD values may point at an entire source tree.
        # The model workspace is now fixed to one dedicated private directory.
        return str(get_file_sandbox_root())

    @staticmethod
    def _resolve_reasoning_effort() -> Any | None:
        if ReasoningEffort is None:
            return None
        value = os.getenv("CODEX_CLI_REASONING_EFFORT", "").strip()
        if not value:
            return None
        try:
            return ReasoningEffort(value)
        except Exception as exc:  # pragma: no cover - invalid env is operational config drift
            raise build_error(
                StatusCode.MODEL_SERVICE_CONFIG_ERROR,
                error_msg=f"unsupported CODEX_CLI_REASONING_EFFORT: {value!r}",
            ) from exc

    @staticmethod
    def _resolve_codex_home() -> str:
        configured = os.getenv("CODEX_CLI_HOME", "").strip()
        path = (
            Path(configured).expanduser().resolve(strict=False)
            if configured
            else (Path.home() / ".jiuwenswarm" / "codex-home").resolve(strict=False)
        )
        if not path.is_dir():
            raise build_error(
                StatusCode.MODEL_SERVICE_CONFIG_ERROR,
                error_msg=f"isolated Codex home is missing: {path}",
            )
        if not (path / "config.toml").is_file():
            raise build_error(
                StatusCode.MODEL_SERVICE_CONFIG_ERROR,
                error_msg=f"isolated Codex permission config is missing: {path / 'config.toml'}",
            )
        return str(path)

    @staticmethod
    def _coerce_text_content(content: Any, *, role: str) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if isinstance(item, dict):
                    item_type = str(item.get("type") or "").strip()
                    if item_type in _TEXT_PART_TYPES and isinstance(item.get("text"), str):
                        parts.append(item["text"])
                        continue
                raise build_error(
                    StatusCode.MODEL_INVOKE_PARAM_ERROR,
                    error_msg=f"CodexCLI does not support multimodal message content for role={role!r}.",
                )
            return "\n".join(part for part in parts if part)
        raise build_error(
            StatusCode.MODEL_INVOKE_PARAM_ERROR,
            error_msg=f"unsupported CodexCLI message content type for role={role!r}: {type(content).__name__}",
        )

    @classmethod
    def _render_message_block(cls, message: dict[str, Any]) -> str:
        role = str(message.get("role") or "user").strip() or "user"
        content = cls._coerce_text_content(message.get("content"), role=role)
        lines = [f"{role.upper()}:", content]
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            rendered_calls: list[str] = []
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if isinstance(function, dict):
                    name = function.get("name") or ""
                    arguments = function.get("arguments") or ""
                else:
                    name = tool_call.get("name") or ""
                    arguments = tool_call.get("arguments") or ""
                rendered_calls.append(f"- {name}: {arguments}")
            if rendered_calls:
                lines.extend(["Tool calls:", *rendered_calls])
        if role == "tool" and message.get("tool_call_id"):
            lines.append(f"tool_call_id={message['tool_call_id']}")
        return "\n".join(line for line in lines if line != "")

    @classmethod
    def _build_prompt(
        cls,
        messages: str | Sequence[BaseMessage] | Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | Sequence[Any] | None,
    ) -> str:
        if isinstance(messages, str):
            prompt = messages
        else:
            message_dicts = cls._convert_messages_to_dict(messages)
            prompt = "\n\n".join(cls._render_message_block(message) for message in message_dicts)
        tool_text = cls._render_tools_prompt(tools)
        if tool_text:
            prompt = f"{prompt.rstrip()}\n\n{tool_text}"
        return prompt.strip()

    @staticmethod
    def _render_tools_prompt(tools: Sequence[dict[str, Any]] | Sequence[Any] | None) -> str:
        if not tools:
            return ""
        tool_dicts = BaseModelClient._convert_tools_to_dict(list(tools))
        if not tool_dicts:
            return ""
        tools_payload = json.dumps(tool_dicts, ensure_ascii=False, indent=2)
        return (
            "AVAILABLE_TOOLS:\n"
            f"{tools_payload}\n\n"
            "If a tool is required, respond with strict JSON only:\n"
            '{"tool_calls":[{"name":"tool_name","arguments":{}}]}\n'
            "Otherwise answer normally."
        )

    @staticmethod
    def _strip_markdown_fences(text: str) -> str:
        stripped = text.strip()
        if not stripped.startswith("```"):
            return stripped
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
        return stripped

    @classmethod
    def _extract_tool_calls(cls, text: str) -> tuple[str, list[ToolCall] | None]:
        payload_text = cls._strip_markdown_fences(text)
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return text, None
        if not isinstance(payload, dict):
            return text, None
        raw_tool_calls = payload.get("tool_calls")
        if not isinstance(raw_tool_calls, list) or not raw_tool_calls:
            return text, None

        tool_calls: list[ToolCall] = []
        call_batch_id = uuid.uuid4().hex
        for index, item in enumerate(raw_tool_calls):
            if not isinstance(item, dict):
                return text, None
            name = item.get("name")
            arguments = item.get("arguments", {})
            if not isinstance(name, str) or not name.strip():
                return text, None
            if isinstance(arguments, str):
                arguments_text = arguments
            else:
                arguments_text = json.dumps(arguments, ensure_ascii=False)
            tool_calls.append(
                ToolCall(
                    id=f"codexcli-tool-{call_batch_id}-{index}",
                    type="function",
                    name=name.strip(),
                    arguments=arguments_text,
                    index=index,
                )
            )
        return "", tool_calls

    def _build_usage_metadata(self, result: Any, model_name: str) -> UsageMetadata | None:
        usage = getattr(result, "usage", None)
        if usage is None:
            return None
        totals = getattr(usage, "total", None)
        if totals is None:
            return None
        duration_ms = getattr(result, "duration_ms", None)
        latency = float(duration_ms) / 1000.0 if isinstance(duration_ms, (int, float)) else 0.0
        return UsageMetadata(
            model_name=model_name,
            total_latency=latency,
            input_tokens=int(getattr(totals, "input_tokens", 0) or 0),
            output_tokens=int(getattr(totals, "output_tokens", 0) or 0),
            total_tokens=int(getattr(totals, "total_tokens", 0) or 0),
            cache_tokens=int(getattr(totals, "cached_input_tokens", 0) or 0),
            reasoning_tokens=int(getattr(totals, "reasoning_output_tokens", 0) or 0),
        )

    async def _parse_assistant_message(
        self,
        *,
        result: Any,
        model_name: str,
        output_parser: BaseOutputParser | None,
    ) -> AssistantMessage:
        final_text = getattr(result, "final_response", None) or ""
        content, tool_calls = self._extract_tool_calls(final_text)
        parser_content = None
        if output_parser is not None and content:
            parser_content = await output_parser.parse(content)
        finish_reason = "tool_calls" if tool_calls else "stop"
        status = getattr(result, "status", None)
        if status is not None and str(getattr(status, "value", status)) != "completed":
            raise build_error(
                StatusCode.MODEL_CALL_FAILED,
                error_msg=f"CodexCLI turn did not complete successfully: {getattr(status, 'value', status)}",
            )
        return AssistantMessage(
            content=content,
            tool_calls=tool_calls,
            usage_metadata=self._build_usage_metadata(result, model_name),
            finish_reason=finish_reason,
            parser_content=parser_content,
        )

    async def _run_turn(self, *, prompt: str, model_name: str, timeout: float) -> Any:
        self._ensure_sdk_available()
        codex_bin = os.getenv("CODEX_CLI_BIN", "").strip() or None
        cwd = self._resolve_cwd()
        effort = self._resolve_reasoning_effort()
        config = CodexConfig(
            codex_bin=codex_bin,
            cwd=cwd,
            env={"CODEX_HOME": self._resolve_codex_home()},
        )

        async with AsyncCodex(config=config) as codex:
            thread = await codex.thread_start(
                approval_mode=ApprovalMode.deny_all,
                cwd=cwd,
                developer_instructions=(
                    f"All model-visible file reads and writes must remain under {cwd}. "
                    "Use the supplied Jiuwen tools for Registry discovery and A2A sending; "
                    "do not inspect legacy OpenClaw configuration."
                ),
                ephemeral=True,
                model=model_name,
            )
            try:
                return await asyncio.wait_for(
                    thread.run(
                        prompt,
                        approval_mode=ApprovalMode.deny_all,
                        cwd=cwd,
                        effort=effort,
                        model=model_name,
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError as exc:
                raise build_error(
                    StatusCode.MODEL_CALL_FAILED,
                    error_msg=f"CodexCLI turn timed out after {timeout}s",
                ) from exc

    async def invoke(
        self,
        messages: str | list[BaseMessage] | list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | list[Any] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        stop: str | None = None,
        output_parser: BaseOutputParser | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> AssistantMessage:
        del temperature, top_p, max_tokens, stop, kwargs
        model_name = self._resolve_model_name(model)
        prompt = self._build_prompt(messages, tools=tools)
        result = await self._run_turn(
            prompt=prompt,
            model_name=model_name,
            timeout=self._resolve_timeout(timeout),
        )
        return await self._parse_assistant_message(
            result=result,
            model_name=model_name,
            output_parser=output_parser,
        )

    async def stream(
        self,
        messages: str | list[BaseMessage] | list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | list[Any] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        stop: str | None = None,
        output_parser: BaseOutputParser | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ):
        assistant_message = await self.invoke(
            messages,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            model=model,
            max_tokens=max_tokens,
            stop=stop,
            output_parser=output_parser,
            timeout=timeout,
            **kwargs,
        )
        yield AssistantMessageChunk(
            content=assistant_message.content,
            tool_calls=assistant_message.tool_calls,
            usage_metadata=assistant_message.usage_metadata,
            finish_reason=assistant_message.finish_reason,
            parser_content=assistant_message.parser_content,
            reasoning_content=assistant_message.reasoning_content,
            prompt_token_ids=assistant_message.prompt_token_ids,
            completion_token_ids=assistant_message.completion_token_ids,
            logprobs=assistant_message.logprobs,
        )

    async def generate_image(self, messages: Iterable[Any], **kwargs: Any):  # pragma: no cover - simple guard
        del messages, kwargs
        raise build_error(
            StatusCode.MODEL_INVOKE_PARAM_ERROR,
            error_msg="CodexCLI does not support image generation.",
        )

    async def generate_speech(self, messages: Iterable[Any], **kwargs: Any):  # pragma: no cover - simple guard
        del messages, kwargs
        raise build_error(
            StatusCode.MODEL_INVOKE_PARAM_ERROR,
            error_msg="CodexCLI does not support speech generation.",
        )

    async def generate_video(self, messages: Iterable[Any], **kwargs: Any):  # pragma: no cover - simple guard
        del messages, kwargs
        raise build_error(
            StatusCode.MODEL_INVOKE_PARAM_ERROR,
            error_msg="CodexCLI does not support video generation.",
        )
