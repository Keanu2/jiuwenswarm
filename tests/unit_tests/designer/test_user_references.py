# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore
from jiuwenswarm.server.runtime.designer.user_references import (
    UserReferenceError,
    analysis_prompt_with_references,
    attach_user_references_to_graph,
    normalize_user_references,
    prompt_slot_roster,
    user_reference_image_paths,
)


def _png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"ref"


@pytest.fixture()
def designer_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DesignerGraphStore:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.graph_store.get_agent_root_dir",
        lambda: tmp_path,
    )
    return DesignerGraphStore()


def test_normalize_copies_path_and_assigns_ordered_slots(tmp_path: Path) -> None:
    source = tmp_path / "face.png"
    source.write_bytes(_png_bytes())
    dest = tmp_path / "refs"
    refs = normalize_user_references(
        [{"kind": "image", "path": str(source), "filename": "face.png", "mime_type": "image/png"}],
        dest_dir=dest,
    )
    assert len(refs) == 1
    assert refs[0]["id"] == "ref_01"
    assert refs[0]["kind"] == "image"
    assert refs[0]["role"] == "reference"
    copied = Path(refs[0]["path"])
    assert copied.is_file()
    assert copied.parent == dest.resolve()
    assert copied.read_bytes() == _png_bytes()
    assert "image 1 = " in prompt_slot_roster(refs)
    assert "face.png" in prompt_slot_roster(refs)
    assert str(copied) not in prompt_slot_roster(refs)


def test_normalize_decodes_base64_and_rejects_over_limit(tmp_path: Path) -> None:
    dest = tmp_path / "refs"
    payload = base64.b64encode(_png_bytes()).decode("ascii")
    refs = normalize_user_references(
        [{"kind": "image", "filename": "shot.png", "base64_data": payload, "mime_type": "image/png"}],
        dest_dir=dest,
    )
    assert Path(refs[0]["path"]).read_bytes() == _png_bytes()
    with pytest.raises(UserReferenceError, match="at most 3 image"):
        normalize_user_references(
            [
                {"kind": "image", "filename": f"{index}.png", "base64_data": payload}
                for index in range(4)
            ],
            dest_dir=dest,
        )


def test_attach_user_references_stays_on_metadata_not_brief_body(tmp_path: Path) -> None:
    source = tmp_path / "look.png"
    source.write_bytes(_png_bytes())
    refs = normalize_user_references(
        [{"kind": "image", "path": str(source), "filename": "look.png"}],
        dest_dir=tmp_path / "refs",
    )
    graph = {
        "nodes": [
            {
                "id": "n_brief",
                "type": "text",
                "config": {"role": "brief", "prompt": "火车站短片", "supervisor_task": "Write a brief."},
            }
        ],
        "metadata": {},
    }
    attached = attach_user_references_to_graph(graph, refs)
    stored = attached["metadata"]["user_references"]
    assert stored[0]["path"] == refs[0]["path"]
    brief = attached["nodes"][0]["config"]
    assert brief["prompt"] == "火车站短片"
    assert brief["user_reference_ids"] == ["ref_01"]
    assert "image 1 =" in brief["supervisor_task"]
    assert user_reference_image_paths(attached)[0].is_file()
    assert "火车站短片" in analysis_prompt_with_references("火车站短片", refs)


def test_bootstrap_graph_registers_user_references(
    designer_store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.gateway_adapter import designer_adapter as adapter

    monkeypatch.setattr(adapter, "_store", designer_store)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.script_analysis._llm_configured",
        lambda: False,
    )
    monkeypatch.setattr(
        adapter.project_store,
        "get_project_by_id",
        lambda project_id, cache_bust=False: SimpleNamespace(
            project_id=project_id,
            project_dir=str(tmp_path / "proj"),
            work_mode="work",
            hidden=False,
        ),
    )
    (tmp_path / "proj").mkdir()
    image = tmp_path / "hero.png"
    image.write_bytes(_png_bytes())
    payload, error, code = adapter._bootstrap_graph(
        {
            "prompt": "按参考图做一个短片",
            "project_id": "proj_refs01",
            "references": [
                {
                    "kind": "image",
                    "filename": "hero.png",
                    "mime_type": "image/png",
                    "path": str(image),
                }
            ],
        },
        "web",
    )
    assert error is None
    assert code is None
    assert payload is not None
    refs = payload["graph"]["metadata"]["user_references"]
    assert refs[0]["kind"] == "image"
    assert Path(refs[0]["path"]).is_file()
    brief = next(node for node in payload["graph"]["nodes"] if node["id"] == "n_brief")
    assert brief["config"]["prompt"] == "按参考图做一个短片"
    assert brief["config"]["user_reference_ids"] == ["ref_01"]


