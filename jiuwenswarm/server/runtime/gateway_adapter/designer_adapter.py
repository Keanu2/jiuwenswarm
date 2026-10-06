# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""DesignerAdapter: designer.graph.* / designer.run.* executed in AgentServer."""

from __future__ import annotations

import asyncio
import logging
import os
from copy import deepcopy
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse

from jiuwenswarm.common.schema.designer_graph import (
    CONFIG_DELEGATE_HANDLER,
    DesignerGraphValidationError,
    apply_graph_patch,
    build_bootstrap_graph,
    node_role,
    NODE_ROLE_COMPOSE,
    normalize_execution_graph,
    utc_now_ms,
)
from jiuwenswarm.common.schema.message import EventType, ReqMethod
from jiuwenswarm.common.work_mode import DEFAULT_WEB_WORK_MODE, is_default_project_id
from jiuwenswarm.server.runtime.designer.executor import GraphExecutor
from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore
from jiuwenswarm.server.runtime.gateway_adapter.base import (
    GatewayAdapter,
    build_error_response,
)
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.work_mode import resolve_request_work_mode

logger = logging.getLogger(__name__)

_store = DesignerGraphStore()
_executor = GraphExecutor(_store)


def _prefer_runtime_pipeline(graph: dict[str, Any]) -> dict[str, Any]:
    """Prefer node agents + tools when an LLM is configured; else role handlers."""
    from jiuwenswarm.server.runtime.designer.smart_graph import apply_runtime_delegate

    return apply_runtime_delegate(graph)


# Back-compat alias
def _prefer_handler_pipeline(graph: dict[str, Any]) -> dict[str, Any]:
    return _prefer_runtime_pipeline(graph)


def _ok_response(request: AgentRequest, payload: Any) -> AgentResponse:
    return AgentResponse(
        request_id=request.request_id,
        channel_id=request.channel_id,
        ok=True,
        payload=payload,
        metadata=request.metadata,
    )


def _request_params(request: AgentRequest) -> dict[str, Any]:
    return request.params if isinstance(request.params, dict) else {}


def _error(
    request: AgentRequest,
    message: str,
    code: str = "BAD_REQUEST",
) -> AgentResponse:
    return build_error_response(request, message, code=code)


def hydrate_graph_node_outputs(
    graph: dict[str, Any],
    run: dict[str, Any] | None,
) -> dict[str, Any]:
    """Copy the latest run's node outputs onto the graph so switching graphs shows real previews."""
    if not graph or not run:
        return graph
    states = run.get("node_states") or {}
    if not isinstance(states, dict) or not states:
        return graph
    nodes = []
    changed = False
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            nodes.append(node)
            continue
        state = states.get(str(node.get("id") or "")) or {}
        ref = state.get("output_ref") if isinstance(state, dict) else None
        if isinstance(ref, dict) and str(ref.get("uri") or "").strip():
            if node.get("output_ref") != ref:
                node = {**node, "output_ref": dict(ref)}
                changed = True
        nodes.append(node)
    if not changed:
        return graph
    return {**graph, "nodes": nodes}


