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
