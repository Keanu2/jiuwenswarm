# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Startup assembly for Agent-Core automatic file backup.

Agent-Core exposes :class:`openjiuwen.core.sys_operation.local.file_backup.FileBackupManager`
and a process-level registry (``set_backup_manager`` / ``set_round_provider``).
JiuwenSwarm owns the product-facing configuration and wires the manager up here
at AgentServer startup, mirroring :mod:`jiuwenswarm.server.runtime.file_operation_history_patch`.

The manager is process-global and shares one total quota across all projects and
sessions.  Dedup uses the current session id (``CURRENT_SESSION_ID``): one
snapshot per file for the life of that session.
"""

from __future__ import annotations

import logging
from typing import Any

from jiuwenswarm.common.config import get_file_backup_settings

logger = logging.getLogger(__name__)

_PATCHED = False


def _current_backup_scope() -> str | None:
    """Session id used as the backup dedup scope.

    The same session and the same absolute path keep a single snapshot.
    An empty context returns ``None`` so agent-core can fall back to its
    own session id.
    """
    try:
        from jiuwenswarm.agents.harness.common.channel_runtime_context import (
            CURRENT_SESSION_ID,
        )

        session_id = (CURRENT_SESSION_ID.get() or "").strip()
        if session_id:
            return session_id
    except Exception:  # noqa: BLE001 - fail-open; dedup falls back to session
        logger.debug("[FileBackupPatch] session_id resolve failed", exc_info=True)
    return None


def configure_file_backup(config: dict[str, Any] | None) -> None:
    """Apply the auto-backup policy from an already loaded config.

    Registers a :class:`FileBackupManager` when ``file_backup.enabled`` is true,
    otherwise clears any previously registered manager. Always installs the
    session-scope provider so dedup keys are stable regardless of enablement.
    """
    try:
        from openjiuwen.core.sys_operation.local.file_backup import (
            FileBackupManager,
            set_backup_manager,
            set_round_provider,
        )
    except ImportError as exc:
        # 钉版 agent-core 尚未带上 file_backup 时不阻断 AgentServer 启动。
        logger.warning(
            "[FileBackupPatch] agent-core has no file_backup; skip auto backup: %s",
            exc,
        )
        return

    set_round_provider(_current_backup_scope)

    enabled, backup_dir, max_bytes = get_file_backup_settings(config)
    if not enabled or not max_bytes or not backup_dir:
        set_backup_manager(None)
        logger.info(
            "[FileBackupPatch] auto backup disabled (enabled=%s max_bytes=%s)",
            enabled,
            max_bytes,
        )
        return

    try:
        manager = FileBackupManager(
            backup_dir=backup_dir,
            max_total_bytes=max_bytes,
        )
        set_backup_manager(manager)
        logger.info(
            "[FileBackupPatch] auto backup enabled backup_dir=%s max_bytes=%d",
            backup_dir,
            max_bytes,
        )
    except Exception as exc:  # noqa: BLE001 - fail-open at startup
        logger.warning("[FileBackupPatch] auto backup setup failed: %s", exc)
        set_backup_manager(None)


__all__ = ["configure_file_backup", "_current_backup_scope"]