def test_user_reference_video_and_audio_paths(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.designer.user_references import (
        user_reference_audio_path,
        user_reference_video_path,
    )

    video = tmp_path / "motion.mp4"
    audio = tmp_path / "theme.mp3"
    image = tmp_path / "face.png"
    video.write_bytes(b"fake-mp4")
    audio.write_bytes(b"fake-mp3")
    image.write_bytes(_png_bytes())
    refs = normalize_user_references(
        [
            {"kind": "image", "path": str(image), "filename": "face.png", "mime_type": "image/png"},
            {"kind": "video", "path": str(video), "filename": "motion.mp4", "mime_type": "video/mp4"},
            {"kind": "audio", "path": str(audio), "filename": "theme.mp3", "mime_type": "audio/mpeg"},
        ],
        dest_dir=tmp_path / "refs",
    )
    graph = attach_user_references_to_graph({"nodes": [], "metadata": {}}, refs)
    assert user_reference_image_paths(graph)[0].is_file()
    assert user_reference_video_path(graph) is not None
    assert user_reference_video_path(graph).suffix == ".mp4"
    assert user_reference_audio_path(graph) is not None
    assert "video 1 = " in prompt_slot_roster(refs)
    assert "audio 1 = " in prompt_slot_roster(refs)


def test_vision_user_content_keeps_original_image(tmp_path: Path) -> None:
    from jiuwenswarm.server.runtime.designer.model_tools import vision_user_content

    image = tmp_path / "hero.png"
    image.write_bytes(_png_bytes())
    content = vision_user_content(
        "Slots: image 1 = hero.png. Do not summarize the picture.",
        [str(image)],
    )
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "hero.png" in content[0]["text"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_analyze_creative_brief_sends_reference_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.designer import script_analysis as analysis

    image = tmp_path / "hero.png"
    image.write_bytes(_png_bytes())
    seen: dict[str, object] = {}

    async def fake_call_model_tool(**kwargs):
        seen["images"] = kwargs.get("images")
        seen["prompt"] = kwargs.get("prompt")
        return {
            "ok": True,
            "fallback": False,
            "text": (
                '{"characters":[{"id":"char_1","name":"Hero","description":"from image 1"}],'
                '"scenes":[{"id":"scene_1","name":"Street","description":"urban"}],'
                '"shots":[{"shot_index":1,"title":"Walk","action":"walks","camera":"medium",'
                '"character_ids":["char_1"],"keyframe_prompt":"Hero walks","timeline":"0.0-5.0s"}],'
                '"cast_layout":"single","target_shot_count":1,"prefer_combined_cast":false,'
                '"prefer_split_cast":false,'
                '"audio":{"policy":"optional_music","include_speech":false,"include_music":true,"notes":""},'
                '"summary":"hero walks"}'
            ),
        }

    monkeypatch.setattr(analysis, "_llm_configured", lambda: True)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.call_model_tool",
        fake_call_model_tool,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.skills_loader.load_orchestration_skill",
        lambda name: "",
    )
    result = await analysis.analyze_creative_brief(
        "按参考图做一个短片",
        use_llm=True,
        timeout_sec=5.0,
        reference_images=[str(image)],
    )
    assert seen["images"] == [str(image)]
    assert result.get("source") == "llm"
    names = [item.get("name") for item in result.get("characters") or []]
    assert "Hero" in names


@pytest.mark.asyncio
async def test_clip_passes_user_video_as_file_not_first_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.schema.designer_graph import (
        NODE_ROLE_CLIP,
        NODE_ROLE_FRAME,
        NODE_TYPE_IMAGE,
        NODE_TYPE_VIDEO,
    )
    from jiuwenswarm.server.runtime.designer.handlers.clip import (
        ClipNodeHandler,
        build_clip_prompt,
    )
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    frame = tmp_path / "keyframe.png"
    video = tmp_path / "ref.mp4"
    out = tmp_path / "clip.mp4"
    frame.write_bytes(_png_bytes())
    video.write_bytes(b"fake-mp4")
    out.write_bytes(b"out-mp4")
    refs = normalize_user_references(
        [{"kind": "video", "path": str(video), "filename": "ref.mp4", "mime_type": "video/mp4"}],
        dest_dir=tmp_path / "refs",
    )
    graph = {
        "nodes": [
            {
                "id": "n_frame",
                "type": NODE_TYPE_IMAGE,
                "label": "frame",
                "config": {"role": NODE_ROLE_FRAME, "shot_index": 1},
            },
            {
                "id": "n_clip",
                "type": NODE_TYPE_VIDEO,
                "label": "clip",
                "config": {"role": NODE_ROLE_CLIP, "shot_index": 1},
            },
        ],
        "metadata": {},
    }
    graph = attach_user_references_to_graph(graph, refs)
    prompt = build_clip_prompt(graph, graph["nodes"][-1])
    assert "video 1" in prompt
    assert "not the first frame" in prompt.lower() or "NOT the first frame" in prompt
    seen: dict[str, object] = {}

    async def fake_generate(
        prompt: str,
        save_dir: str | None = None,
        first_frame: str | None = None,
        reference_images: list[str] | None = None,
        reference_file: str | None = None,
        duration: int = 5,
        audio: bool | None = None,
    ) -> dict[str, str]:
        seen["first_frame"] = first_frame
        seen["reference_file"] = reference_file
        return {"video_path": str(out), "revised_prompt": prompt}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.generate_clip_video",
        fake_generate,
    )
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_user_video",
        node_id="n_clip",
        run={
            "node_states": {
                "n_frame": {
                    "status": "completed",
                    "output_ref": file_output_ref(
                        frame, kind=NODE_TYPE_IMAGE, mime_type="image/png"
                    ),
                }
            }
        },
    )
    await ClipNodeHandler().execute(graph["nodes"][-1], ctx)
    assert seen["first_frame"] == str(frame.resolve())
    assert seen["reference_file"]
    assert Path(str(seen["reference_file"])).suffix == ".mp4"
    assert Path(str(seen["reference_file"])) != frame.resolve()


@pytest.mark.asyncio
async def test_music_handler_copies_user_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.schema.designer_graph import NODE_TYPE_AUDIO
    from jiuwenswarm.server.runtime.designer.handlers.audio_nodes import MusicNodeHandler
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    audio = tmp_path / "theme.mp3"
    audio.write_bytes(b"user-audio-bytes")
    refs = normalize_user_references(
        [{"kind": "audio", "path": str(audio), "filename": "theme.mp3", "mime_type": "audio/mpeg"}],
        dest_dir=tmp_path / "refs",
    )
    graph = attach_user_references_to_graph(
        {
            "nodes": [{"id": "n_music", "type": NODE_TYPE_AUDIO, "config": {"role": "music"}}],
            "metadata": {},
        },
        refs,
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.audio_nodes.get_agent_workspace_dir",
        lambda: workspace,
    )
    result = await MusicNodeHandler().execute(
        graph["nodes"][0],
        NodeExecutionContext(graph=graph, run_id="run_audio", node_id="n_music", run={}),
    )
    produced = next(path for path in workspace.iterdir() if path.suffix.lower() == ".mp3")
    assert produced.read_bytes() == b"user-audio-bytes"
    assert "user audio" in result.message

