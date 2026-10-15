# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise northbound authentication over real loopback SSH connections."""

from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.gateway.channel_manager.protocol.ssh.config import ProxyConfig
from jiuwenswarm.gateway.channel_manager.protocol.ssh.server import (
    ProxySSHServer,
    SSHAgentHooks,
    SSHProxy,
)

asyncssh = pytest.importorskip("asyncssh")


@pytest.fixture
async def ssh_endpoint(tmp_path):
    alice_key = asyncssh.generate_private_key("ssh-ed25519")
    bob_key = asyncssh.generate_private_key("ssh-ed25519")
    key_paths = {}
    for username, key in (("alice", alice_key), ("bob", bob_key)):
        path = tmp_path / f"{username}_authorized_keys"
        key.write_public_key(str(path))
        key_paths[username] = str(path)
    restricted_path = tmp_path / "restricted_authorized_keys"
    restricted_path.write_text(
        'from="192.0.2.0/24" ' + alice_key.export_public_key().decode(),
        encoding="utf-8",
    )
    key_paths["restricted"] = str(restricted_path)

    hooks = SSHAgentHooks(
        register_session=AsyncMock(),
        unregister_session=AsyncMock(),
        submit_relay=AsyncMock(),
        wait_relay_done=AsyncMock(return_value=0),
    )
    proxy = SSHProxy(
        ProxyConfig(
            listen_port=0,
            host_key_path=str(tmp_path / "host_key"),
            authorized_keys=key_paths,
        ),
        hooks,
    )
    await proxy.start()
    try:
        yield proxy, hooks, alice_key, bob_key
    finally:
        await proxy.stop()


@pytest.mark.parametrize("username", ["alice", "bob"])
async def test_authorized_user_reaches_relay_with_verified_identity(ssh_endpoint, username):
    proxy, hooks, alice_key, bob_key = ssh_endpoint
    key = alice_key if username == "alice" else bob_key
    async with asyncssh.connect(
        "127.0.0.1", port=proxy._server.get_port(), username=username,
        client_keys=[key], agent_path=None, known_hosts=None, config=None,
        login_timeout=5,
    ) as conn:
        result = await conn.run("agent test", check=True, timeout=5)
        assert result.exit_status == 0
    hooks.register_session.assert_awaited_once()
    assert hooks.register_session.call_args.kwargs["username"] == username
    hooks.submit_relay.assert_awaited_once()
    assert hooks.submit_relay.call_args.args[1]["username"] == username
    hooks.unregister_session.assert_awaited_once()


@pytest.mark.parametrize("attack", [
    "password", "empty_password", "no_credentials", "unknown_key",
    "unknown_user", "other_user_key", "restricted_key",
])
async def test_unauthorized_client_cannot_reach_relay(ssh_endpoint, attack):
    proxy, hooks, alice_key, bob_key = ssh_endpoint
    username = "mallory" if attack == "unknown_user" else "alice"
    if attack == "restricted_key":
        username = "restricted"
    keys = []
    password = None
    if attack == "password":
        password = "arbitrary-password"
    elif attack == "empty_password":
        password = ""
    elif attack == "unknown_key":
        keys = [asyncssh.generate_private_key("ssh-ed25519")]
    elif attack in ("unknown_user", "restricted_key"):
        keys = [alice_key]
    elif attack == "other_user_key":
        keys = [bob_key]

    with pytest.raises(asyncssh.PermissionDenied):
        async with asyncssh.connect(
            "127.0.0.1", port=proxy._server.get_port(), username=username,
            client_keys=keys, password=password, agent_path=None,
            known_hosts=None, config=None, login_timeout=5,
        ):
            pytest.fail("Unauthorized SSH login succeeded")
    hooks.register_session.assert_not_awaited()
    hooks.submit_relay.assert_not_awaited()


@pytest.mark.parametrize("key_file", ["missing", "empty", "invalid", "unconfigured"])
async def test_invalid_authorization_fails_before_listener_opens(tmp_path, key_file):
    path = tmp_path / "authorized_keys"
    if key_file == "empty":
        path.write_text("# No authorized keys\n", encoding="utf-8")
    elif key_file == "invalid":
        path.write_text("not a public key\n", encoding="utf-8")
    proxy = SSHProxy(ProxyConfig(
        listen_port=0,
        host_key_path=str(tmp_path / "host_key"),
        authorized_keys={} if key_file == "unconfigured" else {"alice": str(path)},
    ))
    with pytest.raises((ValueError, OSError)):
        await proxy.start()
    assert proxy._server is None
    assert not (tmp_path / "host_key").exists()


def test_auth_retry_clears_trust_for_previous_username():
    trusted_keys = asyncssh.import_authorized_keys(
        asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()
    )
    server = ProxySSHServer({"alice": trusted_keys})
    conn = Mock()
    server.connection_made(conn)
    assert server.begin_auth("alice") is True
    conn.set_authorized_keys.assert_called_with(trusted_keys)
    assert server.begin_auth("mallory") is True
    conn.set_authorized_keys.assert_called_with(None)
    assert server.public_key_auth_supported() is False
    assert server.password_auth_supported() is False
    assert server.validate_password("alice", "anything") is False
    assert server.validate_public_key("alice", Mock()) is False
