# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Live group subscriptions: one relay connection per watched group.

The relay's subscribe endpoint is a read-only WebSocket that replays a backlog
and then streams frames. This module owns those connections and hands each
frame to whatever callback the AgentServer registered, so the RPC layer stays
free of socket lifecycle concerns.

Design notes worth keeping in mind when editing:

* **One connection per group, not per viewer.** Group chat is chat: several
  open project panels watching the same group should share a single upstream
  connection rather than one each. The hub therefore reference-counts
  bindings per group and tears the connection down when the last one detaches.
  A "binding" is a caller-owned handle string (the AgentServer passes the
  requesting channel/session pair), not a browser socket — the AgentServer
  reaches browsers through the Gateway's single push channel, which broadcasts
  by ``group_id`` and lets each panel filter.
* **The relay's own cursor is authoritative.** Reconnects resume from the
  highest ``seq`` seen so far rather than from a fresh backlog, because the
  relay keeps ``last_read_seq`` server-side and would otherwise replay history
  the client already has. Duplicates from the overlap are harmless (the panel
  dedupes on ``seq``); gaps are not.
* **Failures back off, never raise.** A relay that is down (or a group that
  was deleted underneath us) must not take the AgentServer with it; the hub
  retries with capped exponential backoff and reports state changes through
  the same callback channel the frames use.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from .client import AsyncGroupChatClient
from .config import GroupChatConfig, resolve_groupchat_config
from .errors import GroupChatError

logger = logging.getLogger(__name__)

RECONNECT_INITIAL_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0
FRAME_CALLBACK_TIMEOUT = 10.0
"""Cap on a single downstream callback. A wedged consumer must not stall the
upstream pump, which would let the relay's queue grow without bound."""

ACK_INTERVAL_SECONDS = 60.0
"""How often to send the relay a keepalive ``ack``.

The relay's subscribe route runs a reader task that gives up after
``_IDLE_LIMIT_SECONDS`` (300s) with no client frame and then cancels the socket
— so a consumer that only ever *reads* gets disconnected every five minutes.
The relay honors exactly one client frame (``{"type":"ack","seq":N}``), so we
send it periodically. 60s leaves plenty of margin under the 300s cutoff and is
long enough not to add chatter.
"""


FrameCallback = Callable[[dict[str, Any]], Awaitable[None]]
"""Receives every frame destined for a group, already tagged with ``group_id``.

Three frame shapes reach it: ``{"frame": "backlog"|"message"|"event"|"error"}``
for relay traffic, plus ``{"frame": "state"}`` for subscription lifecycle
(status transitions, exhaustion) so the UI can distinguish "quiet group" from
"connection broken".
"""


def _delay_for(attempt: int) -> float:
    return min(RECONNECT_INITIAL_DELAY * (2 ** max(0, attempt - 1)), RECONNECT_MAX_DELAY)


@dataclass
class _Subscription:
    """Bookkeeping for one watched group."""

    group_id: int
    client: AsyncGroupChatClient
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


