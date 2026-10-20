"""软删除与批量删除审批配置。写入 ``permissions.soft_delete``。"""

from __future__ import annotations

from typing import Any

_PATCH_KEYS = frozenset({"enabled", "archive_dir", "batch_delete_threshold"})


def live_soft_delete_enabled(_config: Any = None) -> bool:
    """每次判定时读当前配置。只认布尔 ``true``。"""
    from jiuwenswarm.common.config import get_config, get_file_soft_delete_settings

    enabled, _archive = get_file_soft_delete_settings(get_config())
    return enabled


def get_soft_delete_config() -> dict[str, Any]:
    """返回安全页要展示的三个字段。缺省阈值按 100。"""
    from jiuwenswarm.common.config import get_config

    cfg = get_config() or {}
    permissions = cfg.get("permissions") if isinstance(cfg.get("permissions"), dict) else {}
    section = permissions.get("soft_delete") if isinstance(permissions, dict) else None
    if not isinstance(section, dict):
        section = {}
    archive = section.get("archive_dir", "")
    if not isinstance(archive, str):
        archive = ""
    return {
        "enabled": section.get("enabled") is True,
        "archive_dir": archive,
        "batch_delete_threshold": _threshold(section.get("batch_delete_threshold", 100)),
    }


def patch_soft_delete(data: dict[str, Any], patch: Any) -> dict[str, Any]:
    """把补丁写进内存中的配置。未知字段、非法类型直接拒绝。"""
    if not isinstance(patch, dict) or not patch or set(patch) - _PATCH_KEYS:
        raise ValueError("patch accepts enabled, archive_dir, batch_delete_threshold")
    if "enabled" in patch and not isinstance(patch["enabled"], bool):
        raise ValueError("enabled must be boolean")
    if "archive_dir" in patch and not isinstance(patch["archive_dir"], str):
        raise ValueError("archive_dir must be a string")
    if "batch_delete_threshold" in patch:
        raw = patch["batch_delete_threshold"]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ValueError("batch_delete_threshold must be an integer >= 0")

    section = data.setdefault("permissions", {}).setdefault("soft_delete", {})
    if "enabled" in patch:
        section["enabled"] = patch["enabled"]
    if "archive_dir" in patch:
        section["archive_dir"] = patch["archive_dir"].strip()
    if "batch_delete_threshold" in patch:
        section["batch_delete_threshold"] = patch["batch_delete_threshold"]
    return data


def update_soft_delete_config(patch: Any) -> dict[str, Any]:
    from jiuwenswarm.common.config import update_config

    update_config(lambda data: patch_soft_delete(data, patch))
    return get_soft_delete_config()


def _threshold(raw: Any) -> int:
    if isinstance(raw, bool):
        return 100
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 100
    return value if value >= 0 else 0
