# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_ROLE_FRAME,
    NODE_ROLE_SCENE,
    NODE_ROLE_STORYBOARD,
    NODE_TYPE_IMAGE,
    NODE_TYPE_TABLE,
    NODE_TYPE_TEXT,
    SCHEMA_VERSION,
    normalize_execution_graph,
)
from jiuwenswarm.server.runtime.designer.handlers.image_nodes import (
    CharacterDesignNodeHandler,
    FrameNodeHandler,
    _shot_frame_prompt,
    _strip_markdown_tables,
)
from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
    BriefNodeHandler,
    StoryboardNodeHandler,
    brief_duration_seconds,
    brief_logline,
    brief_story_focus,
    build_storyboard_llm_prompt,
    fallback_brief,
    fallback_storyboard,
    parse_storyboard_shots,
    shot_generate_prompt,
    storyboard_shots_or_default,
)
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext


def _graph():
    return normalize_execution_graph(
        {
            "schema_version": SCHEMA_VERSION,
            "graph_id": "graph_mid01",
            "project_id": "proj_mid01",
            "title": "火车站",
            "description": "火车站晨间短片",
            "source": "manual",
            "nodes": [
                {
                    "id": "n_brief",
                    "type": NODE_TYPE_TEXT,
                    "label": "brief",
                    "config": {"role": NODE_ROLE_BRIEF, "prompt": "火车站晨间短片"},
                },
                {
                    "id": "n_storyboard",
                    "type": NODE_TYPE_TABLE,
                    "label": "storyboard",
                    "config": {"role": NODE_ROLE_STORYBOARD},
                },
                {
                    "id": "n_character",
                    "type": NODE_TYPE_IMAGE,
                    "label": "character",
                    "config": {"role": NODE_ROLE_CHARACTER_DESIGN},
                },
                {
                    "id": "n_frame",
                    "type": NODE_TYPE_IMAGE,
                    "label": "frame",
                    "config": {"role": NODE_ROLE_FRAME},
                },
            ],
            "edges": [],
        }
    )


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.get_agent_workspace_dir",
        lambda: tmp_path,
    )
    return tmp_path


def _png(workspace: Path, name: str) -> Path:
    path = workspace / name
    path.write_bytes(b"png")
    return path


def _frame_run_with_refs(
    workspace: Path,
    *,
    extra_states: dict | None = None,
) -> dict:
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    character = _png(workspace, "character.png")
    scene = _png(workspace, "scene.png")
    states = {
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
    }
    if extra_states:
        states.update(extra_states)
    return {
        "character": character,
        "scene": scene,
        "run": {"node_states": states},
    }


@pytest.mark.asyncio
async def test_brief_and_storyboard_write_markdown(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_text(prompt: str, max_tokens: int = 1200) -> str:
        if "Storyboard" in prompt or "分镜" in prompt or "运镜" in prompt:
            return "## Storyboard\n| Shot | Timeline |\n| 1 | 0.0-2.0s |\n"
        return "# Brief\n火车站短片"

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.complete_designer_text",
        fake_text,
    )

    async def no_image(
        prompt: str,
        size: str = "512x512",
        reference_image: str | None = None,
        reference_images: list[str] | None = None,
    ):
        return None

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.generate_designer_image",
        no_image,
    )
    graph = _graph()
    ctx = NodeExecutionContext(graph=graph, run_id="run_mid01", node_id="n_brief")
    brief = await BriefNodeHandler().execute(graph["nodes"][0], ctx)
    assert brief.output_ref is not None
    assert brief.output_ref["uri"].endswith(".md")
    brief_path = workspace / Path(brief.output_ref["label"])
    assert "火车站" in brief_path.read_text(encoding="utf-8")

    run = {
        "node_states": {
            "n_brief": {"status": "completed", "output_ref": brief.output_ref},
        }
    }
    story = await StoryboardNodeHandler().execute(
        graph["nodes"][1],
        NodeExecutionContext(graph=graph, run_id="run_mid01", node_id="n_storyboard", run=run),
    )
    assert story.output_ref is not None
    text = (workspace / Path(story.output_ref["label"])).read_text(encoding="utf-8")
    assert "Storyboard" in text
    assert "Timeline" in text


