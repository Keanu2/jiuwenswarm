# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.composer import (
    compose_execution_graph,
    detect_scenario,
)

_NAPOLEON = (
    "《奶破龙翻越阿尔卑斯山》（致敬达维特）原画精髓。"
    "画面完美保留了原作巴洛克式的厚重油画质感、雪山寒风以及戏剧性的逆光。"
)


def test_chinese_conjunction_is_not_multimodal() -> None:
    assert detect_scenario(_NAPOLEON) == "video"
    assert detect_scenario("雪山寒风以及戏剧性的逆光") == "video"


def test_compose_does_not_dump_catalog_template(monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.script_analysis._llm_configured",
        lambda: False,
    )
    graph = compose_execution_graph(
        project_id="proj_canvas01",
        prompt=_NAPOLEON,
        scenario="multimodal",
    )
    ids = {str(node.get("id") or "") for node in (graph.get("nodes") or [])}
    assert "n_brief" in ids
    assert "n_storyboard" in ids
    assert any(nid.startswith("n_clip") for nid in ids)
    assert "n_intent_brief" not in ids
    assert not any(nid.startswith("n_multi_") for nid in ids)
    meta = graph.get("metadata") or {}
    assert str(meta.get("scenario") or "") == "video"
    assert str(meta.get("bootstrap") or "").startswith("designer.graph.smart_video")
