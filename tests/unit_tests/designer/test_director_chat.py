# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import json
from copy import deepcopy

import pytest

from jiuwenswarm.common.schema.designer_graph import DesignerGraphValidationError
from jiuwenswarm.server.runtime.designer import director_chat, model_tools
from jiuwenswarm.server.runtime.designer.chat_document_sync import ChatDocumentConflict
from jiuwenswarm.server.runtime.designer.orchestration import Director


@pytest.fixture(autouse=True)
def _wan_video_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.pipeline.model_capacity.configured_video_model_id",
        lambda: "wan3.0-video",
    )


def _graph(metadata: dict | None = None) -> dict:
    return {
        "schema_version": "designer-execution-graph.v1",
        "graph_id": "graph_test",
        "project_id": "p1",
        "title": "Demo",
        "description": "alley at night",
        "metadata": metadata or {},
        "nodes": [
            {
                "id": "n_brief",
                "type": "text",
                "label": "Text 1",
                "config": {"role": "text", "pipeline": "brief", "prompt": "alley at night"},
                "layout": {"x": 40, "y": 240, "width": 280, "height": 160},
            },
            {
                "id": "n_character",
                "type": "image",
                "label": "Image 1",
                "config": {"role": "image", "pipeline": "character", "prompt": "officer"},
                "layout": {"x": 400, "y": 40, "width": 280, "height": 160},
            },
        ],
        "edges": [
            {"id": "e_brief_character", "source": "n_brief", "target": "n_character", "kind": "data"},
        ],
        "created_at": 1,
        "updated_at": 1,
    }


def test_director_chat_context_carries_bootstrap_decisions() -> None:
    graph = _graph(
        {
            "active_director_skill": "d" * 3000,
            "scenario_skill_excerpt": "film scenario",
            "director_brief_notes": "keep the rain",
            "spatial_lock": {"setting": "alley"},
            "style_lock": {"look": "noir rain", "medium": "live action"},
            "video_settings": {"aspect_ratio": "9:16", "resolution": "720p"},
            "image_settings": {"aspect_ratio": "1:1"},
            "language_lock": "zh",
            "approved_brief": "already sent as a chat document",
        }
    )

    context = director_chat.director_chat_context(graph)

    assert context == {
        "director_skill": "d" * 2000,
        "scenario_skill": "film scenario",
        "brief_notes": "keep the rain",
        "spatial_lock": {"setting": "alley"},
        "style_lock": {"look": "noir rain", "medium": "live action"},
        "video_settings": {"aspect_ratio": "9:16", "resolution": "720P"},
        "image_settings": {"aspect_ratio": "1:1", "resolution": "1K"},
        "language_lock": "zh",
    }
    assert director_chat.director_chat_context(_graph()) == {
        "video_settings": {"aspect_ratio": "16:9", "resolution": "1080P"},
        "image_settings": {"aspect_ratio": "16:9", "resolution": "1K"},
    }


async def test_plan_chat_turn_speaks_as_director_with_context(monkeypatch) -> None:
    captured: dict = {}

    async def fake_call_model_tool(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "text": json.dumps({"intent": "answer", "summary": "Two shots."})}

    monkeypatch.setattr(model_tools, "call_model_tool", fake_call_model_tool)
    graph = _graph({"director_skill_excerpt": "director rules", "language_lock": "zh"})

    plan = await director_chat.plan_chat_turn(graph, "有几个镜头？", documents={})

    assert plan["intent"] == "answer"
    assert captured["system"].startswith("You are the Designer Director.")
    assert "clip_action" in captured["system"]
    assert "shot_action" not in captured["system"]
    snapshot = json.loads(captured["prompt"])
    assert snapshot["director_context"] == {
        "director_skill": "director rules",
        "video_settings": {"aspect_ratio": "16:9", "resolution": "1080P"},
        "image_settings": {"aspect_ratio": "16:9", "resolution": "1K"},
        "language_lock": "zh",
    }
    assert snapshot["user"] == "有几个镜头？"


