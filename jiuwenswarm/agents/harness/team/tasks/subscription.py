# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Live task subscriptions: one relay connection per watched task.

The relay's subscribe endpoint is a read-only WebSocket that replays a backlog
and then streams frames — live ``message`` entries, plus the two lease frames a
running task cares about: ``lease_lost`` (this device has been fenced off by a
takeover) and ``lease_preempt`` (a graceful handoff is asking this device to
flush and release). This module owns those connections and fans each frame out
to the registered consumers.

Design notes, mirroring the group chat subscription:

* **One connection per task, not per viewer.** Several features can watch the
  same task (a web panel and the proactive bridge), so the hub reference-counts
  bindings per task and tears the connection down when the last one detaches.
* **The relay's own cursor is authoritative but best-effort.** Reconnects resume
  from the highest ``seq`` seen so far; a fresh device falls back to the task's
  authoritative checkpoint on the pull path, not in the live stream.
* **Failures back off, never raise.** A relay that is down must not take the
  AgentServer with it; the hub retries with capped exponential backoff and
  reports state changes through the same callback channel the frames use.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from .client import AsyncTasksClient
from .config import TasksConfig, resolve_tasks_config
from .errors import TaskError

logger = logging.getLogger(__name__)

RECONNECT_INITIAL_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0
FRAME_CALLBACK_TIMEOUT = 10.0
"""Cap on a single downstream callback. A wedged consumer must not stall the
upstream pump, which would let the relay's queue grow without bound."""

ACK_INTERVAL_SECONDS = 60.0
"""How often to send the relay a keepalive ``ack``.

The relay's subscribe route runs a reader task that gives up after
``_IDLE_LIMIT_SECONDS`` (300s) with no client frame and then tears the socket
down, so a consumer that only ever *reads* gets disconnected every five
minutes. The relay honors exactly one client frame (``{"type":"ack","seq":N}``),
so we send it periodically — like the group chat subscription does.
"""


FrameCallback = Callable[[dict[str, Any]], Awaitable[None]]
"""Receives every frame destined for a task, already tagged with ``task_id``.

Relay traffic arrives as ``{"frame": "backlog"|"message"|"lease_lost"|"lease_preempt"}``;
lifecycle as ``{"frame": "state"}`` so the consumer can tell "quiet task" from
"connection broken".
"""


def _delay_for(attempt: int) -> float:
    return min(RECONNECT_INITIAL_DELAY * (2 ** max(0, attempt - 1)), RECONNECT_MAX_DELAY)


@dataclass
class _Subscription:
    """Bookkeeping for one watched task."""

    task_id: int
    client: AsyncTasksClient
    subscribers: set[str] = field(default_factory=set)
    task: asyncio.Task | None = None
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    last_seq: int = 0
    """Highest ``seq`` seen; the resume cursor for reconnects."""

    failures: int = 0
    state: str = "idle"


def _max_seq(messages: Any) -> int:
    best = 0
    if isinstance(messages, list):
        for item in messages:
            if not isinstance(item, dict):
                continue
            try:
                seq = int(item.get("seq") or 0)
            except (TypeError, ValueError):
                continue
            best = max(best, seq)
    return best


