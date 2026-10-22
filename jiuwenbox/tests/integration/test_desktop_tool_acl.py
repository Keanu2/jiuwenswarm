# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Real NTFS checks: protected, existing tool files are RX and retain denials.

XIAOYI_WORK_ADAPTER: apply_desktop_data_rw cases belong to the host adapter.
Preserve generic RX, protected-DACL, explicit-deny and idempotence assertions.
"""

import sys

import pytest

from jiuwenbox.supervisor import win_acl, win_constants as const


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows ACLs")
def test_existing_heavy_tool_files_receive_read_execute_not_write(tmp_path, monkeypatch):
    import win32security as security

    sid = "S-1-5-21-111111111-222222222-333333333-12345"
    monkeypatch.setattr(win_acl, "get_synthetic_write_sid", lambda: sid)
    desktop = tmp_path / "desktop"
    tool = desktop / "runtime" / "node-win"
    executable = tool / "node.exe"
    denied = tool / "private" / "secret.txt"
    sibling = desktop / "private-account" / "credential.txt"
    for file in (executable, denied, sibling):
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("fixture")
    sd = security.GetFileSecurity(str(executable), security.DACL_SECURITY_INFORMATION)
    sd.SetSecurityDescriptorControl(security.SE_DACL_PROTECTED, security.SE_DACL_PROTECTED)
    security.SetFileSecurity(str(executable), security.DACL_SECURITY_INFORMATION, sd)
    policy = {"allow_read": [], "allow_write": [], "deny_read": [str(denied.parent)], "deny_write": []}
    win_acl.apply_desktop_data_rw(str(desktop), filesystem_policy=policy)
    def entries(path):
        sd = security.GetFileSecurity(str(path), security.DACL_SECURITY_INFORMATION)
        return sd, win_acl._acl_entries(sd.GetSecurityDescriptorDacl())
    unchanged = entries(sibling)[1]
    owner_before = security.GetFileSecurity(str(executable), security.OWNER_SECURITY_INFORMATION).GetSecurityDescriptorOwner()
    win_acl.apply_tool_acl([str(tool)], sid, policy)
    first = entries(executable)[1]
    win_acl.apply_desktop_data_rw(str(desktop), filesystem_policy=policy,
                                  preserve_read_roots=[str(tool)])
    assert entries(executable)[1] == first, "desktop RW must not overwrite existing tool RX"
    win_acl.apply_tool_acl([str(tool)], sid, policy)
    assert entries(executable)[1] == first, "reapplying must be idempotent"
    assert entries(sibling)[1] == unchanged, "never authorize neighboring user data"
    assert security.GetFileSecurity(str(executable), security.OWNER_SECURITY_INFORMATION).GetSecurityDescriptorOwner() == owner_before
    sd, actual = entries(executable)
    assert sd.GetSecurityDescriptorControl()[0] & security.SE_DACL_PROTECTED
    allows = [mask for kind, _, mask, trustee in actual if trustee == sid and kind == const.ACCESS_ALLOWED_ACE_TYPE]
    assert allows and allows[0] & const.ALLOW_READ_EXECUTE_RIGHTS == const.ALLOW_READ_EXECUTE_RIGHTS
    assert not allows[0] & const.DENY_WRITE_RIGHTS
    assert any(kind == const.ACCESS_DENIED_ACE_TYPE and mask & const.DENY_READ_RIGHTS
               for kind, _, mask, trustee in entries(denied)[1] if trustee == sid)
    # An explicit deny on an ancestor must not be lifted by automatic tool RX.
    policy["deny_read"] = [str(desktop)]
    win_acl.apply_tool_acl([str(tool)], sid, policy)
    assert any(kind == const.ACCESS_DENIED_ACE_TYPE and mask & const.DENY_READ_RIGHTS
               for kind, _, mask, trustee in entries(executable)[1] if trustee == sid)


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows ACLs")
def test_writable_venv_replaces_old_rx_denials_and_keeps_explicit_denies(tmp_path, monkeypatch):
    import win32security as security

    sid = "S-1-5-21-111111111-222222222-333333333-12345"
    monkeypatch.setattr(win_acl, "get_synthetic_write_sid", lambda: sid)
    common = tmp_path / "skill-deps"
    venv = common / "py-venv"
    executable = venv / "Scripts" / "python.exe"
    denied = venv / "private.txt"
    executable.parent.mkdir(parents=True)
    executable.touch()
    denied.touch()
    policy = {"allow_read": [str(common)], "allow_write": [str(common)],
              "deny_read": [], "deny_write": [str(denied)]}
    win_acl.apply_tool_acl([str(common), str(venv)], sid, policy)
    win_acl.apply_tool_acl([str(common), str(venv)], sid, policy, writable_roots=[str(common)])

    def entries(path):
        sd = security.GetFileSecurity(str(path), security.DACL_SECURITY_INFORMATION)
        return win_acl._acl_entries(sd.GetSecurityDescriptorDacl())

    expected = entries(executable)
    allows = [mask for kind, _, mask, trustee in expected
              if kind == const.ACCESS_ALLOWED_ACE_TYPE and trustee == sid]
    assert allows[0] & const.ALLOW_WRITE_RIGHTS == const.ALLOW_WRITE_RIGHTS
    assert not any(kind == const.ACCESS_DENIED_ACE_TYPE and mask & const.DENY_WRITE_RIGHTS
                   for kind, _, mask, trustee in expected if trustee == sid)
    assert any(kind == const.ACCESS_DENIED_ACE_TYPE and mask & const.DENY_WRITE_RIGHTS
               for kind, _, mask, trustee in entries(denied) if trustee == sid)
    win_acl.apply_tool_acl([str(common), str(venv)], sid, policy, writable_roots=[str(common)])
    assert entries(executable) == expected


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows ACLs")
def test_explicit_writable_child_is_preserved_under_readonly_tool_root(tmp_path, monkeypatch):
    import win32security as security

    sid = "S-1-5-21-111111111-222222222-333333333-12345"
    monkeypatch.setattr(win_acl, "get_synthetic_write_sid", lambda: sid)
    root = tmp_path / "runtime"
    child = root / "venv"
    child.mkdir(parents=True)
    executable = child / "python.exe"
    executable.touch()
    policy = {"allow_read": [str(root)], "allow_write": [str(child)],
              "deny_read": [], "deny_write": []}
    win_acl.apply_tool_acl([str(child), str(root)], sid, policy, writable_roots=[str(child)])
    sd = security.GetFileSecurity(str(executable), security.DACL_SECURITY_INFORMATION)
    entries = win_acl._acl_entries(sd.GetSecurityDescriptorDacl())
    allows = [mask for kind, _, mask, trustee in entries
              if kind == const.ACCESS_ALLOWED_ACE_TYPE and trustee == sid]
    assert allows[0] & const.ALLOW_WRITE_RIGHTS == const.ALLOW_WRITE_RIGHTS
    assert not any(kind == const.ACCESS_DENIED_ACE_TYPE and mask & const.DENY_WRITE_RIGHTS
                   for kind, _, mask, trustee in entries if trustee == sid)
