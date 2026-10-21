# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Client for the relay's group chat API (the "项目" feature).

A "project" in the web UI is a group on an ``agent-registry-relay``
deployment. This package owns the HTTP conversation with that API; the
AgentServer exposes it to the frontend through the ``team.create`` /
``team.projects.list`` RPCs.

Deliberately independent of ``jiuwenswarm.agents.harness.team.a2x``: that
client talks to the *registry* (datasets, services, reservations) and its
``_internal`` module is documented as not importing from the rest of the
project.
"""

from .bridge import (
    GroupChatBridge,
    get_bridge,
    start_bridge,
    stop_bridge,
)
from .client import AsyncGroupChatClient
from .config import (
    DEFAULT_AGENT_CONTEXT_MESSAGES,
    DEFAULT_AGENT_TURN_TIMEOUT,
    DEFAULT_BASE_URL,
    DEFAULT_MAX_MEMBERS,
    DEFAULT_TIMEOUT,
    JOIN_POLICIES,
    MAX_MESSAGE_CHARS,
    MAX_NAME_CHARS,
    ROLES,
    GroupChatAgentConfig,
    GroupChatConfig,
    resolve_groupchat_config,
)
from .errors import (
    GroupChatError,
    GroupChatNotConfiguredError,
    GroupChatRequestInvalidError,
    GroupChatUnavailableError,
)
from .progress import (
    PROGRESS_EVENT_TYPE,
    TurnProgressTracker,
)
from .runtime import (
    accept_invite,
    ack_messages,
    build_groupchat_client,
    create_group,
    create_invite,
    delete_group,
    fetch_messages,
    get_group,
    leave_group,
    list_groups,
    list_members,
    remove_member,
    send_message,
    set_archived,
    set_member_role,
    whoami,
)
from .subscription import (
    GroupSubscriptionHub,
    get_subscription_hub,
    reset_subscription_hub_for_tests,
)

__all__ = [
    "AsyncGroupChatClient",
    "DEFAULT_AGENT_CONTEXT_MESSAGES",
    "DEFAULT_AGENT_TURN_TIMEOUT",
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_MEMBERS",
    "DEFAULT_TIMEOUT",
    "GroupChatAgentConfig",
    "GroupChatBridge",
    "GroupChatConfig",
    "GroupChatError",
    "GroupChatNotConfiguredError",
    "GroupChatRequestInvalidError",
    "GroupChatUnavailableError",
    "GroupSubscriptionHub",
    "JOIN_POLICIES",
    "MAX_MESSAGE_CHARS",
    "MAX_NAME_CHARS",
    "PROGRESS_EVENT_TYPE",
    "ROLES",
    "TurnProgressTracker",
    "accept_invite",
    "ack_messages",
    "build_groupchat_client",
    "create_group",
    "create_invite",
    "delete_group",
    "fetch_messages",
    "get_bridge",
    "get_group",
    "get_subscription_hub",
    "leave_group",
    "list_groups",
    "list_members",
    "remove_member",
    "reset_subscription_hub_for_tests",
    "resolve_groupchat_config",
    "send_message",
    "set_archived",
    "set_member_role",
    "start_bridge",
    "stop_bridge",
    "whoami",
]
