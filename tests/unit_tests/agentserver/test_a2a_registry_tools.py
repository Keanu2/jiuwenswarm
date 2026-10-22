from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from jiuwenswarm.agents.harness.common.tools import a2a_registry_tools as registry_tools


@pytest.fixture
def configured(monkeypatch, tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("JIUWENSWARM_FILE_SANDBOX_ROOT", str(sandbox))
    monkeypatch.setenv("JIUWENSWARM_A2A_REGISTRY_DATASET", "openclaw_devices")
    monkeypatch.setenv("JIUWENSWARM_A2A_SERVICE_ID", "JiuwenSwarm-Mac")
    monkeypatch.setenv(
        "JIUWENSWARM_A2A_SIDECAR_CONTROL_URL",
        "http://127.0.0.1:19802/gateway/call",
    )
    monkeypatch.setenv(
        "JIUWENSWARM_A2A_SIDECAR_CONTROL_TOKEN",
        "test-sidecar-token-that-is-long-enough",
    )
    registry_tools._REGISTRY_CACHE["ts"] = 0.0
    registry_tools._REGISTRY_CACHE["payload"] = None
    return sandbox


def _install_mock_client(monkeypatch, handler):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def factory(**kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(registry_tools.httpx, "AsyncClient", factory)


def _registry_payload() -> dict:
    return {
        "ok": True,
        "payload": {
            "ok": True,
            "dataset": "openclaw_devices",
            "selfServiceId": "JiuwenSwarm-Mac",
            "selfTunnelDeviceId": "JiuwenSwarm-Mac",
            "count": 2,
            "services": [
                {
                    "serviceId": "JiuwenSwarm-Mac",
                    "tunnelDeviceId": "JiuwenSwarm-Mac",
                    "isSelf": True,
                    "status": "active",
                    "agentCard": {"name": "JiuwenSwarm Mac", "protocolVersion": "0.3.0"},
                },
                {
                    "serviceId": "HW-Phone1",
                    "tunnelDeviceId": "hw-phone",
                    "isSelf": False,
                    "status": "active",
                    "agentCard": {"name": "HW Phone"},
                },
            ],
        },
    }


@pytest.mark.asyncio
async def test_registry_list_uses_authenticated_sidecar_tunnel(monkeypatch, configured: Path):
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["authorization"] = request.headers.get("authorization")
        observed["json"] = json.loads(request.content)
        return httpx.Response(200, json=_registry_payload())

    _install_mock_client(monkeypatch, handler)
    result = json.loads(await registry_tools.a2a_registry_list())

    assert observed["path"] == "/gateway/call"
    assert observed["authorization"].startswith("Bearer ")
    assert observed["json"] == {"method": "a2a.registry.list", "params": {}}
    assert result["transport"] == "jiuwen-a2a-websocket-tunnel"
    assert result["registered_service_count"] == 2
    assert result["services"][0]["is_self"] is True


@pytest.mark.asyncio
async def test_a2a_send_uses_tunnel_and_accepts_only_sandbox_files(
    monkeypatch,
    configured: Path,
):
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        observed.append(body)
        if body["method"] == "a2a.registry.list":
            return httpx.Response(200, json=_registry_payload())
        return httpx.Response(
            200,
            json={
                "ok": True,
                "payload": {
                    "statusCode": 200,
                    "response": {
                        "jsonrpc": "2.0",
                        "result": {
                            "status": {"state": "completed"},
                            "artifacts": [{"parts": [{"kind": "text", "text": "ok"}]}],
                        },
                    },
                },
            },
        )

    _install_mock_client(monkeypatch, handler)
    file_path = configured / "payload.txt"
    file_path.write_text("payload", encoding="utf-8")

    result = json.loads(
        await registry_tools.a2a_send_message(
            service_id="HW-Phone1",
            text="hello",
            file_paths=[str(file_path)],
        )
    )

    send_call = observed[1]
    assert send_call["method"] == "a2a.send"
    assert send_call["params"]["peer"] == "HW-Phone1"
    assert send_call["params"]["message"]["parts"][1]["kind"] == "file"
    assert send_call["params"]["message"]["parts"][1]["file"]["mimeType"] == "text/plain"
    assert result["transport"] == "jiuwen-a2a-websocket-tunnel"
    assert result["status"] == "completed"
    assert result["file_count"] == 1

    outside = configured.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(PermissionError):
        await registry_tools.a2a_send_message(
            service_id="HW-Phone1",
            file_paths=[str(outside)],
        )


@pytest.mark.asyncio
async def test_a2a_send_preserves_target_tunnel_error(monkeypatch, configured: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "a2a.registry.list":
            return httpx.Response(200, json=_registry_payload())
        return httpx.Response(
            502,
            json={
                "ok": False,
                "payload": {
                    "statusCode": 401,
                    "response": {"error": "missing peer authorization"},
                },
            },
        )

    _install_mock_client(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="missing peer authorization"):
        await registry_tools.a2a_send_message(service_id="HW-Phone1", text="hello")


@pytest.mark.asyncio
async def test_a2a_send_raises_for_failed_a2a_task(monkeypatch, configured: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "a2a.registry.list":
            return httpx.Response(200, json=_registry_payload())
        return httpx.Response(
            200,
            json={
                "ok": True,
                "payload": {
                    "statusCode": 200,
                    "response": {
                        "result": {
                            "status": {
                                "state": "failed",
                                "message": {
                                    "parts": [
                                        {
                                            "kind": "text",
                                            "text": "MIME type rejected: application/octet-stream",
                                        }
                                    ]
                                },
                            }
                        }
                    },
                },
            },
        )

    _install_mock_client(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="MIME type rejected"):
        await registry_tools.a2a_send_message(service_id="HW-Phone1", text="hello")


def test_markdown_mime_type_is_portable_across_macos(monkeypatch, configured: Path):
    file_path = configured / "测试.md"
    file_path.write_text("# 测试\n", encoding="utf-8")
    monkeypatch.setattr(registry_tools.mimetypes, "guess_type", lambda _name: (None, None))

    part = registry_tools._file_part(str(file_path))

    assert part["file"]["mimeType"] == "text/markdown"


def test_default_service_id_follows_platform(monkeypatch):
    monkeypatch.delenv("JIUWENSWARM_A2A_SERVICE_ID", raising=False)
    import sys

    if sys.platform == "win32":
        assert registry_tools._self_service_id() == "JiuwenSwarm-Windows"
    elif sys.platform == "darwin":
        assert registry_tools._self_service_id() == "JiuwenSwarm-Mac"
    else:
        assert registry_tools._self_service_id() == "JiuwenSwarm-Linux"


def test_default_inline_file_limit_is_50mb():
    assert registry_tools._DEFAULT_MAX_INLINE_FILE_BYTES == 50 * 1024 * 1024
