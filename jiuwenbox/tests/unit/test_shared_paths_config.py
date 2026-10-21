"""Generic shared-path configuration works without desktop product variables."""

import json
import os
from pathlib import Path

import pytest

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.server import policy_reader, shared_paths


@pytest.fixture(autouse=True)
def explicit_tool_defaults(monkeypatch):
    monkeypatch.setenv("JIUWENBOX_TOOL_PATHS", "{}")


def test_configured_paths_expand_and_work_without_desktop_tools(tmp_path, monkeypatch):
    root = tmp_path / "shared dependencies"
    monkeypatch.setenv("DEPENDENCIES_ROOT", str(root))
    monkeypatch.setenv("JIUWENBOX_PATH_ALLOWLIST", json.dumps(["$DEPENDENCIES_ROOT", str(root)]))
    for name in ("CLAW_PYTHON_EXE", "CLAW_NODE_EXE", "JIUWENBOX_VENV_DIR", "JIUWENBOX_BUNDLED_PYTHON"):
        monkeypatch.delenv(name, raising=False)
    assert shared_paths.configured_shared_paths() == [str(root)]
    assert shared_paths.shared_tool_paths() == ([str(root)], [str(root)])
    monkeypatch.setenv("JIUWENBOX_PATH_ALLOWLIST", "[]")
    assert shared_paths.shared_tool_paths() == ([], [])


@pytest.mark.parametrize("value", [None, "path", [1], [""], ["relative"], ["*"], [os.path.abspath(os.sep)]])
def test_invalid_shared_paths_fail_closed(monkeypatch, value):
    monkeypatch.setenv("JIUWENBOX_PATH_ALLOWLIST", json.dumps(value))
    with pytest.raises(ValueError):
        shared_paths.configured_shared_paths()


@pytest.mark.parametrize("setting", ["shared", "read_roots", "executable_dirs"])
def test_host_paths_reject_root_after_normalization(tmp_path, monkeypatch, setting):
    disguised_root = str(Path(tmp_path.anchor) / "dependencies" / "..")
    monkeypatch.setenv("HOST_SHARED_ROOT", disguised_root)
    for path in (disguised_root, "$HOST_SHARED_ROOT"):
        if setting == "shared":
            monkeypatch.setenv("JIUWENBOX_PATH_ALLOWLIST", json.dumps([path]))
            read = shared_paths.configured_shared_paths
        else:
            monkeypatch.setenv("JIUWENBOX_TOOL_PATHS", json.dumps({setting: [path]}))
            read = shared_paths.configured_tool_paths
        with pytest.raises(ValueError, match="absolute non-root"):
            read()


def test_no_host_declarations_mean_no_extra_permissions(tmp_path, monkeypatch):
    from jiuwenbox.server.sandbox_manager import _build_windows_exec_env

    monkeypatch.delenv("JIUWENBOX_PATH_ALLOWLIST", raising=False)
    monkeypatch.setenv("JIUWENBOX_ENV_ALLOWLIST", "[]")
    for name in ("JIUWENBOX_VENV_DIR", "JIUWENBOX_BUNDLED_PYTHON"):
        monkeypatch.delenv(name, raising=False)
    # Product variables alone must neither discover paths nor reach commands.
    for name in ("CLAW_PYTHON_EXE", "CLAW_PYTHON_HOME", "CLAW_NODE_EXE", "JIUWENSWARM_DATA_DIR"):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.setenv("CLAW_SKILL_TOKEN", "unlisted-host-token")
    assert shared_paths.shared_tool_paths() == ([], [])
    env = _build_windows_exec_env(None)
    assert not any(name.startswith("CLAW_") for name in env)
    assert "JIUWENSWARM_DATA_DIR" not in env


@pytest.mark.parametrize("value", [None, [], {"other": []}, {"read_roots": ["relative"]},
                                   {"executable_dirs": ["C:/uncovered"]}])
def test_invalid_host_tool_declarations_fail_closed(monkeypatch, value):
    monkeypatch.setenv("JIUWENBOX_TOOL_PATHS", json.dumps(value))
    with pytest.raises(ValueError, match="JIUWENBOX_TOOL_PATHS"):
        shared_paths.configured_tool_paths()


def test_configured_paths_reach_linux_mounts_without_overriding_readonly(tmp_path, monkeypatch):
    root = str(tmp_path / "shared")
    monkeypatch.setenv("JIUWENBOX_PATH_ALLOWLIST", json.dumps([root]))
    monkeypatch.setattr(policy_reader.sys, "platform", "linux")
    policy = SecurityPolicy(version=1)
    resolved = policy_reader._resolve_tool_paths(policy)
    assert root in resolved.filesystem_policy.read_write
    assert any(m.host_path == root and m.sandbox_path == root and m.mode == "rw"
               for m in resolved.filesystem_policy.bind_mounts)
    policy.filesystem_policy.read_only.append(root)
    resolved = policy_reader._resolve_tool_paths(policy)
    assert root not in resolved.filesystem_policy.read_write
    from jiuwenbox.supervisor.bwrap import BwrapConfig

    cfg = BwrapConfig(command=["true"])
    BwrapConfig._apply_filesystem(cfg, resolved.filesystem_policy)
    assert root in cfg.remount_ro