def _get_graph(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    graph_id = str(params.get("graph_id") or "").strip()
    if not graph_id:
        return None, "graph_id is required", "BAD_REQUEST"
    graph = _store.get_graph(graph_id)
    if graph is None:
        return None, "graph not found", "NOT_FOUND"
    graph = _executor.reconcile_loaded_graph(graph)
    run = _store.get_latest_run_for_graph(graph_id)
    return {"graph": dict(hydrate_graph_node_outputs(graph, run))}, None, None


def _run_clip_summary(run: dict[str, Any] | None) -> tuple[bool, str | None]:
    if not run:
        return False, None
    states = run.get("node_states") or {}
    ordered = []
    compose = states.get("n_compose")
    if isinstance(compose, dict):
        ordered.append(compose)
    ordered.extend(
        state
        for key, state in states.items()
        if key != "n_compose" and isinstance(state, dict)
    )
    for state in ordered:
        ref = state.get("output_ref") or {}
        if not isinstance(ref, dict):
            continue
        uri = str(ref.get("uri") or "")
        if ref.get("kind") == "video" and uri.startswith("file:"):
            label = str(ref.get("label") or "").strip() or None
            return True, label
    return False, None


def _summarize_graph(graph: dict[str, Any], run: dict[str, Any] | None = None) -> dict[str, Any]:
    if run is None:
        run = _store.get_latest_run_for_graph(str(graph.get("graph_id") or ""))
    has_video, clip_label = _run_clip_summary(run)
    return {
        "graph_id": graph.get("graph_id"),
        "project_id": graph.get("project_id"),
        "title": graph.get("title") or graph.get("graph_id"),
        "updated_at": graph.get("updated_at"),
        "run_id": run.get("run_id") if run else None,
        "run_status": run.get("status") if run else None,
        "has_video": has_video,
        "clip_label": clip_label,
    }


def _list_graphs(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    project_id = str(params.get("project_id") or "").strip()
    if is_default_project_id(project_id):
        project_id = ""
    if project_id:
        project = project_store.get_project_by_id(project_id, cache_bust=True)
        if project is None or project.hidden:
            return None, "project not found", "NOT_FOUND"
        graphs = _store.list_graphs_for_project(project_id)
    else:
        graphs = _store.list_graphs()
    payload: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for graph in graphs:
        item = dict(graph)
        run = _store.get_latest_run_for_graph(str(item.get("graph_id") or ""))
        item = hydrate_graph_node_outputs(item, run)
        payload.append(item)
        summaries.append(_summarize_graph(item, run))
    return {
        "graphs": payload,
        "summaries": summaries,
    }, None, None


async def _push_designer_event(
    *,
    request: AgentRequest,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    try:
        from jiuwenswarm.server.gateway_push.transport import WebSocketGatewayPushTransport

        body = {"event_type": event_type, **payload}
        await WebSocketGatewayPushTransport().send_push(
            {
                "request_id": request.request_id or f"designer-{event_type}-{utc_now_ms()}",
                "channel_id": request.channel_id or "web",
                "session_id": request.session_id,
                "payload": body,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[DesignerAdapter] push %s failed: %s", event_type, exc)


def _run_update_callback(request: AgentRequest):
    loop = asyncio.get_running_loop()

    def on_update(updated: dict[str, Any], node_id: str | None = None) -> None:
        snapshot = deepcopy(updated)
        node_payload = dict(snapshot)

        async def _emit() -> None:
            await _push_designer_event(
                request=request,
                event_type=EventType.DESIGNER_RUN_UPDATED.value,
                payload={"run": snapshot},
            )
            if node_id:
                await _push_designer_event(
                    request=request,
                    event_type=EventType.DESIGNER_NODE_UPDATED.value,
                    payload={"run": node_payload, "node_id": node_id},
                )

        loop.call_soon_threadsafe(lambda: asyncio.create_task(_emit()))

    return on_update


def _graph_update_callback(request: AgentRequest):
    loop = asyncio.get_running_loop()

    def on_graph(graph: dict[str, Any]) -> None:
        snapshot = deepcopy(graph)

        async def _emit() -> None:
            await _push_designer_event(
                request=request,
                event_type=EventType.DESIGNER_GRAPH_UPDATED.value,
                payload={"graph": snapshot},
            )

        loop.call_soon_threadsafe(lambda: asyncio.create_task(_emit()))

    return on_graph


def _save_graph(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    raw_graph = params.get("graph")
    if not isinstance(raw_graph, dict):
        return None, "graph is required", "BAD_REQUEST"
    try:
        saved = _store.save_graph(normalize_execution_graph(raw_graph))
    except DesignerGraphValidationError as exc:
        return None, str(exc), "BAD_REQUEST"
    return {"graph": dict(saved)}, None, None


def _patch_graph(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    graph_id = str(params.get("graph_id") or "").strip()
    if not graph_id:
        return None, "graph_id is required", "BAD_REQUEST"
    graph = _store.get_graph(graph_id)
    if graph is None:
        return None, "graph not found", "NOT_FOUND"
    raw_patch = params.get("patch")
    if raw_patch is None:
        raw_patch = {
            key: params[key]
            for key in (
                "title",
                "description",
                "upsert_nodes",
                "upsert_edges",
                "remove_node_ids",
                "remove_edge_ids",
            )
            if key in params
        }
    try:
        saved = _store.save_graph(apply_graph_patch(graph, raw_patch))
    except DesignerGraphValidationError as exc:
        return None, str(exc), "BAD_REQUEST"
    return {"graph": dict(saved)}, None, None


def _bootstrap_graph(
    params: dict[str, Any],
    channel_id: str,
    analysis: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        return None, "prompt is required", "BAD_REQUEST"

    project_id = str(params.get("project_id") or "").strip()
    if is_default_project_id(project_id):
        project_id = ""
    project_payload: dict[str, Any] | None = None

    if project_id:
        project = project_store.get_project_by_id(project_id, cache_bust=True)
        if project is None or project.hidden:
            return None, "project not found", "NOT_FOUND"
    else:
        name = str(params.get("name") or prompt[:40] or "Designer Project").strip()
        work_mode, mode_error = resolve_request_work_mode(params, channel_id)
        if mode_error is not None:
            return None, f"invalid work_mode: {params.get('work_mode')!r}", mode_error
        project_dir = str(params.get("project_dir") or "").strip()
        if project_dir and not os.path.isabs(project_dir):
            return None, "project_dir must be an absolute path", "BAD_REQUEST"
        if project_dir and not os.path.isdir(project_dir):
            return None, "project directory does not exist", "PROJECT_DIR_MISSING"
        if not project_dir:
            try:
                project_dir = project_store.resolve_default_project_dir(name, work_mode)
            except ValueError as exc:
                return None, str(exc), "BAD_REQUEST"
            try:
                os.makedirs(project_dir, exist_ok=True)
            except OSError as exc:
                return None, f"failed to create project directory: {exc}", "INTERNAL_ERROR"
        try:
            project, restored = project_store.create_or_restore_project(
                name,
                project_dir,
                work_mode,
            )
        except project_store.ProjectDirConflict:
            existing = project_store.get_project_by_dir_and_mode(
                project_dir, work_mode, cache_bust=True
            )
            if existing is None or existing.hidden:
                return None, "project_dir already exists", "CONFLICT"
            project, restored = existing, True
        except project_store.ProjectNameConflict:
            return None, "project name already exists", "CONFLICT"
        except ValueError as exc:
            return None, str(exc), "BAD_REQUEST"
        project_id = project.project_id
        project_payload = {
            "project_id": project.project_id,
            "project_dir": project.project_dir,
            "restored": restored,
            "work_mode": project.work_mode or DEFAULT_WEB_WORK_MODE,
        }

    title = params.get("title")
    optimize_raw = str(params.get("optimize_for") or params.get("optimizeFor") or "quality")
    optimize_for = "cost" if optimize_raw.strip().lower() == "cost" else "quality"
    scenario_raw = params.get("scenario")
    scenario = (
        str(scenario_raw).strip().lower()
        if isinstance(scenario_raw, str) and scenario_raw.strip()
        else None
    )
    from jiuwenswarm.server.runtime.designer.composer import (
        compose_execution_graph,
        detect_scenario,
    )
    from jiuwenswarm.server.runtime.designer.script_analysis import (
        analyze_creative_brief_sync,
        heuristic_analysis,
        _llm_configured,
    )
    from jiuwenswarm.server.runtime.designer.skills_loader import attach_skills_metadata
    from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph

    detected = scenario or detect_scenario(prompt)
    # Prefer supervisor-style LLM cast/shot analysis when models exist; heuristics only as fallback.
    if detected == "video":
        if analysis is None:
            if _llm_configured():
                try:
                    analysis = analyze_creative_brief_sync(
                        prompt, use_llm=True, timeout_sec=20.0
                    )
                except Exception:  # noqa: BLE001
                    logger.info("Bootstrap LLM analysis failed; using heuristics", exc_info=True)
                    analysis = heuristic_analysis(prompt)
            else:
                analysis = heuristic_analysis(prompt)
        if not isinstance(analysis, dict):
            analysis = heuristic_analysis(prompt)
        graph = _prefer_runtime_pipeline(
            build_smart_video_graph(
                project_id=project_id,
                prompt=prompt,
                analysis=analysis,
                title=str(title).strip() if isinstance(title, str) else None,
                optimize_for=optimize_for,
                ai_mode=_llm_configured(),
            )
        )
        meta = dict(graph.get("metadata") or {})
        meta["optimize_for"] = optimize_for
        meta["scenario"] = "video"
        meta["script_analysis"] = analysis
        meta["script_analysis_mode"] = str(analysis.get("source") or "heuristic")
        # Play may still refine analysis/plan with supervisor+manager agents once.
        meta["pending_llm_analysis"] = bool(
            _llm_configured() and str(analysis.get("source") or "") != "llm"
        )
        meta["one_pass"] = True
        meta["auto_accept_outputs"] = True
        graph["metadata"] = meta
        graph = attach_skills_metadata(graph, prompt)
    else:
        graph = _prefer_runtime_pipeline(
            compose_execution_graph(
                project_id=project_id,
                prompt=prompt,
                title=str(title).strip() if isinstance(title, str) else None,
                optimize_for=optimize_for,  # type: ignore[arg-type]
                scenario=detected,
            )
        )
    saved = _store.save_graph(graph)
    payload: dict[str, Any] = {"graph": dict(saved), "project_id": project_id}
    if project_payload is not None:
        payload["project"] = project_payload
    return payload, None, None


def _get_run(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    run_id = str(params.get("run_id") or "").strip()
    if run_id:
        run = _store.get_run(run_id)
        if run is None:
            return None, "run not found", "NOT_FOUND"
        return {"run": dict(run)}, None, None
    graph_id = str(params.get("graph_id") or "").strip()
    if graph_id:
        run = _store.get_latest_run_for_graph(graph_id)
        if run is None:
            return None, "run not found", "NOT_FOUND"
        return {"run": dict(run)}, None, None
    return None, "run_id or graph_id is required", "BAD_REQUEST"


def _rerun_error_message(graph: dict[str, Any] | None, node_id: str, exc: Exception) -> str:
    message = str(exc)
    if "upstream not ready" not in message:
        return message
    role = ""
    if graph is not None:
        for node in graph.get("nodes") or []:
            if str(node.get("id") or "") == node_id:
                role = node_role(node)
                break
    if role == NODE_ROLE_COMPOSE or node_id == "n_compose":
        return "请先让所有视频片段生成完成，再重新生成成片。"
    return message


def _start_run(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    graph_id = str(params.get("graph_id") or "").strip()
    run_id = str(params.get("run_id") or "").strip()
    node_id = str(params.get("node_id") or "").strip()
    graph = _store.get_graph(graph_id) if graph_id else None
    if run_id:
        existing = _store.get_run(run_id)
        if existing is None:
            return None, "run not found", "NOT_FOUND"
        if graph is None:
            graph = _store.get_graph(existing["graph_id"])
        elif str(existing.get("graph_id") or "") != str(graph.get("graph_id") or ""):
            return None, "run does not belong to graph", "BAD_REQUEST"
        if node_id:
            if graph is None:
                return None, "graph not found", "NOT_FOUND"
            try:
                run = _executor.create_rerun(graph, source_run=existing, node_id=node_id)
            except ValueError as exc:
                return None, _rerun_error_message(graph, node_id, exc), "BAD_REQUEST"
            except KeyError:
                return None, "node not found", "NOT_FOUND"
            run_id = run["run_id"]
    elif graph is not None and node_id:
        source = _store.get_latest_run_for_graph(graph["graph_id"])
        if source is None:
            return None, "no previous run to rerun from", "BAD_REQUEST"
        try:
            run = _executor.create_rerun(graph, source_run=source, node_id=node_id)
        except ValueError as exc:
            return None, _rerun_error_message(graph, node_id, exc), "BAD_REQUEST"
        except KeyError:
            return None, "node not found", "NOT_FOUND"
        run_id = run["run_id"]
    elif graph is not None:
        run = _executor.create_run(graph)
        run_id = run["run_id"]
    else:
        return None, "graph_id or run_id is required", "BAD_REQUEST"
    if graph is None:
        return None, "graph not found", "NOT_FOUND"
    return {"run_id": run_id, "graph_id": graph["graph_id"]}, None, None


def _pause_run(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    run_id = str(params.get("run_id") or "").strip()
    if not run_id:
        return None, "run_id is required", "BAD_REQUEST"
    try:
        run = _executor.pause_run(run_id)
    except KeyError:
        return None, "run not found", "NOT_FOUND"
    return {"run": dict(run)}, None, None


def _cancel_run(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    run_id = str(params.get("run_id") or "").strip()
    if not run_id:
        return None, "run_id is required", "BAD_REQUEST"
    try:
        run = _executor.cancel_run(run_id)
    except KeyError:
        return None, "run not found", "NOT_FOUND"
    return {"run": dict(run)}, None, None


def _choose_output(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    run_id = str(params.get("run_id") or "").strip()
    node_id = str(params.get("node_id") or "").strip()
    choice = str(params.get("choice") or "").strip()
    if not run_id:
        return None, "run_id is required", "BAD_REQUEST"
    if not node_id:
        return None, "node_id is required", "BAD_REQUEST"
    try:
        run = _executor.choose_output(run_id, node_id, choice)
    except KeyError:
        return None, "run or node not found", "NOT_FOUND"
    except ValueError as exc:
        return None, str(exc), "BAD_REQUEST"
    return {"run": dict(run)}, None, None


class DesignerAdapter(GatewayAdapter):
    """Designer execution graph adapter."""

    methods: frozenset[str] = frozenset(
        {
            ReqMethod.DESIGNER_GRAPH_GET.value,
            ReqMethod.DESIGNER_GRAPH_LIST.value,
            ReqMethod.DESIGNER_GRAPH_SAVE.value,
            ReqMethod.DESIGNER_GRAPH_BOOTSTRAP.value,
            ReqMethod.DESIGNER_GRAPH_PATCH.value,
            ReqMethod.DESIGNER_RUN_START.value,
            ReqMethod.DESIGNER_RUN_GET.value,
            ReqMethod.DESIGNER_RUN_PAUSE.value,
            ReqMethod.DESIGNER_RUN_CANCEL.value,
            ReqMethod.DESIGNER_RUN_CHOOSE_OUTPUT.value,
        }
    )

    async def handle(self, request: AgentRequest) -> AgentResponse:
        method = request.req_method
        params = _request_params(request)
        try:
            if method == ReqMethod.DESIGNER_GRAPH_GET:
                payload, error, code = await asyncio.to_thread(_get_graph, params)
            elif method == ReqMethod.DESIGNER_GRAPH_LIST:
                payload, error, code = await asyncio.to_thread(_list_graphs, params)
            elif method == ReqMethod.DESIGNER_GRAPH_SAVE:
                payload, error, code = await asyncio.to_thread(_save_graph, params)
            elif method == ReqMethod.DESIGNER_GRAPH_BOOTSTRAP:
                # Never block the UI RPC on LLM casting — that caused Request timed out.
                # Build immediately with heuristics (or a precomputed analysis); LLM may
                # refine later when the user hits Run via supervisor metadata only.
                payload, error, code = await asyncio.to_thread(
                    _bootstrap_graph,
                    params,
                    request.channel_id,
                    None,
                )
            elif method == ReqMethod.DESIGNER_GRAPH_PATCH:
                payload, error, code = await asyncio.to_thread(_patch_graph, params)
            elif method == ReqMethod.DESIGNER_RUN_GET:
                payload, error, code = await asyncio.to_thread(_get_run, params)
            elif method == ReqMethod.DESIGNER_RUN_START:
                payload, error, code = await asyncio.to_thread(_start_run, params)
                if error is None and payload is not None:
                    run = await _executor.start_run(
                        str(payload["run_id"]),
                        on_update=_run_update_callback(request),
                        on_graph_update=_graph_update_callback(request),
                    )
                    await _push_designer_event(
                        request=request,
                        event_type=EventType.DESIGNER_RUN_UPDATED.value,
                        payload={"run": dict(run)},
                    )
                    return _ok_response(request, {"run": dict(run)})
            elif method == ReqMethod.DESIGNER_RUN_PAUSE:
                payload, error, code = await asyncio.to_thread(_pause_run, params)
            elif method == ReqMethod.DESIGNER_RUN_CANCEL:
                payload, error, code = await asyncio.to_thread(_cancel_run, params)
            elif method == ReqMethod.DESIGNER_RUN_CHOOSE_OUTPUT:
                payload, error, code = await asyncio.to_thread(_choose_output, params)
            else:
                return build_error_response(
                    request,
                    f"unsupported method: {method}",
                    code="NOT_IMPLEMENTED",
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DesignerAdapter] %s failed: %s", method, exc)
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")

        if error is not None:
            return build_error_response(request, error, code=code or "BAD_REQUEST")
        if isinstance(payload, dict) and payload.get("graph") is not None and method in {
            ReqMethod.DESIGNER_GRAPH_BOOTSTRAP,
            ReqMethod.DESIGNER_GRAPH_PATCH,
        }:
            await _push_designer_event(
                request=request,
                event_type=EventType.DESIGNER_GRAPH_UPDATED.value,
                payload={"graph": payload["graph"]},
            )
        if isinstance(payload, dict) and payload.get("run") is not None and method in {
            ReqMethod.DESIGNER_RUN_PAUSE,
            ReqMethod.DESIGNER_RUN_CANCEL,
            ReqMethod.DESIGNER_RUN_CHOOSE_OUTPUT,
        }:
            await _push_designer_event(
                request=request,
                event_type=EventType.DESIGNER_RUN_UPDATED.value,
                payload={"run": payload["run"]},
            )
        return _ok_response(request, payload)
