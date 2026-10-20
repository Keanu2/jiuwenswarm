"""MCP deadlines must include cleanup and preserve lifecycle task ownership."""

import asyncio
import time

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["healthy", "stalled", "raises"])
async def test_call_timeout_bounds_cleanup_and_invalidates_failed_session(monkeypatch, cleanup):
    from jiuwenswarm.common import mcp_call_timeout_patch as module
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import StreamableHttpClient
    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient
    from openjiuwen.core.runner.resources_manager.tool_manager import ToolMgr

    # Register the original attributes with monkeypatch before apply() replaces
    # them, so this test cannot leave a second layer of process-global wrappers.
    for cls in (StreamableHttpClient, SseClient):
        for name in ("call_tool", "list_tools"):
            monkeypatch.setattr(cls, name, getattr(cls, name))
    monkeypatch.setattr(ToolMgr, "_create_client", ToolMgr.__dict__["_create_client"])
    monkeypatch.setattr(module, "_PATCHED", False)
    monkeypatch.setattr(module, "_wrapped_methods", set())
    monkeypatch.setattr(module, "DISCONNECT_TIMEOUT", 0.03)
    module.apply_mcp_call_timeout_patch(default_timeout=0.02)

    class StalledSession:
        async def list_tools(self):
            await asyncio.Event().wait()

    owner = asyncio.current_task()
    disconnected = []

    async def disconnect():
        assert asyncio.current_task() is owner
        disconnected.append(True)
        if cleanup == "stalled":
            await asyncio.Event().wait()
        if cleanup == "raises":
            raise RuntimeError("cleanup failed")
        client._session = None

    client = StreamableHttpClient.__new__(StreamableHttpClient)
    client._session = StalledSession()
    client._server_path = "http://unused.invalid/mcp"
    client.disconnect = disconnect
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await client.list_tools()
    assert time.monotonic() - started < 1.0
    assert disconnected == [True]
    assert client._session is None
    # A dead connection fails promptly on the next call.
    with pytest.raises(RuntimeError, match="Not connected"):
        await client.list_tools()


@pytest.mark.parametrize("value", [None, 0, -1, True, "30", float("nan"), float("inf")])
def test_invalid_timeout_cannot_remove_deadline(value):
    from jiuwenswarm.common.mcp_call_timeout_patch import DEFAULT_CALL_TIMEOUT, resolve_mcp_timeout

    assert resolve_mcp_timeout(value) == DEFAULT_CALL_TIMEOUT