@pytest.mark.asyncio
async def test_storyboard_writes_table_and_does_not_generate_image(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    async def fake_text(prompt: str, max_tokens: int = 1200) -> str:
        seen["prompt"] = prompt
        assert "Timeline" in prompt or "Camera" in prompt
        assert "Comment" in prompt
        assert "## Brief" in prompt
        assert "Storyboard requirements" in prompt
        assert "火车站晨间短片" in prompt
        return (
            "## 分镜表\n"
            "| 镜号 | 时间轴 | 镜头视角 | 运镜 | 人物变化 | 场景变化 |\n"
            "| 1 | 0.0-2.0s | 全景/平视 | 缓摇 | 未入画 | 站台 |\n"
        )

    async def fake_image(*_args, **_kwargs):
        raise AssertionError("storyboard must not call image generation")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.complete_designer_text",
        fake_text,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.generate_designer_image",
        fake_image,
    )
    graph = _graph()
    result = await StoryboardNodeHandler().execute(
        graph["nodes"][1],
        NodeExecutionContext(graph=graph, run_id="run_mid01", node_id="n_storyboard", run={}),
    )
    assert result.output_ref is not None
    assert result.output_ref["kind"] == NODE_TYPE_TABLE
    assert result.output_ref["uri"].endswith(".md")
    text = (workspace / Path(result.output_ref["label"])).read_text(encoding="utf-8")
    assert "时间轴" in text
    assert "运镜" in text
    assert "Storyboard requirements" in seen["prompt"]
    assert "## Brief" in seen["prompt"]