class TaskSubscriptionHub:
    """Manages relay task subscriptions and fans frames out to each consumer.

    Consumers are independent: several features can watch the same task (the web
    panel's push relay, the proactive bridge), and one consumer raising does not
    disturb the others or the upstream pump.
    """

    def __init__(
        self,
        *,
        config_base: Mapping[str, Any] | None = None,
        on_frame: FrameCallback | None = None,
    ) -> None:
        self._config_base = config_base
        self._consumers: list[FrameCallback] = [on_frame] if on_frame else []
        self._subs: dict[int, _Subscription] = {}
        self._lock = asyncio.Lock()

    def add_frame_consumer(self, on_frame: FrameCallback) -> Callable[[], None]:
        """Register a consumer; returns a function that removes it."""
        self._consumers.append(on_frame)

        def _remove() -> None:
            with contextlib.suppress(ValueError):
                self._consumers.remove(on_frame)

        return _remove

    @property
    def watched_tasks(self) -> list[int]:
        return sorted(self._subs)

    async def attach(self, *, task_id: int, subscriber_id: str) -> None:
        """Register interest in a task, starting the relay connection if needed."""
        async with self._lock:
            sub = self._subs.get(task_id)
            if sub is None:
                sub = _Subscription(task_id=task_id, client=self._build_client())
                self._subs[task_id] = sub
                sub.subscribers.add(subscriber_id)
                sub.task = asyncio.create_task(self._run(sub))
                logger.info(
                    "[TaskHub] subscription started: task_id=%s subscriber=%s",
                    task_id,
                    subscriber_id,
                )
                return

            sub.subscribers.add(subscriber_id)
            # A subscriber arriving after the connection gave up deserves an
            # immediate retry rather than waiting out the remaining backoff.
            if sub.task is None or sub.task.done():
                sub.stop_event = asyncio.Event()
                sub.task = asyncio.create_task(self._run(sub))
                logger.info(
                    "[TaskHub] subscription restarted: task_id=%s subscriber=%s",
                    task_id,
                    subscriber_id,
                )

    async def detach(self, *, task_id: int, subscriber_id: str) -> None:
        """Drop interest; stops the relay connection when the last one leaves."""
        async with self._lock:
            sub = self._subs.get(task_id)
            if sub is None:
                return
            sub.subscribers.discard(subscriber_id)
            if sub.subscribers:
                return
            self._subs.pop(task_id, None)

        await self._shutdown(sub)
        logger.info(
            "[TaskHub] subscription stopped (no subscribers): task_id=%s", task_id
        )

    async def aclose(self) -> None:
        """Stop every subscription. Safe to call more than once."""
        async with self._lock:
            subs = list(self._subs.values())
            self._subs.clear()
        for sub in subs:
            await self._shutdown(sub)

    def _build_client(self) -> AsyncTasksClient:
        config: TasksConfig = resolve_tasks_config(self._config_base)
        return AsyncTasksClient(config)

    async def _shutdown(self, sub: _Subscription) -> None:
        sub.stop_event.set()
        task, sub.task = sub.task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await sub.client.aclose()

    async def _emit(self, payload: dict[str, Any]) -> None:
        # Snapshot: a consumer may add or remove another while running.
        for consumer in list(self._consumers):
            try:
                await asyncio.wait_for(consumer(payload), timeout=FRAME_CALLBACK_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning(
                    "[TaskHub] frame consumer timed out: task_id=%s",
                    payload.get("task_id"),
                )
            except Exception:  # noqa: BLE001 - a bad consumer must not kill the pump
                logger.exception(
                    "[TaskHub] frame consumer failed: task_id=%s",
                    payload.get("task_id"),
                )

    async def _emit_state(self, sub: _Subscription, state: str, detail: str = "") -> None:
        if sub.state == state and not detail:
            return
        sub.state = state
        await self._emit(
            {
                "task_id": sub.task_id,
                "frame": "state",
                "state": state,
                "detail": detail,
            }
        )

    async def _run(self, sub: _Subscription) -> None:
        """Maintain one relay subscription until stopped."""
        attempt = 0
        while not sub.stop_event.is_set():
            try:
                await self._connect_and_pump(sub)
                # Clean close: the relay ended the stream. Reconnect unless the
                # task vanished, which _connect_and_pump reports as fatal.
                attempt = 0
            except asyncio.CancelledError:
                raise
            except _FatalSubscription as exc:
                await self._emit_state(sub, "unavailable", str(exc))
                logger.info(
                    "[TaskHub] subscription abandoned: task_id=%s reason=%s",
                    sub.task_id,
                    exc,
                )
                return
            except TaskError as exc:
                # Configuration/auth problems will not fix themselves on retry.
                await self._emit_state(sub, "unavailable", str(exc))
                return
            except Exception as exc:  # noqa: BLE001 - transport errors are expected
                await self._emit_state(sub, "reconnecting", str(exc))
                logger.info(
                    "[TaskHub] subscription dropped: task_id=%s error=%s",
                    sub.task_id,
                    exc,
                )

            if sub.stop_event.is_set():
                return
            attempt += 1
            delay = _delay_for(attempt)
            try:
                await asyncio.wait_for(sub.stop_event.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                logger.debug(
                    "[TaskHub] reconnecting: task_id=%s attempt=%s delay=%.1fs",
                    sub.task_id,
                    attempt,
                    delay,
                )

    async def _connect_and_pump(self, sub: _Subscription) -> None:
        import json

        import websockets

        url, subprotocols = sub.client.subscribe_url(
            task_id=sub.task_id, after_seq=sub.last_seq
        )
        headers = sub.client.subscribe_headers()
        await self._emit_state(sub, "connecting")

        async with websockets.connect(
            url,
            additional_headers=headers,
            subprotocols=subprotocols or None,
            open_timeout=15,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            max_size=4 * 1024 * 1024,
        ) as socket:
            await self._emit_state(sub, "live")
            sub.failures = 0
            loop = asyncio.get_running_loop()
            last_ack = loop.time()
            while not sub.stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=60)
                except asyncio.TimeoutError:
                    raise TaskError(
                        "任务接续订阅超时", code="unavailable"
                    )

                now = loop.time()
                if now - last_ack >= ACK_INTERVAL_SECONDS:
                    last_ack = now
                    try:
                        await socket.send(
                            json.dumps({"type": "ack", "seq": int(sub.last_seq)})
                        )
                    except Exception as exc:  # noqa: BLE001 - reconnect handles it
                        raise TaskError(
                            f"任务接续订阅保活失败: {exc}", code="unavailable"
                        ) from exc

                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                try:
                    frame = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(frame, dict):
                    continue

                kind = str(frame.get("type") or "")
                if kind == "ping":
                    continue

                # Advance the resume cursor before emitting: if the consumer
                # then fails, the next connect still starts past these seqs.
                sub.last_seq = max(
                    sub.last_seq, _max_seq(frame.get("messages") or []), _int(frame.get("seq"))
                )

                payload: dict[str, Any] = {
                    "task_id": sub.task_id,
                    "frame": kind,
                }
                if kind == "backlog":
                    payload["messages"] = frame.get("messages") or []
                    payload["next_seq"] = frame.get("next_seq")
                    payload["has_more"] = frame.get("has_more")
                    payload["latest_seq"] = frame.get("latest_seq")
                elif kind in ("lease_lost", "lease_preempt"):
                    # Flat lease frames: the bridge reacts to these directly.
                    payload["fence"] = frame.get("fence")
                else:
                    # A live ``message`` frame carries the wire message fields
                    # at the top level; normalize into ``message`` so the
                    # consumer never confuses message fields with the envelope.
                    payload["message"] = {
                        key: frame[key]
                        for key in (
                            "seq",
                            "message_id",
                            "sender_id",
                            "device_id",
                            "kind",
                            "payload",
                            "reply_to",
                            "trusted",
                            "created_at",
                            "deleted_at",
                        )
                        if key in frame
                    }
                await self._emit(payload)

    async def ensure_connected(self, task_id: int) -> None:
        """Restart a subscription that has stopped, if anyone still wants it."""
        async with self._lock:
            sub = self._subs.get(task_id)
            if sub is None or (sub.task is not None and not sub.task.done()):
                return
            sub.stop_event = asyncio.Event()
            sub.task = asyncio.create_task(self._run(sub))


class _FatalSubscription(Exception):
    """Raised when retrying cannot help (task deleted, not the owner)."""


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


_HUBS: dict[str, TaskSubscriptionHub] = {}
"""One hub per *credential*, not one per process.

The relay scopes delivery by principal (and the device is the key), so two
callers authenticating with different keys must not share a connection — the
frames a task streams to its owner's device is key to which device holds the
lease, and sharing a socket between them means one silently never receives what
the other does. A config with no key collapses to the empty string and shares
the anonymous hub, which is what an unconfigured deployment gets anyway.
"""


def _credential_key(config_base: Mapping[str, Any] | None) -> str:
    try:
        return resolve_tasks_config(config_base).api_key or ""
    except Exception:  # noqa: BLE001 - a broken config must not break the hub
        return ""


def _push_suffix(frame: dict[str, Any]) -> str:
    """A stable per-frame id suffix, for log correlation only."""
    kind = str(frame.get("frame") or "")
    if kind in ("lease_lost", "lease_preempt"):
        return kind
    message = frame.get("message")
    if isinstance(message, dict) and message.get("seq") is not None:
        return str(message.get("seq"))
    return str(_int(frame.get("next_seq")) or 0)


def set_task_frame_callback(hub: TaskSubscriptionHub) -> None:
    """Route one hub's frames through the AgentServer's server-push channel.

    Idempotent *per hub*: the callback is added as a consumer rather than
    installed as the only one, because the bridge registers its own consumer on
    the hub it holds and must not be unregistered by a panel subscribing later.

    The relay's task channel streams only the log (``backlog``/``message``) and
    the lease frames (``lease_lost``/``lease_preempt``) — there is no task-state
    push — so the lease frames double as the panel's "this device no longer
    holds the task" signal and surface as ``task.state`` events.
    """
    if getattr(hub, "_push_bound", False):
        return

    from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport

    transport = WebSocketGatewayPushTransport()

    async def _push(frame: dict[str, Any]) -> None:
        task_id = frame.get("task_id")
        kind = str(frame.get("frame") or "")
        if kind in ("lease_lost", "lease_preempt"):
            payload: dict[str, Any] = {
                "event_type": "task.state",
                "task_id": task_id,
                "state": kind,
                "fence": frame.get("fence"),
            }
        else:
            payload = {
                "event_type": "task.message",
                "task_id": task_id,
                "frame": kind,
            }
            # Pass the hub's normalized fields straight through; the panel reads
            # `message` for a live entry and `messages` for a backlog page.
            for key in ("messages", "message", "next_seq", "has_more"):
                if key in frame:
                    payload[key] = frame[key]

        await transport.send_push(
            {
                # No session_id on purpose: these events belong to the global
                # task panel, and the web channel broadcasts this event type to
                # every connection instead of routing by session.
                "request_id": f"task-{task_id}-{_push_suffix(frame)}",
                "channel_id": "web",
                "payload": payload,
            }
        )

    hub.add_frame_consumer(_push)
    hub._push_bound = True  # type: ignore[attr-defined]


def get_subscription_hub(config_base: Mapping[str, Any] | None = None) -> TaskSubscriptionHub:
    """The hub for this credential, created on first use."""
    key = _credential_key(config_base)
    hub = _HUBS.get(key)
    if hub is None:
        hub = TaskSubscriptionHub(config_base=config_base)
        _HUBS[key] = hub
    return hub


def reset_subscription_hub_for_tests() -> None:
    _HUBS.clear()