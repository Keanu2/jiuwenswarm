# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compose node handler: concatenate per-shot clip videos into one film."""

from __future__ import annotations

import asyncio
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


def _video_path_from_ref(ref: object) -> Path | None:
    if not isinstance(ref, dict):
        return None
    uri = str(ref.get("uri") or "")
    # Never treat markdown / text stubs as video inputs for compose.
    low = uri.lower()
    if any(low.endswith(suf) for suf in (".md", ".markdown", ".txt", ".json", ".html")):
        return None
    mime = str(ref.get("mime_type") or "").lower()
    if mime.startswith("text/") or "markdown" in mime:
        return None
    path = handler_io.path_from_uri(uri)
    if (
        path is not None
        and path.is_file()
        and path.suffix.lower() in _VIDEO_SUFFIXES
        and path.stat().st_size > 0
    ):
        return path.resolve()
    return None


def _video_from_state(state: dict) -> Path | None:
    candidates: list[object] = []
    for key in ("output_ref", "candidate_output_ref"):
        primary = state.get(key)
        if primary is not None:
            candidates.append(primary)
    for key in ("output_refs", "candidate_output_refs"):
        for ref in state.get(key) or []:
            if ref is not None:
                candidates.append(ref)
    for ref in candidates:
        path = _video_path_from_ref(ref)
        if path is not None:
            return path
    return None


def _workspace_clip_videos(run_id: str) -> list[Path]:
    """Fallback: clip mp4s already on disk for this run (state lag / early compose)."""
    root = handler_io.get_agent_workspace_dir()
    if not root.is_dir():
        return []
    rid = str(run_id or "").strip()
    found: list[Path] = []
    patterns = (
        f"designer_clip_{rid}*.mp4",
        f"*{rid}*clip*.mp4",
        f"designer_compose_{rid}*.mp4",
    )
    # Never treat still→mp4 freezes as real clip fallbacks in the quality path.
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if (
                path.is_file()
                and path.stat().st_size > 0
                and "compose" not in path.name.lower()
                and "designer_clip_still_" not in path.name.lower()
            ):
                resolved = path.resolve()
                if resolved not in found:
                    found.append(resolved)
    if found:
        return found
    # I2V backends often write generated_<timestamp>.mp4 without embedding run_id.
    import time

    now = time.time()
    generated = [
        p.resolve()
        for p in sorted(
            root.glob("generated_*.mp4"),
            key=lambda item: item.stat().st_mtime if item.is_file() else 0,
            reverse=True,
        )
        if p.is_file()
        and p.stat().st_size > 0
        and (now - p.stat().st_mtime) < 45 * 60
    ]
    return generated[:3]


def collect_clip_video_paths(ctx: NodeExecutionContext) -> list[Path]:
    """Collect every shot clip mp4 in storyboard order — all must feed the final film."""
    states = (ctx.run or {}).get("node_states") or {}
    clips = sorted(
        [node for node in (ctx.graph.get("nodes") or []) if node_role(node) == NODE_ROLE_CLIP],
        key=node_shot_index,
    )
    # Prefer edged clips when present, but never drop other completed clips.
    pred_ids = {
        str(e.get("source") or "")
        for e in (ctx.graph.get("edges") or [])
        if str(e.get("target") or "") == str(ctx.node_id or "")
    }
    if pred_ids:
        edged = [n for n in clips if str(n.get("id") or "") in pred_ids]
        extras = [n for n in clips if str(n.get("id") or "") not in pred_ids]
        clips = [*edged, *extras] if edged else clips

    paths: list[Path] = []
    missing: list[str] = []

    for node in clips:
        node_id = str(node.get("id") or "")
        state = states.get(node_id) or {}
        if not isinstance(state, dict):
            state = {}
        path = _video_from_state(state)
        if path is None:
            missing.append(node_id or f"shot {node_shot_index(node)}")
            continue
        paths.append(path)

    if missing:
        # Disk fallback when clip agents finished I2V but state still shows .md,
        # or compose was spawned before node_states were published.
        disk = _workspace_clip_videos(str(ctx.run_id or ""))
        if disk and not paths:
            logger.warning(
                "compose using workspace clip mp4 fallback for run=%s missing=%s",
                ctx.run_id,
                missing,
            )
            return disk
        raise RuntimeError(
            "以下视频片段尚未生成，无法成片（全部镜头必须进入成片）："
            + "、".join(missing)
        )
    return paths


def _audio_path_from_ref(ref: dict | None) -> Path | None:
    if not isinstance(ref, dict):
        return None
    uri = str(ref.get("uri") or "").strip()
    if not uri:
        return None
    try:
        from urllib.parse import unquote, urlparse

        parsed = urlparse(uri)
        raw = unquote(parsed.path or "")
        if raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
            raw = raw[1:]
        path = Path(raw) if uri.startswith("file:") else Path(uri)
    except Exception:  # noqa: BLE001
        return None
    audio_ext = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
    if path.is_file() and path.suffix.lower() in audio_ext and path.stat().st_size > 64:
        return path.resolve()
    return None


