# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Offline checks for the standalone Java host test's connection boundaries."""

import base64
import importlib.util
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest


SETTINGS = (
    "JIUWENBOX_TEST_SERVER",
    "JIUWENBOX_TEST_SSH_HOST",
    "JIUWENBOX_TEST_SSH_USER",
    "JIUWENBOX_TEST_SSH_KEY",
)


@pytest.fixture
def java_runner(monkeypatch):
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)

    def load():
        path = Path(__file__).parents[1] / "integration" / "test_jiuwenbox_java_e2e.py"
        name = "java_host_test_runner"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    module = load()
    yield module, load
    module.S.close()


def test_import_has_no_default_remote_target_or_account(java_runner):
    runner, _ = java_runner
    assert runner.ENDPOINT == runner.HOST == runner.SSH_USER == runner.SSH_KEY == ""


@pytest.mark.parametrize("missing", ["ENDPOINT", "HOST", "SSH_USER"])
def test_main_rejects_incomplete_configuration_before_network(java_runner, monkeypatch, missing):
    runner, _ = java_runner
    for setting, value in {
        "ENDPOINT": "http://sandbox.example.invalid:8321",
        "HOST": "sandbox.example.invalid",
        "SSH_USER": "test-operator",
    }.items():
        monkeypatch.setattr(runner, setting, "" if setting == missing else value)
    create = Mock(side_effect=AssertionError("Unexpected network request"))
    monkeypatch.setattr(runner, "create_sb", create)
    with pytest.raises(RuntimeError, match="Missing required test settings"):
        runner.main()
    create.assert_not_called()


@pytest.mark.parametrize("method", ["api_get", "api_post", "api_delete"])
def test_api_without_target_never_sends_request(java_runner, monkeypatch, method):
    runner, _ = java_runner
    request = Mock(side_effect=AssertionError("Unexpected network request"))
    monkeypatch.setattr(runner.S, "request", request)
    with pytest.raises(RuntimeError, match="JIUWENBOX_TEST_SERVER"):
        getattr(runner, method)("/api/v1/sandboxes")
    request.assert_not_called()


def test_ssh_without_target_never_starts_process(java_runner, monkeypatch):
    runner, _ = java_runner
    run = Mock(side_effect=AssertionError("Unexpected SSH connection"))
    monkeypatch.setattr(runner.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="JIUWENBOX_TEST_SSH_HOST"):
        runner.ssh_exec("true")
    run.assert_not_called()


@pytest.mark.parametrize("identity", ["", "~/test key"])
def test_ssh_uses_verified_public_key_auth_without_password_files(
    java_runner, monkeypatch, identity,
):
    runner, _ = java_runner
    monkeypatch.setattr(runner, "HOST", "sandbox.example.invalid")
    monkeypatch.setattr(runner, "SSH_USER", "test-operator")
    monkeypatch.setattr(runner, "SSH_KEY", identity)
    run = Mock(return_value=subprocess.CompletedProcess([], 7, "output", "error"))
    monkeypatch.setattr(runner.subprocess, "run", run)
    monkeypatch.setattr("builtins.open", Mock(side_effect=AssertionError("Unexpected file write")))
    command = "printf '%s' \"a'b;$HOME\"\nprintf test"
    assert runner.ssh_exec(command, timeout=23) == ("output", "error", 7)
    args = run.call_args.args[0]
    assert args[0] == "ssh"
    assert "BatchMode=yes" in args
    assert "PreferredAuthentications=publickey" in args
    assert "StrictHostKeyChecking=yes" in args
    assert args[-5:-1] == ["-l", "test-operator", "--", "sandbox.example.invalid"]
    assert base64.b64decode(args[-1].split()[1]).decode() == command
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL
    assert run.call_args.kwargs["timeout"] == 23
    assert not run.call_args.kwargs.get("shell", False)
    if identity:
        assert "IdentitiesOnly=yes" in args
        assert Path(args[args.index("-i") + 1]) == Path(identity).expanduser()
    else:
        assert "-i" not in args


def test_ssh_timeout_preserves_result_contract(java_runner, monkeypatch):
    runner, _ = java_runner
    monkeypatch.setattr(runner, "HOST", "sandbox.example.invalid")
    monkeypatch.setattr(runner, "SSH_USER", "test-operator")
    monkeypatch.setattr(runner.subprocess, "run", Mock(
        side_effect=subprocess.TimeoutExpired("ssh", 1),
    ))
    assert runner.ssh_exec("true", timeout=1) == ("", "TIMEOUT", -1)


def test_explicit_environment_is_loaded_without_password(java_runner, monkeypatch):
    _, load = java_runner
    values = {
        "JIUWENBOX_TEST_SERVER": "http://sandbox.example.invalid:8321/",
        "JIUWENBOX_TEST_SSH_HOST": "sandbox.example.invalid",
        "JIUWENBOX_TEST_SSH_USER": "test-operator",
        "JIUWENBOX_TEST_SSH_KEY": "~/test key",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    runner = load()
    try:
        assert runner.ENDPOINT == values["JIUWENBOX_TEST_SERVER"].rstrip("/")
        assert runner.HOST == values["JIUWENBOX_TEST_SSH_HOST"]
        assert runner.SSH_USER == values["JIUWENBOX_TEST_SSH_USER"]
        assert runner.SSH_KEY == values["JIUWENBOX_TEST_SSH_KEY"]
    finally:
        runner.S.close()