class GroupSubscriptionHub:
    """Manages relay subscriptions and fans frames out to every consumer.

    Consumers are independent: several features can watch the same group (the
    web panel's push relay, the agent bridge that answers @-mentions), and one
    consumer raising does not disturb the others or the upstream pump.
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

    def set_frame_callback(self, on_frame: FrameCallback | None) -> None:
        """Replace every consumer with a single one (the original contract).

        Kept because the web-push binding calls it on each subscribe; new code
        that must coexist with others should use ``add_frame_consumer``.
        """
        self._consumers = [on_frame] if on_frame else []

    def add_frame_consumer(self, on_frame: FrameCallback) -> Callable[[], None]:
        """Register an additional consumer; returns a function that removes it."""
        self._consumers.append(on_frame)

        def _remove() -> None:
            with contextlib.suppress(ValueError):
                self._consumers.remove(on_frame)

        return _remove

    @property
    def watched_groups(self) -> list[int]:
        return sorted(self._subs)

    async def attach(self, *, group_id: int, subscriber_id: str) -> None:
        """Register interest in a group, starting the relay connection if needed."""
        async with self._lock:
            sub = self._subs.get(group_id)
            if sub is None:
                sub = _Subscription(group_id=group_id, client=self._build_client())
                self._subs[group_id] = sub
                sub.subscribers.add(subscriber_id)
                sub.task = asyncio.create_task(self._run(sub))
                logger.info(
                    "[GroupChatHub] subscription started: group_id=%s subscriber=%s",
                    group_id,
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
                    "[GroupChatHub] subscription restarted: group_id=%s subscriber=%s",
                    group_id,
                    subscriber_id,
                )

    async def detach(self, *, group_id: int, subscriber_id: str) -> None:
        """Drop interest; stops the relay connection when the last one leaves."""
        async with self._lock:
            sub = self._subs.get(group_id)
            if sub is None:
                return
            sub.subscribers.discard(subscriber_id)
            if sub.subscribers:
                return
            self._subs.pop(group_id, None)

        await self._shutdown(sub)
        logger.info(
            "[GroupChatHub] subscription stopped (no subscribers): group_id=%s", group_id
        )

    async def aclose(self) -> None:
        """Stop every subscription. Safe to call more than once."""
        async with self._lock:
            subs = list(self._subs.values())
            self._subs.clear()
        for sub in subs:
            await self._shutdown(sub)

    def _build_client(self) -> AsyncGroupChatClient:
        config: GroupChatConfig = resolve_groupchat_config(self._config_base)
        return AsyncGroupChatClient(config)

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
                    "[GroupChatHub] frame consumer timed out: group_id=%s",
                    payload.get("group_id"),
                )
            except Exception:  # noqa: BLE001 - a bad consumer must not kill the pump
                logger.exception(
                    "[GroupChatHub] frame consumer failed: group_id=%s",
                    payload.get("group_id"),
                )

    async def _emit_state(self, sub: _Subscription, state: str, detail: str = "") -> None:
        # ``deleted`` is terminal and must always reach the consumer, so it
        # bypasses the dedupe that collapses repeat state transitions.
        if sub.state == state and not detail and state != "deleted":
            return
        sub.state = state
        await self._emit(
            {
                "group_id": sub.group_id,
                "frame": "state",
                "state": state,
                "detail": detail,
            }
        )

    async def _run(self, sub: _Subscription) -> None:
        """Maintain one relay subscription until stopped or the group vanishes."""
        attempt = 0
        while not sub.stop_event.is_set():
            try:
                await self._connect_and_pump(sub)
                # Clean close: the relay ended the stream. Reconnect unless the
                # group is gone, which _connect_and_pump reports as fatal.
                attempt = 0
            except asyncio.CancelledError:
                raise
            except _FatalSubscription as exc:
                await self._emit_state(sub, "unavailable", str(exc))
                logger.info(
                    "[GroupChatHub] subscription abandoned: group_id=%s reason=%s",
                    sub.group_id,
                    exc,
                )
                return
            except GroupChatError as exc:
                # Configuration/auth problems will not fix themselves on retry.
                await self._emit_state(sub, "unavailable", str(exc))
                return
            except Exception as exc:  # noqa: BLE001 - transport errors are expected
                await self._emit_state(sub, "reconnecting", str(exc))
                logger.info(
                    "[GroupChatHub] subscription dropped: group_id=%s error=%s",
                    sub.group_id,
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
                    "[GroupChatHub] reconnecting: group_id=%s attempt=%s delay=%.1fs",
                    sub.group_id,
                    attempt,
                    delay,
                )

    async def _connect_and_pump(self, sub: _Subscription) -> None:
        import json

        import websockets

        url, subprotocols = sub.client.subscribe_url(
            group_id=sub.group_id, after_seq=sub.last_seq
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
                    # The relay pings every 30s; silence past 60s means the
                    # connection is a zombie, so force a reconnect.
                    raise GroupChatError("群聊订阅超时", code="unavailable")

                # Keep the relay's idle reader alive. Without this the relay
                # closes the socket after 300s of client silence, so every
                # subscription would churn once every five minutes — and each
                # reconnect is a window in which live frames are missed.
                now = loop.time()
                if now - last_ack >= ACK_INTERVAL_SECONDS:
                    last_ack = now
                    try:
                        await socket.send(
                            json.dumps({"type": "ack", "seq": int(sub.last_seq)})
                        )
                    except Exception as exc:  # noqa: BLE001 - reconnect handles it
                        raise GroupChatError(f"群聊订阅保活失败: {exc}", code="unavailable") from exc

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
                if kind == "group_deleted":
                    # The group is gone for everyone: the relay tells subscribers
                    # before it drops the rows, because afterwards an empty log
                    # is indistinguishable from "no new messages". Stop here
                    # rather than reconnecting — there is nothing left to watch.
                    await self._emit_state(sub, "deleted", str(frame.get("deleted_by") or ""))
                    raise _FatalSubscription("group deleted")
                # Advance the resume cursor before emitting: if the consumer
                # then fails, the next connect still starts past these seqs.
                sub.last_seq = max(
                    sub.last_seq, _max_seq(frame.get("messages") or []), _int(frame.get("seq"))
                )
                # The relay's frames are *flat* — a live entry arrives as
                # ``{"type": "message", "seq": …, "sender_id": …, "payload": …}``,
                # with the message's own ``payload`` at the top level. Normalize
                # to a `message` sub-object here so consumers never have to know
                # that, and so the message's payload cannot collide with the
                # envelope's own keys.
                payload: dict[str, Any] = {
                    "group_id": sub.group_id,
                    "frame": kind,
                }
                if kind == "backlog":
                    payload["messages"] = frame.get("messages") or []
                    payload["next_seq"] = frame.get("next_seq")
                    payload["has_more"] = frame.get("has_more")
                else:
                    payload["message"] = {
                        key: frame[key]
                        for key in (
                            "seq",
                            "message_id",
                            "sender_id",
                            "kind",
                            "payload",
                            "mentions",
                            "reply_to",
                            "created_at",
                            "deleted_at",
                        )
                        if key in frame
                    }
                await self._emit(payload)

    async def ensure_connected(self, group_id: int) -> None:
        """Restart a subscription that has stopped, if anyone still wants it."""
        async with self._lock:
            sub = self._subs.get(group_id)
            if sub is None or (sub.task is not None and not sub.task.done()):
                return
            sub.stop_event = asyncio.Event()
            sub.task = asyncio.create_task(self._run(sub))


class _FatalSubscription(Exception):
    """Raised when retrying cannot help (group deleted, not a member)."""


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _push_suffix(frame: dict[str, Any]) -> str:
    """A stable per-frame id suffix, for log correlation only."""
    if frame.get("frame") == "state":
        return str(frame.get("state") or "state")
    message = frame.get("message")
    if isinstance(message, dict) and message.get("seq") is not None:
        return str(message.get("seq"))
    return str(_int(frame.get("next_seq")) or 0)


_HUBS: dict[str, GroupSubscriptionHub] = {}
"""One hub per *credential*, not one per process.

