import base64
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI

from jiuwenswarm.common.obs_files import AttachmentStore, object_prefix
from jiuwenswarm.gateway.channel_manager.web.enterprise_files import (
    register_enterprise_file_routes,
)


@pytest.mark.asyncio
async def test_upload_uses_verified_scope_and_sanitized_name(monkeypatch):
    app = FastAPI()

    @app.middleware("http")
    async def verified(request, call_next):
        request.state.enterprise_identity = {
            "group_id": "g",
            "bot_id": "b",
            "user_id": "u",
        }
        return await call_next(request)

    captured = []

    def upload(*args):
        captured.append(args)
        return {"url": "http://obs/signed", "name": args[1], "size": len(args[0])}

    monkeypatch.setattr(
        AttachmentStore, "from_env", lambda: SimpleNamespace(upload=upload)
    )
    register_enterprise_file_routes(app, SimpleNamespace())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/file-api/upload-obs",
            headers={
                "X-Group-Id": "untrusted",
                "X-Bot-Id": "untrusted",
                "X-User-Id": "untrusted",
            },
            json={
                "filename": "../中文.txt",
                "content_base64": base64.b64encode(b"hello").decode(),
            },
        )
        assert response.json()["ok"]
        assert captured == [(b"hello", "中文.txt", object_prefix("g", "b", "u"))]
        invalid = await client.post(
            "/file-api/upload-obs", json={"content_base64": "!"}
        )
        assert invalid.status_code == 400


@pytest.mark.asyncio
async def test_unwired_upload_fails_closed():
    app = FastAPI()
    register_enterprise_file_routes(app, SimpleNamespace())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        assert (await client.post("/file-api/upload-obs", json={})).status_code == 503


@pytest.mark.parametrize(
    "url",
    [
        "http://other/bucket/uploads/x/a",
        "http://obs/other/uploads/x/a",
        "http://obs/bucket/uploads/y/a",
        "http://obs/bucket/uploads/x/../a",
        "http://obs/bucket/uploads/x/%2e%2e/a",
        "http://obs/bucket/uploads/x/a#fragment",
    ],
)
def test_import_rejects_unconfigured_or_cross_scope_urls(url):
    store = AttachmentStore(None, "bucket", "http://obs")
    with pytest.raises(ValueError):
        store.read(url, "uploads/x/")


@pytest.mark.parametrize(
    "scope",
    ["own", "user", "group", "bot", "bucket", "origin", "download", "traversal"],
)
@pytest.mark.asyncio
async def test_upload_cleanup_is_restricted_to_verified_temporary_scope(
    monkeypatch, scope
):
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.enterprise_identity = {
            "group_id": "g",
            "bot_id": "b",
            "user_id": "u",
        }
        return await call_next(request)

    sdk = Mock()
    store = AttachmentStore(sdk, "bucket", "http://obs")
    monkeypatch.setattr(AttachmentStore, "from_env", lambda: store)
    prefix = object_prefix("g", "b", "u")
    key = f"{prefix}{'a' * 32}/中文.txt"
    url = f"http://obs/bucket/{key}?signature=test"
    if scope in {"user", "group", "bot"}:
        triple = {"group": "g", "bot": "b", "user": "u"}
        triple[scope] = "other"
        url = url.replace(
            prefix, object_prefix(triple["group"], triple["bot"], triple["user"])
        )
    elif scope == "bucket":
        url = url.replace("/bucket/", "/other/")
    elif scope == "origin":
        url = url.replace("http://obs/", "http://other/")
    elif scope == "download":
        url = url.replace("uploads/", "downloads/")
    elif scope == "traversal":
        url = url.replace("中文.txt", "%2e%2e/中文.txt")
    register_enterprise_file_routes(app, SimpleNamespace())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://gateway"
    ) as client:
        response = await client.request(
            "DELETE", "/file-api/upload-obs", json={"url": url}
        )
    assert response.status_code == (200 if scope == "own" else 400)
    if scope == "own":
        sdk.remove_object.assert_called_once_with("bucket", key)
    else:
        sdk.remove_object.assert_not_called()


@pytest.mark.asyncio
async def test_unwired_cleanup_fails_closed():
    app = FastAPI()
    register_enterprise_file_routes(app, SimpleNamespace())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://gateway"
    ) as client:
        assert (
            await client.request("DELETE", "/file-api/upload-obs", json={})
        ).status_code == 503
