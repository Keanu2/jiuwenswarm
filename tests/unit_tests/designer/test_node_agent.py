# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Node Agent host, template loading, and agent-owned graph scheduler."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from jiuwenswarm.agents.swarm.agent_group import load_agent_group_package
from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_STATUS_COMPLETED,
    NODE_STATUS_PENDING,
    NODE_TYPE_TEXT,
    ROLE_DEFAULT_TEMPLATES,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_COMPLETED,
    DesignerGraphNode,
    build_bootstrap_graph,
    node_agent_template,
    node_delegate,
    normalize_node,
)
from jiuwenswarm.server.runtime.designer.executor import GraphExecutor
from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore
from jiuwenswarm.server.runtime.designer.handlers.types import NodeResult
from jiuwenswarm.server.runtime.designer.node_agent import (
    DesignerGraphToolkit,
    designer_agent_group_dir,
    flatten_template_prompt,
    load_node_agent_template,
    parse_agent_template_ref,
)


@pytest.fixture()
def designer_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DesignerGraphStore:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.graph_store.get_agent_root_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.get_agent_workspace_dir",
        lambda: tmp_path,
    )

    async def fake_image(prompt: str, **kwargs) -> dict[str, str]:
        path = tmp_path / "agent_stub.png"
        path.write_bytes(b"png")
        return {"image_path": str(path)}

    async def fake_video(prompt: str, **kwargs) -> dict[str, str]:
        path = tmp_path / "agent_stub.mp4"
        path.write_bytes(b"mp4" * 200)
        return {"video_path": str(path), "revised_prompt": prompt}

    def fake_concat(paths: list[Path], dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"mp4-merged" * 80)
        return dest.resolve()

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.generate_designer_image",
        fake_image,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.generate_clip_video",
        fake_video,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.compose.concatenate_clip_videos",
        fake_concat,
    )
    return DesignerGraphStore()


async def _complete_with_dummy_media(
    tmp_path: Path,
    node: DesignerGraphNode,
    toolkit: DesignerGraphToolkit,
) -> NodeResult:
    node_type = str(node.get("type") or "text")
    if node_type in {"image", "video", "audio"}:
        suffix = { "image": ".png", "video": ".mp4", "audio": ".m4a" }[node_type]
        mime = {
            "image": "image/png",
            "video": "video/mp4",
            "audio": "audio/mp4",
        }[node_type]
        path = tmp_path / f"{node['id']}{suffix}"
        path.write_bytes(b"x" * 16)
        await toolkit.node_complete(
            uri=path.resolve().as_uri(),
            kind=node_type,
            mime_type=mime,
        )
    else:
        await toolkit.node_complete(text=f"done {node['id']}")
    assert toolkit.completed is not None
    return toolkit.completed


async def _await_executor_task(executor: GraphExecutor, run_id: str) -> None:
    task = executor._tasks.get(run_id)
    if task is not None:
        await task


def test_parse_agent_template_ref() -> None:
    assert parse_agent_template_ref("designer/leader") == ("designer", "leader")
    assert parse_agent_template_ref("my_template") == (None, "my_template")


def test_node_agent_template_uses_role_default() -> None:
    node = normalize_node(
        {
            "id": "n_brief",
            "type": NODE_TYPE_TEXT,
            "label": "brief",
            "config": {"role": NODE_ROLE_BRIEF},
        }
    )
    assert node_delegate(node) == "agent"
    assert node_agent_template(node) == ROLE_DEFAULT_TEMPLATES[NODE_ROLE_BRIEF]
    explicit = normalize_node(
        {
            "id": "n_brief",
            "type": NODE_TYPE_TEXT,
            "label": "brief",
            "config": {
                "role": NODE_ROLE_BRIEF,
                "agent_template": "custom/leader",
            },
        }
    )
    assert node_agent_template(explicit) == "custom/leader"


def test_builtin_designer_group_has_six_members() -> None:
    templates = load_agent_group_package(designer_agent_group_dir())
    assert set(templates) == {
        "leader",
        "character",
        "scene",
        "storyboard",
        "frame",
        "clip",
    }
    prompt = flatten_template_prompt(templates["leader"])
    assert "designer_node_run" in prompt
    assert "designer_node_complete" in prompt


