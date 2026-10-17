# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bearer authentication with an unauthenticated local-transport boundary."""

from __future__ import annotations

import hmac
import ipaddress
import os
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_ENV_API_TOKEN_NAME = "JIUWENBOX_API_TOKEN"


def get_configured_token() -> str | None:
    """Return the configured API token, or ``None`` for local-only access."""
    raw = os.environ.get(_ENV_API_TOKEN_NAME)
    if raw is None:
        return None
    token = raw.strip()
    return token or None


def is_loopback_host(host: str) -> bool:
    """Recognize loopback literals without resolving arbitrary DNS names."""
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback


def require_token_for_bind(host: str, token: str | None) -> None:
    if not is_loopback_host(host) and not token:
        raise ValueError("Non-loopback HTTP listeners require JIUWENBOX_API_TOKEN or --api-token")


def validate_configured_listener() -> None:
    """Check explicit listener settings before starting sandbox resources."""
    uri = os.environ.get("JIUWENBOX_LISTEN")
    if not uri:
        return
    parsed = urlsplit(uri)
    if parsed.scheme == "unix":
        return
    if parsed.scheme != "http":
        raise ValueError("JIUWENBOX_LISTEN must use http:// or unix:///")
    require_token_for_bind(parsed.hostname or "", get_configured_token())


def _is_local_transport(request: Request) -> bool:
    # Use ASGI socket addresses, never Host/X-Forwarded-* request headers or
    # the configured URI: direct uvicorn invocations can bypass the launcher.
    server = request.scope.get("server")
    client = request.scope.get("client")
    if client is None and (server is None or server[1] is None):
        # Uvicorn represents UDS as None or (socket_path, None).
        # Access is controlled by socket permissions, not HTTP headers.
        return True
    return bool(
        server and client
        and is_loopback_host(server[0]) and is_loopback_host(client[0])
    )


def extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    stripped = token.strip()
    return stripped or None


def token_is_valid(provided: str | None, expected: str) -> bool:
    if provided is None:
        return False
    return hmac.compare_digest(
        provided.encode("utf-8"),
        expected.encode("utf-8"),
    )


class BearerTokenAuthMiddleware(BaseHTTPMiddleware):
    """Allow token-free access only over loopback or a local Unix socket."""

    async def dispatch(self, request: Request, call_next) -> Response:
        expected = get_configured_token()
        if expected is None:
            if _is_local_transport(request):
                return await call_next(request)
            return JSONResponse(
                status_code=503,
                content={"error": "api_token_required", "code": "API_TOKEN_REQUIRED"},
            )

        provided = extract_bearer_token(request.headers.get("Authorization"))
        if not token_is_valid(provided, expected):
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
