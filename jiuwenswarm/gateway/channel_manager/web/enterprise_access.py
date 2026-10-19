# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Identity authenticates the caller; Manager authorizes the requested context."""

from __future__ import annotations

import os

import httpx
from fastapi import HTTPException
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.routing import Match

from jiuwenswarm.common.utils import build_tenant_key
from jiuwenswarm.extensions.runtime_management_extension.agent_authorization import (
    authorize_agent_identity,
)
from jiuwenswarm.extensions.runtime_management_extension.session_route_client import (
    FatalRouteError,
)


class EnterpriseAccess:
    def __init__(self) -> None:
        self.manager = os.getenv("GATEWAY_MANAGER_HTTP_URL", "").rstrip("/")
        self.identity = os.getenv("GATEWAY_IDENTITY_HTTP_URL", "").rstrip("/")

    async def authorize(self, connection: HTTPConnection) -> dict[str, str]:
        if not all((self.manager, self.identity)):
            raise HTTPException(503, "enterprise identity configuration missing")
        authorization = connection.headers.get("authorization", "")
        if not authorization:
            token = connection.cookies.get("openjiuwen_access_token", "")
            authorization = f"Bearer {token}" if token else ""
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "Not authenticated")
        headers = {"Authorization": authorization}
        try:
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=False, trust_env=False
            ) as client:
                me = await client.get(f"{self.identity}/v1/auth/me", headers=headers)
                if me.status_code in {401, 403}:
                    raise HTTPException(401, "Not authenticated")
                me.raise_for_status()
                user_id = str(me.json().get("user_id") or "")
                contexts = await client.get(
                    f"{self.manager}/api/v1/user-console/agent-contexts",
                    headers=headers,
                )
                if contexts.status_code in {401, 403}:
                    raise HTTPException(contexts.status_code, "context not admitted")
                contexts.raise_for_status()
                body = contexts.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise HTTPException(503, "identity or manager unavailable") from exc
        if not user_id or not isinstance(body, dict) or body.get("code") != 200:
            raise HTTPException(503, "invalid identity or manager response")
        declared = {
            key: connection.headers.get(f"x-{key.replace('_', '-')}")
            or connection.query_params.get(key, "")
            for key in ("user_id", "group_id", "bot_id")
        }
        if declared["user_id"] and declared["user_id"] != user_id:
            # debug_context restores only the UI; no server-side impersonation.
            raise HTTPException(403, "user mismatch")
        token_tenant = None
        if connection.url.path == "/file-api/download":
            from jiuwenswarm.gateway.channel_manager.web.container_file_http import (
                _decode_download_token_payload,
            )

            token = connection.query_params.get("token", "")
            payload = (
                _decode_download_token_payload(token) if len(token) <= 16384 else None
            )
            token_tenant = payload.get("tenant_key") if payload else None
            if not isinstance(token_tenant, str) or not token_tenant:
                raise HTTPException(403, "invalid download context")
        data = body.get("data")
        allowed = data.get("contexts", []) if isinstance(data, dict) else []
        if not isinstance(allowed, list):
            raise HTTPException(503, "invalid manager contexts")
        for context in allowed:
            if not isinstance(context, dict):
                continue
            candidate = {key: str(context.get(key) or "") for key in declared}
            if candidate["user_id"] != user_id:
                continue
            active_instance = connection.cookies.get("jiuwenclaw_id")
            if active_instance and context.get("jiuwenclaw_id") != active_instance:
                continue
            if not all(candidate.values()):
                continue
            if token_tenant:
                if (
                    build_tenant_key(
                        candidate["group_id"], candidate["bot_id"], user_id
                    )
                    != token_tenant
                ):
                    continue
            elif any(declared[key] != candidate[key] for key in ("group_id", "bot_id")):
                continue
            if any(
                connection.query_params.get(key, value) != value
                for key, value in candidate.items()
            ):
                raise HTTPException(403, "query context mismatch")
            try:
                # GATEWAY_INSTANCE_ID identifies a replica, not a Manager
                # instance. The local resource repository is the instance boundary.
                await authorize_agent_identity(
                    candidate["group_id"], candidate["bot_id"], user_id
                )
            except FatalRouteError as exc:
                raise HTTPException(403, "Agent access denied") from exc
            except Exception as exc:
                raise HTTPException(503, "Agent admission unavailable") from exc
            return candidate
        raise HTTPException(403, "context not admitted")


class EnterpriseAccessMiddleware:
    """Guard user routes only; Manager's service-authenticated config routes stay separate."""

    def __init__(self, app, *, routes, websocket_paths, access=None) -> None:
        self.app = app
        self.routes = routes
        self.websocket_paths = websocket_paths
        self.access = access or EnterpriseAccess()

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        protected = (scope["type"] == "websocket" and path in self.websocket_paths) or (
            scope["type"] == "http"
            and path != "/api/v1/health"
            and (
                path.startswith("/file-api/")
                or any(route.matches(scope)[0] == Match.FULL for route in self.routes)
            )
        )
        if protected:
            try:
                identity = await self.access.authorize(HTTPConnection(scope))
            except HTTPException as exc:
                if scope["type"] == "websocket":
                    await send(
                        {
                            "type": "websocket.close",
                            "code": 4401 if exc.status_code == 401 else 4403,
                        }
                    )
                else:
                    await JSONResponse(
                        {
                            "ok": False,
                            "error": {
                                "code": "UNAUTHORIZED"
                                if exc.status_code == 401
                                else "FORBIDDEN"
                                if exc.status_code == 403
                                else "SERVICE_UNAVAILABLE",
                                "message": exc.detail,
                            },
                        },
                        status_code=exc.status_code,
                        headers={"Cache-Control": "no-store"},
                    )(scope, receive, send)
                return
            scope.setdefault("state", {})["enterprise_identity"] = identity
            replacements = {
                f"x-{key.replace('_', '-')}".encode(): value.encode()
                for key, value in identity.items()
            }
            scope["headers"] = [
                (key, value)
                for key, value in scope["headers"]
                if key.lower() not in replacements
            ] + list(replacements.items())
        await self.app(scope, receive, send)
