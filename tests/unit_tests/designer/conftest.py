# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Designer unit tests execute graphs without a complete image/video Settings block."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _skip_media_config_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.require_media_models",
        lambda *, image=False, video=False: None,
    )
