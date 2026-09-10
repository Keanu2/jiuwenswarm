# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CLIP,
    NODE_TYPE_TEXT,
    NODE_TYPE_VIDEO,
    SCHEMA_VERSION,
    normalize_execution_graph,
)
from jiuwenswarm.server.runtime.designer.handlers.clip import (
    ClipNodeHandler,
    build_clip_prompt,
    generate_clip_video,
)
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext


def _graph(*, brief: str = "火车站晨间短片", clip_prompt: str | None = None):
    clip_config: dict[str, object] = {"role": NODE_ROLE_CLIP}
    if clip_prompt is not None:
        clip_config["prompt"] = clip_prompt
    return normalize_execution_graph(
        {
            "schema_version": SCHEMA_VERSION,
            "graph_id": "graph_clip01",
            "project_id": "proj_clip01",
            "title": "火车站",
            "description": "火车进站，年轻人走下车",
            "source": "manual",
            "nodes": [
                {
                    "id": "n_brief",
                    "type": NODE_TYPE_TEXT,
                    "label": "brief",
                    "config": {"role": NODE_ROLE_BRIEF, "prompt": brief},
                },
                {
                    "id": "n_clip",
                    "type": NODE_TYPE_VIDEO,
                    "label": "clip",
                    "config": clip_config,
                },
            ],
            "edges": [],
        }
    )


def test_build_clip_prompt_uses_brief_then_graph_text() -> None:
    graph = _graph()
    clip = graph["nodes"][1]
    assert "火车站晨间短片" in build_clip_prompt(graph, clip)

    clip["config"] = {"role": NODE_ROLE_CLIP, "prompt": "只拍站台"}
    assert "只拍站台" in build_clip_prompt(graph, clip)


def test_build_clip_prompt_reads_upstream_brief_and_storyboard(tmp_path: Path) -> None:
    from jiuwenswarm.common.schema.designer_graph import NODE_ROLE_STORYBOARD, NODE_TYPE_TABLE
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    brief = tmp_path / "brief.md"
    story = tmp_path / "storyboard.md"
    brief.write_text("# Brief\n火车进站", encoding="utf-8")
    story.write_text("## 分镜表\n缓推进站\n\n## 运镜脚本\n跟移", encoding="utf-8")
    graph = _graph()
    graph["nodes"].insert(
        1,
        {
            "id": "n_storyboard",
            "type": NODE_TYPE_TABLE,
            "label": "storyboard",
            "config": {"role": NODE_ROLE_STORYBOARD},
        },
    )
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_clip_up",
        node_id="n_clip",
        run={
            "node_states": {
                "n_brief": {
                    "status": "completed",
                    "output_ref": file_output_ref(brief, kind="text", mime_type="text/markdown"),
                },
                "n_storyboard": {
                    "status": "completed",
                    "output_ref": file_output_ref(story, kind="table", mime_type="text/markdown"),
                },
            }
        },
    )
    prompt = build_clip_prompt(graph, graph["nodes"][-1], ctx)
    assert "火车进站" in prompt
    assert "缓推进站" in prompt
    assert "运镜脚本" in prompt


