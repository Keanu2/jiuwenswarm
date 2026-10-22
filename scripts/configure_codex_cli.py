#!/usr/bin/env python3
"""Configure JiuwenSwarm to use the authenticated local Codex CLI backend."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

from file_privacy import (
    default_a2a_service_id,
    ensure_private_directory,
    link_or_copy_file,
    write_private_text,
)


_ASSIGNMENT_RE = re.compile(r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)=")


def _quote_env(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("environment values must be single-line")
    return json.dumps(value, ensure_ascii=False)


def _update_private_env(path: Path, updates: dict[str, str | None]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    result: list[str] = []
    for line in lines:
        match = _ASSIGNMENT_RE.match(line)
        if match and match.group("key") in remaining:
            key = match.group("key")
            value = remaining.pop(key)
            if value is not None:
                result.append(f"{match.group('prefix')}{key}={_quote_env(value)}")
        else:
            result.append(line)
    remaining_values = {key: value for key, value in remaining.items() if value is not None}
    if remaining_values:
        if result and result[-1].strip():
            result.append("")
        result.append("# Codex CLI model backend (managed by configure_codex_cli.py)")
        result.extend(f"{key}={_quote_env(value)}" for key, value in remaining_values.items())

    write_private_text(path, "\n".join(result).rstrip() + "\n")


def _catalog_contains_model(codex_bin: str, model: str) -> bool:
    completed = subprocess.run(
        [codex_bin, "debug", "models"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout)
    entries = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug") or entry.get("id") or entry.get("model")
        if slug == model:
            return True
    return False


def _configure_isolated_codex_home(codex_home: Path, source_auth: Path) -> Path:
    """Create a private Codex home that reuses only the current authentication."""
    home = ensure_private_directory(codex_home)
    auth_source = source_auth.expanduser().resolve(strict=True)
    if not auth_source.is_file():
        raise ValueError(f"Codex auth file is unavailable: {auth_source}")
    link_or_copy_file(auth_source, home / "auth.json")
    write_private_text(
        home / "config.toml",
        '''default_permissions = "jiuwen-file-sandbox"

[permissions.jiuwen-file-sandbox]
description = "JiuwenSwarm single-directory file sandbox"
extends = ":workspace"

[permissions.jiuwen-file-sandbox.filesystem]
":root" = "deny"
":minimal" = "read"
":tmpdir" = "deny"
":slash_tmp" = "deny"

[permissions.jiuwen-file-sandbox.network]
enabled = false
''',
    )
    return home


def _configure_sidecar_control(path: Path) -> tuple[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("sidecar config must be a JSON object")
    plugin_config = payload.get("pluginConfig")
    if not isinstance(plugin_config, dict):
        raise ValueError("sidecar config is missing pluginConfig")
    server = plugin_config.get("server") if isinstance(plugin_config.get("server"), dict) else {}
    server_port = int(server.get("port") or 19800)
    existing = (
        plugin_config.get("sidecarControl")
        if isinstance(plugin_config.get("sidecarControl"), dict)
        else {}
    )
    token = str(existing.get("token") or "").strip() or secrets.token_urlsafe(32)
    host = "127.0.0.1"
    port = int(existing.get("port") or (server_port + 2))
    plugin_config["sidecarControl"] = {
        "enabled": True,
        "host": host,
        "port": port,
        "token": token,
    }
    write_private_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    return f"http://{host}:{port}/gateway/call", token


def main() -> int:
    home = Path.home()
    parser = argparse.ArgumentParser(description="Use Codex CLI as JiuwenSwarm's model backend.")
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--codex-bin", default=shutil.which("codex") or "codex")
    parser.add_argument(
        "--file-sandbox-root",
        type=Path,
        default=home / ".jiuwenswarm" / "sandbox",
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=home / ".jiuwenswarm" / "codex-home",
    )
    parser.add_argument(
        "--source-codex-auth",
        type=Path,
        default=home / ".codex" / "auth.json",
    )
    parser.add_argument(
        "--registry-base-url",
        default="http://121.37.53.35:8000",
    )
    parser.add_argument("--registry-dataset", default="openclaw_devices")
    parser.add_argument("--service-id", default=default_a2a_service_id())
    parser.add_argument(
        "--sidecar-config",
        type=Path,
        default=home / ".jiuwenswarm" / "a2a-sidecar.json",
    )
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument(
        "--jiuwenswarm-env",
        type=Path,
        default=home / ".jiuwenswarm" / "config" / ".env",
    )
    parser.add_argument("--skip-model-check", action="store_true")
    args = parser.parse_args()

    if not args.skip_model_check and not _catalog_contains_model(args.codex_bin, args.model):
        raise ValueError(f"Codex CLI model is not available: {args.model}")
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than zero")
    if args.reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
        raise ValueError("unsupported Codex reasoning effort")

    sandbox_root = ensure_private_directory(args.file_sandbox_root)
    isolated_codex_home = _configure_isolated_codex_home(
        args.codex_home,
        args.source_codex_auth,
    )
    sidecar_control_url, sidecar_control_token = _configure_sidecar_control(
        args.sidecar_config.expanduser().resolve(strict=True)
    )

    _update_private_env(
        args.jiuwenswarm_env,
        {
            "API_BASE": "codex://local",
            # Jiuwen's generic model config expects a non-empty value. The
            # CodexCLI client ignores it and reuses ~/.codex authentication.
            "API_KEY": "codex-cli-auth",
            "MODEL_NAME": args.model,
            "MODEL_PROVIDER": "CodexCLI",
            "CODEX_CLI_MODEL": args.model,
            "CODEX_CLI_BIN": str(Path(args.codex_bin).expanduser()),
            "CODEX_CLI_CWD": str(sandbox_root),
            "CODEX_CLI_HOME": str(isolated_codex_home),
            "CODEX_CLI_SANDBOX": None,
            "CODEX_CLI_REASONING_EFFORT": args.reasoning_effort,
            "CODEX_CLI_TIMEOUT_S": str(args.timeout_seconds),
            "JIUWENSWARM_FILE_SANDBOX_ROOT": str(sandbox_root),
            "JIUWENSWARM_A2A_REGISTRY_BASE_URL": args.registry_base_url.rstrip("/"),
            "JIUWENSWARM_A2A_REGISTRY_DATASET": args.registry_dataset,
            "JIUWENSWARM_A2A_SERVICE_ID": args.service_id,
            "JIUWENSWARM_A2A_SIDECAR_CONTROL_URL": sidecar_control_url,
            "JIUWENSWARM_A2A_SIDECAR_CONTROL_TOKEN": sidecar_control_token,
        },
    )
    print(
        json.dumps(
            {
                "ok": True,
                "provider": "CodexCLI",
                "model": args.model,
                "file_sandbox_root": str(sandbox_root),
                "codex_home": str(isolated_codex_home),
                "registry_dataset": args.registry_dataset,
                "service_id": args.service_id,
                "sidecar_control_url": sidecar_control_url,
                "target": str(args.jiuwenswarm_env),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
