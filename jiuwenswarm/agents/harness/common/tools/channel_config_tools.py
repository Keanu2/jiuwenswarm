"""Model tool for applying third-party channel configuration in the Gateway."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any
from urllib.parse import urljoin

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.agents.harness.common.channel_runtime_context import (
    CURRENT_CHANNEL_ID,
    CURRENT_SESSION_ID,
)
from jiuwenswarm.common.channel_config_registry import (
    CONFIGURABLE_THIRD_PARTY_CHANNEL_ID_TEXT,
    normalize_configurable_channel_id,
)


def _gateway_control_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
    request_id = f"channel-control-{uuid.uuid4().hex}"
    request_params = dict(params)
    requester_channel_id = CURRENT_CHANNEL_ID.get().strip()
    requester_session_id = CURRENT_SESSION_ID.get().strip()
    if requester_channel_id or requester_session_id:
        request_params["requester"] = {
            "channel_id": requester_channel_id,
            "session_id": requester_session_id,
        }
    return {
        "type": "req",
        "id": request_id,
        "method": method,
        "params": request_params,
    }


def _gateway_control_url() -> str:
    configured = str(os.getenv("JIUWENSWARM_GATEWAY_CONTROL_URL") or "").strip()
    if configured:
        configured = configured.rstrip("/")
        if configured.endswith("/channel-config"):
            return configured
        return urljoin(configured + "/", "channel-config")

    host = str(os.getenv("GATEWAY_HOST", "127.0.0.1")).strip() or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = int(
        os.getenv("GATEWAY_PORT")
        or os.getenv("JIUWENSWARM_GATEWAY_PORT")
        or "19001"
    )
    return f"ws://{host}:{port}/channel-config"


def _gateway_control_pipe_credentials() -> tuple[str, str]:
    from jiuwenswarm.common.secrets_bootstrap import get_secret

    path = str(get_secret("pipes.cron", "") or "").strip()
    token = str(get_secret("e2aToken", "") or "").strip()
    if not path or not token:
        raise RuntimeError("Gateway control named pipe is not configured")
    return path, token


def _gateway_control_payload(response: dict[str, Any], request_id: str) -> dict[str, Any] | None:
    if response.get("type") != "res" or response.get("id") != request_id:
        return None
    if not response.get("ok"):
        raise RuntimeError(str(response.get("error") or "channel configuration failed"))
    payload = response.get("payload")
    return payload if isinstance(payload, dict) else {}


async def _request_gateway_control_pipe(
    request: dict[str, Any], path: str, token: str,
) -> dict[str, Any]:
    from jiuwenswarm.common.np_transport import open_pipe

    stream = await open_pipe(path, timeout=10)
    try:
        await stream.send_frame({"type": "auth", "token": token})
        await stream.recv_frame(timeout=10)
        await stream.send_frame(request)
        while True:
            response = await stream.recv_frame(timeout=30)
            if not isinstance(response, dict):
                continue
            payload = _gateway_control_payload(response, str(request["id"]))
            if payload is not None:
                return payload
    finally:
        await stream.close()


async def _request_gateway_control_websocket(request: dict[str, Any]) -> dict[str, Any]:
    import inspect
    import websockets
    from jiuwenswarm.common.security.web_identity import web_client_headers

    url = _gateway_control_url()
    headers = web_client_headers(url)
    header_key = "additional_headers" if "additional_headers" in inspect.signature(websockets.connect).parameters else "extra_headers"
    async with websockets.connect(url, open_timeout=10, close_timeout=5,
                                  **({header_key: headers} if headers else {})) as ws:
        await asyncio.wait_for(ws.recv(), timeout=10)
        await ws.send(json.dumps(request, ensure_ascii=False))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            response = json.loads(raw)
            if not isinstance(response, dict):
                continue
            payload = _gateway_control_payload(response, str(request["id"]))
            if payload is not None:
                return payload


async def _request_gateway_control(method: str, params: dict[str, Any]) -> dict[str, Any]:
    request = _gateway_control_request(method, params)
    try:
        credentials = _gateway_control_pipe_credentials()
    except RuntimeError:
        return await _request_gateway_control_websocket(request)
    return await _request_gateway_control_pipe(request, *credentials)


@tool(
    name="configure_channel",
    description=(
        "Configure and immediately reconnect a JiuwenSwarm third-party messaging channel. Use this "
        "only when the user explicitly asks to connect, modify, or disconnect a third-party channel; "
        "do not treat ordinary conversation as a configuration request. channel_id must be one of "
        f"{CONFIGURABLE_THIRD_PARTY_CHANNEL_ID_TEXT}. settings is the object to merge into the current "
        "configuration; common fields include enabled=true, app_id, app_secret, bot_token, client_id, "
        "client_secret, corp_id, agent_id, secret, and webhook_url. For WeChat QR login, use "
        "enabled=true and auto_login=true, then call get_wechat_login_status to retrieve the QR code. "
        "Do not call this tool just to query WeChat login status; pass refresh_qr=true only when the user "
        "explicitly asks to refresh or regenerate the QR code. After success, the target channel is "
        "applied in the Gateway. Do not generate a WeChat QR image with bash or Python."
    ),
)
async def configure_channel(channel_id: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Apply a channel configuration through the running Gateway."""
    return await _request_gateway_control(
        "channel.configure",
        {"channel_id": normalize_configurable_channel_id(channel_id), "settings": settings},
    )


@tool(
    name="get_wechat_login_status",
    description=(
        "Query the WeChat QR login status and current QR code. Use this when the user asks to bind WeChat, "
        "view the QR code, scan again, or confirm the login status. The qr field may be a url, data_url, "
        "encode value, or text; when kind=encode, show or relay value directly as the QR content for the "
        "user to scan. The third-party channel automatically delivers a QR image or link based on the "
        "current state; do not generate a QR image with bash or Python. This tool only queries status and "
        "does not refresh the QR code. If the QR code expires or the user explicitly asks to regenerate it, "
        "call configure_channel(channel_id='wechat', settings={'enabled': true, 'auto_login': true, "
        "'refresh_qr': true}) to trigger login again."
    ),
)
async def get_wechat_login_status() -> dict[str, Any]:
    """Return the current WeChat QR login state from the Gateway."""
    return await _request_gateway_control("wechat.login_status", {})
