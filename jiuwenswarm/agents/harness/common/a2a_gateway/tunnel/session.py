# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""WebSocket relay tunnel client for NAT traversal.

Ports OpenClaw ``src/tunnel/session.ts`` ``TunnelSession`` to Python. This is
the only component JiuwenSwarm previously lacked entirely. The relay is a
JSON-over-WebSocket store-and-forward: this client registers its device id,
then can forward HTTP requests to a peer device (the peer's local inbound
A2A server) and await the response.

Lifecycle::

    start() → connectAndRegister() → openSocket() → REGISTER → REGISTERED
            → start_heartbeat() (PING/pong, timeout forces reconnect)

Reconnect: exponential backoff (2^n, capped at ``max_reconnect_delay_ms``),
unlimited by default (``max_reconnect_attempts=0``). Enriched REGISTER
(dataset/service_id/agent_card) that the relay rejects falls back to
device_id-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from .protocol import (
    MessageType,
    TunnelHttpRequest,
    TunnelHttpResponse,
    TunnelRegisterExtras,
    filter_headers,
)

logger = logging.getLogger(__name__)

DEFAULT_HEARTBEAT_INTERVAL_MS = 15_000
DEFAULT_HEARTBEAT_TIMEOUT_MS = 45_000
DEFAULT_RECONNECT_INTERVAL_MS = 5_000
DEFAULT_MAX_RECONNECT_INTERVAL_MS = 60_000
DEFAULT_MAX_RECONNECT_ATTEMPTS = 0  # 0 = unlimited
DEFAULT_REQUEST_TIMEOUT_MS = 300_000


@dataclass
class TunnelSessionOptions:
    relay_url: str
    device_id: str
    local_service_port: int = 19100
    heartbeat_interval_ms: int = DEFAULT_HEARTBEAT_INTERVAL_MS
    heartbeat_timeout_ms: int = DEFAULT_HEARTBEAT_TIMEOUT_MS
    request_timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS
    reconnect_interval_ms: int = DEFAULT_RECONNECT_INTERVAL_MS
    max_reconnect_interval_ms: int = DEFAULT_MAX_RECONNECT_INTERVAL_MS
    max_reconnect_attempts: int = DEFAULT_MAX_RECONNECT_ATTEMPTS
    register_extras: TunnelRegisterExtras | None = None


@dataclass
class _Pending:
    future: asyncio.Future
    timer: asyncio.TimerHandle


