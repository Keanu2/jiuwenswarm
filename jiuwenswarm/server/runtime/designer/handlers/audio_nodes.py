# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Fast speech / music (BGM) handlers — avoid multi-iteration agent stalls."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from jiuwenswarm.common.schema.designer_graph import NODE_TYPE_AUDIO, DesignerGraphNode
from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref, write_workspace_text
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext, NodeResult

logger = logging.getLogger(__name__)
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        exe = str(imageio_ffmpeg.get_ffmpeg_exe() or "").strip()
        return exe or None
    except Exception:  # noqa: BLE001
        return None


def _bed_duration_sec(cfg: dict) -> float:
    mode = str(cfg.get("optimize_for") or "quality").lower()
    # Cost: short bed; quality: still short so BGM never outlasts video gens.
    try:
        explicit = float(cfg.get("duration_sec") or 0)
    except (TypeError, ValueError):
        explicit = 0.0
    if explicit > 0:
        return max(2.0, min(12.0, explicit))
    return 4.0 if mode == "cost" else 6.0


def _synthesize_bed(dest: Path, *, duration: float, kind: str) -> bool:
    """Generate a short non-vocal bed with ffmpeg lavfi (no remote music API)."""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Soft low pad — not a full song; keeps compose moving.
    freq = "196" if kind == "music" else "0"
    if kind == "speech":
        # Near-silent placeholder so speech mix has a track slot without TTS API.
        lavfi = f"anullsrc=r=44100:cl=mono"
    else:
        lavfi = (
            f"sine=frequency={freq}:sample_rate=44100:duration={duration},"
            f"volume=0.08"
        )
    args = [
        "-y",
        "-f",
        "lavfi",
        "-i",
        lavfi,
        "-t",
        f"{duration:.2f}",
        "-ac",
        "2",
        "-ar",
        "44100",
        str(dest.resolve()),
    ]
    try:
        proc = subprocess.run(
            [ffmpeg, *args],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=_CREATE_NO_WINDOW,
        )
        return proc.returncode == 0 and dest.is_file() and dest.stat().st_size > 0
    except Exception:  # noqa: BLE001
        logger.info("ffmpeg audio bed failed", exc_info=True)
        return False


class MusicNodeHandler:
    """BGM/audio-bed: local short bed (≤20s). No multi-minute agent loops."""

    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        duration = _bed_duration_sec(cfg)
        stem = f"designer_music_{ctx.run_id}_{ctx.node_id}"
        dest = Path(get_agent_workspace_dir()) / f"{stem}.mp3"
        if _synthesize_bed(dest, duration=duration, kind="music"):
            return NodeResult(
                output_ref=file_output_ref(dest, kind=NODE_TYPE_AUDIO, mime_type="audio/mpeg"),
                message=f"music bed {duration:.0f}s (local ffmpeg; no remote music API)",
            )
        notes = (
            "# Music / BGM\n\n"
            "Remote `call_music_model` is not configured. "
            f"Wrote notes instead of a {duration:.0f}s bed "
            "(install ffmpeg for a local placeholder bed).\n"
        )
        path = write_workspace_text(stem, notes)
        return NodeResult(
            output_ref=file_output_ref(path, kind="text", mime_type="text/markdown"),
            message="music notes (no ffmpeg / no music API)",
        )


class SpeechNodeHandler:
    """Speech/TTS placeholder: fast local silent/near-silent track or notes."""

    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        duration = _bed_duration_sec(cfg)
        stem = f"designer_speech_{ctx.run_id}_{ctx.node_id}"
        dest = Path(get_agent_workspace_dir()) / f"{stem}.mp3"
        if _synthesize_bed(dest, duration=duration, kind="speech"):
            return NodeResult(
                output_ref=file_output_ref(dest, kind=NODE_TYPE_AUDIO, mime_type="audio/mpeg"),
                message=f"speech placeholder {duration:.0f}s (local; wire TTS API for real speech)",
            )
        notes = (
            "# Speech / TTS\n\n"
            "Speech model not configured. Placeholder notes written.\n"
        )
        path = write_workspace_text(stem, notes)
        return NodeResult(
            output_ref=file_output_ref(path, kind="text", mime_type="text/markdown"),
            message="speech notes (no TTS API)",
        )
