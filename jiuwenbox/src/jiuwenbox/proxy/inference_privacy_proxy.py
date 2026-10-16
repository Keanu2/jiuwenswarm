"""HTTP-aware inference privacy proxy with path-based routing.

Supports multiple target endpoints through path prefix routing:
- POST http://127.0.0.1:8080/openai/v1/chat/completions
  -> forwards to https://api.openai.com/v1/chat/completions
  -> injects API key for openai route

Features:
- Path-based routing to multiple targets
- API key injection via Authorization header
- Request/response logging
- HTTPS target support
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import re
import secrets
import ssl
from dataclasses import dataclass, field
from typing import Callable

import h11

from jiuwenbox.proxy.target_security import ProxyPolicyError, TargetPolicy, origin, parse_endpoint
from jiuwenbox.logging_config import configure_logging

configure_logging()
logger = logging.getLogger(__name__)


PLACEHOLDER = "<placeholder>"


@dataclass
class ProxyRoute:
    path_prefix: str
    target_endpoint: str
    api_key: str = field(default="", repr=False)
    skip_cert_verify: bool = False
    _target_host: str = ""
    _target_port: int = 0
    _use_tls: bool = False
    _target_base_path: str = ""

    def __post_init__(self):
        if "://" not in self.target_endpoint:
            scheme = "http" if ":" in self.target_endpoint and not self.target_endpoint.endswith(":443") else "https"
            self.target_endpoint = f"{scheme}://{self.target_endpoint}"
        (
            self._target_host,
            self._target_port,
            self._use_tls,
            self._target_base_path,
        ) = self._parse_endpoint(self.target_endpoint)
        if any(ord(c) < 32 or ord(c) == 127 for c in self.api_key + self.path_prefix):
            raise ValueError("Invalid proxy route characters")
        if not self.path_prefix.startswith("/"):
            self.path_prefix = "/" + self.path_prefix
        self.path_prefix = self.path_prefix.rstrip("/")

    @staticmethod
    def _parse_endpoint(endpoint: str) -> tuple[str, int, bool, str]:
        if "://" not in endpoint:
            scheme = "http" if ":" in endpoint and not endpoint.endswith(":443") else "https"
            endpoint = f"{scheme}://{endpoint}"
        return parse_endpoint(endpoint)

    @property
    def target_host(self) -> str:
        return self._target_host

    @property
    def target_port(self) -> int:
        return self._target_port

    @property
    def use_tls(self) -> bool:
        return self._use_tls

    @property
    def target_base_path(self) -> str:
        return self._target_base_path


@dataclass
class InferencePrivacyProxyConfig:
    listen_port: int = 0
    listen_host: str = "127.0.0.1"
    routes: list[ProxyRoute] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)
    insecure_tls_origins: list[str] = field(default_factory=list)
    ca_file: str | None = None
    target_policy: TargetPolicy | None = field(default=None, repr=False)

    def freeze_policy(self) -> TargetPolicy:
        """Capture deployment trust once, before management API route mutations."""
        if self.target_policy is None:
            self.target_policy = TargetPolicy.from_config(self)
        return self.target_policy


class InferencePrivacyProxy:
    def __init__(
        self,
        config: InferencePrivacyProxyConfig,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self._policy = config.freeze_policy()
        self._token = os.environ.get("JIUWENBOX_PROXY_TOKEN", "").strip().encode()
        self._connections: set[asyncio.Task] = set()
        self._server: asyncio.Server | None = None
        self._running = False
        self._log_callback = log_callback
        self._ssl_contexts: dict[tuple, ssl.SSLContext] = {}
        self._enabled_routes: set[str] = set()

    @property
    def is_running(self) -> bool:
        """Return whether the proxy accept loop is active."""
        return self._running

    @property
    def server(self) -> asyncio.Server | None:
        """Expose the listener for tests and management code."""
        return self._server

    def enable_route(self, path_prefix: str) -> None:
        """Enable a route for routing."""
        normalized = path_prefix.rstrip("/")
        self._enabled_routes.add(normalized)
        self._log(f"Route '{normalized}' enabled")

    def disable_route(self, path_prefix: str) -> None:
        """Disable a route (requests will return 404)."""
        normalized = path_prefix.rstrip("/")
        self._enabled_routes.discard(normalized)
        self._log(f"Route '{normalized}' disabled")

    def is_route_enabled(self, path_prefix: str) -> bool:
        """Check if a route is enabled."""
        normalized = path_prefix.rstrip("/")
        return normalized in self._enabled_routes

    def _log(self, message: str) -> None:
        logger.info(message)
        if self._log_callback:
            self._log_callback(message)

    def _get_ssl_context(self, route: ProxyRoute) -> ssl.SSLContext:
        """Get or create SSL context for a route."""
        self._policy.validate(route)
        cache_key = (origin(route.target_endpoint), route.skip_cert_verify, self.config.ca_file)
        if cache_key not in self._ssl_contexts:
            ctx = ssl.create_default_context()
            if self.config.ca_file:
                ctx.load_verify_locations(cafile=self.config.ca_file)
            if route.skip_cert_verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self._ssl_contexts[cache_key] = ctx
        return self._ssl_contexts[cache_key]

    def _match_route(self, path: str) -> ProxyRoute | None:
        """Match an enabled route at a path segment boundary."""
        path = path.split("?", 1)[0].rstrip("/")
        matches = [r for r in self.config.routes if self.is_route_enabled(r.path_prefix)
                   and (path == r.path_prefix or path.startswith(r.path_prefix + "/"))]
        return max(matches, key=lambda r: len(r.path_prefix), default=None)

    def match_route(self, path: str) -> ProxyRoute | None:
        """Public wrapper for route matching."""
        return self._match_route(path)

    @staticmethod
    def _rewrite_path(path: str, route: ProxyRoute) -> str:
        """Preserve encoded path/query while removing the local route prefix."""
        path_part, separator, query = path.partition("?")
        suffix = path_part[len(route.path_prefix):]
        rewritten = route.target_base_path + suffix
        if suffix == "/" and route.target_base_path:
            rewritten = route.target_base_path
        return (rewritten or "/") + separator + query

    def rewrite_path(self, path: str, route: ProxyRoute) -> str:
        """Public wrapper for request path rewriting."""
        return self._rewrite_path(path, route)

    def _inject_api_key(self, headers: bytes, route: ProxyRoute) -> bytes:
        """Inject API key into headers (wildcard replacement).

        Replaces ANY existing key with configured key:
        - Authorization: Bearer <any-key> -> Authorization: Bearer {route.api_key}
        - X-Api-Key: <any-key> -> X-Api-Key: {route.api_key}
        """
        if not route.api_key:
            return headers

        headers_str = headers.decode(errors="replace")

        patterns = [
            (r'Authorization:\s*Bearer\s+\S+', f'Authorization: Bearer {route.api_key}'),
            (r'X-Api-Key:\s*\S+', f'X-Api-Key: {route.api_key}'),
        ]

        result = headers_str
        injected = False

        for pattern, replacement in patterns:
            new_result = re.sub(pattern, replacement, result)
            if new_result != result:
                injected = True
                result = new_result

        if injected:
            self._log(f"Injected API key for route '{route.path_prefix}'")

        return result.encode()

    def inject_api_key(self, headers: bytes, route: ProxyRoute) -> bytes:
        """Public wrapper for API key header injection."""
        return self._inject_api_key(headers, route)

    @staticmethod
    def _loopback(address) -> bool:
        try:
            return ipaddress.ip_address(address).is_loopback
        except (ValueError, TypeError):
            return False

    def _authenticate(self, headers, writer) -> set[bytes]:
        credentials = {}
        for name, value in headers:
            if name in {b"authorization", b"x-api-key", b"proxy-authorization"}:
                if name in credentials:
                    raise PermissionError("Ambiguous proxy credentials")
                credentials[name] = value
        if not self._token:
            peer, local = writer.get_extra_info("peername"), writer.get_extra_info("sockname")
            if not peer or not local or not self._loopback(peer[0]) or not self._loopback(local[0]):
                raise PermissionError("Proxy token required")
            return set()
        matched = set()
        names = ([b"proxy-authorization"] if b"proxy-authorization" in credentials
                 else [b"authorization", b"x-api-key"])
        for name in names:
            value = credentials.get(name, b"")
            if name != b"x-api-key":
                scheme, _, value = value.partition(b" ")
                if scheme.lower() != b"bearer":
                    continue
            if secrets.compare_digest(value, self._token):
                matched.add(name)
        if not matched:
            raise PermissionError("Invalid proxy credentials")
        # Strip all occurrences of the proxy secret, including duplicate credential formats.
        for name, value in credentials.items():
            if value == self._token or (value.lower().startswith(b"bearer ") and value[7:] == self._token):
                matched.add(name)
        return matched

    @staticmethod
    def _headers(headers):
        hop = {b"connection", b"keep-alive", b"proxy-connection", b"proxy-authorization",
               b"proxy-authenticate", b"te", b"trailer", b"upgrade"}
        tokens = {v.strip().lower() for n, value in headers if n == b"connection"
                  for v in value.split(b",")}
        protected = {b"authorization", b"x-api-key", b"host", b"content-length", b"transfer-encoding"}
        if tokens & protected:
            raise ValueError("Invalid Connection header")
        return [(n, v) for n, v in headers if n not in hop | tokens]

    def _request_headers(self, request, route, consumed):
        result = []
        for name, value in self._headers(request.headers):
            if name in {b"host", b"expect"}:
                continue
            if name == b"authorization" and value.lower().startswith(b"bearer ") and route.api_key:
                value = b"Bearer " + route.api_key.encode()
            elif name == b"x-api-key" and route.api_key:
                value = route.api_key.encode()
            elif name in consumed:
                continue
            result.append((name, value))
        if (route.api_key and b"proxy-authorization" in consumed
                and not any(n in {b"authorization", b"x-api-key"} for n, _ in result)):
            result.append((b"authorization", b"Bearer " + route.api_key.encode()))
        host = route.target_host
        if ":" in host:
            host = f"[{host}]"
        if route.target_port != (443 if route.use_tls else 80):
            host += f":{route.target_port}"
        result.extend([(b"host", host.encode()), (b"connection", b"close")])
        return result

    @staticmethod
    async def _event(connection, reader):
        while True:
            event = connection.next_event()
            if event is not h11.NEED_DATA:
                return event
            # Model prefill / SSE pauses may be long; do not impose a response deadline.
            connection.receive_data(await reader.read(65536))

    @staticmethod
    async def _send(connection, writer, event):
        data = connection.send(event)
        if data:
            writer.write(data)
            await writer.drain()

    async def _forward(self, front, reader, writer, request, route, consumed):
        headers = self._request_headers(request, route, consumed)
        upstream_reader, upstream_writer = await self._policy.connect(
            route, self._get_ssl_context(route) if route.use_tls else None)
        back = h11.Connection(h11.CLIENT, max_incomplete_event_size=65536)
        upload = None
        response_started = False
        try:
            await self._send(back, upstream_writer, h11.Request(
                method=request.method, target=self._rewrite_path(request.target.decode("ascii"), route),
                headers=headers))
            if front.they_are_waiting_for_100_continue:
                await self._send(front, writer, h11.InformationalResponse(status_code=100, headers=[]))

            async def send_body():
                while True:
                    event = await self._event(front, reader)
                    if not isinstance(event, (h11.Data, h11.EndOfMessage)):
                        raise ValueError("Incomplete proxy request body")
                    await self._send(back, upstream_writer, event)
                    if isinstance(event, h11.EndOfMessage):
                        return

            upload = asyncio.create_task(send_body())
            while True:
                receive = asyncio.create_task(self._event(back, upstream_reader))
                try:
                    if not upload.done():
                        done, _ = await asyncio.wait([upload, receive], return_when=asyncio.FIRST_COMPLETED)
                        if upload in done:
                            upload.result()
                    else:
                        upload.result()
                    event = await receive
                finally:
                    if not receive.done():
                        receive.cancel()
                    await asyncio.gather(receive, return_exceptions=True)
                if isinstance(event, h11.InformationalResponse):
                    if event.status_code == 101:
                        raise ValueError("Protocol upgrades are not supported")
                    await self._send(front, writer, h11.InformationalResponse(
                        status_code=event.status_code, headers=self._headers(event.headers)))
                elif isinstance(event, h11.Response):
                    response_started = True
                    headers = self._headers(event.headers)
                    if front.their_state is not h11.DONE:
                        headers.append((b"connection", b"close"))
                    await self._send(front, writer, h11.Response(
                        status_code=event.status_code, headers=headers, reason=event.reason))
                elif isinstance(event, (h11.Data, h11.EndOfMessage)):
                    await self._send(front, writer, event)
                    if isinstance(event, h11.EndOfMessage):
                        return
                else:
                    raise OSError("Incomplete inference response")
        except Exception:
            if response_started:
                raise ConnectionAbortedError("Inference stream interrupted") from None
            raise
        finally:
            if upload:
                upload.cancel()
                await asyncio.gather(upload, return_exceptions=True)
            upstream_writer.close()
            with contextlib.suppress(Exception):
                await upstream_writer.wait_closed()

    async def _handle_connection(self, reader, writer):
        task = asyncio.current_task()
        self._connections.add(task)
        front = h11.Connection(h11.SERVER, max_incomplete_event_size=65536)
        try:
            while self._running:
                request = await self._event(front, reader)
                if isinstance(request, h11.ConnectionClosed):
                    return
                if not isinstance(request, h11.Request):
                    raise ValueError("Invalid proxy request")
                consumed = self._authenticate(request.headers, writer)
                names = {name for name, _ in request.headers}
                if {b"content-length", b"transfer-encoding"} <= names:
                    raise ValueError("Ambiguous HTTP request framing")
                if (request.method == b"CONNECT" or not request.target.startswith(b"/")
                        or request.target.startswith(b"//")
                        or any(n == b"upgrade" for n, _ in request.headers)):
                    raise ValueError("Unsupported proxy request target or protocol")
                route = self._match_route(request.target.decode("ascii"))
                if route is None:
                    await self._error(writer, 404, "No route matched")
                    return
                await self._forward(front, reader, writer, request, route, consumed)
                if front.our_state is not h11.DONE or front.their_state is not h11.DONE:
                    return
                front.start_next_cycle()
        except PermissionError:
            await self._error(writer, 401, "Proxy authentication required")
        except ProxyPolicyError:
            await self._error(writer, 403, "Inference target denied by policy")
        except (ValueError, h11.RemoteProtocolError, h11.LocalProtocolError):
            await self._error(writer, 400, "Invalid proxy request")
        except ConnectionAbortedError:
            pass
        except Exception as exc:
            self._log(f"Inference request failed ({type(exc).__name__})")
            await self._error(writer, 502, "Inference upstream unavailable")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self._connections.discard(task)

    @staticmethod
    async def _error(writer, status, message):
        reasons = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
                   404: "Not Found", 502: "Bad Gateway"}
        body = message.encode()
        response = (f"HTTP/1.1 {status} {reasons[status]}\r\nConnection: close\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n").encode() + body
        with contextlib.suppress(Exception):
            writer.write(response)
            await writer.drain()

    async def start(self) -> None:
        if self._running:
            return
        if not self._loopback(self.config.listen_host) and not self._token:
            raise ProxyPolicyError("Non-loopback inference proxy requires JIUWENBOX_PROXY_TOKEN")
        for route in self.config.routes:
            self._policy.validate(route)
            if route.use_tls:
                self._get_ssl_context(route)
        self._server = await asyncio.start_server(
            self._handle_connection, self.config.listen_host, self.config.listen_port)
        self._running = True
        self._log("Inference proxy started")

    async def stop(self) -> None:
        self._running = False
        if self._server:
            self._server.close()
        tasks = list(self._connections)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._server:
            await self._server.wait_closed()
            self._server = None
        self._log("Inference proxy stopped")