The relay scopes delivery by principal: an @-mention is fanned out only to the
connections of the mentioned principals. The projects panel subscribes as the
person and the bridge subscribes as the agent, so sharing a single upstream
connection between them means one of the two silently never receives its
frames — the bridge would sit subscribed to every group and still never see a
mention addressed to it.
"""


def _credential_key(config_base: Mapping[str, Any] | None) -> str:
    """Identify the principal a hub would authenticate as.

    The key itself is the identity: two callers passing the same credential
    share a connection, and the agent's hub is distinct from the person's. A
    config with no key at all collapses to the empty string and shares the
    anonymous hub, which is what an unconfigured deployment gets anyway.
    """
    try:
        return resolve_groupchat_config(config_base).api_key or ""
    except Exception:  # noqa: BLE001 - a broken config must not break the hub
        return ""


def set_group_frame_callback(hub: GroupSubscriptionHub) -> None:
    """Route one hub's frames through the AgentServer's server-push channel.

    Idempotent *per hub*: the callback is added as a consumer rather than
    installed as the only one, because the bridge registers its own consumer on
    the hub it holds and must not be unregistered by a panel subscribing later.
    """
    if getattr(hub, "_push_bound", False):
        return

    from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport

    transport = WebSocketGatewayPushTransport()

    async def _push(frame: dict[str, Any]) -> None:
        group_id = frame.get("group_id")
        if frame.get("frame") == "state":
            event_type = "team.group.state"
            payload: dict[str, Any] = {
                "event_type": event_type,
                "group_id": group_id,
                "state": frame.get("state"),
                "detail": frame.get("detail", ""),
            }
        else:
            event_type = "team.group.message"
            payload = {
                "event_type": event_type,
                "group_id": group_id,
                "frame": frame.get("frame"),
            }
            # Pass the hub's normalized fields straight through; the panel reads
            # `message` for a live entry and `messages` for a backlog page.
            for key in ("messages", "message", "next_seq", "has_more"):
                if key in frame:
                    payload[key] = frame[key]

        await transport.send_push(
            {
                # No session_id on purpose: these events belong to the global
                # projects panel, and the web channel broadcasts this event type
                # to every connection instead of routing by session.
                "request_id": f"group-{group_id}-{_push_suffix(frame)}",
                "channel_id": "web",
                "payload": payload,
            }
        )

    hub.add_frame_consumer(_push)
    hub._push_bound = True  # type: ignore[attr-defined]


def get_subscription_hub(config_base: Mapping[str, Any] | None = None) -> GroupSubscriptionHub:
    """The hub for this credential, created on first use."""
    key = _credential_key(config_base)
    hub = _HUBS.get(key)
    if hub is None:
        hub = GroupSubscriptionHub(config_base=config_base)
        _HUBS[key] = hub
    return hub


def reset_subscription_hub_for_tests() -> None:
    _HUBS.clear()
