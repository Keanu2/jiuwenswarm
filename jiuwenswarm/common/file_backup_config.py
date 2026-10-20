"""写前自动备份配置。写入顶层 ``file_backup``。"""

from __future__ import annotations

from typing import Any

_PATCH_KEYS = frozenset({"enabled", "backup_dir", "max_total_mb"})


def get_file_backup_config() -> dict[str, Any]:
    """返回安全页要展示的字段。未配置时视为关闭，配额缺省 500 MiB。"""
    from jiuwenswarm.common.config import get_config

    cfg = get_config() or {}
    section = cfg.get("file_backup")
    if not isinstance(section, dict):
        section = {}
    backup_dir = section.get("backup_dir", "")
    if not isinstance(backup_dir, str):
        backup_dir = ""
    return {
        "enabled": section.get("enabled") is True,
        "backup_dir": backup_dir,
        "max_total_mb": _quota_mb(section.get("max_total_mb", 500)),
    }


def patch_file_backup(data: dict[str, Any], patch: Any) -> dict[str, Any]:
    """把补丁写进内存中的配置。未知字段、非法类型直接拒绝。"""
    if not isinstance(patch, dict) or not patch or set(patch) - _PATCH_KEYS:
        raise ValueError("patch accepts enabled, backup_dir, max_total_mb")
    if "enabled" in patch and not isinstance(patch["enabled"], bool):
        raise ValueError("enabled must be boolean")
    if "backup_dir" in patch and not isinstance(patch["backup_dir"], str):
        raise ValueError("backup_dir must be a string")
    if "max_total_mb" in patch:
        raw = patch["max_total_mb"]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ValueError("max_total_mb must be an integer >= 0")

    section = data.setdefault("file_backup", {})
    if "enabled" in patch:
        section["enabled"] = patch["enabled"]
    if "backup_dir" in patch:
        section["backup_dir"] = patch["backup_dir"].strip()
    if "max_total_mb" in patch:
        section["max_total_mb"] = patch["max_total_mb"]
    return data


def update_file_backup_config(patch: Any) -> dict[str, Any]:
    from jiuwenswarm.common.config import update_config

    update_config(lambda data: patch_file_backup(data, patch))
    return get_file_backup_config()


def apply_file_backup_config() -> None:
    """按当前配置重建进程内备份管理器。"""
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.server.runtime.file_backup_patch import configure_file_backup

    configure_file_backup(get_config())


def _quota_mb(raw: Any) -> int:
    if isinstance(raw, bool):
        return 500
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 500
    return value if value >= 0 else 0