def test_load_node_agent_template_resolves_designer_leader() -> None:
    node: DesignerGraphNode = {
        "id": "n_brief",
        "type": NODE_TYPE_TEXT,
        "label": "brief",
        "config": {"role": NODE_ROLE_BRIEF},
    }
    template = load_node_agent_template(node)
    assert template is not None
    assert template.agent_card.id == "leader"
    character: DesignerGraphNode = {
        "id": "n_character",
        "type": "image",
        "label": "角色",
        "config": {"role": NODE_ROLE_CHARACTER_DESIGN},
    }
    loaded = load_node_agent_template(character)
    assert loaded is not None
    assert loaded.agent_card.id == "character"


@pytest.mark.asyncio
async def test_agent_scheduler_starts_only_ready_root(
    designer_store: DesignerGraphStore,
    tmp_path: Path,
) -> None:
    started: list[str] = []

    async def runner(node, ctx, toolkit: DesignerGraphToolkit) -> NodeResult:
        if not started:
            assert node["id"] == "n_brief"
        started.append(node["id"])
        return await _complete_with_dummy_media(tmp_path, node, toolkit)

    graph = designer_store.save_graph(
        build_bootstrap_graph(project_id="proj_agent_root", prompt="only root"),
    )
    executor = GraphExecutor(designer_store, runner=runner)
    run = executor.create_run(graph)
    await executor.start_run(run["run_id"])
    await _await_executor_task(executor, run["run_id"])
    finished = designer_store.get_run(run["run_id"])
    assert finished is not None
    assert finished["status"] == RUN_STATUS_COMPLETED
    assert started[0] == "n_brief"
    assert finished["node_states"]["n_brief"]["status"] == NODE_STATUS_COMPLETED
    assert "n_character" in started


@pytest.mark.asyncio
async def test_agent_node_run_starts_companion(
    designer_store: DesignerGraphStore,
    tmp_path: Path,
) -> None:
    started: list[str] = []

    async def runner(node, ctx, toolkit: DesignerGraphToolkit) -> NodeResult:
        started.append(node["id"])
        if node["id"] == "n_brief":
            await toolkit.node_run("n_character")
        return await _complete_with_dummy_media(tmp_path, node, toolkit)

    graph = designer_store.save_graph(
        build_bootstrap_graph(project_id="proj_agent_run", prompt="pull companion"),
    )
    executor = GraphExecutor(designer_store, runner=runner)
    run = executor.create_run(graph)
    await executor.start_run(run["run_id"])
    await _await_executor_task(executor, run["run_id"])
    finished = designer_store.get_run(run["run_id"])
    assert finished is not None
    assert finished["status"] == RUN_STATUS_COMPLETED
    assert started[:2] == ["n_brief", "n_character"]
    assert finished["node_states"]["n_character"]["status"] == NODE_STATUS_COMPLETED
    assert finished["node_states"]["n_storyboard"]["status"] == NODE_STATUS_COMPLETED


@pytest.mark.asyncio
async def test_agent_patch_then_run_new_node(
    designer_store: DesignerGraphStore,
    tmp_path: Path,
) -> None:
    started: list[str] = []

    async def runner(node, ctx, toolkit: DesignerGraphToolkit) -> NodeResult:
        started.append(node["id"])
        if node["id"] == "n_brief":
            toolkit.graph_patch(
                {
                    "upsert_nodes": [
                        {
                            "id": "n_extra",
                            "type": NODE_TYPE_TEXT,
                            "label": "备注",
                            "config": {"role": NODE_ROLE_BRIEF, "prompt": "extra"},
                        }
                    ]
                }
            )
            await toolkit.node_run("n_extra")
        return await _complete_with_dummy_media(tmp_path, node, toolkit)

    graph = designer_store.save_graph(
        build_bootstrap_graph(project_id="proj_agent_patch", prompt="patch then run"),
    )
    executor = GraphExecutor(designer_store, runner=runner)
    run = executor.create_run(graph)
    await executor.start_run(run["run_id"])
    await _await_executor_task(executor, run["run_id"])
    finished = designer_store.get_run(run["run_id"])
    assert finished is not None
    assert finished["status"] == RUN_STATUS_COMPLETED
    assert "n_extra" in started
    patched = designer_store.get_graph(graph["graph_id"])
    assert patched is not None
    assert any(node["id"] == "n_extra" for node in patched["nodes"])
    assert finished["node_states"]["n_extra"]["status"] == NODE_STATUS_COMPLETED