@pytest.mark.asyncio
async def test_clip_handler_sends_keyframes_and_storyboard_as_multimodal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_ROLE_FRAME,
        NODE_ROLE_STORYBOARD,
        NODE_TYPE_IMAGE,
        NODE_TYPE_TABLE,
    )
    from jiuwenswarm.server.runtime.designer.handlers.clip import collect_clip_reference_images
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    shot1 = tmp_path / "shot1.png"
    shot2 = tmp_path / "shot2.png"
    story = tmp_path / "storyboard.md"
    shot1.write_bytes(b"png-1")
    shot2.write_bytes(b"png-2")
    story.write_text(
        "## 分镜表\n"
        "| 镜号 | 时间轴 | 镜头视角 | 运镜 | 人物变化 | 场景变化 |\n"
        "| 1 | 0.0-2.0s | 全景/平视 | 缓摇 | 未入画 | 站台 |\n"
        "| 2 | 2.0-5.0s | 中景/平视 | 跟移 | 主体入画 | 出站 |\n",
        encoding="utf-8",
    )
    video = tmp_path / "generated_clip.mp4"
    video.write_bytes(b"fake-mp4")
    graph = _graph()
    graph["nodes"][1:1] = [
        {
            "id": "n_storyboard",
            "type": NODE_TYPE_TABLE,
            "label": "storyboard",
            "config": {"role": NODE_ROLE_STORYBOARD},
        },
        {
            "id": "n_frame",
            "type": NODE_TYPE_IMAGE,
            "label": "frame",
            "config": {"role": NODE_ROLE_FRAME},
        },
    ]
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_clip_mm",
        node_id="n_clip",
        run={
            "node_states": {
                "n_storyboard": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        story, kind=NODE_TYPE_TABLE, mime_type="text/markdown"
                    ),
                },
                "n_frame": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        shot1, kind=NODE_TYPE_IMAGE, mime_type="image/png"
                    ),
                    "output_refs": [
                        file_output_ref(shot1, kind=NODE_TYPE_IMAGE, mime_type="image/png"),
                        file_output_ref(shot2, kind=NODE_TYPE_IMAGE, mime_type="image/png"),
                    ],
                },
            }
        },
    )
    assert collect_clip_reference_images(ctx) == [shot1.resolve()]
    prompt = build_clip_prompt(graph, graph["nodes"][-1], ctx)
    assert "shot 1" in prompt.lower()
    assert "缓摇" in prompt
    assert "This shot from the storyboard" in prompt
    assert "Full storyboard table" in prompt

    seen: dict[str, object] = {}

    async def fake_generate(
        prompt: str,
        save_dir: str | None = None,
        first_frame: str | None = None,
        reference_images: list[str] | None = None,
        reference_file: str | None = None,
        duration: int = 5,
    ) -> dict[str, str]:
        seen["first_frame"] = first_frame
        seen["reference_images"] = reference_images
        seen["reference_file"] = reference_file
        seen["prompt"] = prompt
        seen["duration"] = duration
        return {"video_path": str(video), "revised_prompt": prompt}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.generate_clip_video",
        fake_generate,
    )
    await ClipNodeHandler().execute(graph["nodes"][-1], ctx)
    assert seen["first_frame"] is None
    assert seen["reference_images"] == [str(shot1.resolve())]
    assert seen["reference_file"] == str(story.resolve())
    assert seen["duration"] == 2
    assert "Shot 1" in str(seen["prompt"])
    assert "Full storyboard table" in str(seen["prompt"])


@pytest.mark.asyncio
async def test_clip_handler_sends_character_and_keyframe_as_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_ROLE_CHARACTER_DESIGN,
        NODE_ROLE_FRAME,
        NODE_ROLE_SCENE,
        NODE_ROLE_STORYBOARD,
        NODE_TYPE_IMAGE,
        NODE_TYPE_TABLE,
    )
    from jiuwenswarm.server.runtime.designer.handlers.clip import (
        collect_clip_reference_images,
    )
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    frame = tmp_path / "keyframe.png"
    character = tmp_path / "character.png"
    scene = tmp_path / "scene.png"
    story = tmp_path / "storyboard.md"
    frame.write_bytes(b"png-frame")
    character.write_bytes(b"png-character")
    scene.write_bytes(b"png-scene")
    story.write_text(
        "## 分镜表\n"
        "| 镜号 | 时间轴 | 镜头视角 | 运镜 | 人物变化 | 场景变化 |\n"
        "| 1 | 0.0-5.0s | 中景/平视 | 缓推 | 主体入画 | 站台 |\n",
        encoding="utf-8",
    )
    video = tmp_path / "generated_clip.mp4"
    video.write_bytes(b"fake-mp4")
    graph = _graph()
    graph["nodes"][1:1] = [
        {
            "id": "n_character",
            "type": NODE_TYPE_IMAGE,
            "label": "character",
            "config": {"role": NODE_ROLE_CHARACTER_DESIGN},
        },
        {
            "id": "n_scene",
            "type": NODE_TYPE_IMAGE,
            "label": "scene",
            "config": {"role": NODE_ROLE_SCENE},
        },
        {
            "id": "n_storyboard",
            "type": NODE_TYPE_TABLE,
            "label": "storyboard",
            "config": {"role": NODE_ROLE_STORYBOARD},
        },
        {
            "id": "n_frame",
            "type": NODE_TYPE_IMAGE,
            "label": "frame",
            "config": {"role": NODE_ROLE_FRAME},
        },
    ]
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_clip_r2va",
        node_id="n_clip",
        run={
            "node_states": {
                "n_character": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        character, kind=NODE_TYPE_IMAGE, mime_type="image/png"
                    ),
                },
                "n_scene": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        scene, kind=NODE_TYPE_IMAGE, mime_type="image/png"
                    ),
                },
                "n_storyboard": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        story, kind=NODE_TYPE_TABLE, mime_type="text/markdown"
                    ),
                },
                "n_frame": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        frame, kind=NODE_TYPE_IMAGE, mime_type="image/png"
                    ),
                },
            }
        },
    )
    assert collect_clip_reference_images(ctx) == [
        frame.resolve(),
        character.resolve(),
        scene.resolve(),
    ]
    prompt = build_clip_prompt(graph, graph["nodes"][-1], ctx)
    assert "Image 1 is this shot's keyframe" in prompt
    assert "Image 2 is the character sheet" in prompt
    assert "Image 3 is the scene" in prompt
    assert "attached file is the storyboard" in prompt
    assert "缓推" in prompt
    assert "No music" in prompt
    assert "no BGM" in prompt

    seen: dict[str, object] = {}

    async def fake_generate(
        prompt: str,
        save_dir: str | None = None,
        first_frame: str | None = None,
        reference_images: list[str] | None = None,
        **kwargs,
    ) -> dict[str, str]:
        seen["first_frame"] = first_frame
        seen["reference_images"] = reference_images
        seen["reference_file"] = kwargs.get("reference_file")
        seen["prompt"] = prompt
        return {"video_path": str(video), "revised_prompt": prompt}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.generate_clip_video",
        fake_generate,
    )
    await ClipNodeHandler().execute(graph["nodes"][-1], ctx)
    assert seen["first_frame"] is None
    assert seen["reference_images"] == [
        str(frame.resolve()),
        str(character.resolve()),
        str(scene.resolve()),
    ]
    assert seen["reference_file"] == str(story.resolve())
    assert "Image 1 is this shot's keyframe" in str(seen["prompt"])


