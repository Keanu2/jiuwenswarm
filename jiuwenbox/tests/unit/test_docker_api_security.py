# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Inspect Docker launch arguments without starting Docker or any service."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.fixture
def docker_script(tmp_path, monkeypatch):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required for Docker script tests")
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$TEST_DOCKER_ARGS"\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    output = tmp_path / "docker-args"
    script = Path(__file__).parents[2] / "scripts" / "run_docker.sh"
    for name in (
        "JIUWENBOX_LISTEN", "JIUWENBOX_API_TOKEN", "JIUWENBOX_HOST_BIND",
        "JIUWENBOX_SAVE_LOGS_HOST_DIR", "JIUWENBOX_POLICY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEST_DOCKER_ARGS", output.as_posix())
    monkeypatch.setenv("JIUWENBOX_HOST_PORT", "8321")

    def run():
        # Git Bash needs its POSIX spelling of the mock binary directory in PATH.
        if os.name == "nt":
            command = 'export PATH="$(cygpath -u "$1"):$PATH"; exec bash "$2"'
        else:
            command = 'export PATH="$1:$PATH"; exec bash "$2"'
        return subprocess.run(
            [bash, "-c", command, "test", tmp_path.as_posix(), script.as_posix()],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )

    return run, output


@pytest.mark.parametrize("token", [None, "", " \t "])
def test_docker_http_without_token_never_invokes_docker(docker_script, monkeypatch, token):
    run, output = docker_script
    if token is not None:
        monkeypatch.setenv("JIUWENBOX_API_TOKEN", token)
    result = run()
    assert result.returncode == 2
    assert "JIUWENBOX_API_TOKEN" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("bind", [None, "0.0.0.0"])
def test_docker_publishes_api_locally_and_forwards_token_by_name(docker_script, monkeypatch, bind):
    run, output = docker_script
    token = "test-docker-api-token"
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", token)
    if bind:
        monkeypatch.setenv("JIUWENBOX_HOST_BIND", bind)
    result = run()
    assert result.returncode == 0, result.stderr
    args = output.read_text(encoding="utf-8").splitlines()
    assert f"{bind or '127.0.0.1'}:8321:8321" in args
    assert "JIUWENBOX_API_TOKEN" in args
    assert token not in output.read_text(encoding="utf-8") + result.stdout + result.stderr