class TunnelSession:
    """Async WebSocket relay client.

    Use one instance per process. ``start()`` connects and registers; the
    session auto-reconnects on close/heartbeat-timeout. Call ``forward()``
    to send an HTTP request through a peer device's relay; call ``stop()``
    to tear down (rejecting all pending forwards).
    """

    def __init__(self, options: TunnelSessionOptions) -> None:
        self.opts = options
        self._ws: ClientConnection | None = None
        self._connected = False
        self._running = False
        self._should_reconnect = False
        self._reconnect_attempts = 0
        self._reconnect_scheduled = False
        self._reconnect_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._recv_task: asyncio.Task | None = None
        self._last_recv_at: float = 0.0
        self._pending: dict[str, _Pending] = {}
        self._starting: asyncio.Task | None = None
        self._register_event = asyncio.Event()
        self._register_error: BaseException | None = None
        # After enriched REGISTER is rejected, stick to device_id-only.
        self._prefer_plain_register = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def device_id(self) -> str:
        return self.opts.device_id

    async def start(self) -> None:
        if self._starting is not None:
            await self._starting
            return
        self._starting = asyncio.ensure_future(self._do_start())
        try:
            await self._starting
        finally:
            self._starting = None

    async def _do_start(self) -> None:
        self._should_reconnect = True
        self._running = False
        self._reconnect_attempts = 0
        self._cancel_reconnect()
        try:
            await self._connect_and_register()
            self._running = True
            self._start_heartbeat()
            logger.info(
                "a2a-tunnel: connected as %s → %s (local service :%d)",
                self.opts.device_id, self.opts.relay_url, self.opts.local_service_port,
            )
        except Exception:
            self._should_reconnect = False
            self._running = False
            self._stop_heartbeat()
            self._fail_register(asyncio.CancelledError("Tunnel start aborted"))
            self._detach_socket()
            self._connected = False
            raise

    async def stop(self) -> None:
        self._should_reconnect = False
        self._running = False
        self._stop_heartbeat()
        self._cancel_reconnect()
        self._fail_register(asyncio.CancelledError("Tunnel stopped"))
        for mid, pending in list(self._pending.items()):
            pending.timer.cancel()
            if not pending.future.done():
                pending.future.set_exception(asyncio.CancelledError("Tunnel stopped"))
            del self._pending[mid]
        self._detach_socket()
        self._connected = False
        logger.info("a2a-tunnel: stopped")

    # ---- outbound forward ------------------------------------------------- #

    async def forward(
        self,
        target_device: str,
        http_request: TunnelHttpRequest,
        *,
        timeout_ms: int | None = None,
    ) -> TunnelHttpResponse:
        if not self._connected or self._ws is None:
            raise ConnectionError("Tunnel not connected")

        timeout_ms = timeout_ms or self.opts.request_timeout_ms
        message_id = str(uuid.uuid4())
        message = {
            "type": MessageType.FORWARD_REQUEST,
            "message_id": message_id,
            "source_device": self.opts.device_id,
            "target_device": target_device,
            "http_request": {
                "method": http_request.method,
                "path": http_request.path,
                "headers": filter_headers(http_request.headers),
                "body": http_request.body or "",
            },
            "timeout": timeout_ms // 1000,
        }

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def _on_timeout() -> None:
            if message_id in self._pending:
                del self._pending[message_id]
            if not future.done():
                future.set_exception(TimeoutError(f"Tunnel timeout: {timeout_ms}ms"))

        timer = loop.call_later(timeout_ms / 1000.0, _on_timeout)
        self._pending[message_id] = _Pending(future=future, timer=timer)

        try:
            await self._ws.send(json.dumps(message))
        except Exception as exc:
            timer.cancel()
            self._pending.pop(message_id, None)
            raise ConnectionError(f"Failed to send forward_request: {exc}") from exc

        return await future

    # ---- connect / register / reconnect ----------------------------------- #

    def _build_register_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": MessageType.REGISTER,
            "device_id": self.opts.device_id,
        }
        extras = self.opts.register_extras
        if not self._prefer_plain_register and extras:
            if extras.dataset:
                payload["dataset"] = extras.dataset
            if extras.service_id:
                payload["service_id"] = extras.service_id
            if extras.agent_card is not None:
                payload["agent_card"] = extras.agent_card
        return payload

    async def _connect_and_register(self) -> None:
        await self._open_socket()
        try:
            await self._try_register()
        except Exception as err:
            had_extras = (
                not self._prefer_plain_register
                and bool(
                    self.opts.register_extras
                    and (self.opts.register_extras.dataset or self.opts.register_extras.service_id)
                )
            )
            if had_extras:
                logger.warning(
                    "Tunnel enriched REGISTER failed; falling back to device_id-only: %s", err,
                )
                self._prefer_plain_register = True
                self._force_close_socket()
                await self._open_socket()
                try:
                    await self._try_register()
                except Exception as err2:
                    self._force_close_socket()
                    raise err2 from err
            else:
                self._force_close_socket()
                raise
        self._reconnect_attempts = 0

    async def _try_register(self) -> None:
        self._register_event = asyncio.Event()
        self._register_error = None
        payload = self._build_register_payload()
        if not await self._send(payload):
            self._fail_register(ConnectionError("Failed to send register message"))
            self._force_close_socket()
            raise ConnectionError("Failed to send register message")
        try:
            await asyncio.wait_for(self._register_event.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            self._register_event = None
            raise TimeoutError("Tunnel register timeout")
        if self._register_error is not None:
            err = self._register_error
            self._register_error = None
            raise err

    def _fail_register(self, err: BaseException) -> None:
        if self._register_event is not None and not self._register_event.is_set():
            self._register_error = err
            self._register_event.set()

    async def _open_socket(self) -> None:
        self._detach_socket()
        try:
            ws = await asyncio.wait_for(
                websockets.connect(self.opts.relay_url, max_size=None, ping_interval=None),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            raise TimeoutError("Tunnel connection timeout")
        self._ws = ws
        self._connected = True
        self._last_recv_at = time.monotonic()
        # Start the receive loop for this socket.
        self._recv_task = asyncio.ensure_future(self._recv_loop())

    async def _recv_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            async for raw in ws:
                self._last_recv_at = time.monotonic()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                self._handle_message(raw)
        except ConnectionClosed:
            pass
        except Exception as exc:
            logger.error("a2a-tunnel: recv loop error: %s", exc)
        finally:
            self._connected = False
            logger.warning("a2a-tunnel: websocket closed")
            if self._ws is ws:
                self._ws = None
            self._schedule_reconnect()

    def _detach_socket(self) -> None:
        if self._recv_task is not None and not self._recv_task.done():
            self._recv_task.cancel()
        self._recv_task = None
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                # fire-and-forget; can't await here (may be called from sync path)
                asyncio.ensure_future(ws.close())
            except Exception:
                pass

    def _force_close_socket(self) -> None:
        self._connected = False
        ws = self._ws
        if ws is None:
            self._schedule_reconnect()
            return
        self._ws = None
        if self._recv_task is not None and not self._recv_task.done():
            self._recv_task.cancel()
        self._recv_task = None
        try:
            asyncio.ensure_future(ws.close())
        except Exception:
            self._schedule_reconnect()

    def _reconnect_delay_ms(self, attempt: int) -> int:
        exp = min(attempt - 1, 6)
        delay = int(self.opts.reconnect_interval_ms * (2 ** exp))
        return min(delay, self.opts.max_reconnect_interval_ms)

    def _schedule_reconnect(self) -> None:
        if not self._should_reconnect or not self._running:
            return
        if self._reconnect_scheduled:
            return
        mx = self.opts.max_reconnect_attempts
        if mx > 0 and self._reconnect_attempts >= mx:
            logger.error("a2a-tunnel: max reconnect attempts reached")
            return
        self._reconnect_attempts += 1
        attempt = self._reconnect_attempts
        delay_ms = self._reconnect_delay_ms(attempt)
        max_label = str(mx) if mx > 0 else "∞"
        self._reconnect_scheduled = True
        logger.info("a2a-tunnel: reconnecting (%s/%s) in %dms", attempt, max_label, delay_ms)

        async def _reconnect() -> None:
            self._reconnect_task = None
            self._reconnect_scheduled = False
            if not self._should_reconnect or not self._running:
                return
            try:
                await self._connect_and_register()
                self._start_heartbeat()
                logger.info("a2a-tunnel: reconnected as %s → %s", self.opts.device_id, self.opts.relay_url)
            except Exception as exc:
                logger.error("a2a-tunnel: reconnect failed: %s", exc)
                self._schedule_reconnect()

        self._reconnect_task = asyncio.get_running_loop().call_later(
            delay_ms / 1000.0,
            lambda: asyncio.ensure_future(_reconnect()),
        )

    def _cancel_reconnect(self) -> None:
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        self._reconnect_scheduled = False

    # ---- message handling ------------------------------------------------- #

    def _handle_message(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.error("a2a-tunnel: failed to parse message: %s", exc)
            return
        if not isinstance(message, dict):
            return
        msg_type = str(message.get("type") or "")
        if msg_type == MessageType.FORWARD_REQUEST:
            asyncio.ensure_future(self._handle_inbound_forward(message))
        elif msg_type == MessageType.FORWARD_RESPONSE:
            self._handle_forward_response(message)
        elif msg_type == MessageType.ERROR:
            self._handle_error(message)
        elif msg_type == MessageType.PING:
            asyncio.ensure_future(self._send({"type": MessageType.PONG}))
        elif msg_type == MessageType.PONG:
            pass
        elif msg_type == MessageType.REGISTERED:
            if self._register_event is not None and not self._register_event.is_set():
                self._register_event.set()
        else:
            logger.warning("a2a-tunnel: unknown message type: %s", msg_type)

    def _handle_forward_response(self, message: dict[str, Any]) -> None:
        message_id = str(message.get("message_id") or "")
        pending = self._pending.pop(message_id, None)
        if pending is None:
            return
        pending.timer.cancel()
        http_response = message.get("http_response")
        if not isinstance(http_response, dict) or not isinstance(http_response.get("status"), int):
            if not pending.future.done():
                pending.future.set_exception(ValueError("Invalid forward_response"))
            return
        status = int(http_response["status"])
        body = str(http_response.get("body") or "")
        if status >= 400:
            logger.info(
                "a2a-tunnel: forward_response target=%s status=%s body_head=%r",
                message.get("target_device") or str(message.get("target") or ""),
                status, body[:500],
            )
        if not pending.future.done():
            pending.future.set_result(TunnelHttpResponse(
                status=status,
                headers=filter_headers(http_response.get("headers")),
                body=body,
            ))

    def _handle_error(self, message: dict[str, Any]) -> None:
        message_id = str(message.get("message_id") or "")
        err_text = str(message.get("error") or "Tunnel error")
        logger.error("a2a-tunnel: error from relay: %s", err_text)
        if message_id:
            pending = self._pending.pop(message_id, None)
            if pending is not None:
                pending.timer.cancel()
                if not pending.future.done():
                    pending.future.set_exception(ConnectionError(err_text))
        else:
            self._fail_register(ConnectionError(err_text))

    async def _handle_inbound_forward(self, message: dict[str, Any]) -> None:
        message_id = str(message.get("message_id") or "")
        http_request = message.get("http_request") if isinstance(message.get("http_request"), dict) else {}
        path = str(http_request.get("path") or "/")
        method = str(http_request.get("method") or "GET").upper()
        local_url = f"http://127.0.0.1:{self.opts.local_service_port}{path}"
        body = str(http_request.get("body") or "")
        headers = filter_headers(http_request.get("headers"))
        try:
            async with httpx.AsyncClient(timeout=self.opts.request_timeout_ms / 1000.0) as client:
                resp = await client.request(method, local_url, content=body if body else None,
                                            headers=headers)
            await self._send({
                "type": MessageType.FORWARD_RESPONSE,
                "message_id": message_id,
                "http_response": {
                    "status": resp.status_code,
                    "headers": filter_headers(dict(resp.headers)),
                    "body": resp.text,
                },
                "status": resp.status_code,
            })
        except Exception as exc:
            err_msg = str(exc)
            logger.error(
                "a2a-tunnel: inbound forward failed target=%s local=%s: %s",
                message_id, local_url, err_msg,
            )
            await self._send({
                "type": MessageType.FORWARD_RESPONSE,
                "message_id": message_id,
                "http_response": {
                    "status": 500,
                    "headers": {"content-type": "application/json"},
                    "body": json.dumps({"error": err_msg}),
                },
                "status": 500,
            })

    async def _send(self, message: dict[str, Any]) -> bool:
        ws = self._ws
        if ws is None or not self._connected:
            logger.warning("a2a-tunnel: cannot send, socket not open")
            return False
        try:
            await ws.send(json.dumps(message))
            return True
        except Exception as exc:
            logger.error("a2a-tunnel: send failed: %s", exc)
            return False

    # ---- heartbeat --------------------------------------------------------- #

    def _start_heartbeat(self) -> None:
        self._stop_heartbeat()
        self._last_recv_at = time.monotonic()
        self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())

    def _stop_heartbeat(self) -> None:
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        interval = self.opts.heartbeat_interval_ms / 1000.0
        while True:
            await asyncio.sleep(interval)
            if not self._connected or self._ws is None:
                return
            silent_for = (time.monotonic() - self._last_recv_at) * 1000.0
            if silent_for >= self.opts.heartbeat_timeout_ms:
                logger.warning(
                    "a2a-tunnel: heartbeat timeout (%.0fms without recv), forcing reconnect",
                    silent_for,
                )
                self._force_close_socket()
                return
            await self._send({"type": MessageType.PING})


def create_tunnel_session(options: TunnelSessionOptions) -> TunnelSession:
    return TunnelSession(options)
