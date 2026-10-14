# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""DESIGNER_MOCK_MEDIA seam — one implementation, two consumers.

``DESIGNER_MOCK_MEDIA=1`` swaps real image/video model calls for
deterministic local files (real PNGs / tiny MP4s shipped in the repo's
tests tree) so the whole pipeline — prompt construction, reference binding,
multi-artifact submit, uri materialization, frontend preview — runs
end-to-end without any media model. The LLM orchestration stays real: only
the file production is swapped.

Consumers:

* the designer AgentGroup package's ``generate_image`` / ``generate_video``
  tools (the worker's creative path);
* the design layer's host media guarantee (``media_guarantee``) — with the
  seam on, a materializing node gets a mock file too, so a mocked debug run
  never touches real media backends from either path.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_MOCK_ENV = "DESIGNER_MOCK_MEDIA"
_SEQ: dict[str, int] = {"image": 0, "video": 0}

_IMAGE_POOL = [
    "storyboard_1.png",
    "storyboard_2.png",
    "storyboard_3.png",
    "character_solo.png",
    "scene_card.png",
    "frame_key.png",
]
_VIDEO_POOL = ["clip_1.mp4", "clip_2.mp4", "clip_3.mp4"]

# 1x1 transparent PNG — the never-hard-fail fallback when the pool file is
# missing (keep the flow alive; the file is real and has a real extension).
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def mock_media_enabled() -> bool:
    return str(os.environ.get(_MOCK_ENV) or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def mock_assets_dir() -> Path:
    """Locate the shipped mock assets (repo checkout path, stable layout)."""
    here = Path(__file__).resolve()
    # repo layout: <repo>/jiuwenswarm/server/runtime/designer/mock_media.py;
    # the walk also tolerates other install depths and falls back to cwd.
    for base in (here.parents[4], here.parents[5], Path.cwd()):
        cand = base / "tests" / "system_tests" / "designer_graph_samples" / "mock_media"
        if cand.is_dir():
            return cand
    return Path.cwd() / "mock_media"


def next_mock_image(dest_dir: Path) -> Path:
    """Copy the next pool PNG into ``dest_dir``; never hard-fails."""
    _SEQ["image"] += 1
    src = mock_assets_dir() / _IMAGE_POOL[(_SEQ["image"] - 1) % len(_IMAGE_POOL)]
    if not src.is_file():
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(_TINY_PNG)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"mock_img_{_SEQ['image']:03d}_{src.name}"
    shutil.copyfile(src, target)
    return target


def next_mock_video(dest_dir: Path) -> Path:
    """Copy the next tiny MP4 (ffmpeg testsrc, 3s) into ``dest_dir``."""
    _SEQ["video"] += 1
    src = mock_assets_dir() / _VIDEO_POOL[(_SEQ["video"] - 1) % len(_VIDEO_POOL)]
    if not src.is_file():
        raise FileNotFoundError(f"mock video asset missing: {src}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"mock_clip_{_SEQ['video']:03d}_{src.name}"
    shutil.copyfile(src, target)
    return target


def media_save_dir(run_id: str) -> Path:
    """Team-owned media dir for one run; falls back to the legacy layout."""
    from jiuwenswarm.server.runtime.designer.node_agent import (
        _media_dir_from_team_workspace,
    )

    root = _media_dir_from_team_workspace(run_id)
    if root is not None:
        return root
    from jiuwenswarm.common.utils import get_agent_workspace_dir

    root = get_agent_workspace_dir() / "designer_media" / run_id
    root.mkdir(parents=True, exist_ok=True)
    return root


__all__ = [
    "media_save_dir",
    "mock_assets_dir",
    "mock_media_enabled",
    "next_mock_image",
    "next_mock_video",
]