def test_edit_graph_rejects_answer_that_modifies_workflow() -> None:
    plan = {
        "intent": "answer",
        "summary": "ok",
        "patch": {"description": "new"},
        "remove_shot_ids": [],
        "prompt_updates": [],
        "edit_documents": False,
        "run_node_ids": [],
    }
    with pytest.raises(DesignerGraphValidationError):
        director_chat.edit_graph(_graph(), plan, "hi")


def test_edit_graph_blocks_content_edits_while_documents_pending() -> None:
    plan = {
        "intent": "refine_node",
        "summary": "Updated",
        "patch": {"upsert_nodes": [{"id": "n_character", "config": {"prompt": "cyber officer"}}]},
        "remove_shot_ids": [],
        "prompt_updates": [],
        "edit_documents": False,
        "run_node_ids": [],
    }
    with pytest.raises(ChatDocumentConflict):
        director_chat.edit_graph(_graph(), plan, "改成赛博风", pending_documents=True)

    next_graph, run_ids, _summary = director_chat.edit_graph(_graph(), plan, "改成赛博风")
    character = next(node for node in next_graph["nodes"] if node["id"] == "n_character")
    assert character["config"]["prompt"] == "cyber officer"
    assert run_ids == []


def _graph_with_user_added_node() -> dict:
    graph = _graph()
    graph["nodes"].append(
        {
            "id": "n_text_2",
            "type": "text",
            "label": "Text 2",
            "config": {"role": "text", "prompt": "extra notes", "user_added": True},
            "layout": {"x": 40, "y": 480, "width": 280, "height": 160},
        }
    )
    return graph


@pytest.mark.parametrize("changed", [True, False])
async def test_director_chat_onboards_user_nodes_only_after_changes(monkeypatch, changed) -> None:
    edited = _graph_with_user_added_node()

    async def fake_run_director_chat(graph, message, **kwargs):
        return {"graph": deepcopy(edited), "changed": changed, "summary": "ok", "run_node_ids": []}

    monkeypatch.setattr(director_chat, "run_director_chat", fake_run_director_chat)

    result = await Director().chat(_graph(), "加一段笔记", documents={})

    metadata = result["graph"].get("metadata") or {}
    added = next(node for node in result["graph"]["nodes"] if node["id"] == "n_text_2")
    if changed:
        assert metadata["director_user_node_onboard"]["onboarded"] == ["n_text_2"]
        assert added["config"]["kind"] == "agent"
    else:
        assert "director_user_node_onboard" not in metadata
        assert "kind" not in added["config"]


def _clip_graph() -> dict:
    graph = _graph()
    for index in (1, 2):
        graph["nodes"].append(
            {
                "id": f"n_clip_{index}",
                "type": "video",
                "label": f"Video {index}",
                "config": {
                    "role": "video",
                    "pipeline": "clip",
                    "clip_index": index,
                    "timeline": "0-5s",
                    "duration_sec": 7,
                },
                "layout": {"x": 400, "y": 240 * index, "width": 280, "height": 160},
            }
        )
    return graph


def test_retiming_a_clip_drops_its_manual_length_unless_the_director_sets_one() -> None:
    plan = {
        "patch": {
            "upsert_nodes": [
                {"id": "n_clip_1", "config": {"timeline": "0-9s"}},
                {"id": "n_clip_2", "config": {"timeline": "0-9s", "duration_sec": 9}},
            ]
        }
    }
    patch = director_chat._merge_prompt_updates(_clip_graph(), plan)
    configs = {node["id"]: node["config"] for node in patch["upsert_nodes"]}
    assert "duration_sec" not in configs["n_clip_1"]
    assert configs["n_clip_2"]["duration_sec"] == 9

    untouched = director_chat._merge_prompt_updates(
        _clip_graph(), {"patch": {"upsert_nodes": [{"id": "n_clip_1", "config": {"camera": "close"}}]}}
    )
    assert untouched["upsert_nodes"][0]["config"]["duration_sec"] == 7
