from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "configure_from_openclaw.py"
SCRIPT_DIR = SCRIPT.parent


def test_copies_only_selected_model_and_a2a_settings(tmp_path: Path) -> None:
    source_config = tmp_path / "openclaw.json"
    source_auth = tmp_path / "auth-profiles.json"
    target_env = tmp_path / "jiuwenswarm.env"
    sidecar_config = tmp_path / "a2a-sidecar.json"

    source_config.write_text(
        json.dumps(
            {
                "models": {
                    "providers": {
                        "custom": {
                            "api": "anthropic-messages",
                            "baseUrl": "https://model.example.test",
                            "models": [{"id": "model-one"}],
                        }
                    }
                },
                "plugins": {
                    "entries": {
                        "a2a-gateway": {
                            "config": {
                                "tunnel": {
                                    "enabled": True,
                                    "relayUrl": "ws://relay.example.test:8001",
                                    "deviceId": "source-device-must-not-copy",
                                    "localServicePort": 18800,
                                    "heartbeatIntervalMs": 15000,
                                },
                                "security": {
                                    "inboundAuth": "bearer",
                                    "token": "shared-peer-token",
                                },
                                "registry": {
                                    "defaultPeerAuth": {
                                        "type": "bearer",
                                        "token": "shared-peer-token",
                                    }
                                }
                            }
                        }
                    }
                },
                "gateway": {"token": "must-not-copy"},
                "channels": {"secret": "must-not-copy"},
            }
        ),
        encoding="utf-8",
    )
    source_auth.write_text(
        json.dumps(
            {
                "profiles": {
                    "custom:default": {
                        "type": "token",
                        "provider": "custom",
                        "token": "secret-model-token",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    target_env.write_text('UNCHANGED="yes"\nAPI_KEY="old"\n', encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--openclaw-config",
            str(source_config),
            "--openclaw-auth-profiles",
            str(source_auth),
            "--jiuwenswarm-env",
            str(target_env),
            "--sidecar-config",
            str(sidecar_config),
            "--registry-base-url",
            "http://registry.example.test:8000",
            "--service-id",
            "Jiuwen-Test",
            "--a2a-server-port",
            "19123",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(SCRIPT_DIR),
    )

    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert "secret-model-token" not in completed.stdout

    written = target_env.read_text(encoding="utf-8")
    assert 'UNCHANGED="yes"' in written
    assert 'API_BASE="https://model.example.test"' in written
    assert 'API_KEY="secret-model-token"' in written
    assert 'MODEL_NAME="model-one"' in written
    assert 'MODEL_PROVIDER="Anthropic"' in written
    assert 'A2A_SERVER_ENABLED="true"' in written
    assert 'A2A_SERVER_PORT="19123"' in written
    assert 'A2A_SERVER_PROTOCOL_VERSION="0.3.0"' in written
    assert "must-not-copy" not in written
    if os.name != "nt":
        assert target_env.stat().st_mode & 0o777 == 0o600

    generated = json.loads(sidecar_config.read_text(encoding="utf-8"))
    plugin_config = generated["pluginConfig"]
    assert plugin_config["agentCard"]["url"] == "http://127.0.0.1:19123/a2a"
    assert plugin_config["server"] == {"host": "127.0.0.1", "port": 19800}
    assert plugin_config["tunnel"]["relayUrl"] == "ws://relay.example.test:8001"
    assert plugin_config["tunnel"]["deviceId"] == "Jiuwen-Test"
    assert plugin_config["tunnel"]["localServicePort"] == 19123
    assert plugin_config["registry"]["enabled"] is True
    assert plugin_config["registry"]["serviceId"] == "Jiuwen-Test"
    assert plugin_config["registry"]["defaultPeerAuth"] == {
        "type": "bearer",
        "token": "shared-peer-token",
    }
    assert plugin_config["fileStorage"]["tempDir"].endswith("JiuwenSwarm")
    assert "source-device-must-not-copy" not in sidecar_config.read_text(encoding="utf-8")
    assert "must-not-copy" not in sidecar_config.read_text(encoding="utf-8")
    if os.name != "nt":
        assert sidecar_config.stat().st_mode & 0o777 == 0o600


def test_rejects_unmapped_api_shape_without_writing(tmp_path: Path) -> None:
    source_config = tmp_path / "openclaw.json"
    source_auth = tmp_path / "auth-profiles.json"
    target_env = tmp_path / "jiuwenswarm.env"
    sidecar_config = tmp_path / "a2a-sidecar.json"
    source_config.write_text(
        json.dumps(
            {
                "models": {
                    "providers": {
                        "custom": {
                            "api": "unknown-shape",
                            "baseUrl": "https://model.example.test",
                            "models": [{"id": "model-one"}],
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    source_auth.write_text(
        json.dumps(
            {
                "profiles": {
                    "custom:default": {
                        "provider": "custom",
                        "token": "secret-model-token",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--openclaw-config",
            str(source_config),
            "--openclaw-auth-profiles",
            str(source_auth),
            "--jiuwenswarm-env",
            str(target_env),
            "--sidecar-config",
            str(sidecar_config),
            "--registry-base-url",
            "http://registry.example.test:8000",
            "--service-id",
            "Jiuwen-Test",
        ],
        capture_output=True,
        text=True,
        cwd=str(SCRIPT_DIR),
    )

    assert completed.returncode != 0
    assert "secret-model-token" not in completed.stdout
    assert "secret-model-token" not in completed.stderr
    assert not target_env.exists()
    assert not sidecar_config.exists()


def test_requires_explicit_model_when_provider_has_multiple_models(tmp_path: Path) -> None:
    source_config = tmp_path / "openclaw.json"
    source_auth = tmp_path / "auth-profiles.json"
    target_env = tmp_path / "jiuwenswarm.env"
    sidecar_config = tmp_path / "a2a-sidecar.json"
    source_config.write_text(
        json.dumps(
            {
                "models": {
                    "providers": {
                        "custom": {
                            "api": "anthropic-messages",
                            "baseUrl": "https://model.example.test",
                            "models": [{"id": "model-one"}, {"id": "model-two"}],
                        }
                    }
                },
                "plugins": {
                    "entries": {
                        "a2a-gateway": {
                            "config": {
                                "tunnel": {
                                    "enabled": True,
                                    "relayUrl": "ws://relay.example.test:8001",
                                }
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    source_auth.write_text(
        json.dumps(
            {
                "profiles": {
                    "custom:default": {
                        "provider": "custom",
                        "token": "secret-model-token",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    base_args = [
        sys.executable,
        str(SCRIPT),
        "--openclaw-config",
        str(source_config),
        "--openclaw-auth-profiles",
        str(source_auth),
        "--jiuwenswarm-env",
        str(target_env),
        "--sidecar-config",
        str(sidecar_config),
        "--registry-base-url",
        "http://registry.example.test:8000",
        "--service-id",
        "Jiuwen-Test",
    ]
    rejected = subprocess.run(base_args, capture_output=True, text=True)
    assert rejected.returncode != 0
    assert "select one with --model" in rejected.stderr
    assert not target_env.exists()

    selected = subprocess.run(
        [*base_args, "--model", "model-two"],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(SCRIPT_DIR),
    )
    assert json.loads(selected.stdout)["model"] == "model-two"
    assert 'MODEL_NAME="model-two"' in target_env.read_text(encoding="utf-8")
