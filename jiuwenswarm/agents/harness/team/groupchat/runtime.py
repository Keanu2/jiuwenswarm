# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime entry points for the group chat client.

Mirrors ``a2x_registry_runtime``: the client class is imported late (so the
AgentServer can boot without httpx-backed modules being touched), construction
never raises, and every call site closes the client in a ``finally``.

Every public function here is a thin "open a client, do one thing, close it"
wrapper. They exist as free functions rather than methods so the RPC handlers
above them stay free of client lifecycle code — which is also why the
signatures take raw, possibly-unvalidated params: the client's validators are
the single place those are checked.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from .client import AsyncGroupChatClient
from .config import GroupChatConfig, resolve_groupchat_config
from .errors import (
    GroupChatError,
    GroupChatNotConfiguredError,
    GroupChatRequestInvalidError,
    GroupChatUnavailableError,
)

logger = logging.getLogger(__name__)

DEFAULT_CALL_TIMEOUT = 20.0
"""Wall-clock cap around a single group API call, independent of the httpx timeout.

The httpx timeout covers connect/read/write individually; this bounds the whole
operation so a wedged relay cannot hold an AgentServer request handler open.
"""


def build_groupchat_client(
    config_base: Mapping[str, Any] | None = None,
) -> tuple[AsyncGroupChatClient, GroupChatConfig]:
    """Create a client from resolved config."""
    config = resolve_groupchat_config(config_base)
    return AsyncGroupChatClient(config), config


async def _call(
    method: str,
    *,
    config_base: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Invoke one client method on a short-lived client, closing it either way."""
    client, config = build_groupchat_client(config_base)
    try:
        return await asyncio.wait_for(
            getattr(client, method)(**kwargs), timeout=_call_timeout(config)
        )
    except asyncio.TimeoutError as exc:
        raise GroupChatUnavailableError("群聊服务响应超时") from exc
    finally:
        await client.aclose()


def _call_timeout(config: GroupChatConfig) -> float:
    return max(config.timeout, DEFAULT_CALL_TIMEOUT)


# ── Groups ───────────────────────────────────────────────────────────────────


async def create_group(
    *,
    name: str,
    owner_dataset: str | None = None,
    join_policy: str = "invite_only",
    max_members: Any = None,
    config_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return await _call(
        "create_group",
        config_base=config_base,
        name=name,
        owner_dataset=owner_dataset,
        join_policy=join_policy,
        max_members=max_members,
    )


async def list_groups(
    *, limit: int = 50, config_base: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    return await _call("list_groups", config_base=config_base, limit=limit)


async def get_group(
    *, group_id: Any, config_base: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return await _call("get_group", config_base=config_base, group_id=group_id)


async def whoami(
    *, config_base: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Resolve the identity behind the configured groupchat credential."""
    return await _call("whoami", config_base=config_base)


async def delete_group(
    *, group_id: Any, config_base: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return await _call("delete_group", config_base=config_base, group_id=group_id)


async def set_archived(
    *, group_id: Any, archived: bool, config_base: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return await _call(
        "set_archived", config_base=config_base, group_id=group_id, archived=archived
    )


async def leave_group(
    *, group_id: Any, config_base: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return await _call("leave_group", config_base=config_base, group_id=group_id)


# ── Members ──────────────────────────────────────────────────────────────────


async def list_members(
    *, group_id: Any, config_base: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    return await _call("list_members", config_base=config_base, group_id=group_id)


async def remove_member(
    *,
    group_id: Any,
    principal_id: Any,
    config_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return await _call(
        "remove_member",
        config_base=config_base,
        group_id=group_id,
        principal_id=principal_id,
    )


async def set_member_role(
    *,
    group_id: Any,
    principal_id: Any,
    role: Any,
    config_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return await _call(
        "set_member_role",
        config_base=config_base,
        group_id=group_id,
        principal_id=principal_id,
        role=role,
    )


# ── Invitations ──────────────────────────────────────────────────────────────


async def create_invite(
    *,
    group_id: Any,
    invitee_id: Any = None,
    max_uses: Any = None,
    ttl_seconds: Any = None,
    config_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return await _call(
        "create_invite",
        config_base=config_base,
        group_id=group_id,
        invitee_id=invitee_id,
        max_uses=max_uses,
        ttl_seconds=ttl_seconds,
    )


async def accept_invite(
    *, token: Any, config_base: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return await _call("accept_invite", config_base=config_base, token=token)


# ── Messages ─────────────────────────────────────────────────────────────────


async def send_message(
    *,
    group_id: Any,
    text: Any,
    client_msg_id: Any = None,
    mentions: Any = None,
    reply_to: Any = None,
    config_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return await _call(
        "send_message",
        config_base=config_base,
        group_id=group_id,
        text=text,
        client_msg_id=client_msg_id,
        mentions=mentions,
        reply_to=reply_to,
    )


async def fetch_messages(
    *,
    group_id: Any,
    after_seq: Any = None,
    before_seq: Any = None,
    limit: Any = None,
    config_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return await _call(
        "fetch_messages",
        config_base=config_base,
        group_id=group_id,
        after_seq=after_seq,
        before_seq=before_seq,
        limit=limit,
    )


async def ack_messages(
    *,
    group_id: Any,
    seq: Any,
    config_base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return await _call("ack", config_base=config_base, group_id=group_id, seq=seq)


__all__ = [
    "AsyncGroupChatClient",
    "GroupChatConfig",
    "GroupChatError",
    "GroupChatNotConfiguredError",
    "GroupChatRequestInvalidError",
    "GroupChatUnavailableError",
    "accept_invite",
    "ack_messages",
    "build_groupchat_client",
    "create_group",
    "create_invite",
    "fetch_messages",
    "get_group",
    "list_groups",
    "list_members",
    "remove_member",
    "resolve_groupchat_config",
    "send_message",
    "set_member_role",
]
