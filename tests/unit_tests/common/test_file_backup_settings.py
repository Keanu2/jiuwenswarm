# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""jiuwenswarm.config 批量删除审批与自动备份配置解析单元测试。"""

from __future__ import annotations

import os

from jiuwenswarm.common.config import (
    get_batch_delete_guard_settings,
    get_file_backup_settings,
)


def test_batch_delete_guard_off_when_soft_delete_off() -> None:
    cfg = {"permissions": {"soft_delete": {"enabled": False}}}
    assert get_batch_delete_guard_settings(cfg) == (False, 0)


def test_batch_delete_guard_default_threshold_100() -> None:
    cfg = {"permissions": {"soft_delete": {"enabled": True}}}
    assert get_batch_delete_guard_settings(cfg) == (True, 100)


def test_batch_delete_guard_custom_threshold() -> None:
    cfg = {
        "permissions": {
            "soft_delete": {"enabled": True, "batch_delete_threshold": 50},
        },
    }
    assert get_batch_delete_guard_settings(cfg) == (True, 50)


def test_batch_delete_guard_zero_threshold_disables_count() -> None:
    cfg = {
        "permissions": {
            "soft_delete": {"enabled": True, "batch_delete_threshold": 0},
        },
    }
    assert get_batch_delete_guard_settings(cfg) == (True, 0)


def test_file_backup_off_by_default() -> None:
    assert get_file_backup_settings({}) == (False, "", 0)


def test_file_backup_on_explicit_dir() -> None:
    cfg = {"file_backup": {"enabled": True, "backup_dir": "D:/tmp/bak"}}
    enabled, d, quota = get_file_backup_settings(cfg, workspace_dir="D:/ws")
    assert enabled is True
    assert os.path.abspath(d) == os.path.abspath("D:/tmp/bak")
    assert quota == 524288000


def test_file_backup_default_dir() -> None:
    cfg = {"file_backup": {"enabled": True}}
    enabled, d, quota = get_file_backup_settings(cfg, workspace_dir="D:/ws")
    assert enabled is True
    assert os.path.abspath(d) == os.path.abspath("D:/ws/.backup")
    assert quota == 524288000


def test_file_backup_custom_quota() -> None:
    cfg = {"file_backup": {"enabled": True, "max_total_mb": 1}}
    enabled, d, quota = get_file_backup_settings(cfg, workspace_dir="D:/ws")
    assert enabled is True
    assert os.path.abspath(d) == os.path.abspath("D:/ws/.backup")
    assert quota == 1024 * 1024


def test_file_backup_zero_quota_disables() -> None:
    cfg = {"file_backup": {"enabled": True, "max_total_mb": 0}}
    enabled, d, quota = get_file_backup_settings(cfg, workspace_dir="D:/ws")
    assert enabled is True
    assert os.path.abspath(d) == os.path.abspath("D:/ws/.backup")
    assert quota == 0