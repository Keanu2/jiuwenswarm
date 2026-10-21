# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration for the group chat data plane client.

The group chat API lives on an ``agent-registry-relay`` deployment (see the
``a2x_registry/groupchat`` package there) and is deliberately configured
separately from ``react.a2x_registry``: that section describes the *registry*
this process registers into (role, dataset, tunnel), while this one only
answers "where is the group API and who am I there".

The ``groupchat`` section is optional. When it is absent, disabled, or has no
``base_url``, the feature reports itself as not configured rather than
failing — same posture as ``RegistryClient.enabled`` in the AgentOS router.
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Mapping

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_MEMBERS = 50

JOIN_POLICIES: tuple[str, ...] = ("invite_only", "approval", "link", "open")
"""Accepted ``join_policy`` values; mirrors the relay's ``models.JOIN_POLICIES``.

Kept as a local copy on purpose — the client package ships independently and
must not import from the relay. The relay does *not* validate this field, so a
value outside this tuple reaches ``Group.__post_init__`` and surfaces as an
unhandled 500 rather than a clean 400. Rejecting it client-side is what keeps
that from ever being a user-visible failure.
"""

MAX_NAME_CHARS = 120
"""Relay-side ``MAX_NAME_CHARS``; longer names are rejected with 400."""

MAX_MESSAGE_CHARS = 16000
"""Local ceiling on a message body.

The relay caps the *serialized JSON* at ``A2X_GROUPCHAT_MAX_MESSAGE_BYTES``
(64 KiB by default) and answers 413; this is a cheaper guard that keeps an
accidental paste from leaving the process at all.
"""

ROLES: tuple[str, ...] = ("owner", "admin", "member", "readonly")
"""Mirrors the relay's ``models.ROLES``. ``owner`` is not grantable."""


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip() or default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


DEFAULT_AGENT_CONTEXT_MESSAGES = 50
DEFAULT_AGENT_TURN_TIMEOUT = 300.0


@dataclass(frozen=True)
class GroupChatAgentConfig:
    """The agent's own identity in group chats, plus its reply behaviour.

    Separate from the human-facing ``groupchat`` credentials on purpose: the
    relay addresses @-mentions by principal, so only a distinct identity lets a
    mention wake *this* agent and nobody else.
    """

    enabled: bool = False
    api_key: str | None = None
    """The agent's own credential. Falls back to the human key when unset —
    which works but makes @-mentions indistinguishable, so it is logged."""

    display_name: str = ""
    max_context_messages: int = DEFAULT_AGENT_CONTEXT_MESSAGES
    turn_timeout: float = DEFAULT_AGENT_TURN_TIMEOUT

    @property
    def shares_human_identity(self) -> bool:
        return not self.api_key

    @classmethod
    def from_section(cls, section: Mapping[str, Any]) -> "GroupChatAgentConfig":
        raw = section.get("agent")
        agent: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
        reply_raw = section.get("reply")
        reply: Mapping[str, Any] = reply_raw if isinstance(reply_raw, Mapping) else {}

        api_key = str(agent.get("api_key") or "").strip() or None

        try:
            max_context = int(reply.get("max_context_messages", DEFAULT_AGENT_CONTEXT_MESSAGES))
        except (TypeError, ValueError):
            max_context = DEFAULT_AGENT_CONTEXT_MESSAGES
        if max_context < 1:
            max_context = DEFAULT_AGENT_CONTEXT_MESSAGES

        try:
            turn_timeout = float(reply.get("timeout_seconds", DEFAULT_AGENT_TURN_TIMEOUT))
        except (TypeError, ValueError):
            turn_timeout = DEFAULT_AGENT_TURN_TIMEOUT
        if turn_timeout <= 0:
            turn_timeout = DEFAULT_AGENT_TURN_TIMEOUT

        return cls(
            enabled=_env_bool("GROUPCHAT_AGENT_ENABLED", bool(agent.get("enabled", False))),
            api_key=_env_str("GROUPCHAT_AGENT_API_KEY", api_key or "") or None,
            display_name=_env_str(
                "GROUPCHAT_AGENT_DISPLAY_NAME", str(agent.get("display_name") or "").strip()
            ),
            max_context_messages=_env_int(
                "GROUPCHAT_AGENT_MAX_CONTEXT_MESSAGES", max_context
            ),
            turn_timeout=_env_float("GROUPCHAT_AGENT_TURN_TIMEOUT", turn_timeout),
        )