def _collect_role_audio_paths(ctx: NodeExecutionContext, roles: set[str]) -> list[Path]:
    """Collect real audio files from speech/music nodes (skip markdown stubs)."""
    states = (ctx.run or {}).get("node_states") or {}
    found: list[Path] = []
    for node in ctx.graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        role = str((node.get("config") or {}).get("role") or "")
        if role not in roles:
            continue
        nid = str(node.get("id") or "")
        state = states.get(nid) if isinstance(states.get(nid), dict) else {}
        refs: list[dict] = []
        if isinstance(state.get("output_ref"), dict):
            refs.append(state["output_ref"])
        for r in state.get("output_refs") or []:
            if isinstance(r, dict):
                refs.append(r)
        for ref in refs:
            path = _audio_path_from_ref(ref)
            if path is not None:
                found.append(path)
                break
    return found


def _mix_audio_tracks(
    ffmpeg: str, tracks: list[Path], dest: Path, duration: float
) -> Path | None:
    """Amix speech + music (+ optional bed) to a single AAC bed of film length."""
    if not tracks:
        return None
    seconds = max(1.0, float(duration))
    dest = Path(dest).with_suffix(".m4a")
    dest.parent.mkdir(parents=True, exist_ok=True)
    args: list[str] = ["-y"]
    for track in tracks:
        args.extend(["-i", str(track.resolve())])
    n = len(tracks)
    # Loud enough to hear; speech slightly above music.
    filters = []
    for i in range(n):
        vol = 0.85 if i == 0 and n > 1 else 0.55
        filters.append(
            f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo,"
            f"volume={vol},atrim=0:{seconds},apad=whole_dur={seconds}[a{i}]"
        )
    mix_ins = "".join(f"[a{i}]" for i in range(n))
    filters.append(
        f"{mix_ins}amix=inputs={n}:duration=longest:dropout_transition=0,"
        f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:st=0:d=0.4,"
        f"afade=t=out:st={max(0.0, seconds - 0.8)}:d=0.7[out]"
    )
    args.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-t",
            f"{seconds:.2f}",
            str(dest),
        ]
    )
    mixed = _run_ffmpeg(ffmpeg, args)
    if mixed.returncode != 0 or not _output_ok(dest):
        logger.warning(
            "compose audio mix failed: %s",
            (mixed.stderr or mixed.stdout or "")[:800],
        )
        dest.unlink(missing_ok=True)
        return None
    return dest.resolve()


def mix_compose_soundtrack(
    video: Path,
    dest: Path,
    *,
    ctx: NodeExecutionContext | None = None,
    brief: str = "",
) -> Path:
    """Mux audible soundtrack: prefer upstream speech/music nodes, else cinematic bed."""
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

    upstream: list[Path] = []
    if ctx is not None:
        # Prefer music then speech so speech sits on top when mixed (order in _mix).
        speech = _collect_role_audio_paths(ctx, {"speech"})
        music = _collect_role_audio_paths(ctx, {"music"})
        # Put speech first for slightly higher volume in mixer.
        upstream = [*speech, *music]

    score: Path | None = None
    if upstream:
        score = _mix_audio_tracks(
            ffmpeg, upstream, dest.parent / f"{dest.stem}_mix", duration
        )
    if score is None:
        # Always add audible bed — silent films are not acceptable for quality path.
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


def mix_compose_bgm(video: Path, dest: Path, brief: str = "") -> Path:
    """Backward-compatible wrapper — always produce audible score."""
    return mix_compose_soundtrack(video, dest, ctx=None, brief=brief)


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
                # Audible cinematic bed (previous 0.07 levels were effectively silent).
                "[0]volume=0.28[a];[1]volume=0.18[b];[2]lowpass=f=280,volume=0.12[c];"
                "[a][b][c]amix=inputs=3:duration=longest:dropout_transition=0,"
                f"loudnorm=I=-16:TP=-1.5:LRA=11,"
                f"afade=t=in:st=0:d=1.0,afade=t=out:st={fade_out}:d=1.4"
            ),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
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
        paths: list[Path] = []
        last_exc: Exception | None = None
        # Brief retries: compose is often spawned before clip node_states publish.
        for attempt in range(4):
            try:
                paths = collect_clip_video_paths(ctx)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt >= 3:
                    break
                await asyncio.sleep(0.4 * (attempt + 1))
        if not paths:
            raise RuntimeError(str(last_exc or "没有可合并的视频片段"))
        logger.info(
            "Designer compose concatenating %s clip(s) in shot order for run=%s",
            len(paths),
            ctx.run_id,
        )
        dest = handler_io.get_agent_workspace_dir() / f"designer_compose_{ctx.run_id}.mp4"
        merged = concatenate_clip_videos(paths, dest)
        scored = mix_compose_soundtrack(
            merged,
            dest.parent / f"designer_compose_{ctx.run_id}_bgm.mp4",
            ctx=ctx,
            brief=role_output_text(ctx, NODE_ROLE_BRIEF) or "",
        )
        scored = Path(scored)
        if (
            not scored.is_file()
            or scored.suffix.lower() != ".mp4"
            or scored.stat().st_size < 512
        ):
            raise RuntimeError(
                f"Compose failed to produce a real mp4 film (got {scored})"
            )
        output_ref: AssetRef = {
            "kind": NODE_TYPE_VIDEO,
            "uri": scored.resolve().as_uri(),
            "mime_type": "video/mp4",
            "label": scored.name,
        }
        return NodeResult(output_ref=output_ref, message="composed video")
