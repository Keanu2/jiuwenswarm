# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""cron_push_gate 闸门单测（路由增强定案：占位帧不下发 + 无会话不推送）."""

from __future__ import annotations

from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import (
    cron_push_gate,
)


def test_placeholder_frame_is_skipped() -> None:
    cron_block = {"job_id": "j1", "is_placeholder": True, "status": "running"}
    should_push, reason = cron_push_gate(cron_block, "sess-1")
    assert should_push is False
    assert "placeholder" in reason


def test_result_frame_pushes() -> None:
    cron_block = {"job_id": "j1", "is_placeholder": False, "status": "succeeded"}
    should_push, reason = cron_push_gate(cron_block, "sess-1")
    assert should_push is True
    assert reason == ""


def test_no_session_blocks_push() -> None:
    cron_block = {"job_id": "j1", "is_placeholder": False}
    should_push, reason = cron_push_gate(cron_block, "")
    assert should_push is False
    assert "session" in reason


def test_missing_cron_block_pushes_with_session() -> None:
    should_push, _ = cron_push_gate(None, "sess-1")
    assert should_push is True


def test_missing_cron_block_and_session_blocks() -> None:
    should_push, reason = cron_push_gate(None, None)
    assert should_push is False
    assert "session" in reason


def test_placeholder_flag_defaults_false_when_absent() -> None:
    cron_block = {"job_id": "j1"}
    should_push, _ = cron_push_gate(cron_block, "sess-1")
    assert should_push is True