@dataclass(frozen=True)
class GroupChatConfig:
    """Resolved ``groupchat`` section plus its env overrides."""

    enabled: bool
    base_url: str
    api_key: str | None
    timeout: float
    default_max_members: int
    agent: "GroupChatAgentConfig" = field(default_factory=lambda: GroupChatAgentConfig())

    @property
    def configured(self) -> bool:
        """Whether enough is set to attempt a request.

        ``enabled`` is the user's switch; ``base_url`` is the mechanical
        precondition. Requiring both means a half-filled section degrades to
        "not configured" instead of issuing a request against a placeholder.
        """
        return bool(self.enabled and self.base_url)

    @property
    def agent_configured(self) -> bool:
        """Whether the agent bridge should run at all."""
        return bool(self.configured and self.agent.enabled)

    @classmethod
    def from_config(cls, config_base: Mapping[str, Any] | None) -> "GroupChatConfig":
        raw = config_base.get("groupchat") if isinstance(config_base, Mapping) else None
        section: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}

        base_url = str(section.get("base_url") or DEFAULT_BASE_URL).strip()
        # An explicitly empty base_url means "configured off", not "use the
        # default" — the default only applies when the key is absent entirely,
        # so clearing the value in config.yaml actually disables the feature.
        if "base_url" in section and not str(section.get("base_url") or "").strip():
            base_url = ""
        try:
            parsed = urllib.parse.urlparse(base_url)
        except ValueError:
            parsed = None
        if base_url and (
            parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc
        ):
            # An unusable URL is treated as "not configured" rather than a hard
            # error: this section is optional and a typo must not take down the
            # AgentServer at import time.
            base_url = ""

        api_key = str(section.get("api_key") or "").strip() or None
        timeout_raw = section.get("timeout", DEFAULT_TIMEOUT)
        try:
            timeout = float(timeout_raw)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        if timeout <= 0:
            timeout = DEFAULT_TIMEOUT

        max_members_raw = section.get("default_max_members", DEFAULT_MAX_MEMBERS)
        try:
            default_max_members = int(max_members_raw)
        except (TypeError, ValueError):
            default_max_members = DEFAULT_MAX_MEMBERS
        if default_max_members < 1:
            default_max_members = DEFAULT_MAX_MEMBERS

        enabled = bool(section.get("enabled", False))

        # Env wins over config.yaml, matching the A2A gateway's convention.
        base_url = _env_str("GROUPCHAT_BASE_URL", base_url)
        api_key = _env_str("GROUPCHAT_API_KEY", api_key or "") or None
        timeout = _env_float("GROUPCHAT_TIMEOUT", timeout)
        default_max_members = _env_int("GROUPCHAT_DEFAULT_MAX_MEMBERS", default_max_members)
        if "GROUPCHAT_ENABLED" in os.environ:
            enabled = _env_bool("GROUPCHAT_ENABLED", enabled)

        return cls(
            enabled=enabled,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            default_max_members=default_max_members,
            agent=GroupChatAgentConfig.from_section(section),
        )


def resolve_groupchat_config(config_base: Mapping[str, Any] | None = None) -> GroupChatConfig:
    """Resolve the ``groupchat`` section, reading ``get_config()`` when omitted."""
    if config_base is None:
        from jiuwenswarm.common.config import get_config

        config_base = get_config()
    return GroupChatConfig.from_config(config_base)
