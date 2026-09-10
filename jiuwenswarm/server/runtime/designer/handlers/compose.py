# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compose node handler: concatenate per-shot clip videos into one film."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CLIP,
    NODE_TYPE_VIDEO,
    AssetRef,
    DesignerGraphNode,
    node_role,
    node_shot_index,
)
from jiuwenswarm.server.runtime.designer.handlers import common as handler_io
from jiuwenswarm.server.runtime.designer.handlers.common import role_output_text
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext, NodeResult

logger = logging.getLogger(__name__)

_VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".m4v"}
_STREAM_SIZE = re.compile(r"Stream #0:\d+.*.*?Video:.*?(\d{2,5})x(\d{2,5})")
_MEDIA_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _find_ffmpeg() -> str:
    env = str(os.environ.get("FFMPEG_BINARY") or os.environ.get("FFMPEG_PATH") or "").strip()
    if env and Path(env).is_file():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        exe = str(imageio_ffmpeg.get_ffmpeg_exe() or "").strip()
        if exe and Path(exe).is_file():
            return exe
    except Exception:
        logger.debug("imageio_ffmpeg is not available", exc_info=True)
    raise RuntimeError("未找到 ffmpeg，无法合并视频片段。请安装 ffmpeg 或 imageio-ffmpeg。")


