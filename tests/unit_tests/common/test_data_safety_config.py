"""软删除与自动备份配置接口。"""

from copy import deepcopy

import pytest
import yaml

from jiuwenswarm.common import config
from jiuwenswarm.common import file_backup_config as backup
from jiuwenswarm.common import soft_delete_config as soft
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import (
    dispatch_permissions_config_request,
    get_permissions_read_only_req_methods,
)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump({"permissions": {"enabled": True, "rules": []}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "_CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "get_config_file", lambda: path)
    monkeypatch.setattr(backup, "apply_file_backup_config", lambda: None)
    return path


def request(method, params):
    return dispatch_permissions_config_request(AgentRequest(
        request_id="data-safety", channel_id="web", req_method=method, params=params,
    ))


def test_soft_delete_roundtrip_keeps_other_permissions(config_file):
    before = deepcopy(config.get_config()["permissions"])
    assert soft.get_soft_delete_config() == {
        "enabled": False,
        "archive_dir": "",
        "batch_delete_threshold": 100,
    }
    saved = soft.update_soft_delete_config({
        "enabled": True,
        "archive_dir": " D:/trash ",
        "batch_delete_threshold": 20,
    })
    assert saved == {
        "enabled": True,
        "archive_dir": "D:/trash",
        "batch_delete_threshold": 20,
    }
    persisted = yaml.safe_load(config_file.read_text(encoding="utf-8"))["permissions"]
    section = persisted.pop("soft_delete")
    assert section == {
        "enabled": True,
        "archive_dir": "D:/trash",
        "batch_delete_threshold": 20,
    }
    assert persisted == before


@pytest.mark.parametrize("patch", [
    None,
    {},
    {"enabled": "true"},
    {"batch_delete_threshold": -1},
    {"batch_delete_threshold": True},
    {"unknown": 1},
])
def test_soft_delete_invalid_patch_does_not_write(config_file, patch):
    before = config_file.read_bytes()
    with pytest.raises(ValueError):
        soft.update_soft_delete_config(patch)
    assert config_file.read_bytes() == before


def test_file_backup_roundtrip(config_file):
    assert backup.get_file_backup_config()["enabled"] is False
    assert backup.get_file_backup_config()["max_total_mb"] == 500
    saved = backup.update_file_backup_config({
        "enabled": True,
        "backup_dir": " D:/bak ",
        "max_total_mb": 8,
    })
    assert saved == {"enabled": True, "backup_dir": "D:/bak", "max_total_mb": 8}


def test_rpc_get_set_and_read_only_classification(config_file):
    result = request(ReqMethod.PERMISSIONS_SOFT_DELETE_GET, {})
    assert result.ok and result.payload["soft_delete"]["batch_delete_threshold"] == 100
    result = request(ReqMethod.PERMISSIONS_SOFT_DELETE_SET, {"patch": {"enabled": True, "batch_delete_threshold": 0}})
    assert result.ok
    assert result.payload["soft_delete"]["enabled"] is True
    assert result.payload["soft_delete"]["batch_delete_threshold"] == 0

    result = request(ReqMethod.FILE_BACKUP_SET, {"patch": {"enabled": True, "max_total_mb": 2}})
    assert result.ok and result.payload["applied"] is True
    assert result.payload["file_backup"]["max_total_mb"] == 2
    result = request(ReqMethod.FILE_BACKUP_SET, {"patch": {"max_total_mb": "500"}})
    assert not result.ok and result.payload["code"] == "BAD_REQUEST"

    assert ReqMethod.PERMISSIONS_SOFT_DELETE_GET in get_permissions_read_only_req_methods()
    assert ReqMethod.FILE_BACKUP_GET in get_permissions_read_only_req_methods()
    assert ReqMethod.PERMISSIONS_SOFT_DELETE_SET not in get_permissions_read_only_req_methods()
    assert ReqMethod.FILE_BACKUP_SET not in get_permissions_read_only_req_methods()


def test_upgrade_keeps_clicked_values():
    from jiuwenswarm.common.config_split import extract_user_keep_from_legacy

    user = {
        "permissions": {"soft_delete": {"enabled": True, "batch_delete_threshold": 20, "archive_dir": "D:/trash"}},
        "file_backup": {"enabled": True, "backup_dir": "D:/bak", "max_total_mb": 8},
    }
    kept = extract_user_keep_from_legacy(user, {})
    assert kept["permissions"]["soft_delete"]["enabled"] is True
    assert kept["permissions"]["soft_delete"]["batch_delete_threshold"] == 20
    assert kept["file_backup"]["max_total_mb"] == 8
