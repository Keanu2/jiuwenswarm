# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host-side A2A Registry tools for natural-language JiuwenSwarm requests."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import sys
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard, ToolExposure

from jiuwenswarm.common.file_sandbox import (
    describe_file_sandbox_dir,
    resolve_file_sandbox_path,
)


_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_SEND_TIMEOUT_SECONDS = 300.0
_DEFAULT_MAX_INLINE_FILE_BYTES = 50 * 1024 * 1024
_REGISTRY_CACHE_TTL_S = 8.0
_REGISTRY_CACHE: dict[str, Any] = {"ts": 0.0, "payload": None}
_PORTABLE_MIME_TYPES = {
    ".csv": "text/csv",
    ".json": "application/json",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
}


class A2ARegistryConfigError(ValueError):
    """Raised when the fixed Registry integration is not safely configured."""


def _sidecar_control_url() -> str:
    value = os.getenv("JIUWENSWARM_A2A_SIDECAR_CONTROL_URL", "").strip()
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise A2ARegistryConfigError(
            "JIUWENSWARM_A2A_SIDECAR_CONTROL_URL must use HTTP on loopback"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise A2ARegistryConfigError(
            "sidecar control URL must not contain credentials or query data"
        )
    if parsed.path.rstrip("/") != "/gateway/call":
        raise A2ARegistryConfigError("sidecar control URL path must be /gateway/call")
    return value


def _sidecar_control_token() -> str:
    token = os.getenv("JIUWENSWARM_A2A_SIDECAR_CONTROL_TOKEN", "").strip()
    if len(token) < 32:
        raise A2ARegistryConfigError(
            "JIUWENSWARM_A2A_SIDECAR_CONTROL_TOKEN must contain at least 32 characters"
        )
    return token


def _registry_dataset() -> str:
    value = os.getenv("JIUWENSWARM_A2A_REGISTRY_DATASET", "openclaw_devices").strip()
    if not value or len(value) > 128:
        raise A2ARegistryConfigError("invalid Registry dataset")
    return value


def _default_service_id() -> str:
    if sys.platform == "win32":
        return "JiuwenSwarm-Windows"
    if sys.platform == "darwin":
        return "JiuwenSwarm-Mac"
    return "JiuwenSwarm-Linux"


def _self_service_id() -> str:
    return os.getenv("JIUWENSWARM_A2A_SERVICE_ID", _default_service_id()).strip()


def _timeout_from_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    value = float(raw)
    if value <= 0:
        raise A2ARegistryConfigError(f"{name} must be greater than zero")
    return value


def _normalize_file_paths(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            return [stripped]
        if isinstance(decoded, list):
            return [str(item).strip() for item in decoded if str(item).strip()]
        return [str(decoded).strip()] if str(decoded).strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


async def _sidecar_call(method: str, params: dict[str, Any], *, timeout: float) -> Any:
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        response = await client.post(
            _sidecar_control_url(),
            headers={"authorization": f"Bearer {_sidecar_control_token()}"},
            json={"method": method, "params": params},
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Jiuwen A2A Tunnel control returned HTTP {response.status_code} with invalid JSON"
        ) from exc
    if not isinstance(body, dict):
        raise RuntimeError("Jiuwen A2A Tunnel control returned an unexpected response shape")
    if response.is_error or body.get("ok") is not True:
        detail = body.get("payload", body.get("error", body))
        raise RuntimeError(
            f"Jiuwen A2A Tunnel call {method} failed (HTTP {response.status_code}): "
            f"{json.dumps(detail, ensure_ascii=False)}"
        )
    return body.get("payload")


async def _get_registry_snapshot() -> dict[str, Any]:
    now = time.monotonic()
    cached = _REGISTRY_CACHE.get("payload")
    cached_ts = float(_REGISTRY_CACHE.get("ts") or 0.0)
    if isinstance(cached, dict) and now - cached_ts < _REGISTRY_CACHE_TTL_S:
        return cached
    timeout = _timeout_from_env("JIUWENSWARM_A2A_REGISTRY_TIMEOUT_S", _DEFAULT_TIMEOUT_SECONDS)
    payload = await _sidecar_call("a2a.registry.list", {}, timeout=timeout)
    if not isinstance(payload, dict) or not isinstance(payload.get("services"), list):
        raise RuntimeError("Jiuwen A2A Tunnel returned an unexpected Registry response shape")
    _REGISTRY_CACHE["ts"] = now
    _REGISTRY_CACHE["payload"] = payload
    return payload


async def a2a_registry_list(**_ignored: Any) -> str:
    """List registered services through Jiuwen's integrated A2A Tunnel sidecar."""
    snapshot = await _get_registry_snapshot()
    services = [item for item in snapshot["services"] if isinstance(item, dict)]
    normalized_services = []
    for item in services:
        card = item.get("agentCard") if isinstance(item.get("agentCard"), dict) else {}
        normalized_services.append(
            {
                "service_id": str(item.get("serviceId") or ""),
                "name": str(card.get("name") or item.get("serviceId") or ""),
                "description": str(card.get("description") or ""),
                "protocol_version": str(card.get("protocolVersion") or ""),
                "tunnel_device_id": str(item.get("tunnelDeviceId") or ""),
                "is_self": bool(item.get("isSelf")),
                "registry_status": item.get("status"),
            }
        )
    result = {
        "transport": "jiuwen-a2a-websocket-tunnel",
        "dataset": str(snapshot.get("dataset") or _registry_dataset()),
        "self_service_id": str(snapshot.get("selfServiceId") or _self_service_id()),
        "self_tunnel_device_id": str(snapshot.get("selfTunnelDeviceId") or ""),
        "registered_service_count": len(normalized_services),
        "services": normalized_services,
        "online_semantics": (
            "Registry rows are discovery records. A peer is confirmed reachable only when an "
            "A2A request succeeds through the WebSocket Tunnel."
        ),
    }
    return json.dumps(result, ensure_ascii=False)


def _file_part(path_text: str) -> dict[str, Any]:
    path = resolve_file_sandbox_path(path_text, must_exist=True, require_file=True)
    max_bytes = int(
        os.getenv(
            "JIUWENSWARM_A2A_MAX_INLINE_FILE_BYTES",
            str(_DEFAULT_MAX_INLINE_FILE_BYTES),
        )
    )
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"sandbox file exceeds A2A inline limit ({size} > {max_bytes}): {path}")
    mime_type = (
        _PORTABLE_MIME_TYPES.get(path.suffix.lower())
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )
    return {
        "kind": "file",
        "file": {
            "name": path.name,
            "mimeType": mime_type,
            "bytes": base64.b64encode(path.read_bytes()).decode("ascii"),
        },
    }


async def a2a_send_message(
    service_id: str,
    text: str = "",
    file_paths: Any = None,
    **_ignored: Any,
) -> str:
    """Send text and optional sandbox files through Jiuwen's A2A WebSocket Tunnel."""
    snapshot = await _get_registry_snapshot()
    known_ids = {
        str(item.get("serviceId") or "")
        for item in snapshot["services"]
        if isinstance(item, dict)
    }
    normalized_service_id = str(service_id or "").strip()
    if normalized_service_id not in known_ids:
        raise ValueError(
            f"service_id is not registered in dataset {_registry_dataset()!r}: "
            f"{normalized_service_id!r}"
        )

    parts: list[dict[str, Any]] = []
    if str(text or "").strip():
        parts.append({"kind": "text", "text": str(text).strip()})
    for path_text in _normalize_file_paths(file_paths):
        parts.append(_file_part(path_text))
    if not parts:
        raise ValueError("text or at least one sandbox file is required")

    timeout = _timeout_from_env(
        "JIUWENSWARM_A2A_SEND_TIMEOUT_S",
        _DEFAULT_SEND_TIMEOUT_SECONDS,
    )
    payload = await _sidecar_call(
        "a2a.send",
        {
            "peer": normalized_service_id,
            "message": {"role": "user", "parts": parts},
        },
        timeout=timeout,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Jiuwen A2A Tunnel returned an unexpected send response shape")
    body = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    result = body.get("result") if isinstance(body.get("result"), dict) else body
    status = result.get("status") if isinstance(result.get("status"), dict) else {}
    artifact_texts: list[str] = []
    for artifact in result.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        for part in artifact.get("parts") or []:
            if isinstance(part, dict) and part.get("kind") == "text":
                artifact_texts.append(str(part.get("text") or ""))
    state = str(status.get("state") or "")
    if state in {"failed", "rejected", "canceled"}:
        status_message = status.get("message") if isinstance(status.get("message"), dict) else {}
        failure_texts = [
            str(part.get("text") or "")
            for part in status_message.get("parts") or []
            if isinstance(part, dict) and part.get("kind") == "text"
        ]
        detail = "; ".join(text for text in failure_texts if text) or json.dumps(
            status,
            ensure_ascii=False,
        )
        raise RuntimeError(
            f"A2A peer {normalized_service_id!r} returned task state {state!r}: {detail}"
        )
    return json.dumps(
        {
            "transport": "jiuwen-a2a-websocket-tunnel",
            "service_id": normalized_service_id,
            "http_status": payload.get("statusCode"),
            "status": state,
            "artifact_texts": artifact_texts,
            "file_count": sum(1 for part in parts if part.get("kind") == "file"),
            "response": body,
        },
        ensure_ascii=False,
    )


class A2ARegistryToolkit:
    """Expose least-privilege Registry discovery and Tunnel sending tools."""

    @staticmethod
    def get_tools() -> list[Tool]:
        list_card = ToolCard(
            name="a2a_registry_list",
            description=(
                "通过 Jiuwen 已集成的 A2A Tunnel 查询注册中心当前服务记录。这是 direct 工具，直接调用，不要 tool_search。"
                "用户询问‘注册中心有什么设备’、‘哪些 Agent 在线’、‘发现设备/服务’时必须调用本工具，"
                "不要读取旧 OpenClaw 配置或自行 curl。"
            ),
            input_params={"type": "object", "properties": {}},
            exposure=ToolExposure.DIRECT,
        )
        sandbox = describe_file_sandbox_dir()
        send_card = ToolCard(
            name="a2a_send_message",
            description=(
                "跨设备发送消息或文件的 direct 工具。用户说传文件、发给另一台设备时直接调用，禁止 tool_search。"
                f"本机文件目录：{sandbox}。file_paths 填该目录下的真实文件名。"
                "用户给出完整文件名（含扩展名）时直接填。只描述了文件时，先 glob 这个目录对上真实文件名，不要猜。"
                "通过 Jiuwen 已集成的 A2A WebSocket Tunnel 向注册中心发现的 service_id 发送消息或文件。"
                "这是设备间传文件的唯一工具；不要用 send_file_to_user（那是给当前网页/IM 对话里的人投递的）。"
                "file_paths 必须位于该目录中，原样发送，不要转 HEIC/JPG。"
                "text 必须非空（对端拒绝 empty query）。请对端把文件发回来时，text 里写明本机 service_id，"
                "禁止只写「发给我」。问 HW-Phone1 / OpenClaw 时，让对方用 a2a_send_local_file（等同 a2a_send_file），"
                "不要写 a2a_send_message / file_paths / send_file_to_user（对方没有这些接口）。原文件、不转格式。"
                "问 JiuwenSwarm-* 时才写 a2a_send_message + file_paths。"
                "正文已给出 service_id 和路径时立刻调用，不要 glob/bash。"
                "范例：a2a_send_message(service_id=\"HW-Phone1\", "
                "text=\"请用 a2a_send_local_file（等同 a2a_send_file）把最新照片原文件发给 service_id=JiuwenSwarm-Windows。不要转格式。\")；"
                "带文件：a2a_send_message(service_id=\"JiuwenSwarm-HW-PC1\", "
                "text=\"发送文件：example.pptx，请查收。\", file_paths=[\"example.pptx\"])。"
            ),
            input_params={
                "type": "object",
                "properties": {
                    "service_id": {
                        "type": "string",
                        "description": "目标设备的 service_id。用户已经说出设备名时按原名填写，不必先调用别的工具。",
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "必填说明文字。请对端发文件时写明本机 service_id；"
                            "问 HW-Phone1 用 a2a_send_local_file，问 JiuwenSwarm 用 a2a_send_message。"
                            "不要只写「发给我」。"
                        ),
                    },
                    "file_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            f"本机文件目录 {sandbox} 下的真实文件名。只写文件名即可。"
                            "完整文件名直接填写。只描述了文件时先 glob 该目录，不要猜文件名。"
                        ),
                    },
                },
                "required": ["service_id"],
            },
            exposure=ToolExposure.DIRECT,
        )
        return [
            LocalFunction(card=list_card, func=a2a_registry_list),
            LocalFunction(card=send_card, func=a2a_send_message),
        ]


__all__ = [
    "A2ARegistryConfigError",
    "A2ARegistryToolkit",
    "a2a_registry_list",
    "a2a_send_message",
]
