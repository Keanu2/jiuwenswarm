# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SessionMap Repository process entry (injection only).

仅具备注入能力，不切换 ``session_map.py`` 的现网读写。
"""

from __future__ import annotations

from jiuwenswarm.gateway.routing.session_map_repository import SessionMapRepository

_repo: SessionMapRepository | None = None


def set_session_map_repository(repo: SessionMapRepository | None) -> None:
    global _repo
    _repo = repo


def get_session_map_repository() -> SessionMapRepository | None:
    return _repo


def clear_session_map_repository() -> None:
    set_session_map_repository(None)


__all__ = [
    "clear_session_map_repository",
    "get_session_map_repository",
    "set_session_map_repository",
]