@pytest.mark.asyncio
async def test_clip_handler_returns_file_output_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "generated_clip.mp4"
    video.write_bytes(b"fake-mp4")

    async def fake_generate(
        prompt: str,
        save_dir: str | None = None,
        first_frame: str | None = None,
        reference_images: list[str] | None = None,
        **kwargs,
    ) -> dict[str, str]:
        assert "火车站" in prompt
        return {"video_path": str(video), "revised_prompt": prompt}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.generate_clip_video",
        fake_generate,
    )
    graph = _graph()
    result = await ClipNodeHandler().execute(
        graph["nodes"][1],
        NodeExecutionContext(graph=graph, run_id="run_clip01", node_id="n_clip"),
    )
    assert result.output_ref is not None
    assert result.output_ref["kind"] == NODE_TYPE_VIDEO
    assert result.output_ref["mime_type"] == "video/mp4"
    assert result.output_ref["label"] == "generated_clip.mp4"
    assert result.output_ref["uri"].startswith("file:")
    assert result.output_ref["uri"].endswith("generated_clip.mp4")


@pytest.mark.asyncio
async def test_generate_clip_video_raises_on_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_invoke(prompt: str, **kwargs):
        return {"error": "[ERROR]: MiniMax video create failed 402"}

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.video_tools._invoke_model_video_generation",
        fake_invoke,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.multimodal_config.apply_video_gen_model_config_from_yaml",
        lambda cfg: None,
    )
    with pytest.raises(RuntimeError, match="402"):
        await generate_clip_video("a boy playing basketball")


@pytest.mark.asyncio
async def test_clip_handler_rejects_storyboard_notes_without_keyframes(
    tmp_path: Path,
) -> None:
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_ROLE_FRAME,
        NODE_ROLE_STORYBOARD,
        NODE_TYPE_IMAGE,
        NODE_TYPE_TABLE,
    )
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    notes = tmp_path / "frame_notes.md"
    story = tmp_path / "storyboard.md"
    notes.write_text("image_gen failed, wrote notes", encoding="utf-8")
    story.write_text("## 分镜表\n缓推", encoding="utf-8")
    graph = _graph()
    graph["nodes"][1:1] = [
        {
            "id": "n_storyboard",
            "type": NODE_TYPE_TABLE,
            "label": "storyboard",
            "config": {"role": NODE_ROLE_STORYBOARD},
        },
        {
            "id": "n_frame",
            "type": NODE_TYPE_IMAGE,
            "label": "frame",
            "config": {"role": NODE_ROLE_FRAME},
        },
    ]
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_clip_notes",
        node_id="n_clip",
        run={
            "node_states": {
                "n_storyboard": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        story, kind=NODE_TYPE_TABLE, mime_type="text/markdown"
                    ),
                },
                "n_frame": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        notes, kind="text", mime_type="text/markdown"
                    ),
                },
            }
        },
    )
    with pytest.raises(RuntimeError, match="no matching keyframe"):
        await ClipNodeHandler().execute(graph["nodes"][-1], ctx)


