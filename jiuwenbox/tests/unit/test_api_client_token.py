# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Test HTTP and MCP credential forwarding without contacting a service."""

from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.fixture
def client_modules(monkeypatch):
    result = []
    for filename in ("conftest.py", "test_mcp_default.py"):
        name = "auth_client_" + Path(filename).stem
        path = Path(__file__).parents[1] / "integration" / filename
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        result.append(module)
    return result


@pytest.mark.parametrize("endpoint", ["http://192.0.2.10:8321", "unix:///run/test.sock"])
@pytest.mark.parametrize("token", ["", "test-client-api-token"])
def test_api_clients_forward_only_explicit_token(client_modules, monkeypatch, endpoint, token):
    shared, mcp = client_modules
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", token)
    with shared._build_httpx_client(endpoint) as client:
        assert client.headers.get("Authorization") == (f"Bearer {token}" if token else None)
    fixture = mcp.api_client.__wrapped__(endpoint)
    try:
        client = next(fixture)
        assert client.headers.get("Authorization") == (f"Bearer {token}" if token else None)
    finally:
        fixture.close()


@pytest.mark.parametrize("endpoint", ["http://192.0.2.10:8321", "unix:///run/test.sock"])
@pytest.mark.parametrize("token", ["", "test-client-api-token"])
@pytest.mark.asyncio
async def test_mcp_stream_client_forwards_token(client_modules, monkeypatch, endpoint, token):
    _, mcp = client_modules
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", token)
    captured = []

    class StopBeforeConnect(Exception):
        pass

    @asynccontextmanager
    async def fake_stream(url, *, http_client):
        captured.append(http_client.headers.get("Authorization"))
        raise StopBeforeConnect
        yield  # pragma: no cover - establishes an async context manager

    monkeypatch.setattr(mcp, "streamable_http_client", fake_stream)
    with pytest.raises(StopBeforeConnect):
        async with mcp._mcp_session(endpoint):
            pytest.fail("A network connection must not be attempted")
    assert captured == [f"Bearer {token}" if token else None]
