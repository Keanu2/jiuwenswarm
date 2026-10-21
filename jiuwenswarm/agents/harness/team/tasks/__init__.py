# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Client + bridge for the relay's personal task-continuation API.

A "task" here is the *personal* cross-device continuation feature: one principal
starts an Agent task on one device (identifed by an API key) and continues it on
another. The relay (``a2x_registry/tasks``) is the authority for task state; this
package owns the HTTP conversation with that API, plus the proactive bridge that
holds a task's lease while the local Agent runs it.

Deliberately independent of ``jiuwenswarm.agents.harness.team.groupchat``: that
client talks to the *project* (multi-agent group-chat) data plane, whose
authority is the member list and the @-mention, while this one's authority is the
exclusive fencing token on a single owner's task.
"""

from .bridge import TaskBridge, get_bridge, start_bridge, stop_bridge
from .client import AsyncTasksClient
from .config import (
    CLIENT_WRITABLE_KINDS,
    DEFAULT_BASE_URL,
    DEFAULT_LEASE_TTL_SECONDS,
    DEFAULT_RENEW_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT,
    MAX_MESSAGE_BYTES,
    MAX_TITLE_CHARS,
    MESSAGE_KINDS,
    TASK_STATES,
    TERMINAL_STATES,
    TasksConfig,
    resolve_tasks_config,
)
from .errors import (
    TaskError,
    TaskFenceStaleError,
    TaskNotConfiguredError,
    TaskRequestInvalidError,
    TaskUnavailableError,
)
from .runtime import (
    ack_messages,
    acquire,
    append_message,
    build_tasks_client,
    checkpoint,
    create_task,
    fetch_messages,
    get_task,
    list_tasks,
    release,
    renew,
    retire,
    status,
    update_task,
    whoami,
)
from .subscription import (
    TaskSubscriptionHub,
    get_subscription_hub,
    reset_subscription_hub_for_tests,
    set_task_frame_callback,
)

__all__ = [
    "AsyncTasksClient",
    "CLIENT_WRITABLE_KINDS",
    "DEFAULT_BASE_URL",
    "DEFAULT_LEASE_TTL_SECONDS",
    "DEFAULT_RENEW_INTERVAL_SECONDS",
    "DEFAULT_TIMEOUT",
    "MAX_MESSAGE_BYTES",
    "MAX_TITLE_CHARS",
    "MESSAGE_KINDS",
    "TASK_STATES",
    "TERMINAL_STATES",
    "TaskBridge",
    "TaskError",
    "TaskFenceStaleError",
    "TaskNotConfiguredError",
    "TaskRequestInvalidError",
    "TaskSubscriptionHub",
    "TaskUnavailableError",
    "TasksConfig",
    "ack_messages",
    "acquire",
    "append_message",
    "build_tasks_client",
    "checkpoint",
    "create_task",
    "fetch_messages",
    "get_bridge",
    "get_subscription_hub",
    "get_task",
    "list_tasks",
    "release",
    "renew",
    "reset_subscription_hub_for_tests",
    "resolve_tasks_config",
    "retire",
    "set_task_frame_callback",
    "start_bridge",
    "status",
    "stop_bridge",
    "update_task",
    "whoami",
]