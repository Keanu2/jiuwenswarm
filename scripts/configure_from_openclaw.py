#!/usr/bin/env python3
"""Copy one OpenClaw model API profile into JiuwenSwarm without printing secrets.

Only the selected provider/model credential and the A2A Tunnel transport are
copied. Gateway, channel, unrelated plugin, and unrelated credential settings
are intentionally ignored; Registry identity remains an explicit CLI choice.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from file_privacy import credential_storage_label, write_private_text


_ASSIGNMENT_RE = re.compile(r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)=")


def _load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing or invalid {label}")
    return value.strip()


def _resolve_model_settings(
    openclaw_config: dict,
    auth_profiles: dict,
    *,
    provider_name: str,
    profile_id: str,
    model_id: str | None,
) -> dict[str, str]:
    providers = openclaw_config.get("models", {}).get("providers", {})
    provider = providers.get(provider_name) if isinstance(providers, dict) else None
    if not isinstance(provider, dict):
        raise ValueError(f"OpenClaw provider not found: {provider_name}")

    api_shape = _require_string(provider.get("api"), f"models.providers.{provider_name}.api")
    if api_shape != "anthropic-messages":
        raise ValueError(
            f"unsupported OpenClaw API shape {api_shape!r}; only anthropic-messages is mapped safely"
        )

    models = provider.get("models")
    if not isinstance(models, list):
        raise ValueError(f"OpenClaw provider has no model entry: {provider_name}")
    candidates = [entry for entry in models if isinstance(entry, dict) and entry.get("id")]
    if model_id:
        selected = next((entry for entry in candidates if entry.get("id") == model_id), None)
        if selected is None:
            raise ValueError(f"model {model_id!r} not found in OpenClaw provider {provider_name}")
    elif len(candidates) == 1:
        selected = candidates[0]
    else:
        raise ValueError(
            f"OpenClaw provider {provider_name} has {len(candidates)} models; select one with --model"
        )

    profiles = auth_profiles.get("profiles")
    profile = profiles.get(profile_id) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict):
        raise ValueError(f"OpenClaw auth profile not found: {profile_id}")
    if profile.get("provider") != provider_name:
        raise ValueError(f"auth profile {profile_id} does not belong to provider {provider_name}")

    credential = profile.get("token") or profile.get("key")
    return {
        "API_BASE": _require_string(provider.get("baseUrl"), "provider baseUrl"),
        "API_KEY": _require_string(credential, "provider credential"),
        "MODEL_NAME": _require_string(selected.get("id"), "provider model id"),
        "MODEL_PROVIDER": "Anthropic",
    }


def _quote_env(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("environment values must be single-line")
    return json.dumps(value, ensure_ascii=False)


def _update_env_file(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    result: list[str] = []

    for line in lines:
        match = _ASSIGNMENT_RE.match(line)
        if match and match.group("key") in remaining:
            key = match.group("key")
            result.append(f"{match.group('prefix')}{key}={_quote_env(remaining.pop(key))}")
        else:
            result.append(line)

    if remaining:
        if result and result[-1].strip():
            result.append("")
        result.append("# JiuwenSwarm A2A sidecar settings (managed by configure_from_openclaw.py)")
        result.extend(f"{key}={_quote_env(value)}" for key, value in remaining.items())

    write_private_text(path, "\n".join(result).rstrip() + "\n")


def _extract_default_peer_auth(plugin_config: dict) -> dict[str, str] | None:
    """Copy OpenClaw's shared A2A Bearer so discovered peers get the same header."""
    registry = plugin_config.get("registry")
    if isinstance(registry, dict):
        copied = _normalize_peer_auth(registry.get("defaultPeerAuth"))
        if copied:
            return copied
    security = plugin_config.get("security")
    if isinstance(security, dict) and str(security.get("inboundAuth") or "").strip() == "bearer":
        return _normalize_peer_auth({"type": "bearer", "token": security.get("token")})
    return None


