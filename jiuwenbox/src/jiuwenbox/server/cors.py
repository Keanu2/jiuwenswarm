# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Explicit browser origins, including rejection of cross-origin simple requests."""

from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import urlsplit

from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from jiuwenbox.server.auth import _is_local_transport, get_configured_token, is_loopback_host

ENV_CORS_ORIGINS = "JIUWENBOX_CORS_ORIGINS"


def parse_origin(value: str) -> tuple[str, str, int] | None:
    """Parse an HTTP origin, never a URL with credentials, a path, or a wildcard."""
    if not value or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in value):
        return None
    if any(c in value for c in "*\\%?#@"):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if parsed.scheme not in {"http", "https"} or not host or parsed.path:
            return None
        if parsed.netloc.endswith(":"):
            return None
        if ":" in host:
            ipaddress.IPv6Address(host)
        elif not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", host):
            return None
        if port is not None and not 1 <= port <= 65535:
            return None
    except ValueError:
        return None
    return (
        parsed.scheme,
        host,
        port if port is not None else (443 if parsed.scheme == "https" else 80),
    )


def get_cors_origins() -> list[str]:
    """Read an exact allowlist at application creation; fail closed on typos."""
    raw = os.environ.get(ENV_CORS_ORIGINS, "").strip()
    if not raw:
        return []
    origins = list(dict.fromkeys(item.strip() for item in raw.split(",")))
    if any(parse_origin(origin) is None for origin in origins):
        raise ValueError(
            f"{ENV_CORS_ORIGINS} must contain comma-separated exact http(s)://host[:port] "
            "origins, without wildcards, null, credentials, paths, queries, or fragments"
        )
    return origins


def _is_same_origin(request: Request, origin: tuple[str, str, int]) -> bool:
    authority = request.headers.getlist("host")
    if len(authority) != 1:
        return False
    if parse_origin(f"{request.scope['scheme']}://{authority[0]}") != origin:
        return False
    # Do not trust a matching attacker-controlled DNS name and Host on a
    # token-free loopback listener (DNS rebinding). Local docs still work.
    return get_configured_token() is not None or (
        is_loopback_host(origin[1]) and _is_local_transport(request)
    )


class BrowserOriginMiddleware(CORSMiddleware):
    """Check origins before dispatch; CORS headers alone cannot prevent writes."""

    def __init__(self, app: ASGIApp, *, allow_origins: list[str]) -> None:
        super().__init__(
            app,
            allow_origins=allow_origins,
            allow_credentials=False,
            allow_methods=["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Mcp-Session-Id",
                "MCP-Protocol-Version",
                "Last-Event-ID",
            ],
            expose_headers=["Mcp-Session-Id", "WWW-Authenticate"],
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            request = Request(scope)
            values = request.headers.getlist("origin")
            if values:
                parsed = parse_origin(values[0]) if len(values) == 1 else None
                same_origin = parsed is not None and _is_same_origin(request, parsed)
                allowed_origin = parsed is not None and self.is_allowed_origin(values[0])
                if parsed is None or not (allowed_origin or same_origin):
                    response = JSONResponse(
                        {"error": "origin_not_allowed", "code": "ORIGIN_NOT_ALLOWED"},
                        status_code=403,
                        headers={"Vary": "Origin"},
                    )
                    await response(scope, receive, send)
                    return
                if same_origin and not allowed_origin:
                    # Same-origin requests need no CORS grant. All requests
                    # still pass through the inner authentication middleware.
                    await self.app(scope, receive, send)
                    return
        # Only allowed cross-origin preflights are answered here, without
        # entering auth or a business handler. Actual requests go through auth.
        await super().__call__(scope, receive, send)