@pytest.mark.asyncio
async def test_cancel_stops_node_agent_host(
    designer_store: DesignerGraphStore,
) -> None:
    started = asyncio.Event()

    async def runner(node, ctx, toolkit: DesignerGraphToolkit) -> NodeResult:
        started.set()
        await asyncio.sleep(30)
        toolkit.node_complete(text="late")
        assert toolkit.completed is not None
        return toolkit.completed

    graph = designer_store.save_graph(
        build_bootstrap_graph(project_id="proj_agent_cancel", prompt="cancel me"),
    )
    executor = GraphExecutor(designer_store, runner=runner)
    run = executor.create_run(graph)
    await executor.start_run(run["run_id"])
    await asyncio.wait_for(started.wait(), timeout=2)
    cancelled = executor.cancel_run(run["run_id"])
    assert cancelled["status"] == RUN_STATUS_CANCELLED
    assert executor._host._agents == {}
    task = executor._tasks.get(run["run_id"])
    if task is not None:
        with pytest.raises(asyncio.CancelledError):
            await task
        return
    workers = executor._node_workers.get(run["run_id"]) or {}
    assert not workers


@pytest.mark.asyncio
async def test_completed_output_survives_agent_timeout(
    designer_store: DesignerGraphStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.executor._node_execute_timeout_sec",
        lambda node: 0.35 if node.get("id") == "n_brief" else 20.0,
    )

    async def runner(node, ctx, toolkit: DesignerGraphToolkit) -> NodeResult:
        await toolkit.node_complete(text=f"done {node['id']}")
        if node["id"] == "n_brief":
            await asyncio.sleep(5)
        assert toolkit.completed is not None
        return toolkit.completed

    graph = designer_store.save_graph(
        build_bootstrap_graph(project_id="proj_timeout_keep", prompt="keep completed brief"),
    )
    executor = GraphExecutor(designer_store, runner=runner)
    run = executor.create_run(graph)
    await executor.start_run(run["run_id"])
    await _await_executor_task(executor, run["run_id"])
    finished = designer_store.get_run(run["run_id"])
    assert finished is not None
    assert finished["node_states"]["n_brief"]["status"] == NODE_STATUS_COMPLETED
    assert finished["node_states"]["n_brief"].get("output_ref")


@pytest.mark.asyncio
async def test_node_run_after_complete_does_not_spawn(
    designer_store: DesignerGraphStore,
    tmp_path: Path,
) -> None:
    spawned_from_brief: list[str] = []

    async def runner(node, ctx, toolkit: DesignerGraphToolkit) -> NodeResult:
        if node["id"] == "n_brief":
            await toolkit.node_complete(text="done brief")
            msg = await toolkit.node_run("n_character")
            spawned_from_brief.append(msg)
            assert toolkit.completed is not None
            return toolkit.completed
        return await _complete_with_dummy_media(tmp_path, node, toolkit)

    graph = designer_store.save_graph(
        build_bootstrap_graph(project_id="proj_no_spawn", prompt="do not spawn after complete"),
    )
    executor = GraphExecutor(designer_store, runner=runner)
    run = executor.create_run(graph)
    await executor.start_run(run["run_id"])
    await _await_executor_task(executor, run["run_id"])
    assert spawned_from_brief
    assert "already completed" in spawned_from_brief[0]
    finished = designer_store.get_run(run["run_id"])
    assert finished is not None
    assert finished["node_states"]["n_character"]["status"] == NODE_STATUS_COMPLETED


class _DummySpawner:
    async def spawn_node_agent(self, run_id: str, node_id: str) -> str:
        return "ok"

    def apply_agent_graph_patch(self, graph_id: str, patch: dict) -> dict:
        return {}

    def load_graph_snapshot(self, graph_id: str, run_id: str) -> dict:
        return {}


def test_toolkit_graph_patch_rejects_extra_clip_nodes() -> None:
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

    graph = {
        "graph_id": "g1",
        "nodes": [
            {"id": "n_clip_1", "type": "video", "config": {"pipeline": "clip"}},
        ],
        "edges": [],
    }

    class _RejectSpawner(_DummySpawner):
        def load_graph_snapshot(self, graph_id: str, run_id: str) -> dict:
            return {"graph": graph}

        def apply_agent_graph_patch(self, graph_id: str, patch: dict) -> dict:
            raise AssertionError("must not add extra clip nodes")

    ctx = NodeExecutionContext(graph=graph, run_id="r1", node_id="n_clip_1")
    toolkit = DesignerGraphToolkit(_RejectSpawner(), ctx)
    result = toolkit.graph_patch(
        {
            "upsert_nodes": [
                {"id": "n_clip_2", "type": "video", "config": {"pipeline": "clip"}},
            ]
        }
    )
    assert result.get("ok") is False
    assert "n_clip_2" in str(result.get("error") or "")


