from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "configure_codex_cli.py"
SCRIPT_DIR = SCRIPT.parent


def test_configures_codex_cli_without_preserving_api_secret(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    target.write_text(
        'API_KEY="old-secret"\nCODEX_CLI_SANDBOX="read-only"\nUNCHANGED="yes"\n',
        encoding="utf-8",
    )
    auth_source = tmp_path / "source-auth.json"
    auth_source.write_text("{}\n", encoding="utf-8")
    sandbox_root = tmp_path / "sandbox"
    codex_home = tmp_path / "codex-home"
    sidecar_config = tmp_path / "a2a-sidecar.json"
    sidecar_config.write_text(
        json.dumps({"pluginConfig": {"server": {"port": 19800}}}),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--skip-model-check",
            "--model",
            "gpt-5.5",
            "--service-id",
            "JiuwenSwarm-Mac",
            "--codex-bin",
            "/usr/local/bin/codex",
            "--file-sandbox-root",
            str(sandbox_root),
            "--codex-home",
            str(codex_home),
            "--source-codex-auth",
            str(auth_source),
            "--sidecar-config",
            str(sidecar_config),
            "--reasoning-effort",
            "medium",
            "--jiuwenswarm-env",
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(SCRIPT_DIR),
    )

    payload = json.loads(completed.stdout)
    assert payload["model"] == "gpt-5.5"
    written = target.read_text(encoding="utf-8")
    assert "old-secret" not in written
    assert 'API_BASE="codex://local"' in written
    assert 'API_KEY="codex-cli-auth"' in written
    assert 'MODEL_PROVIDER="CodexCLI"' in written
    assert 'MODEL_NAME="gpt-5.5"' in written
    assert "CODEX_CLI_SANDBOX" not in written

    def _env_value(key: str) -> str:
        prefix = f'{key}='
        for line in written.splitlines():
            if line.startswith(prefix):
                return json.loads(line[len(prefix) :])
        raise AssertionError(f"missing env key: {key}")

    assert Path(_env_value("CODEX_CLI_CWD")) == sandbox_root.resolve()
    assert Path(_env_value("CODEX_CLI_HOME")) == codex_home.resolve()
    assert Path(_env_value("JIUWENSWARM_FILE_SANDBOX_ROOT")) == sandbox_root.resolve()
    assert 'JIUWENSWARM_A2A_REGISTRY_DATASET="openclaw_devices"' in written
    assert 'JIUWENSWARM_A2A_SERVICE_ID="JiuwenSwarm-Mac"' in written
    assert 'JIUWENSWARM_A2A_SIDECAR_CONTROL_URL="http://127.0.0.1:19802/gateway/call"' in written
    assert "JIUWENSWARM_A2A_SIDECAR_CONTROL_TOKEN=" in written
    assert 'UNCHANGED="yes"' in written
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
        assert sandbox_root.stat().st_mode & 0o777 == 0o700
        assert codex_home.stat().st_mode & 0o777 == 0o700
        assert (codex_home / "auth.json").is_symlink()
    else:
        assert (codex_home / "auth.json").exists()
        assert (codex_home / "auth.json").read_text(encoding="utf-8") == auth_source.read_text(encoding="utf-8")
    if (codex_home / "auth.json").is_symlink():
        assert (codex_home / "auth.json").resolve() == auth_source.resolve()
    codex_config = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'default_permissions = "jiuwen-file-sandbox"' in codex_config
    assert '":root" = "deny"' in codex_config
    assert '":minimal" = "read"' in codex_config
    assert "enabled = false" in codex_config
    if os.name != "nt":
        assert (codex_home / "config.toml").stat().st_mode & 0o777 == 0o600
        assert sidecar_config.stat().st_mode & 0o777 == 0o600
    sidecar_payload = json.loads(sidecar_config.read_text(encoding="utf-8"))
    control = sidecar_payload["pluginConfig"]["sidecarControl"]
    assert control["enabled"] is True
    assert control["host"] == "127.0.0.1"
    assert control["port"] == 19802
    assert len(control["token"]) >= 32


def test_rejects_invalid_reasoning_effort_before_write(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--skip-model-check",
            "--reasoning-effort",
            "impossible",
            "--jiuwenswarm-env",
            str(target),
        ],
        capture_output=True,
        text=True,
        cwd=str(SCRIPT.parent),
    )
    assert completed.returncode != 0
    assert not target.exists()
