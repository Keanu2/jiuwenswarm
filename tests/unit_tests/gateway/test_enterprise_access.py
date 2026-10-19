"""Identity/Manager contract tests; these do not claim a deployed E2E run."""

import base64
import json

import httpx
import pytest
from fastapi import FastAPI, Request

from jiuwenswarm.gateway.channel_manager.web.enterprise_access import (
    EnterpriseAccess,
    EnterpriseAccessMiddleware,
)


@pytest.fixture
def setup(monkeypatch):
    from unittest.mock import AsyncMock
    from jiuwenswarm.gateway.channel_manager.web import enterprise_access

    monkeypatch.setattr(
        enterprise_access,
        "authorize_agent_identity",
        AsyncMock(return_value=("g", "b", "u")),
    )
    monkeypatch.setenv("GATEWAY_MANAGER_HTTP_URL", "http://manager")
    monkeypatch.setenv("GATEWAY_IDENTITY_HTTP_URL", "http://identity")
    monkeypatch.setenv("GATEWAY_INSTANCE_ID", "instance")
    context = {
        "user_id": "u",
        "group_id": "g",
        "bot_id": "b",
        "jiuwenclaw_id": "instance",
    }
    state = {"identity_status": 200, "contexts": [context], "calls": []}

    def upstream(request):
        state["calls"].append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "identity":
            return httpx.Response(state["identity_status"], json={"user_id": "u"})
        return httpx.Response(
            200, json={"code": 200, "data": {"contexts": state["contexts"]}}
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(upstream), **kw),
    )
    app = FastAPI()

    @app.get("/api/v1/workspace/tree")
    @app.get("/file-api/download")
    async def protected(request: Request):
        return {
            "identity": request.state.enterprise_identity,
            "user": request.headers.get("x-user-id"),
        }

    user_routes = list(app.routes)

    @app.post("/api/v1/model-templates")
    async def service_route():
        return {"service": True}

    app.add_middleware(
        EnterpriseAccessMiddleware,
        routes=user_routes,
        websocket_paths={"/ws"},
        access=EnterpriseAccess(),
    )
    return original, app, state


HEADERS = {
    "Authorization": "Bearer platform",
    "X-User-Id": "u",
    "X-Group-Id": "g",
    "X-Bot-Id": "b",
}


@pytest.mark.asyncio
async def test_verified_scope_and_revocation(setup):
    original, app, state = setup
    async with original(
        transport=httpx.ASGITransport(app), base_url="http://gateway"
    ) as client:
        response = await client.get("/api/v1/workspace/tree", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["identity"] == {
            "user_id": "u",
            "group_id": "g",
            "bot_id": "b",
        }
        assert state["calls"] == [
            ("identity", "Bearer platform"),
            ("manager", "Bearer platform"),
        ]
        state["contexts"] = []
        assert (
            await client.get("/api/v1/workspace/tree", headers=HEADERS)
        ).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [{"X-User-Id": "other"}, {"X-Group-Id": "other"}, {"X-Bot-Id": "other"}]
)
async def test_no_impersonation_or_cross_context(setup, change):
    original, app, _ = setup
    async with original(
        transport=httpx.ASGITransport(app), base_url="http://gateway"
    ) as client:
        assert (
            await client.get("/api/v1/workspace/tree", headers={**HEADERS, **change})
        ).status_code == 403
        assert (
            await client.get("/api/v1/workspace/tree?user_id=other", headers=HEADERS)
        ).status_code == 403


@pytest.mark.asyncio
async def test_cookie_download_resolves_authorized_context(setup):
    original, app, state = setup
    # Routing claims are untrusted here; the actual asset token is verified by AS.
    claims = base64.urlsafe_b64encode(
        json.dumps({"tenant_key": "g::b::u"}).encode()
    ).decode()
    async with original(
        transport=httpx.ASGITransport(app), base_url="http://gateway"
    ) as client:
        response = await client.get(
            f"/file-api/download?token={claims}.opaque",
            headers={"Cookie": "openjiuwen_access_token=platform"},
        )
        assert response.status_code == 200
        assert response.json()["user"] == "u"
        state["contexts"] = []
        assert (
            await client.get(
                f"/file-api/download?token={claims}.opaque", headers=HEADERS
            )
        ).status_code == 403


@pytest.mark.asyncio
async def test_identity_failure_and_service_route_separation(setup):
    original, app, state = setup
    async with original(
        transport=httpx.ASGITransport(app), base_url="http://gateway"
    ) as client:
        assert (await client.get("/api/v1/workspace/tree")).status_code == 401
        state["identity_status"] = 401
        assert (
            await client.get("/api/v1/workspace/tree", headers=HEADERS)
        ).status_code == 401
        state["identity_status"] = 503
        assert (
            await client.get("/api/v1/workspace/tree", headers=HEADERS)
        ).status_code == 503
        assert (await client.post("/api/v1/model-templates")).status_code == 200
