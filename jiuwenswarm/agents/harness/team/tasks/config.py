# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Configuration for the personal task-continuation client.

The tasks API lives on an ``agent-registry-relay`` deployment (see the
``a2x_registry/tasks`` package there) and is configured separately from the
registry this process *registers* into: that section describes discovery, while
this one only answers "where is the task API and who am I there".

The ``tasks`` section is optional. When it is absent, disabled, or has no
``base_url``, the feature reports itself as not configured rather than failing —
same posture as the group chat module.

Unlike group chat there is no ``agent`` subsection: tasks are personal (one
principal), and the relay identifies the *device* as the API key (`key_id`) that
authenticated the request. The extra knobs below are the lease timing the
bridge uses to stay ahead of the relay's TTL sweeper.
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 15.0

# Mirrors the relay's ``a2x_registry/tasks/models.py`` enums. Kept as local
# copies on purpose — the client package ships independently and must not
# import from the relay.
TASK_STATES: tuple[str, ...] = (
    "pending",
    "working",
    "input_required",
    "completed",
    "failed",
    "canceled",
)
TERMINAL_STATES: tuple[str, ...] = ("completed", "failed", "canceled")

MESSAGE_KINDS: tuple[str, ...] = ("text", "event", "system", "a2x_request", "tool_result")
"""Mirrors the relay's ``MESSAGE_KINDS``; ``event``/``system`` are server-only."""

CLIENT_WRITABLE_KINDS: tuple[str, ...] = ("text", "tool_result", "a2x_request")
"""Kinds a client may write; the relay rejects the rest."""

MAX_TITLE_CHARS = 200
"""Relay-side ``MAX_TITLE_CHARS`` (``service.py``); longer titles are rejected."""

MAX_MESSAGE_BYTES = 64 * 1024
"""Local ceiling on a message payload, the cheap guard before the relay's 413."""

# Lease timing. The relay's TTL is crash recovery only (the fencing token is
# the truth); these govern how this client asks the relay to keep it alive.
DEFAULT_LEASE_TTL_SECONDS = 300
DEFAULT_RENEW_INTERVAL_SECONDS = 60

DEFAULT_ARTIFACT_MAX_BYTES = 64 * 1024 * 1024
"""交接上传的单文件上限（本地侧）。relay 侧另有 256MB 的硬顶。"""

DEFAULT_ARTIFACT_TTL_SECONDS = 86_400
"""存档文件的存活时间，对齐 relay 的 24h 上限：交接后另一台设备当天能取。"""


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


@dataclass(frozen=True)
class TasksConfig:
    """Resolved ``tasks`` section plus its env overrides."""

    enabled: bool
    base_url: str
    api_key: str | None
    timeout: float
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS
    renew_interval_seconds: int = DEFAULT_RENEW_INTERVAL_SECONDS
    artifact_base_url: str = ""
    artifact_create_token: str | None = None
    artifact_max_bytes: int = DEFAULT_ARTIFACT_MAX_BYTES
    artifact_ttl_seconds: int = DEFAULT_ARTIFACT_TTL_SECONDS

    @property
    def configured(self) -> bool:
        """Whether enough is set to attempt a request.

        ``enabled`` is the user's switch; ``base_url`` is the mechanical
        precondition. Requiring both means a half-filled section degrades to
        "not configured" instead of issuing a request against a placeholder.
        """
        return bool(self.enabled and self.base_url)

    @property
    def artifacts_configured(self) -> bool:
        """Whether session files can be archived through the artifact relay.

        The artifact relay lives on the same deployment as the task API in
        every planned topology, so ``base_url`` is the natural default for
        ``artifact_base_url``; only the deployment-level create token has no
        default — without it the feature reports itself off rather than
        sending requests that are guaranteed to 401.
        """
        return bool(self.artifact_base_url and self.artifact_create_token)

    @property
    def renew_interval(self) -> float:
        """How often the bridge renews the lease, in seconds.

        Defaults to ``renew_interval_seconds``, but is always clamped to at
        most half the TTL so a renew can never expire before the next one —
        and never below 1s, so a misconfigured 0 cannot busy-loop the relay.
        """
        interval = self.renew_interval_seconds
        ceiling = max(1, self.lease_ttl_seconds // 2)
        return float(max(1, min(interval, ceiling)))

    @classmethod
    def from_config(cls, config_base: Mapping[str, Any] | None) -> "TasksConfig":
        raw = config_base.get("tasks") if isinstance(config_base, Mapping) else None
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

        lease_ttl = _section_int(section, "lease_ttl_seconds", DEFAULT_LEASE_TTL_SECONDS)
        renew_interval = _section_int(
            section, "renew_interval_seconds", DEFAULT_RENEW_INTERVAL_SECONDS
        )

        # artifact-relay：默认与任务 API 同一部署；create token 无默认值，
        # 缺失即视为未启用（见 artifacts_configured）。
        artifact_base_url = (
            str(section.get("artifact_base_url") or "").strip() or base_url
        )
        artifact_create_token = (
            str(section.get("artifact_create_token") or "").strip() or None
        )
        artifact_max_bytes = _section_int(
            section, "artifact_max_bytes", DEFAULT_ARTIFACT_MAX_BYTES
        )
        artifact_ttl_seconds = _section_int(
            section, "artifact_ttl_seconds", DEFAULT_ARTIFACT_TTL_SECONDS
        )

        enabled = bool(section.get("enabled", False))

        # Env wins over config.yaml, matching the A2A gateway's convention.
        base_url = _env_str("TASKS_BASE_URL", base_url)
        api_key = _env_str("TASKS_API_KEY", api_key or "") or None
        timeout = _env_float("TASKS_TIMEOUT", timeout)
        lease_ttl = _env_int("TASKS_LEASE_TTL_SECONDS", lease_ttl)
        renew_interval = _env_int("TASKS_RENEW_INTERVAL_SECONDS", renew_interval)
        artifact_base_url = _env_str("TASKS_ARTIFACT_BASE_URL", artifact_base_url)
        artifact_create_token = (
            _env_str("TASKS_ARTIFACT_CREATE_TOKEN", artifact_create_token or "") or None
        )
        artifact_max_bytes = _env_int(
            "TASKS_ARTIFACT_MAX_BYTES", artifact_max_bytes
        )
        artifact_ttl_seconds = _env_int(
            "TASKS_ARTIFACT_TTL_SECONDS", artifact_ttl_seconds
        )
        if "TASKS_ENABLED" in os.environ:
            enabled = _env_bool("TASKS_ENABLED", enabled)

        return cls(
            enabled=enabled,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            lease_ttl_seconds=lease_ttl,
            renew_interval_seconds=renew_interval,
            artifact_base_url=artifact_base_url.rstrip("/"),
            artifact_create_token=artifact_create_token,
            artifact_max_bytes=artifact_max_bytes,
            artifact_ttl_seconds=artifact_ttl_seconds,
        )


def _section_int(section: Mapping[str, Any], key: str, default: int) -> int:
    try:
        value = int(section.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def resolve_tasks_config(config_base: Mapping[str, Any] | None = None) -> TasksConfig:
    """Resolve the ``tasks`` section, reading ``get_config()`` when omitted."""
    if config_base is None:
        from jiuwenswarm.common.config import get_config

        config_base = get_config()
    return TasksConfig.from_config(config_base)