def _require_live_designer_chat_model() -> None:
    """Skip when the same chat model Agent uses is not configured."""
    from jiuwenswarm.common.config import get_config, get_default_models
    from jiuwenswarm.common.utils import get_env_file
    from jiuwenswarm.dotenv_early import load_dotenv_runtime

    env_file = get_env_file()
    if env_file.is_file():
        load_dotenv_runtime(env_file, override=True)
    entries = get_default_models(get_config())
    entry = next((item for item in entries if item.get("is_default") is True), None)
    if entry is None and entries:
        entry = entries[0]
    client = (entry or {}).get("model_client_config") if isinstance(entry, dict) else {}
    if not isinstance(client, dict):
        pytest.skip("no chat model configured")
    api_key = str(client.get("api_key") or "").strip()
    model_name = str(client.get("model_name") or "").strip()
    if not api_key or not model_name:
        pytest.skip("Designer chat model is not configured")


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
async def test_storyboard_calls_chat_model_and_follows_brief(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Storyboard must come from the chat model reading the Brief, not a canned table."""
    _require_live_designer_chat_model()
    from jiuwenswarm.server.runtime.designer.handlers import common as handler_io
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    user_prompt = (
        "5 second film, two cameras: a grandmother folds dumplings in a sunlit kitchen "
        "while a kettle steams on the stove."
    )
    real_complete = handler_io.complete_designer_text
    llm_replies: list[str] = []
    llm_prompts: list[str] = []

    async def through_real_model(prompt: str, max_tokens: int = 1200) -> str:
        llm_prompts.append(prompt)
        text = await real_complete(prompt, max_tokens=max_tokens)
        llm_replies.append(text)
        return text

    monkeypatch.setattr(handler_io, "complete_designer_text", through_real_model)

    brief_path = workspace / "brief.md"
    brief_path.write_text(
        "# Brief\n\n"
        f"- Logline: {user_prompt}\n"
        "- Duration: 5 seconds\n",
        encoding="utf-8",
    )
    graph = _graph()
    graph["nodes"][0]["config"]["prompt"] = user_prompt
    graph["title"] = user_prompt[:80]
    graph["description"] = user_prompt
    result = await StoryboardNodeHandler().execute(
        graph["nodes"][1],
        NodeExecutionContext(
            graph=graph,
            run_id="run_llm_storyboard",
            node_id="n_storyboard",
            run={
                "node_states": {
                    "n_brief": {
                        "status": "completed",
                        "output_ref": file_output_ref(
                            brief_path, kind=NODE_TYPE_TEXT, mime_type="text/markdown"
                        ),
                    }
                }
            },
        ),
    )
    assert llm_replies, "Storyboard handler never called the chat model"
    assert llm_prompts, "Storyboard handler never sent a chat-API prompt"
    request = llm_prompts[0]
    assert "## Brief" in request
    assert "Storyboard requirements" in request
    assert "grandmother folds dumplings" in request
    assert "Shot | Timeline | Camera | Move | Character action | Scene change | Comment" in request
    llm_table = parse_storyboard_shots(llm_replies[0])
    assert llm_table, (
        "chat model did not return a storyboard table; first 400 chars: "
        + (llm_replies[0] or "")[:400]
    )
    assert result.output_ref is not None
    written = (workspace / Path(result.output_ref["label"])).read_text(encoding="utf-8")
    shots = parse_storyboard_shots(written)
    assert shots
    joined = " ".join(
        f"{shot['character_action']} {shot['scene_change']} {shot['comment']}"
        for shot in shots
    ).casefold()
    assert any(
        token in joined
        for token in ("dumpling", "grandmother", "kitchen", "kettle", "饺子", "奶奶", "厨房", "水壶")
    ), joined
    assert "neon alley" not in joined


@pytest.mark.asyncio
async def test_storyboard_prompt_aligns_with_character_and_scene(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.schema.designer_graph import NODE_ROLE_SCENE
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    character_notes = workspace / "character.md"
    scene_notes = workspace / "scene.md"
    character_notes.write_text("炭黑机器人，左肩钴蓝核", encoding="utf-8")
    scene_notes.write_text("玻璃天棚站台，晨光", encoding="utf-8")
    seen: dict[str, str] = {}

    async def fake_text(prompt: str, max_tokens: int = 1200) -> str:
        seen["prompt"] = prompt
        return (
            "## 分镜表\n"
            "| 镜号 | 时间轴 | 镜头视角 | 运镜 | 人物变化 | 场景变化 |\n"
            "| 1 | 0.0-2.0s | 全景/平视 | 缓摇 | 机器人入画 | 站台 |\n"
        )

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.complete_designer_text",
        fake_text,
    )
    graph = _graph()
    graph["nodes"].append(
        {
            "id": "n_scene",
            "type": NODE_TYPE_IMAGE,
            "label": "scene",
            "config": {"role": NODE_ROLE_SCENE},
        }
    )
    await StoryboardNodeHandler().execute(
        graph["nodes"][1],
        NodeExecutionContext(
            graph=graph,
            run_id="run_mid01",
            node_id="n_storyboard",
            run={
                "node_states": {
                    "n_character": {
                        "status": "completed",
                        "output_ref": file_output_ref(
                            character_notes, kind=NODE_TYPE_TEXT, mime_type="text/markdown"
                        ),
                    },
                    "n_scene": {
                        "status": "completed",
                        "output_ref": file_output_ref(
                            scene_notes, kind=NODE_TYPE_TEXT, mime_type="text/markdown"
                        ),
                    },
                }
            },
        ),
    )
    prompt = seen["prompt"]
    assert "炭黑机器人" in prompt
    assert "站台" in prompt
    assert "character action must match" in prompt
    assert "scene change must match" in prompt
    assert "keyframe prompt" in prompt
    assert "## Brief" in prompt
    assert "Storyboard requirements" in prompt


def test_build_storyboard_llm_prompt_includes_brief_and_requirements() -> None:
    brief = (
        "# Brief\n\n"
        "- Logline: a grandmother folds dumplings in a sunlit kitchen\n"
        "- Duration: 5 seconds\n"
    )
    prompt = build_storyboard_llm_prompt(brief)
    assert prompt.index("Storyboard requirements") < prompt.index("## Brief")
    assert brief in prompt
    assert "grandmother folds dumplings" in prompt
    assert "Shot | Timeline | Camera | Move | Character action | Scene change | Comment" in prompt
    assert "2-6 shots" in prompt
    assert "do not assume 5 seconds" in prompt.lower()
    assert "5-second camera-script" not in prompt
    assert "Whole film about 5 seconds" not in prompt
    assert "keyframe prompt" in prompt
    assert "Write the storyboard table now from the Brief" in prompt
    with_request = build_storyboard_llm_prompt(
        brief,
        user_request="Generate a 10-second video in a medieval classical style",
    )
    assert "## User request" in with_request
    assert "10-second video" in with_request


def test_brief_duration_seconds_reads_user_request() -> None:
    assert brief_duration_seconds("Generate a 10-second video in a medieval style") == 10
    assert brief_duration_seconds("生成一段10秒的短视频，地铁进站") == 10
    assert brief_duration_seconds("**Duration:** 10 seconds.") == 10
    assert (
        brief_duration_seconds(
            "# Brief\n\n- Logline: a 10-second charge\n- Duration: 8 seconds\n"
        )
        == 8
    )
    assert brief_duration_seconds("generate a 480p video in 5 seconds, two cams") == 5
    assert brief_duration_seconds("no length stated") == 5


def test_fallback_brief_and_storyboard_follow_requested_duration() -> None:
    prompt = "Generate a 10-second video: a troop charges a red castle"
    brief = fallback_brief(prompt)
    assert "- Duration: 10 seconds" in brief
    shots = parse_storyboard_shots(fallback_storyboard(brief))
    assert shots[0]["timeline"] == "0.0-4.0s"
    assert shots[1]["timeline"] == "4.0-10.0s"


@pytest.mark.asyncio
async def test_character_falls_back_to_notes_when_image_missing(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_image(
        prompt: str,
        size: str = "512x512",
        reference_image: str | None = None,
        reference_images: list[str] | None = None,
    ):
        return None

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.generate_designer_image",
        no_image,
    )
    graph = _graph()
    result = await CharacterDesignNodeHandler().execute(
        graph["nodes"][2],
        NodeExecutionContext(graph=graph, run_id="run_mid01", node_id="n_character"),
    )
    assert result.output_ref is not None
    assert result.output_ref["uri"].endswith(".md")
    assert "Character" in (workspace / Path(result.output_ref["label"])).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_frame_uses_image_when_available(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = workspace / "keyframe.png"
    image.write_bytes(b"png")

    async def fake_image(
        prompt: str,
        size: str = "512x512",
        reference_image: str | None = None,
        reference_images: list[str] | None = None,
    ):
        assert "关键帧" in prompt or "短片" in prompt or "火车站" in prompt
        return {"image_path": str(image)}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.generate_designer_image",
        fake_image,
    )
    graph = _graph()
    graph["nodes"].append(
        {
            "id": "n_scene",
            "type": NODE_TYPE_IMAGE,
            "label": "scene",
            "config": {"role": NODE_ROLE_SCENE},
        }
    )
    refs = _frame_run_with_refs(workspace)
    result = await FrameNodeHandler().execute(
        graph["nodes"][3],
        NodeExecutionContext(
            graph=graph,
            run_id="run_mid01",
            node_id="n_frame",
            run=refs["run"],
        ),
    )
    assert result.output_ref is not None
    assert result.output_ref["kind"] == NODE_TYPE_IMAGE
    assert result.output_ref["label"].endswith(".png")
    assert len(result.output_refs or []) == 1


@pytest.mark.asyncio
async def test_frame_passes_character_and_scene_as_img2img_references(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.common.schema.designer_graph import NODE_ROLE_SCENE
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    character = workspace / "character.png"
    scene = workspace / "scene.png"
    character.write_bytes(b"png-character")
    scene.write_bytes(b"png-scene")
    keyframe = workspace / "keyframe.png"
    keyframe.write_bytes(b"png-frame")
    seen: dict[str, object] = {}

    async def fake_image(
        prompt: str,
        size: str = "512x512",
        reference_image: str | None = None,
        reference_images: list[str] | None = None,
    ):
        seen["reference_images"] = reference_images
        assert "image-to-image" in prompt
        return {"image_path": str(keyframe)}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.generate_designer_image",
        fake_image,
    )
    graph = _graph()
    graph["nodes"].append(
        {
            "id": "n_scene",
            "type": NODE_TYPE_IMAGE,
            "label": "scene",
            "config": {"role": NODE_ROLE_SCENE},
        }
    )
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_mid01",
        node_id="n_frame",
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
            }
        },
    )
    result = await FrameNodeHandler().execute(graph["nodes"][3], ctx)
    assert result.output_ref is not None
    assert seen["reference_images"] == [str(character.resolve()), str(scene.resolve())]


@pytest.mark.asyncio
async def test_frame_uses_node_generate_prompt_as_shot_comment(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    async def fake_image(
        prompt: str,
        size: str = "512x512",
        reference_image: str | None = None,
        reference_images: list[str] | None = None,
    ):
        seen["prompt"] = prompt
        path = workspace / "keyframe.png"
        path.write_bytes(b"png")
        return {"image_path": str(path)}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.generate_designer_image",
        fake_image,
    )
    graph = _graph()
    graph["nodes"].append(
        {
            "id": "n_scene",
            "type": NODE_TYPE_IMAGE,
            "label": "scene",
            "config": {"role": NODE_ROLE_SCENE},
        }
    )
    graph["nodes"][3]["config"]["generate"] = {
        "prompt": "中景平视，白领推开地铁门",
        "prompt_origin": "user",
    }
    story = workspace / "storyboard.md"
    story.write_text(
        "## 分镜表\n"
        "| 镜号 | 时间轴 | 镜头视角 | 运镜 | 人物变化 | 场景变化 | 注释 |\n"
        "| 1 | 0.0-5.0s | 中景 | 固定 | 入画 | 站台 | 分镜表里的旧注释 |\n",
        encoding="utf-8",
    )
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    refs = _frame_run_with_refs(
        workspace,
        extra_states={
            "n_storyboard": {
                "status": "completed",
                "output_ref": file_output_ref(
                    story, kind=NODE_TYPE_TABLE, mime_type="text/markdown"
                ),
            },
        },
    )
    await FrameNodeHandler().execute(
        graph["nodes"][3],
        NodeExecutionContext(
            graph=graph,
            run_id="run_mid01",
            node_id="n_frame",
            run=refs["run"],
        ),
    )
    assert "Generate the keyframe from this shot description: 中景平视，白领推开地铁门" in seen["prompt"]
    assert "分镜表里的旧注释" not in seen["prompt"]


@pytest.mark.asyncio
async def test_frame_storyboard_origin_prompt_keeps_live_table_comment(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    async def fake_image(
        prompt: str,
        size: str = "512x512",
        reference_image: str | None = None,
        reference_images: list[str] | None = None,
    ):
        seen["prompt"] = prompt
        path = workspace / "keyframe.png"
        path.write_bytes(b"png")
        return {"image_path": str(path)}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.generate_designer_image",
        fake_image,
    )
    graph = _graph()
    graph["nodes"][3]["config"]["generate"] = {
        "prompt": "过期的分镜注释",
        "prompt_origin": "storyboard",
    }
    story = workspace / "storyboard.md"
    story.write_text(
        "## Storyboard\n"
        "| Shot | Timeline | Camera | Move | Character action | Scene change | Comment |\n"
        "| 1 | 0.0-5.0s | medium | static | steps off | platform | 用户刚改过的分镜画面 |\n",
        encoding="utf-8",
    )
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    refs = _frame_run_with_refs(
        workspace,
        extra_states={
            "n_storyboard": {
                "status": "completed",
                "output_ref": file_output_ref(
                    story, kind=NODE_TYPE_TABLE, mime_type="text/markdown"
                ),
            },
        },
    )
    await FrameNodeHandler().execute(
        graph["nodes"][3],
        NodeExecutionContext(
            graph=graph,
            run_id="run_mid01",
            node_id="n_frame",
            run=refs["run"],
        ),
    )
    assert "用户刚改过的分镜画面" in seen["prompt"]
    assert "过期的分镜注释" not in seen["prompt"]


def test_shot_frame_prompt_uses_shot_fields_not_markdown_table() -> None:
    brief = (
        "# Brief\n\n火车站晨间。\n\n"
        "| 镜号 | 时间轴 | 镜头视角 |\n"
        "| --- | --- | --- |\n"
        "| 1 | 0.0-2.0s | 全景 |\n"
    )
    prompt = _shot_frame_prompt(
        {
            "shot_no": "1",
            "timeline": "0.0-2.0s",
            "camera": "全景/平视",
            "move": "缓摇",
            "character_action": "未入画",
            "scene_change": "站台",
        },
        brief,
        has_character=False,
        has_scene=False,
    )
    assert "Shot notes" in prompt
    assert "character action 未入画" in prompt
    assert "scene change 站台" in prompt
    assert "| 镜号 |" not in prompt
    assert "Do not paint words" in prompt
    assert _strip_markdown_tables(brief) == "# Brief\n\n火车站晨间。"


def test_parse_storyboard_shots_reads_fenced_markdown_table() -> None:
    text = (
        "Sure.\n"
        "```markdown\n"
        "## Storyboard\n"
        "| Shot | Timeline | Camera | Move | Character action | Scene change | Comment |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| 1 | 0.0-2.0s | wide | pan | train arrives | station | Wide shot of the train |\n"
        "```\n"
    )
    shots = parse_storyboard_shots(text)
    assert shots[0]["comment"] == "Wide shot of the train"
    assert "train" in shots[0]["character_action"]


def test_parse_storyboard_shots_reads_table_rows() -> None:
    text = (
        "## 分镜表\n\n"
        "| 镜号 | 时间轴 | 镜头视角 | 运镜 | 人物变化 | 场景变化 |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| 1 | 0.0-2.0s | 全景/平视 | 缓摇 | 未入画 | 站台 |\n"
        "| 2 | 2.0-5.0s | 中景/平视 | 跟移 | 主体入画 | 出站 |\n"
    )
    shots = parse_storyboard_shots(text)
    assert [shot["shot_no"] for shot in shots] == ["1", "2"]
    assert shots[0]["timeline"] == "0.0-2.0s"
    assert shots[1]["character_action"] == "主体入画"
    assert shots[0]["comment"] == ""
    assert shots[1]["comment"] == ""


def test_storyboard_shots_or_default_falls_back() -> None:
    shots = storyboard_shots_or_default("没有表格", "雨夜")
    assert len(shots) == 2
    assert shots[0]["comment"]
    assert shots[1]["comment"]
    assert "雨夜" in shots[0]["comment"]
    assert "neon" not in shots[0]["comment"].lower()
    assert "puddle" not in shots[0]["comment"].lower()


def test_fallback_storyboard_follows_brief_not_stock_alley() -> None:
    brief = (
        "# Brief\n\n"
        "- Logline: a train arrives at the station and a young man walks out to work\n"
        "- Duration: 5 seconds\n"
    )
    text = fallback_storyboard(brief)
    shots = parse_storyboard_shots(text)
    joined = " ".join(
        f"{shot['character_action']} {shot['scene_change']} {shot['comment']}" for shot in shots
    )
    assert brief_logline(brief).startswith("a train arrives")
    assert "train" in joined
    assert "young man" in joined or "work" in joined
    assert "neon alley" not in joined
    assert "\n" not in shots[0]["scene_change"]


def test_fallback_storyboard_uses_brief_action_not_generate_boilerplate() -> None:
    from jiuwenswarm.server.runtime.designer.handlers.text_nodes import fallback_brief

    prompt = (
        "generate a 480p video in 5 seconds, at least two cams, "
        "a train arrive at center station and a young man walk out of the train "
        "and ready for a new day's work"
    )
    focus = brief_story_focus(prompt)
    assert "train" in focus
    assert "480p" not in focus.lower()
    brief = fallback_brief(prompt)
    text = fallback_storyboard(brief)
    shots = parse_storyboard_shots(text)
    joined = " ".join(
        f"{shot['character_action']} {shot['scene_change']} {shot['comment']}" for shot in shots
    )
    assert "train" in joined
    assert "young man" in joined
    assert "generate a 480p" not in joined.lower()
    assert shots[0]["character_action"]
    assert "train" in shots[0]["character_action"] or "train" in shots[0]["comment"]


def test_parse_storyboard_shots_reads_comment_column() -> None:
    text = (
        "## 分镜表\n\n"
        "| 镜号 | 时间轴 | 镜头视角 | 运镜 | 人物变化 | 场景变化 | 注释 |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| 1 | 0.0-2.0s | 全景/平视 | 缓摇 | 未入画 | 站台 | 火车进站，全景平视，主体尚未入画 |\n"
        "| 2 | 2.0-5.0s | 中景/平视 | 跟移 | 主体入画 | 出站 | 中景平视，年轻人从车门走出 |\n"
    )
    shots = parse_storyboard_shots(text)
    assert shots[0]["comment"] == "火车进站，全景平视，主体尚未入画"
    assert shots[1]["comment"] == "中景平视，年轻人从车门走出"


def test_shot_frame_prompt_uses_comment_as_generation_prompt() -> None:
    prompt = _shot_frame_prompt(
        {
            "shot_no": "2",
            "timeline": "2.0-5.0s",
            "camera": "中景/平视",
            "move": "跟移",
            "character_action": "主体入画",
            "scene_change": "出站",
            "comment": "中景平视，年轻人从车门走出",
        },
        "火车站",
        has_character=True,
        has_scene=True,
    )
    assert "Generate the keyframe from this shot description: 中景平视，年轻人从车门走出" in prompt
    assert "character action 主体入画" in prompt
    assert "This shot must use a different camera" in prompt


@pytest.mark.asyncio
async def test_frame_sends_previous_keyframe_image_with_shot_prompt(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = workspace / "prev.png"
    previous.write_bytes(b"png-prev")
    keyframe = workspace / "keyframe.png"
    keyframe.write_bytes(b"png-frame")
    seen: dict[str, object] = {}

    async def fake_image(
        prompt: str,
        size: str = "512x512",
        reference_image: str | None = None,
        reference_images: list[str] | None = None,
    ):
        seen["prompt"] = prompt
        seen["reference_images"] = reference_images
        return {"image_path": str(keyframe)}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.generate_designer_image",
        fake_image,
    )
    story = workspace / "storyboard.md"
    story.write_text(
        "## Storyboard\n"
        "| Shot | Timeline | Camera | Move | Character action | Scene change | Comment |\n"
        "| 1 | 0.0-2.0s | wide | static | steps off | platform | Wide shot of the train |\n"
        "| 2 | 2.0-5.0s | medium | pan | walks forward | concourse | Medium shot walking to the exit |\n",
        encoding="utf-8",
    )
    graph = _graph()
    graph["nodes"][3]["id"] = "n_frame_2"
    graph["nodes"][3]["config"]["shot_index"] = 2
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    refs = _frame_run_with_refs(
        workspace,
        extra_states={
            "n_storyboard": {
                "status": "completed",
                "output_ref": file_output_ref(
                    story, kind=NODE_TYPE_TABLE, mime_type="text/markdown"
                ),
            },
            "n_frame_1": {
                "status": "completed",
                "output_ref": file_output_ref(
                    previous, kind=NODE_TYPE_IMAGE, mime_type="image/png"
                ),
            },
        },
    )
    await FrameNodeHandler().execute(
        graph["nodes"][3],
        NodeExecutionContext(
            graph=graph,
            run_id="run_mid01",
            node_id="n_frame_2",
            run=refs["run"],
        ),
    )
    assert seen["reference_images"] in ([], None)
    prompt = str(seen["prompt"])
    assert "Medium shot walking to the exit" in prompt
    assert "later shot generated from text only" in prompt
    assert "first image is the previous keyframe" not in prompt
    assert "Wide shot of the train" not in prompt


def test_shot_generate_prompt_prefers_comment_then_row_fields() -> None:
    with_comment = {
        "shot_no": "1",
        "timeline": "0.0-2.0s",
        "camera": "全景/略俯",
        "move": "缓摇",
        "character_action": "未入画",
        "scene_change": "站台",
        "comment": "火车进站的全景",
    }
    without_comment = {**with_comment, "comment": ""}
    assert shot_generate_prompt(with_comment) == "火车进站的全景"
    assert "Camera 全景/略俯" in shot_generate_prompt(without_comment)
    assert "Character action 未入画" in shot_generate_prompt(without_comment)


@pytest.mark.asyncio
async def test_frame_generates_one_image_per_storyboard_shot(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    story = workspace / "storyboard.md"
    story.write_text(
        "## 分镜表\n\n"
        "| 镜号 | 时间轴 | 镜头视角 | 运镜 | 人物变化 | 场景变化 |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| 1 | 0.0-1.5s | 全景/平视 | 缓摇 | 未入画 | 站台 |\n"
        "| 2 | 1.5-3.5s | 中景/平视 | 跟移 | 主体入画 | 出站 |\n"
        "| 3 | 3.5-5.0s | 近景/平视 | 固定 | 转身 | 月台 |\n",
        encoding="utf-8",
    )
    prompts: list[str] = []
    seen_refs: list[list[str] | None] = []

    async def fake_image(
        prompt: str,
        size: str = "512x512",
        reference_image: str | None = None,
        reference_images: list[str] | None = None,
    ):
        prompts.append(prompt)
        seen_refs.append(reference_images)
        path = workspace / f"keyframe_{len(prompts)}.png"
        path.write_bytes(b"png")
        return {"image_path": str(path)}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.generate_designer_image",
        fake_image,
    )
    graph = _graph()
    graph["nodes"].append(
        {
            "id": "n_scene",
            "type": NODE_TYPE_IMAGE,
            "label": "scene",
            "config": {"role": NODE_ROLE_SCENE},
        }
    )
    from jiuwenswarm.server.runtime.designer.handlers.common import file_output_ref

    refs = _frame_run_with_refs(
        workspace,
        extra_states={
            "n_storyboard": {
                "status": "completed",
                "output_ref": file_output_ref(
                    story, kind=NODE_TYPE_TABLE, mime_type="text/markdown"
                ),
            }
        },
    )
    ctx = NodeExecutionContext(
        graph=graph,
        run_id="run_mid01",
        node_id="n_frame",
        run=refs["run"],
    )
    result = await FrameNodeHandler().execute(graph["nodes"][3], ctx)
    assert len(prompts) == 1
    assert "shot 1" in prompts[0]
    assert "Shot notes" in prompts[0]
    assert "character action 未入画" in prompts[0]
    expected_refs = [str(refs["character"].resolve()), str(refs["scene"].resolve())]
    assert seen_refs[0] == expected_refs
    assert "| 镜号 |" not in prompts[0]
    assert prompts[0].count("|") == 0
    assert result.output_refs is not None
    assert len(result.output_refs) == 1
    assert result.output_refs[0]["label"] == "designer_frame_run_mid01_n_frame_shot1.png"

    frame1 = workspace / "keyframe_1.png"
    graph["nodes"][3]["id"] = "n_frame_2"
    graph["nodes"][3]["config"]["shot_index"] = 2
    refs["run"]["node_states"]["n_frame_1"] = {
        "status": "completed",
        "output_ref": file_output_ref(frame1, kind=NODE_TYPE_IMAGE, mime_type="image/png"),
    }
    second = await FrameNodeHandler().execute(graph["nodes"][3], ctx)
    assert len(prompts) == 2
    assert "shot 2" in prompts[1]
    assert "character action 主体入画" in prompts[1]
    assert seen_refs[1] in ([], None)
    assert "later shot generated from text only" in prompts[1]
    assert "first image is the previous keyframe" not in prompts[1]
    assert second.output_refs is not None
    assert second.output_refs[0]["label"] == "designer_frame_run_mid01_n_frame_shot2.png"


@pytest.mark.asyncio
async def test_frame_requires_character_and_scene_images(workspace: Path) -> None:
    graph = _graph()
    with pytest.raises(RuntimeError, match="must send Character"):
        await FrameNodeHandler().execute(
            graph["nodes"][3],
            NodeExecutionContext(graph=graph, run_id="run_mid01", node_id="n_frame"),
        )
    graph["nodes"].append(
        {
            "id": "n_scene",
            "type": NODE_TYPE_IMAGE,
            "label": "scene",
            "config": {"role": NODE_ROLE_SCENE},
        }
    )
    with pytest.raises(RuntimeError, match="Character and Scene"):
        await FrameNodeHandler().execute(
            graph["nodes"][3],
            NodeExecutionContext(graph=graph, run_id="run_mid01", node_id="n_frame"),
        )


@pytest.mark.asyncio
async def test_complete_designer_text_builds_model_request_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.common import complete_designer_text

    captured: dict[str, object] = {}

    class FakeModel:
        def __init__(self, **kwargs):
            captured["model_init"] = kwargs

        async def invoke(self, **kwargs):
            class Response:
                content = "ok"

            return Response()

    class FakeClientConfig:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

    class FakeRequestConfig:
        def __init__(self, **kwargs):
            captured["request"] = kwargs

    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: {})
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_default_models",
        lambda _cfg: [
            {
                "is_default": True,
                "model_client_config": {
                    "api_key": "sk-test",
                    "api_base": "https://example.invalid",
                    "model_name": "test-model",
                    "client_provider": "OpenAI",
                },
            }
        ],
    )
    monkeypatch.setattr("openjiuwen.core.foundation.llm.Model", FakeModel)
    monkeypatch.setattr("openjiuwen.core.foundation.llm.ModelClientConfig", FakeClientConfig)
    monkeypatch.setattr("openjiuwen.core.foundation.llm.ModelRequestConfig", FakeRequestConfig)

    text = await complete_designer_text("Write a brief")
    assert text == "ok"
    request = captured["request"]
    assert isinstance(request, dict)
    assert request.get("model") == "test-model" or request.get("model_name") == "test-model"
    assert request.get("max_tokens") == 1200
    reasoning = request.get("reasoning")
    if isinstance(reasoning, dict):
        assert reasoning.get("mode") == "disabled"
    model_init = captured["model_init"]
    assert isinstance(model_init, dict)
    assert model_init.get("model_config") is not None


@pytest.mark.asyncio
async def test_complete_designer_text_retries_empty_then_reads_reasoning_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.common import complete_designer_text

    calls = {"n": 0}

    class EmptyThenReasoning:
        def __init__(self, **kwargs):
            pass

        async def invoke(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                class Empty:
                    content = ""
                return Empty()
            class Reasoning:
                content = ""
                reasoning_content = "| Shot | Timeline |\n| 1 | 0.0-2.0s |"
            return Reasoning()

    class FakeConfig:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: {})
    monkeypatch.setattr(
        "jiuwenswarm.common.config.get_default_models",
        lambda _cfg: [
            {
                "is_default": True,
                "model_client_config": {
                    "api_key": "sk-test",
                    "api_base": "https://example.invalid",
                    "model_name": "test-model",
                    "client_provider": "OpenAI",
                },
            }
        ],
    )
    monkeypatch.setattr("openjiuwen.core.foundation.llm.Model", EmptyThenReasoning)
    monkeypatch.setattr("openjiuwen.core.foundation.llm.ModelClientConfig", FakeConfig)
    monkeypatch.setattr("openjiuwen.core.foundation.llm.ModelRequestConfig", FakeConfig)

    text = await complete_designer_text("Write a storyboard")
    assert calls["n"] == 2
    assert "Timeline" in text
