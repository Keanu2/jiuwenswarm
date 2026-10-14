# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The Director chain (creative request → execution graph) as one tool call.

This is the authoring half of the design layer, exposed to the leader as the
``designer_graph_autorun`` tool (user ruling 2026-10-12): the creative-request
→ execution-graph pipeline that used to be the old bootstrap flow. The chain's
steps are NOT reimplemented here — every step delegates to the original
``Director`` methods in ``jiuwenswarm.server.runtime.designer.orchestration``
and to ``script_analysis.analyze_creative_brief``. This module only owns:

* step sequencing (analyze → author brief → review brief → author storyboard
  → review storyboard → design execution graph → validate → delegate stamp);
* stage-progress reporting for the caller (the tool surfaces it in its
  result; the adapter's bootstrap path forwards it to the frontend);
* error translation (``DesignerLlmError`` → ``DirectorChainError`` with a
  user-presentable message).

Module boundary — the design layer's three orchestration modules:

* ``director_chain``  — creative prompt → graph (this module; its LLM calls
  are independent service-side ``call_model_tool`` calls, NOT the leader's);
* ``graph_compiler``  — graph → swarmflow script;
* ``run_coordinator`` — trigger execution / lifecycle.

The manifest package stays worker-facing only; graph authoring is a
design-layer responsibility (F11 separation).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class DirectorChainError(Exception):
    """Graph authoring chain failure with a user-presentable message."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.user_message = message


async def run_director_chain(
    graph: dict[str, Any],
    *,
    prompt: str,
    optimize_for: str = "quality",
    on_stage: Any | None = None,
) -> dict[str, Any]:
    """Run the Director chain against ``graph``; returns the composed graph.

    ``graph`` may already carry ``metadata.script_analysis`` (the caller's
    seed); when absent the chain produces it first. Every step mutates the
    graph through the original Director methods; the caller persists the
    result. ``on_stage(kind, text[, tool])`` receives per-stage progress.
    """
    from jiuwenswarm.server.runtime.designer.model_tools import DesignerLlmError
    from jiuwenswarm.server.runtime.designer.orchestration import Director
    from jiuwenswarm.server.runtime.designer.script_analysis import (
        analyze_creative_brief,
    )
    from jiuwenswarm.server.runtime.designer.smart_graph import apply_runtime_delegate

    def _stage(kind: str, text: str, tool: str = "") -> None:
        if callable(on_stage):
            try:
                on_stage(kind, text, tool) if tool else on_stage(kind, text)
            except Exception:  # noqa: BLE001 — progress is best-effort
                logger.debug("graph authoring stage emit failed", exc_info=True)

    prompt = str(prompt or "").strip()
    if not prompt:
        raise DirectorChainError("prompt 不能为空")

    meta = dict(graph.get("metadata") or {})
    analysis = (
        dict(meta.get("script_analysis") or {})
        if isinstance(meta.get("script_analysis"), dict)
        else {}
    )
    # The original bootstrap built its graph from the CURRENT prompt
    # (``build_smart_video_graph(prompt=prompt)`` → ``description`` = the full
    # prompt); the brief / storyboard / execution-graph steps read their
    # ``user_prompt`` off ``graph["description"]``. The lightweight
    # workspace-create graph carries a stale, 200-char-truncated description,
    # so stamp the current prompt before the chain runs — original behavior.
    graph["description"] = prompt
    director = Director()
    try:
        # Step 1 — seed analysis (skipped when the caller already stamped one).
        if not analysis:
            _stage("thinking", "Director · Extracting cast and scenes (LLM)")
            analysis = await analyze_creative_brief(prompt, timeout_sec=120.0)
            if (
                not isinstance(analysis, dict)
                or str(analysis.get("source") or "") != "llm"
            ):
                raise DirectorChainError("模型未返回可用的角色/镜头分析")
            meta["script_analysis"] = analysis
            graph["metadata"] = meta
        # Steps 2-3 — creative brief author + review.
        _stage("thinking", "Director · Authoring brief (LLM)")
        await director.author_creative_brief(graph)
        _stage("thinking", "Director · Reviewing / approving brief")
        await director.review_brief(graph)
        # Steps 4-5 — storyboard author + review.
        _stage("thinking", "Director · Designing storyboard (LLM)")
        await director.author_storyboard(graph)
        _stage("thinking", "Director · Reviewing / approving storyboard")
        await director.review_storyboard(graph)
        # Step 6 — execution graph (LLM shot expansion + deterministic
        # build_smart_video_graph composition inside).
        _stage(
            "tool_call",
            "Director · Designing execution graph (LLM)",
            "design_execution_graph",
        )
        await director.design_execution_graph(graph, optimize_for=optimize_for)
        # Final — structural validation + delegate stamp.
        _stage("thinking", "Director · Validating / approving graph + locks")
        await director.validate_plan(graph)
        graph = apply_runtime_delegate(graph)
        meta = dict(graph.get("metadata") or {})
        meta["director_composed_on_bootstrap"] = True
        meta["freeze_shot_topology"] = True
        graph["metadata"] = meta
    except DesignerLlmError as exc:
        raise DirectorChainError(str(exc.user_message or exc)) from exc
    except DirectorChainError:
        raise
    except Exception as exc:  # noqa: BLE001 — chain failures are tool errors
        logger.exception("graph authoring chain failed")
        raise DirectorChainError(f"建图链失败: {exc}") from exc

    cast_n = sum(
        1
        for n in (graph.get("nodes") or [])
        if str(n.get("id") or "").startswith("n_character")
    )
    shot_n = sum(
        1
        for n in (graph.get("nodes") or [])
        if str(n.get("id") or "").startswith("n_clip")
    )
    _stage("stage", f"Director · Approved — {cast_n} solo cards, {shot_n} shots")
    return graph


__all__ = ["DirectorChainError", "run_director_chain"]
