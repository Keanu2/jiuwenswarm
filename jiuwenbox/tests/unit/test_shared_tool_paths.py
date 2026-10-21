# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Explicit host tool declarations drive ACL setup and command resolution."""

import os
import json
from types import SimpleNamespace

import pytest

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.server import policy_reader, shared_paths
from jiuwenbox.server.sandbox_manager import _build_windows_exec_env
from jiuwenbox.supervisor.win_setup import collect_preinstall_paths


@pytest.fixture
def tools(tmp_path, monkeypatch):
    base = tmp_path / "runtime" / "python-win"
    venv = tmp_path / "shared-deps" / "py-venv"
    node = tmp_path / "runtime" / "node-win"
    for file in (base / "python.exe", venv / "Scripts" / "python.exe", node / "node.exe"):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.touch()
    (venv / "pyvenv.cfg").write_text(f"home = {base}\nexecutable = {base / 'python.exe'}\n")
    monkeypatch.setenv("JIUWENBOX_TOOL_PATHS", json.dumps({"read_roots": [str(base), str(venv), str(node)],
        "executable_dirs": [str(venv / "Scripts"), str(node)]}))
    monkeypatch.delenv("JIUWENSWARM_DATA_DIR", raising=False)
    monkeypatch.delenv("JIUWENBOX_BUNDLED_PYTHON", raising=False)
    monkeypatch.delenv("JIUWENBOX_VENV_DIR", raising=False)
    monkeypatch.setenv("JIUWENBOX_PATH_ALLOWLIST", json.dumps([str(venv.parent)]))
    return base, venv, node


def test_declared_venv_base_and_node_are_authorized_without_their_parents(tools, tmp_path):
    base, venv, node = tools
    roots, directories = shared_paths.configured_tool_paths()
    assert set(roots) == {str(base), str(venv), str(node)}
    assert directories == [str(venv / "Scripts"), str(node)]
    assert str(tmp_path) not in roots
    assert str(venv.parent.parent) not in roots
    assert shared_paths.shared_tool_paths()[1] == [str(venv.parent)]


def test_tool_roots_reach_setup_even_when_all_tool_fields_are_explicit(tools):
    policy = SecurityPolicy.model_validate({"windows": {"filesystem": {
        "tool_paths": {"python_dir": "configured-python", "node_dir": "configured-node",
                       "git_dir": "configured-git", "bash_path": "configured-bash"},
    }}})
    resolved = policy_reader._resolve_tool_paths(policy)
    assert resolved.windows.filesystem.tool_paths == policy.windows.filesystem.tool_paths
    assert set(shared_paths.configured_tool_paths()[0]).issubset(collect_preinstall_paths(resolved))
    assert policy_reader._resolve_tool_paths(resolved) == resolved


def test_managed_python_precedes_runner_and_request_cannot_add_acl_roots(tools, monkeypatch):
    _, venv, node = tools
    monkeypatch.setenv("JIUWENBOX_ENV_ALLOWLIST", "[]")
    result = _build_windows_exec_env(
        {"PATH": "caller-path", "SELECTED_PYTHON_EXE": "untrusted-request-python",
         "JIUWENBOX_TOOL_PATHS": json.dumps({"read_roots": ["C:/request-secret"],
                                            "executable_dirs": ["C:/request-secret"]}),
         "JIUWENBOX_PATH_ALLOWLIST": json.dumps(["C:/request-secret"])},
        tool_paths=SimpleNamespace(python_dir="runner-python", node_dir="system-node", git_dir="", bash_path=""),
    )
    assert result["PATH"].split(os.pathsep)[:2] == [str(venv / "Scripts"), str(node)]
    assert result["PATH"].endswith("caller-path")
    assert "untrusted-request-python" not in shared_paths.configured_tool_paths()[0]
    assert "C:/request-secret" not in shared_paths.shared_tool_paths()[0]