def _normalize_peer_auth(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    token = value.get("token")
    if not isinstance(token, str) or not token.strip():
        return None
    auth_type = str(value.get("type") or "bearer").strip()
    if auth_type != "apiKey":
        auth_type = "bearer"
    return {"type": auth_type, "token": token.strip()}


def _build_sidecar_config(
    openclaw_config: dict,
    *,
    service_id: str,
    registry_base_url: str,
    registry_dataset: str,
    a2a_server_port: int,
    sidecar_server_port: int,
) -> dict:
    entries = openclaw_config.get("plugins", {}).get("entries", {})
    gateway = entries.get("a2a-gateway") if isinstance(entries, dict) else None
    plugin_config = gateway.get("config") if isinstance(gateway, dict) else None
    tunnel = plugin_config.get("tunnel") if isinstance(plugin_config, dict) else None
    if not isinstance(tunnel, dict) or tunnel.get("enabled") is not True:
        raise ValueError("local OpenClaw a2a-gateway tunnel is not enabled")

    relay_url = _require_string(tunnel.get("relayUrl"), "a2a-gateway tunnel relayUrl")
    safe_tunnel_keys = (
        "heartbeatIntervalMs",
        "heartbeatTimeoutMs",
        "requestTimeoutMs",
        "reconnectIntervalMs",
        "maxReconnectIntervalMs",
        "maxReconnectAttempts",
    )
    tunnel_config = {
        "enabled": True,
        "relayUrl": relay_url,
        "deviceId": service_id,
        "localServicePort": a2a_server_port,
    }
    for key in safe_tunnel_keys:
        value = tunnel.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            tunnel_config[key] = value

    registry_config: dict[str, object] = {
        "enabled": True,
        "baseUrl": registry_base_url,
        "dataset": registry_dataset,
        "serviceId": service_id,
        "registerOnStart": True,
        "discoverIntervalMs": 30_000,
        "mergeWithStatic": True,
    }
    peer_auth = _extract_default_peer_auth(plugin_config if isinstance(plugin_config, dict) else {})
    if peer_auth:
        registry_config["defaultPeerAuth"] = peer_auth

    return {
        "pluginConfig": {
            "agentCard": {
                "name": service_id,
                "description": "JiuwenSwarm A2A endpoint bridged by openclaw-a2a",
                "url": f"http://127.0.0.1:{a2a_server_port}/a2a",
                "skills": [
                    {"id": "chat", "name": "chat", "description": "JiuwenSwarm chat agent"}
                ],
            },
            "server": {"host": "127.0.0.1", "port": sidecar_server_port},
            "storage": {
                "tasksDir": "a2a-sidecar/tasks",
                "taskTtlHours": 72,
                "cleanupIntervalMinutes": 60,
            },
            "peers": [],
            "security": {
                "inboundAuth": "none",
                "allowedMimeTypes": ["text/*", "image/*", "application/pdf", "application/json"],
                "maxFileSizeBytes": 52_428_800,
                "maxInlineFileSizeBytes": 6_291_456,
                "fileUriAllowlist": [],
            },
            "routing": {"defaultAgentId": "default", "rules": []},
            "limits": {"maxConcurrentTasks": 4, "maxQueuedTasks": 100},
            "observability": {
                "structuredLogs": True,
                "exposeMetricsEndpoint": True,
                "metricsPath": "/a2a/metrics",
                "metricsAuth": "none",
                "auditLogPath": "a2a-sidecar/audit.jsonl",
            },
            "tunnel": tunnel_config,
            "fileStorage": {
                "tempDir": str((Path.home() / "Desktop" / "JiuwenSwarm").resolve()),
            },
            "registry": registry_config,
        }
    }


def main() -> int:
    home = Path.home()
    parser = argparse.ArgumentParser(
        description="Copy a selected OpenClaw model API profile to JiuwenSwarm."
    )
    parser.add_argument(
        "--openclaw-config",
        type=Path,
        default=home / ".openclaw" / "openclaw.json",
    )
    parser.add_argument(
        "--openclaw-auth-profiles",
        type=Path,
        default=home / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json",
    )
    parser.add_argument(
        "--jiuwenswarm-env",
        type=Path,
        default=home / ".jiuwenswarm" / "config" / ".env",
    )
    parser.add_argument("--provider", default="custom")
    parser.add_argument("--profile", default="custom:default")
    parser.add_argument("--model")
    parser.add_argument(
        "--sidecar-config",
        type=Path,
        default=home / ".jiuwenswarm" / "a2a-sidecar.json",
    )
    parser.add_argument("--registry-base-url", required=True)
    parser.add_argument("--registry-dataset", default="openclaw_devices")
    parser.add_argument("--service-id", required=True)
    parser.add_argument("--a2a-server-port", type=int, default=19100)
    parser.add_argument("--sidecar-server-port", type=int, default=19800)
    args = parser.parse_args()

    openclaw_config = _load_json(args.openclaw_config)
    settings = _resolve_model_settings(
        openclaw_config,
        _load_json(args.openclaw_auth_profiles),
        provider_name=args.provider,
        profile_id=args.profile,
        model_id=args.model,
    )
    settings.update(
        {
            "A2A_SERVER_ENABLED": "true",
            "A2A_SERVER_HOST": "127.0.0.1",
            "A2A_SERVER_PORT": str(args.a2a_server_port),
            "A2A_SERVER_PATH": "/a2a",
            # The reused openclaw-a2a sidecar and the deployed Relay expose A2A
            # 0.3. Jiuwen still runs a2a-sdk 1.0 internally; this selects its
            # built-in 0.3 compatibility surface for the external endpoint.
            "A2A_SERVER_PROTOCOL_VERSION": "0.3.0",
            "A2A_SERVER_EXPOSE_REASONING": "false",
        }
    )
    _update_env_file(args.jiuwenswarm_env, settings)
    sidecar_config = _build_sidecar_config(
        openclaw_config,
        service_id=_require_string(args.service_id, "service id"),
        registry_base_url=_require_string(args.registry_base_url, "registry base URL"),
        registry_dataset=_require_string(args.registry_dataset, "registry dataset"),
        a2a_server_port=args.a2a_server_port,
        sidecar_server_port=args.sidecar_server_port,
    )
    write_private_text(
        args.sidecar_config,
        json.dumps(sidecar_config, ensure_ascii=False, indent=2) + "\n",
    )

    print(
        json.dumps(
            {
                "ok": True,
                "provider": settings["MODEL_PROVIDER"],
                "model": settings["MODEL_NAME"],
                "credential_copied": True,
                "credential_storage": credential_storage_label(),
                "a2a_server": f"127.0.0.1:{args.a2a_server_port}",
                "target": str(args.jiuwenswarm_env),
                "sidecar_config": str(args.sidecar_config),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
