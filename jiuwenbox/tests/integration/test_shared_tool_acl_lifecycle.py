"""Service-owned tool ACLs are persistent, cached and excluded from session cleanup."""

import sys
import json

import pytest

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.server import shared_paths
from jiuwenbox.supervisor import win_acl, win_constants as const, win_setup

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="real Windows ACLs")


def test_shared_acl_cache_invalidation_and_no_tree_walk_on_warm_start(tmp_path, monkeypatch):
    common = tmp_path / "skill-deps"
    node = tmp_path / "node"
    for root in (common, node):
        root.mkdir()
        (root / "existing.txt").touch()
    cache = tmp_path / "shared-tools-acl.json"
    sid = "S-1-5-21-111111111-222222222-333333333-12345"
    synthetic = "S-1-5-21-111111111-222222222-333333333-12346"
    monkeypatch.setattr(shared_paths, "shared_tool_paths", lambda read_roots=None: ([str(common), str(node)], [str(common)]))
    monkeypatch.setattr(shared_paths, "_shared_acl_cache_path", lambda: cache)
    monkeypatch.setattr(win_setup, "get_sandbox_user_sid", lambda: sid)
    monkeypatch.setattr(win_acl, "get_synthetic_write_sid", lambda: synthetic)
    # Keep fake SIDs confined to this fixture; no host-profile traversal grants.
    monkeypatch.setattr(win_acl, "grant_parent_traverse", lambda *a, **kw: None)
    walked = []
    walk = win_acl._walk_acl_objects

    def counted(root, **kwargs):
        walked.append(root)
        yield from walk(root, **kwargs)

    monkeypatch.setattr(win_acl, "_walk_acl_objects", counted)
    policy = SecurityPolicy(version=1)
    shared_paths.prepare_shared_tool_acl(policy)
    assert set(walked) == {str(common), str(node)}
    walked.clear()
    shared_paths.prepare_shared_tool_acl(policy)
    assert walked == [], "warm service start must not scan either tree"
    # Dependency metadata change: repair only the changed dependency root.
    (common / "pyvenv.cfg").write_text("version = 3.12.13\n")
    shared_paths.prepare_shared_tool_acl(policy)
    assert walked == [str(common)]
    walked.clear()
    sid = "S-1-5-21-111111111-222222222-333333333-12347"
    shared_paths.prepare_shared_tool_acl(policy)
    assert set(walked) == {str(common), str(node)}, "account recreation invalidates old SID grants"
    walked.clear()
    policy.windows.filesystem.deny_write.append(str(common / "existing.txt"))
    shared_paths.prepare_shared_tool_acl(policy)
    assert set(walked) == {str(common), str(node)}, "policy changes invalidate permissions"
    cached_before = cache.read_bytes()
    (common / "pyvenv.cfg").write_text("version = 3.12.14\n")
    monkeypatch.setattr(win_acl, "apply_tool_acl", lambda *a, **kw: (_ for _ in ()).throw(PermissionError("probe")))
    with pytest.raises(PermissionError, match="probe"):
        shared_paths.prepare_shared_tool_acl(policy)
    assert cache.read_bytes() == cached_before, "failed preparation must not be marked successful"
    monkeypatch.setattr(shared_paths, "shared_tool_paths", lambda read_roots=None: ([], []))
    shared_paths.prepare_shared_tool_acl(policy)
    assert json.loads(cache.read_text()) == {}, "removing all paths must retire persistent grants"


def test_session_apply_and_revoke_skip_shared_subtree_even_under_an_ancestor(tmp_path, monkeypatch):
    import win32security as security

    sid = "S-1-5-21-111111111-222222222-333333333-12345"
    monkeypatch.setattr(win_acl, "get_synthetic_write_sid", lambda: sid)
    common = tmp_path / "skill-deps"
    common.mkdir()
    shared_file = common / "existing.py"
    shared_file.touch()
    session_file = tmp_path / "session.txt"
    session_file.touch()
    policy = {"allow_read": [str(common)], "allow_write": [str(common)], "deny_read": [], "deny_write": []}
    win_acl.apply_tool_acl([str(common)], sid, policy, writable_roots=[str(common)], strict=True)

    def entries(path):
        sd = security.GetFileSecurity(str(path), security.DACL_SECURITY_INFORMATION)
        return win_acl._acl_entries(sd.GetSecurityDescriptorDacl())

    before = entries(shared_file)
    applied = win_acl.apply_sandbox_acl(
        str(tmp_path), [str(tmp_path)], [], allow_read=[str(tmp_path)],
        sandbox_user_sid=sid, bounded_roots=[str(tmp_path)], preserve_roots=[str(common)],
    )
    assert entries(shared_file) == before
    win_acl.revoke_sandbox_acl([*applied, str(common)], sandbox_user_sid=sid, preserve_roots=[str(common)])
    assert entries(shared_file) == before
    assert not any(trustee == sid for _, _, _, trustee in entries(session_file))
    assert any(kind == const.ACCESS_ALLOWED_ACE_TYPE and mask & const.FILE_WRITE_DATA
               for kind, _, mask, trustee in entries(shared_file) if trustee == sid)