def test_clip_video_tool_timeout_exempts_ability_manager_default() -> None:
    from openjiuwen.core.single_agent.ability_manager import AbilityManager

    from jiuwenswarm.server.runtime.designer.executor import _node_execute_timeout_sec
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
    from jiuwenswarm.server.runtime.designer.node_agent import build_designer_tools

    node = {
        "id": "n_clip_1",
        "type": "video",
        "config": {"pipeline": "clip", "tools": ["call_video_model"]},
    }
    ctx = NodeExecutionContext(
        graph={"graph_id": "g1", "nodes": [node], "edges": []},
        run_id="r1",
        node_id="n_clip_1",
    )
    tools = build_designer_tools(DesignerGraphToolkit(_DummySpawner(), ctx))
    video = next(tool for tool in tools if tool.card.name == "call_video_model")
    # None = exempt from the 300s default; invoke then uses the 3600s hard cap.
    # Node-level wait_for still caps the clip at _node_execute_timeout_sec.
    timeout = AbilityManager._resolve_call_timeout(video.card)
    assert timeout is None
    assert _node_execute_timeout_sec(node) >= 1800.0

    mgr = AbilityManager(owner_id="designer:r1:n_clip_1")
    mgr.add_ability(video.card, video)
    registered = mgr._tools["call_video_model"]
    assert AbilityManager._resolve_call_timeout(registered) is None


def test_stamp_ability_timeouts_overrides_bare_300s_card() -> None:
    from openjiuwen.core.foundation.tool import ToolCard
    from openjiuwen.core.single_agent.ability_manager import (
        AbilityManager,
        DEFAULT_TOOL_CALL_TIMEOUT,
    )

    from jiuwenswarm.server.runtime.designer.node_agent import (
        _stamp_ability_manager_timeouts,
    )

    class _FakeAM:
        def __init__(self) -> None:
            self._tools = {
                "call_video_model": ToolCard(
                    name="call_video_model",
                    description="video",
                    input_params={},
                ),
                "call_model": ToolCard(
                    name="call_model",
                    description="llm",
                    input_params={},
                ),
            }

        _resolve_call_timeout = staticmethod(AbilityManager._resolve_call_timeout)

    class _FakeAgent:
        def __init__(self) -> None:
            self.ability_manager = _FakeAM()

    node = {
        "id": "n_clip_1",
        "type": "video",
        "config": {"pipeline": "clip"},
    }
    agent = _FakeAgent()
    video_before = AbilityManager._resolve_call_timeout(
        agent.ability_manager._tools["call_video_model"]
    )
    assert video_before == DEFAULT_TOOL_CALL_TIMEOUT

    _stamp_ability_manager_timeouts(agent, node)

    assert agent.ability_manager._resolve_call_timeout(
        agent.ability_manager._tools["call_video_model"]
    ) is None
    llm_timeout = agent.ability_manager._resolve_call_timeout(
        agent.ability_manager._tools["call_model"]
    )
    assert llm_timeout is not None
    assert llm_timeout >= 1800.0


@pytest.mark.asyncio
async def test_call_image_model_uses_designer_helper_not_localfunction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.agents.harness.common.tools.image_tools import generate_image
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
    from jiuwenswarm.server.runtime.designer.node_agent import DesignerGraphToolkit
    from openjiuwen.core.foundation.tool import LocalFunction

    # @tool wraps generate_image as LocalFunction — awaiting it raises TypeError.
    assert isinstance(generate_image, LocalFunction)

    saved = tmp_path / "sheet.png"
    saved.write_bytes(b"png")
    calls: list[str] = []

    async def fake_image(prompt: str, **kwargs) -> dict[str, str]:
        calls.append(prompt)
        return {"image_path": str(saved)}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.generate_designer_image",
        fake_image,
    )
    node = {
        "id": "n_character_1",
        "type": "image",
        "label": "character",
        "config": {"pipeline": "character_design", "prompt": "a hero"},
    }
    ctx = NodeExecutionContext(
        graph={"nodes": [node]},
        run_id="run_img",
        node_id="n_character_1",
    )
    toolkit = DesignerGraphToolkit(_DummySpawner(), ctx)
    out = await toolkit.call_image_model(prompt="a hero")
    assert out.startswith("image_ready")
    assert calls == ["a hero"]
    assert toolkit.completed is not None


