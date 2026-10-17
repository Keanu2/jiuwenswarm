# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Configuration for the SSH server channel."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_config_dir

DEFAULT_LISTEN_HOST = "127.0.0.1"


@dataclass
class ProxyConfig:
    listen_host: str = DEFAULT_LISTEN_HOST
    listen_port: int = 2222
    host_key_path: str = ""
    # SSH login username -> OpenSSH authorized_keys file (never client input).
    authorized_keys: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.authorized_keys, dict) or any(
            not isinstance(username, str)
            or not username.strip()
            or not isinstance(path, str)
            or not path.strip()
            for username, path in self.authorized_keys.items()
        ):
            raise ValueError("channels.ssh.authorized_keys must map usernames to non-empty file paths")
        self.authorized_keys = {
            username: str(Path(path).expanduser())
            for username, path in self.authorized_keys.items()
        }
        if self.host_key_path:
            self.host_key_path = str(Path(self.host_key_path).expanduser())
        else:
            self.host_key_path = str(get_config_dir() / "ssh_host_key")


def proxy_config_from_dict(raw: dict[str, Any]) -> ProxyConfig:
    """Build proxy runtime config from channels.ssh YAML block."""
    return ProxyConfig(
        listen_host=str(raw.get("listen_host") or DEFAULT_LISTEN_HOST),
        listen_port=int(raw.get("listen_port", 2222)),
        host_key_path=str(raw.get("host_key_path") or ""),
        authorized_keys=raw.get("authorized_keys", {}),
    )