@pytest.mark.asyncio
async def test_clip_handler_submits_matching_keyframe_for_shot_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_ROLE_FRAME,
        NODE_ROLE_STORYBOARD,
        NODE_TYPE_IMAGE,
        NODE_TYPE_TABLE,
    )
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    shot1 = tmp_path / "shot1.png"
    shot2 = tmp_path / "shot2.png"
    story = tmp_path / "storyboard.md"
    shot1.write_bytes(b"png-1")
    shot2.write_bytes(b"png-2")
    story.write_text(
        "## 分镜表\n"
        "| 镜号 | 时间轴 | 镜头视角 | 运镜 | 人物变化 | 场景变化 |\n"
        "| 1 | 0.0-2.0s | 全景/平视 | 缓摇 | 未入画 | 站台 |\n"
        "| 2 | 2.0-5.0s | 中景/平视 | 跟移 | 主体入画 | 出站 |\n",
        encoding="utf-8",
    )
    video = tmp_path / "generated_clip_2.mp4"
    video.write_bytes(b"fake-mp4")
    graph = _graph()
    graph["nodes"][1] = {
        "id": "n_clip_2",
        "type": NODE_TYPE_VIDEO,
        "label": "clip 2",
        "config": {"role": NODE_ROLE_CLIP, "shot_index": 2},
    }
    graph["nodes"][1:1] = [
        {
            "id": "n_storyboard",
            "type": NODE_TYPE_TABLE,
            "label": "storyboard",
            "config": {"role": NODE_ROLE_STORYBOARD},
        },
        {
            "id": "n_frame",
            "type": NODE_TYPE_IMAGE,
            "label": "frame",
            "config": {"role": NODE_ROLE_FRAME},
        },
    ]
    clip = next(node for node in graph["nodes"] if node["id"] == "n_clip_2")
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_clip_shot2",
        node_id="n_clip_2",
        run={
            "node_states": {
                "n_storyboard": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        story, kind=NODE_TYPE_TABLE, mime_type="text/markdown"
                    ),
                },
                "n_frame": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        shot1, kind=NODE_TYPE_IMAGE, mime_type="image/png"
                    ),
                    "output_refs": [
                        file_output_ref(shot1, kind=NODE_TYPE_IMAGE, mime_type="image/png"),
                        file_output_ref(shot2, kind=NODE_TYPE_IMAGE, mime_type="image/png"),
                    ],
                },
            }
        },
    )
    seen: dict[str, object] = {}

    async def fake_generate(
        prompt: str,
        save_dir: str | None = None,
        first_frame: str | None = None,
        reference_images: list[str] | None = None,
        duration: int = 5,
        **kwargs,
    ) -> dict[str, str]:
        seen["first_frame"] = first_frame
        seen["reference_images"] = reference_images
        seen["prompt"] = prompt
        seen["duration"] = duration
        return {"video_path": str(video), "revised_prompt": prompt}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.generate_clip_video",
        fake_generate,
    )
    await ClipNodeHandler().execute(clip, ctx)
    assert seen["first_frame"] is None
    assert seen["reference_images"] == [str(shot2.resolve())]
    assert seen["duration"] == 3
    assert "Shot 2" in str(seen["prompt"])
    assert "跟移" in str(seen["prompt"])
    assert "This shot from the storyboard" in str(seen["prompt"])