def test_skill_python_is_not_redirected_to_isolation_venv(
    tools, tmp_path, monkeypatch,
):
    _, shared, _ = tools
    venv = tmp_path / "isolation_venv"
    python = venv / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setenv("JIUWENBOX_VENV_DIR", str(venv))
    selected = str(shared / "Scripts" / "python.exe")
    result = _build_windows_exec_env({"SELECTED_PYTHON_EXE": selected})
    assert result["SELECTED_PYTHON_EXE"] == selected
    assert "VIRTUAL_ENV" not in result
    assert result["PATH"].split(os.pathsep)[0] == str(shared / "Scripts")
    assert str(shared) in shared_paths.configured_tool_paths()[0]
    assert str(venv) not in shared_paths.configured_tool_paths()[0]


@pytest.mark.asyncio
async def test_session_creation_never_grants_or_tracks_shared_tool_acl(tools, tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    import jiuwenbox.supervisor as supervisor
    from jiuwenbox.server.runtime.process import ProcessRuntime

    acl, setup = MagicMock(), MagicMock()
    shared, _ = shared_paths.shared_tool_paths()
    setup.get_preinstalled_read_paths.return_value = {"runner-python"}
    acl.resolve_desktop_data_dir.return_value = str(tmp_path)
    acl.apply_sandbox_acl.return_value = [str(tmp_path), *shared]
    acl.apply_desktop_data_rw.return_value = []
    setup.get_sandbox_user_password.return_value = None
    monkeypatch.setattr(supervisor, "win_acl", acl, raising=False)
    monkeypatch.setattr(supervisor, "win_setup", setup, raising=False)
    runtime = ProcessRuntime()
    monkeypatch.setattr(runtime, "_load_policy", lambda _: SecurityPolicy(version=1))
    monkeypatch.setattr(runtime, "_win_workspace_for", lambda *a: str(tmp_path))
    with pytest.raises(RuntimeError, match="无法读取 jbx-sandbox"):
        await runtime._create_windows("tool-probe", tmp_path / "policy.yaml", {})
    acl.apply_tool_acl.assert_not_called()
    assert set(acl.apply_sandbox_acl.call_args.kwargs["preserve_roots"]) == set(shared)
    assert set(acl.apply_desktop_data_rw.call_args.kwargs["preserve_read_roots"]) == set(shared)
    assert not any(p in shared for p in acl.apply_sandbox_acl.call_args.args[1])
    assert runtime._win_acl_paths["tool-probe"] == [str(tmp_path)]
    first = acl.grant_parent_traverse.call_args_list[0]
    assert "runner-python" in first.kwargs["filesystem_policy"]["allow_read"]


def test_existing_runtime_venv_is_a_service_owned_writable_root(tools, tmp_path, monkeypatch):
    venv = tmp_path / "isolation_venv"
    venv.mkdir()
    monkeypatch.setenv("JIUWENBOX_VENV_DIR", str(venv))
    roots, writable = shared_paths.shared_tool_paths()
    assert str(venv) in roots and str(venv) in writable


@pytest.mark.asyncio
async def test_session_stop_preserves_shared_roots_including_legacy_acl_lists(tools, tmp_path, monkeypatch):
    from unittest.mock import Mock
    from jiuwenbox.server.runtime.process import ProcessRuntime
    from jiuwenbox.supervisor import win_acl

    shared, _ = shared_paths.shared_tool_paths()
    runtime = ProcessRuntime()
    # Include a shared root recorded by the previous implementation and an
    # ancestor that would otherwise walk back into it during revocation.
    runtime._win_acl_paths["legacy"] = [str(tmp_path), *shared]
    runtime._win_sandbox_sids["legacy"] = "sandbox"
    revoke = Mock()
    monkeypatch.setattr(win_acl, "revoke_sandbox_acl", revoke)
    await runtime._stop_windows("legacy")
    assert revoke.call_args.kwargs["preserve_roots"] == shared
    assert "legacy" not in runtime._win_acl_paths