@pytest.mark.asyncio
async def test_call_image_model_attaches_on_screen_character_sheets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
    from jiuwenswarm.server.runtime.designer.node_agent import DesignerGraphToolkit

    young = tmp_path / "young.png"
    partner = tmp_path / "partner.png"
    young.write_bytes(b"png1")
    partner.write_bytes(b"png2")
    out = tmp_path / "frame.png"
    out.write_bytes(b"png3")
    seen: dict[str, object] = {}

    async def fake_image(prompt: str, **kwargs) -> dict[str, str]:
        seen["prompt"] = prompt
        seen["reference_images"] = kwargs.get("reference_images")
        seen["size"] = kwargs.get("size")
        return {"image_path": str(out)}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.common.generate_designer_image",
        fake_image,
    )
    char_1 = {
        "id": "n_character_1",
        "type": "image",
        "label": "character: young",
        "config": {
            "pipeline": "character_design",
            "character_ids": ["char_1"],
            "character_name": "young",
        },
    }
    char_2 = {
        "id": "n_character_2",
        "type": "image",
        "label": "character: partner",
        "config": {
            "pipeline": "character_design",
            "character_ids": ["char_2"],
            "character_name": "partner",
        },
    }
    frame = {
        "id": "n_frame_3",
        "type": "image",
        "label": "scene 2: keyframe 1",
        "config": {
            "pipeline": "frame",
            "character_ids": ["char_1", "char_2"],
            "character_node_ids": ["n_character_1", "n_character_2"],
            "cast_names": ["young", "partner"],
            "aspect_lock": {"ratio": "9:16", "image_size": "576x1024"},
            "identity_refs": {
                "character_ids": ["char_1", "char_2"],
                "character_node_ids": ["n_character_1", "n_character_2"],
            },
        },
    }
    ctx = NodeExecutionContext(
        graph={"nodes": [char_1, char_2, frame]},
        run_id="run_img_refs",
        node_id="n_frame_3",
        run={
            "node_states": {
                "n_character_1": {
                    "output_ref": {
                        "kind": "image",
                        "uri": young.resolve().as_uri(),
                        "mime_type": "image/png",
                    }
                },
                "n_character_2": {
                    "output_ref": {
                        "kind": "image",
                        "uri": partner.resolve().as_uri(),
                        "mime_type": "image/png",
                    }
                },
            }
        },
    )
    toolkit = DesignerGraphToolkit(_DummySpawner(), ctx)
    result = await toolkit.call_image_model(prompt="compose dinner")
    assert result.startswith("image_ready")
    assert seen["reference_images"] == [str(young.resolve()), str(partner.resolve())]
    assert seen["size"] == "576x1024"
    assert "Image 1 is young" in str(seen["prompt"])
    assert "Image 2 is partner" in str(seen["prompt"])


@pytest.mark.asyncio
async def test_call_video_model_reuses_completed_and_skips_second_wan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
    from jiuwenswarm.server.runtime.designer.node_agent import DesignerGraphToolkit

    clip = tmp_path / "shot.mp4"
    clip.write_bytes(b"mp4" * 40)
    started = 0

    async def fake_video(prompt: str, **kwargs) -> dict[str, str]:
        nonlocal started
        started += 1
        return {"video_path": str(clip)}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.handlers.clip.generate_clip_video",
        fake_video,
    )
    node = {
        "id": "n_clip_1",
        "type": "video",
        "label": "clip",
        "config": {"pipeline": "clip", "prompt": "walks in"},
    }
    ctx = NodeExecutionContext(
        graph={"nodes": [node]},
        run_id="run_vid",
        node_id="n_clip_1",
    )
    toolkit = DesignerGraphToolkit(_DummySpawner(), ctx)
    first = await toolkit.call_video_model(prompt="walks in")
    second = await toolkit.call_video_model(prompt="walks in again")
    assert first.startswith("video_ready")
    assert second.startswith("video_ready")
    assert started == 1