@pytest.mark.asyncio
async def test_compose_handler_merges_clips_in_shot_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.schema.designer_graph import NODE_ROLE_COMPOSE
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref
    from jiuwenswarm.server.runtime.designer.handlers.compose import ComposeNodeHandler

    clip1 = tmp_path / "shot1.mp4"
    clip2 = tmp_path / "shot2.mp4"
    clip1.write_bytes(b"mp4-1")
    clip2.write_bytes(b"mp4-2")
    graph = normalize_execution_graph(
        {
            "schema_version": SCHEMA_VERSION,
            "graph_id": "graph_compose01",
            "project_id": "proj_compose01",
            "title": "成片",
            "nodes": [
                {
                    "id": "n_clip_1",
                    "type": NODE_TYPE_VIDEO,
                    "label": "clip 1",
                    "config": {"role": NODE_ROLE_CLIP, "shot_index": 1},
                },
                {
                    "id": "n_clip_2",
                    "type": NODE_TYPE_VIDEO,
                    "label": "clip 2",
                    "config": {"role": NODE_ROLE_CLIP, "shot_index": 2},
                },
                {
                    "id": "n_compose",
                    "type": NODE_TYPE_VIDEO,
                    "label": "成片",
                    "config": {"role": NODE_ROLE_COMPOSE, "inputs": ["n_clip_1", "n_clip_2"]},
                },
            ],
            "edges": [
                {"id": "e_1", "source": "n_clip_1", "target": "n_compose"},
                {"id": "e_2", "source": "n_clip_2", "target": "n_compose"},
            ],
        }
    )
    seen: dict[str, object] = {}

    def fake_concat(paths: list[Path], dest: Path) -> Path:
        seen["paths"] = [str(path) for path in paths]
        dest = Path(dest)
        dest.write_bytes(b"merged")
        return dest.resolve()

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.get_agent_workspace_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.compose.concatenate_clip_videos",
        fake_concat,
    )
    result = await ComposeNodeHandler().execute(
        graph["nodes"][-1],
        NodeExecutionContext(
            graph=graph,
            run_id="run_compose01",
            node_id="n_compose",
            run={
                "node_states": {
                    "n_clip_1": {
                        "status": "completed",
                        "output_ref": file_output_ref(
                            clip1, kind=NODE_TYPE_VIDEO, mime_type="video/mp4"
                        ),
                    },
                    "n_clip_2": {
                        "status": "completed",
                        "output_ref": file_output_ref(
                            clip2, kind=NODE_TYPE_VIDEO, mime_type="video/mp4"
                        ),
                    },
                }
            },
        ),
    )
    assert seen["paths"] == [str(clip1.resolve()), str(clip2.resolve())]
    assert result.output_ref is not None
    assert result.output_ref["kind"] == NODE_TYPE_VIDEO
    assert str(result.output_ref.get("label") or "").endswith(".mp4")


def test_concatenate_clip_videos_concats_shot1_then_shot2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from jiuwenswarm.server.runtime.designer.handlers import compose as compose_mod

    clip1 = tmp_path / "shot1.mp4"
    clip2 = tmp_path / "shot2.mp4"
    dest = tmp_path / "film.mp4"
    clip1.write_bytes(b"clip-1")
    clip2.write_bytes(b"clip-2")
    calls: list[list[str]] = []
    listings: list[str] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        if "-f" in cmd and "concat" in cmd:
            list_path = Path(cmd[cmd.index("-i") + 1])
            listings.append(list_path.read_text(encoding="utf-8"))
        dest.write_bytes(b"concatenated")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(compose_mod, "_find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(compose_mod.subprocess, "run", fake_run)
    merged = compose_mod.concatenate_clip_videos([clip1, clip2], dest)
    assert merged == dest.resolve()
    assert dest.read_bytes() == b"concatenated"
    assert calls
    copy_cmd = calls[0]
    assert copy_cmd[0] == "ffmpeg"
    assert "-f" in copy_cmd and "concat" in copy_cmd
    assert "-c:v" in copy_cmd
    assert "-an" in copy_cmd
    listing = listings[0]
    assert clip1.resolve().as_posix() in listing
    assert clip2.resolve().as_posix() in listing
    assert listing.index(clip1.resolve().as_posix()) < listing.index(clip2.resolve().as_posix())


def test_concatenate_clip_videos_falls_back_to_filter_concat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from jiuwenswarm.server.runtime.designer.handlers import compose as compose_mod

    clip1 = tmp_path / "shot1.mp4"
    clip2 = tmp_path / "shot2.mp4"
    dest = tmp_path / "film.mp4"
    clip1.write_bytes(b"clip-1")
    clip2.write_bytes(b"clip-2")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        joined = " ".join(cmd)
        if "-f" in cmd and "concat" in cmd and "-filter_complex" not in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="copy failed")
        if "-i" in cmd and "-filter_complex" not in cmd:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="Stream #0:0: Video: h264, yuv420p, 1280x720"
            )
        dest.write_bytes(b"reencoded")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(compose_mod, "_find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(compose_mod.subprocess, "run", fake_run)
    merged = compose_mod.concatenate_clip_videos([clip1, clip2], dest)
    assert merged == dest.resolve()
    assert dest.read_bytes() == b"reencoded"
    filter_cmd = next(cmd for cmd in calls if "-filter_complex" in cmd)
    spec = filter_cmd[filter_cmd.index("-filter_complex") + 1]
    assert str(clip1.resolve()) in filter_cmd
    assert str(clip2.resolve()) in filter_cmd
    assert filter_cmd.index(str(clip1.resolve())) < filter_cmd.index(str(clip2.resolve()))
    assert "concat=n=2:v=1:a=0" in spec
    assert "-an" in filter_cmd
    assert "aac" not in filter_cmd

