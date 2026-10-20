# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""jiuwenswarm 自动备份装配（file_backup_patch）单元测试。"""

from __future__ import annotations

import os

from jiuwenswarm.server.runtime.file_backup_patch import (
    _current_backup_scope,
    configure_file_backup,
)
from openjiuwen.core.sys_operation.local.file_backup import (
    get_backup_manager,
    set_backup_manager,
)


def _cfg(enabled: bool, backup_dir: str, max_mb: int) -> dict:
    return {
        "file_backup": {
            "enabled": enabled,
            "backup_dir": backup_dir,
            "max_total_mb": max_mb,
        },
    }


def test_configure_enables_manager(tmp_path) -> None:
    bak = tmp_path / "bak"
    configure_file_backup(_cfg(True, str(bak), 1))
    mgr = get_backup_manager()
    try:
        assert mgr is not None
        assert mgr.enabled is True
        assert os.path.abspath(mgr.backup_dir) == os.path.abspath(str(bak))
        assert mgr.max_total_bytes == 1024 * 1024
    finally:
        set_backup_manager(None)


def test_configure_disabled_clears_manager(tmp_path) -> None:
    bak = tmp_path / "bak"
    # 先启用再禁用，确保 manager 被清空。
    configure_file_backup(_cfg(True, str(bak), 1))
    assert get_backup_manager() is not None
    configure_file_backup(_cfg(False, str(bak), 1))
    assert get_backup_manager() is None


def test_configure_missing_module_does_not_raise(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "openjiuwen.core.sys_operation.local.file_backup":
            raise ImportError("file_backup missing")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _import)
    configure_file_backup(_cfg(True, "D:/bak", 1))


def test_configure_zero_quota_disables(tmp_path) -> None:
    bak = tmp_path / "bak"
    configure_file_backup(_cfg(True, str(bak), 0))
    assert get_backup_manager() is None


def test_backup_scope_from_session_id() -> None:
    from jiuwenswarm.agents.harness.common.channel_runtime_context import (
        CURRENT_SESSION_ID,
    )

    token = CURRENT_SESSION_ID.set("sess-12345")
    try:
        assert _current_backup_scope() == "sess-12345"
    finally:
        CURRENT_SESSION_ID.reset(token)


def test_backup_scope_empty_session_returns_none() -> None:
    from jiuwenswarm.agents.harness.common.channel_runtime_context import (
        CURRENT_SESSION_ID,
    )

    stoken = CURRENT_SESSION_ID.set("")
    try:
        assert _current_backup_scope() is None
    finally:
        CURRENT_SESSION_ID.reset(stoken)
