# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.designer.audio_locks import (
    resolve_video_audio_request,
    video_model_supports_native_audio,
)


def test_seedance_supports_native_audio() -> None:
    assert video_model_supports_native_audio("doubao-seedance-2-5-260628")
    assert video_model_supports_native_audio("wan3.0-video")
    assert not video_model_supports_native_audio("wan2.6-t2v")


def test_seedance_does_not_override_to_wan3(monkeypatch) -> None:
    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "doubao-seedance-2-5-260628")
    want, override = resolve_video_audio_request(
        {
            "clip_embedded_audio": True,
            "prefer_wan3_clip_audio": True,
            "speech_line": "hello",
            "include_speech": True,
        },
        {"prefer_wan3_clip_audio": True, "audio_intent": {"include_speech": True}},
    )
    assert want is True
    assert override is None


def test_video_gen_family_label_follows_configured_model(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.audio_locks import video_gen_family_label

    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "doubao-seedance-2-5-260628")
    assert video_gen_family_label() == "Seedance"
    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "wan2.6-i2v")
    assert video_gen_family_label() == "Wan"
    monkeypatch.delenv("VIDEO_GEN_MODEL_NAME", raising=False)
    assert video_gen_family_label("MiniMax-Hailuo-02") == "MiniMax"


def test_image_gen_family_label_follows_configured_model(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.audio_locks import image_gen_family_label

    monkeypatch.setenv("IMAGE_GEN_MODEL_NAME", "doubao-seedream-4-5-251128")
    assert image_gen_family_label() == "Seedream"
    monkeypatch.setenv("IMAGE_GEN_MODEL_NAME", "qwen-image-3.0")
    assert image_gen_family_label() == "Qwen"
    monkeypatch.delenv("IMAGE_GEN_MODEL_NAME", raising=False)
    assert image_gen_family_label("wan2.7-image") == "Wan"


def test_clip_playbook_uses_configured_video_family(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.media_model_playbook import playbook_for_role

    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "doubao-seedance-2-5-260628")
    monkeypatch.setenv("IMAGE_GEN_MODEL_NAME", "doubao-seedream-4-5-251128")
    text = playbook_for_role("clip")
    assert "Seedance" in text
    assert "Qwen-Image 3.0" not in text
    assert "Prefer 480P" not in text
    assert "aspect_lock" in text


def test_wan2_does_not_swap_model_for_dialogue(monkeypatch) -> None:
    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "wan2.6-t2v")
    want, override = resolve_video_audio_request(
        {"clip_embedded_audio": True, "speech_line": "hello"},
        {"prefer_wan3_clip_audio": True},
    )
    assert want is False
    assert override is None


def test_music_node_always_created_when_brief_wants_bgm(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.capabilities.detect_audio_backends",
        lambda: {
            "can_speech": False,
            "can_music": False,
            "can_video_audio": True,
            "video_audio_model": "wan3.0-video",
        },
    )
    graph = build_smart_video_graph(
        project_id="p",
        prompt="情人节办公室短片，结尾烛光晚餐碰杯",
        analysis={
            "source": "llm",
            "audio": {"include_speech": True, "include_music": True, "policy": "speech_and_music"},
            "characters": [{"id": "char_1", "name": "年轻人"}],
            "scenes": [{"id": "scene_1", "name": "office"}],
            "shots": [
                {
                    "shot_index": 1,
                    "action": "自白",
                    "speech_line": "糟了，今天是情人节",
                    "character_ids": ["char_1"],
                }
            ],
        },
        ai_mode=False,
    )
    ids = [str(n.get("id") or "") for n in graph["nodes"]]
    assert "n_music" in ids
    assert "n_speech" not in ids
    clip = next(
        n
        for n in graph["nodes"]
        if str((n.get("config") or {}).get("role") or "") == "clip"
        or str((n.get("config") or {}).get("pipeline") or "") == "clip"
    )
    prompt = str(((clip.get("config") or {}).get("generate") or {}).get("prompt") or "")
    excerpt = str((clip.get("config") or {}).get("skill_excerpt") or "")
    blob = prompt + excerpt
    assert "Do NOT generate music" in blob or "NOT in this clip" in blob or "dialogue only" in blob.lower()