@pytest.mark.parametrize("axis,writable", [("read", False), ("read", True), ("write", True)])
def test_shared_defaults_do_not_override_ancestor_denials(tmp_path, monkeypatch, axis, writable):
    import win32security as security

    root = tmp_path / "restricted" / "shared"
    root.mkdir(parents=True)
    file = root / "existing.txt"
    file.touch()
    sid = "S-1-5-21-111111111-222222222-333333333-12345"
    monkeypatch.setattr(win_setup, "get_sandbox_user_sid", lambda: sid)
    monkeypatch.setattr(win_acl, "get_synthetic_write_sid", lambda: sid)
    monkeypatch.setattr(win_acl, "grant_parent_traverse", lambda *a, **kw: None)
    monkeypatch.setattr(shared_paths, "shared_tool_paths", lambda read_roots=None: (
        [str(root)], [str(root)] if writable else [],
    ))
    monkeypatch.setattr(shared_paths, "_shared_acl_cache_path", lambda: tmp_path / "acl-cache.json")
    policy = SecurityPolicy(version=1)
    getattr(policy.windows.filesystem, "deny_" + axis).append(str(root.parent))
    denied_mask = const.DENY_READ_RIGHTS if axis == "read" else const.DENY_WRITE_RIGHTS

    def denied():
        sd = security.GetFileSecurity(str(file), security.DACL_SECURITY_INFORMATION)
        return any(kind == const.ACCESS_DENIED_ACE_TYPE and mask & denied_mask == denied_mask
                   for kind, _, mask, trustee in win_acl._acl_entries(sd.GetSecurityDescriptorDacl())
                   if trustee == sid)

    shared_paths.prepare_shared_tool_acl(policy)
    assert denied(), "automatic shared-root grants must not lift an ancestor policy denial"
    # Preserve the existing most-specific explicit policy exception semantics.
    getattr(policy.windows.filesystem, "allow_" + axis).append(str(root))
    shared_paths.prepare_shared_tool_acl(policy)
    assert not denied()


def test_service_orders_readonly_parent_before_writable_child(tmp_path, monkeypatch):
    import win32security as security

    parent = tmp_path / "tools"
    child = parent / "writable-venv"
    child.mkdir(parents=True)
    file = child / "existing.py"
    file.touch()
    sid = "S-1-5-21-111111111-222222222-333333333-12345"
    monkeypatch.setattr(win_setup, "get_sandbox_user_sid", lambda: sid)
    monkeypatch.setattr(win_acl, "get_synthetic_write_sid", lambda: sid)
    monkeypatch.setattr(win_acl, "grant_parent_traverse", lambda *a, **kw: None)
    # Deliberately supply the child first, as a host is permitted to do.
    monkeypatch.setattr(shared_paths, "shared_tool_paths", lambda read_roots=None: (
        [str(child), str(parent)], [str(child)],
    ))
    monkeypatch.setattr(shared_paths, "_shared_acl_cache_path", lambda: tmp_path / "acl-cache.json")
    for _ in range(2):
        shared_paths.prepare_shared_tool_acl(SecurityPolicy(version=1))
        sd = security.GetFileSecurity(str(file), security.DACL_SECURITY_INFORMATION)
        entries = win_acl._acl_entries(sd.GetSecurityDescriptorDacl())
        assert any(kind == const.ACCESS_ALLOWED_ACE_TYPE and mask & const.FILE_WRITE_DATA
                   for kind, _, mask, trustee in entries if trustee == sid)
        assert not any(kind == const.ACCESS_DENIED_ACE_TYPE and mask & const.FILE_WRITE_DATA
                       for kind, _, mask, trustee in entries if trustee == sid)
        # Force a parent-only repair on the next service start.
        (parent / "node.exe").write_text("runtime updated")
    # Removing the writable exception must restore the surviving parent's RX,
    # even if that parent's metadata fingerprint is otherwise unchanged.
    shared_paths.prepare_shared_tool_acl(SecurityPolicy(version=1))
    monkeypatch.setattr(shared_paths, "shared_tool_paths", lambda read_roots=None: ([str(parent)], []))
    shared_paths.prepare_shared_tool_acl(SecurityPolicy(version=1))
    sd = security.GetFileSecurity(str(file), security.DACL_SECURITY_INFORMATION)
    entries = win_acl._acl_entries(sd.GetSecurityDescriptorDacl())
    assert any(kind == const.ACCESS_ALLOWED_ACE_TYPE
               and mask & const.ALLOW_READ_EXECUTE_RIGHTS == const.ALLOW_READ_EXECUTE_RIGHTS
               for kind, _, mask, trustee in entries if trustee == sid)
    assert any(kind == const.ACCESS_DENIED_ACE_TYPE and mask & const.FILE_WRITE_DATA
               for kind, _, mask, trustee in entries if trustee == sid)
