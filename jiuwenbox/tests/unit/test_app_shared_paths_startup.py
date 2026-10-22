"""Windows startup must retain the original policy error before changing ACLs."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.server import app, shared_paths
import jiuwenbox.supervisor as supervisor


@pytest.fixture
def startup(monkeypatch):
    reader = Mock()
    reader.is_proxy_only.return_value = False
    monkeypatch.setattr(app, "PolicyReader", lambda: reader)
    monkeypatch.setattr(app, "sys", SimpleNamespace(platform="win32"))
    for name in ("validate_configured_listener", "make_stream_logging_nonblocking",
                 "_raise_open_file_limit", "_configure_loop_default_executor"):
        monkeypatch.setattr(app, name, lambda: None)
    setup = Mock()
    monkeypatch.setattr(supervisor, "win_setup", setup, raising=False)
    monkeypatch.setattr(supervisor, "win_proxy", Mock(), raising=False)
    monkeypatch.setattr(shared_paths, "shared_tool_paths", lambda: ([], []))
    prepare = Mock()
    monkeypatch.setattr(shared_paths, "prepare_shared_tool_acl", prepare)
    return reader, setup, prepare


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("invalid policy"), FileNotFoundError("missing policy")])
async def test_policy_load_failure_preserves_error_before_setup(startup, error):
    reader, setup, prepare = startup
    reader.load_policy.side_effect = error
    with pytest.raises(type(error)) as caught:
        async with app.lifespan(None):
            pytest.fail("startup accepted an invalid policy")
    assert caught.value is error
    setup.ensure_windows_setup.assert_not_called()
    setup.revoke_stale_acl.assert_not_called()
    prepare.assert_not_called()


@pytest.mark.asyncio
async def test_preinstall_fallback_keeps_the_loaded_policy(startup):
    reader, setup, prepare = startup
    policy = SecurityPolicy(version=1)
    reader.load_policy.return_value = policy
    setup.collect_preinstall_paths.side_effect = RuntimeError("preinstall probe failed")
    stop = RuntimeError("stop after shared ACL preparation")
    prepare.side_effect = stop
    with pytest.raises(RuntimeError) as caught:
        async with app.lifespan(None):
            pytest.fail("expected the preparation sentinel")
    assert caught.value is stop
    setup.ensure_windows_setup.assert_called_once()
    prepare.assert_called_once_with(policy)