def _run_ffmpeg(ffmpeg: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "check": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    return subprocess.run([ffmpeg, *args], **kwargs)


def _concat_file_line(path: Path) -> str:
    posix = path.resolve().as_posix().replace("'", r"'\''")
    return f"file '{posix}'"


def _video_size(ffmpeg: str, path: Path) -> tuple[int, int] | None:
    probed = _run_ffmpeg(ffmpeg, ["-i", str(path.resolve())])
    text = f"{probed.stderr or ''}\n{probed.stdout or ''}"
    match = _STREAM_SIZE.search(text)
    if match is None:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    return width - (width % 2), height - (height % 2)


def _output_ok(dest: Path) -> bool:
    return dest.is_file() and dest.stat().st_size > 0


def _try_concat_copy(ffmpeg: str, paths: list[Path], dest: Path) -> bool:
    handle, list_name = tempfile.mkstemp(prefix="designer_concat_", suffix=".txt")
    os.close(handle)
    list_path = Path(list_name)
    try:
        list_path.write_text(
            "\n".join(_concat_file_line(path) for path in paths) + "\n",
            encoding="utf-8",
        )
        copied = _run_ffmpeg(
            ffmpeg,
            [
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c:v",
                "copy",
                "-an",
                str(dest),
            ],
        )
        if copied.returncode == 0 and _output_ok(dest):
            return True
        logger.warning(
            "ffmpeg concat copy failed: %s",
            (copied.stderr or copied.stdout or "")[:800],
        )
        dest.unlink(missing_ok=True)
        return False
    finally:
        list_path.unlink(missing_ok=True)


def _concat_filter(ffmpeg: str, paths: list[Path], dest: Path) -> None:
    width, height = _video_size(ffmpeg, paths[0]) or (1280, 720)
    inputs: list[str] = ["-y"]
    filters: list[str] = []
    for index, path in enumerate(paths):
        inputs.extend(["-i", str(path.resolve())])
        filters.append(
            f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,fps=24[v{index}]"
        )
    joined = "".join(f"[v{index}]" for index in range(len(paths)))
    filters.append(f"{joined}concat=n={len(paths)}:v=1:a=0[v]")
    encoded = _run_ffmpeg(
        ffmpeg,
        [
            *inputs,
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[v]",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(dest),
        ],
    )
    if encoded.returncode != 0 or not _output_ok(dest):
        detail = (encoded.stderr or encoded.stdout or "").strip()[:800]
        raise RuntimeError(f"合并视频片段失败：{detail or 'ffmpeg concat error'}")


def concatenate_clip_videos(paths: list[Path], dest: Path) -> Path:
    """Join shot clips in order (clip 1, clip 2, ...). Tests monkeypatch this function."""
    if not paths:
        raise RuntimeError("没有可合并的视频片段")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(paths) == 1:
        shutil.copyfile(paths[0], dest)
        return dest.resolve()

    ffmpeg = _find_ffmpeg()
    if _try_concat_copy(ffmpeg, paths, dest):
        return dest.resolve()
    _concat_filter(ffmpeg, paths, dest)
    return dest.resolve()


def collect_clip_video_paths(ctx: NodeExecutionContext) -> list[Path]:
    states = (ctx.run or {}).get("node_states") or {}
    clips = sorted(
        [node for node in (ctx.graph.get("nodes") or []) if node_role(node) == NODE_ROLE_CLIP],
        key=node_shot_index,
    )
    paths: list[Path] = []
    missing: list[str] = []
    for node in clips:
        node_id = str(node.get("id") or "")
        ref = (states.get(node_id) or {}).get("output_ref") or {}
        path = handler_io.path_from_uri(str(ref.get("uri") or ""))
        if (
            path is None
            or not path.is_file()
            or path.suffix.lower() not in _VIDEO_SUFFIXES
        ):
            missing.append(node_id or f"shot {node_shot_index(node)}")
            continue
        paths.append(path.resolve())
    if missing:
        raise RuntimeError(
            "以下视频片段尚未生成，无法成片：" + "、".join(missing)
        )
    return paths


def _media_duration_seconds(ffmpeg: str, path: Path) -> float | None:
    probed = _run_ffmpeg(ffmpeg, ["-i", str(path.resolve())])
    text = f"{probed.stderr or ''}\n{probed.stdout or ''}"
    match = _MEDIA_DURATION.search(text)
    if match is None:
        return None
    hours, minutes, seconds = (
        int(match.group(1)),
        int(match.group(2)),
        float(match.group(3)),
    )
    return hours * 3600 + minutes * 60 + seconds


def _render_cinematic_bgm(ffmpeg: str, dest: Path, duration: float) -> Path | None:
    seconds = max(1.0, float(duration))
    fade_out = max(0.0, seconds - 1.8)
    dest = Path(dest).with_suffix(".m4a")
    rendered = _run_ffmpeg(
        ffmpeg,
        [
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=110:sample_rate=44100:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=164.81:sample_rate=44100:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"anoisesrc=color=brown:sample_rate=44100:duration={seconds}",
            "-filter_complex",
            (
                "[0]volume=0.07[a];[1]volume=0.045[b];[2]lowpass=f=280,volume=0.03[c];"
                "[a][b][c]amix=inputs=3:duration=longest:dropout_transition=0,"
                f"afade=t=in:st=0:d=1.2,afade=t=out:st={fade_out}:d=1.6"
            ),
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(dest),
        ],
    )
    if rendered.returncode != 0 or not _output_ok(dest):
        logger.warning(
            "compose BGM render failed: %s",
            (rendered.stderr or rendered.stdout or "")[:800],
        )
        dest.unlink(missing_ok=True)
        return None
    return dest.resolve()


def _mux_bgm(ffmpeg: str, video: Path, audio: Path, dest: Path) -> bool:
    muxed = _run_ffmpeg(
        ffmpeg,
        [
            "-y",
            "-i",
            str(video.resolve()),
            "-i",
            str(audio.resolve()),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(dest),
        ],
    )
    if muxed.returncode != 0 or not _output_ok(dest):
        logger.warning(
            "compose BGM mux failed: %s",
            (muxed.stderr or muxed.stdout or "")[:800],
        )
        dest.unlink(missing_ok=True)
        return False
    return True


def mix_compose_bgm(video: Path, dest: Path, brief: str = "") -> Path:
    """Add a film-length score after silent clip concat. Skip if probe/mix fails."""
    _ = brief
    video = Path(video)
    dest = Path(dest)
    try:
        ffmpeg = _find_ffmpeg()
    except RuntimeError:
        return video
    duration = _media_duration_seconds(ffmpeg, video)
    if duration is None or duration < 0.4:
        return video
    score = _render_cinematic_bgm(
        ffmpeg, dest.parent / f"{dest.stem}_score", duration
    )
    if score is None:
        return video
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.resolve() == video.resolve():
        dest = video.with_name(f"{video.stem}_bgm{video.suffix}")
    if _mux_bgm(ffmpeg, video, score, dest):
        return dest.resolve()
    return video


class ComposeNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        paths = collect_clip_video_paths(ctx)
        logger.info(
            "Designer compose concatenating %s clip(s) in shot order for run=%s",
            len(paths),
            ctx.run_id,
        )
        dest = handler_io.get_agent_workspace_dir() / f"designer_compose_{ctx.run_id}.mp4"
        merged = concatenate_clip_videos(paths, dest)
        scored = mix_compose_bgm(
            merged,
            dest.parent / f"designer_compose_{ctx.run_id}_bgm.mp4",
            brief=role_output_text(ctx, NODE_ROLE_BRIEF) or "",
        )
        output_ref: AssetRef = {
            "kind": NODE_TYPE_VIDEO,
            "uri": scored.resolve().as_uri(),
            "mime_type": "video/mp4",
            "label": scored.name,
        }
        return NodeResult(output_ref=output_ref, message="composed video")