def test_supervisor_keeps_music_node_without_music_backend(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.orchestration import assign_audio_node_agents
    from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph

    backends = {
        "can_speech": False,
        "can_music": False,
        "can_video_audio": True,
        "video_audio_model": "wan3.0-video",
    }
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.capabilities.detect_audio_backends",
        lambda: backends,
    )
    graph = build_smart_video_graph(
        project_id="p",
        prompt="office short film with a warm score",
        analysis={
            "source": "llm",
            "audio": {"include_speech": True, "include_music": True, "policy": "speech_and_music"},
            "characters": [{"id": "char_1", "name": "Lead"}],
            "scenes": [{"id": "scene_1", "name": "office"}],
            "shots": [{"shot_index": 1, "action": "monologue", "character_ids": ["char_1"]}],
        },
        ai_mode=False,
    )
    assign_audio_node_agents(graph)
    ids = [str(n.get("id") or "") for n in graph["nodes"]]
    assert "n_music" in ids
    assert "n_speech" not in ids
    music = next(n for n in graph["nodes"] if str(n.get("id")) == "n_music")
    cfg = music.get("config") or {}
    assert cfg.get("delegate") == "handler"
    assert cfg.get("force_handler") is True


def test_tts_node_is_never_created_even_when_backend_exists(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.orchestration import assign_audio_node_agents
    from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.capabilities.detect_audio_backends",
        lambda: {
            "can_speech": True,
            "can_music": False,
            "can_video_audio": True,
            "video_audio_model": "wan3.0-video",
        },
    )
    graph = build_smart_video_graph(
        project_id="p",
        prompt="A character says hello over a quiet score",
        analysis={
            "source": "llm",
            "audio": {
                "include_speech": True,
                "include_music": True,
                "policy": "speech_and_music",
            },
            "characters": [{"id": "char_1", "name": "Lead"}],
            "scenes": [{"id": "scene_1", "name": "room"}],
            "shots": [
                {
                    "shot_index": 1,
                    "action": "Lead says hello",
                    "speech_line": "Hello",
                    "character_ids": ["char_1"],
                }
            ],
        },
        ai_mode=False,
    )
    assign_audio_node_agents(graph)
    ids = [str(n.get("id") or "") for n in graph["nodes"]]
    assert "n_speech" not in ids
    assert "n_music" in ids
    routing = graph["metadata"]["audio_routing"]
    assert routing["can_speech"] is False
    assert routing["clip_embedded"] is True


def test_silent_placeholder_is_not_mixed():
    from jiuwenswarm.server.runtime.designer.handlers.audio_nodes import (
        SILENT_MUSIC_PLACEHOLDER_TOKEN,
        is_silent_music_placeholder,
    )
    from jiuwenswarm.server.runtime.designer.handlers.compose import (
        _is_silent_music_placeholder,
    )

    stub = Path(f"designer_music_run_n_music_{SILENT_MUSIC_PLACEHOLDER_TOKEN}.m4a")
    assert is_silent_music_placeholder(stub)
    assert _is_silent_music_placeholder(stub)
    assert not is_silent_music_placeholder(Path("theme.mp3"))


@pytest.mark.asyncio
async def test_music_handler_writes_silent_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.schema.designer_graph import NODE_TYPE_AUDIO
    from jiuwenswarm.server.runtime.designer.handlers.audio_nodes import (
        SILENT_MUSIC_PLACEHOLDER_TOKEN,
        MusicNodeHandler,
    )
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.audio_nodes.get_agent_workspace_dir",
        lambda: workspace,
    )

    async def _noop_async(*_a, **_k):
        return None

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.audio_nodes._try_music_api",
        _noop_async,
    )
    result = await MusicNodeHandler().execute(
        {"id": "n_music", "type": NODE_TYPE_AUDIO, "config": {"role": "music", "film_duration_sec": 8}},
        NodeExecutionContext(
            graph={"nodes": [], "metadata": {}},
            run_id="run_ph",
            node_id="n_music",
            run={},
        ),
    )
    produced = next(workspace.iterdir())
    assert SILENT_MUSIC_PLACEHOLDER_TOKEN in produced.name
    assert "placeholder" in result.message
    assert produced.suffix.lower() in {".m4a", ".mp3"}


def test_clip_audio_is_dialogue_only() -> None:
    from jiuwenswarm.server.runtime.designer.audio_locks import (
        audio_lock_prompt_block,
        should_request_video_audio,
    )

    assert should_request_video_audio(
        {"include_music": True, "bgm_lock": {"mood": "warm"}},
        {"audio_intent": {"include_music": True, "include_speech": False}},
    ) is False
    assert should_request_video_audio(
        {"speech_line": "糟了，今天是情人节"},
        {"audio_intent": {"include_speech": True, "include_music": True}},
    ) is True
    block = audio_lock_prompt_block(
        include_speech=True,
        include_music=True,
        speech_line="hello",
        bgm_lock={"mood": "tender romantic"},
        clip_embedded=True,
    )
    assert "Do NOT generate music" in block
    assert "NATIVE DIALOGUE only" in block
    assert "speech + BGM" not in block

