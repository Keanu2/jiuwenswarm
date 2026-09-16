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
    node_pipeline,
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


def _leader_progress_callback(request: AgentRequest):
    loop = asyncio.get_running_loop()

    def on_progress(kind: str, text: str, tool: str = "") -> None:
        activity = {
            "kind": str(kind or "stage"),
            "text": str(text or ""),
            "tool": str(tool or ""),
            "at": utc_now_ms(),
        }

        async def _emit() -> None:
            await _push_designer_event(
                request=request,
                event_type=EventType.DESIGNER_LEADER_ACTIVITY.value,
                payload={"activity": activity},
            )

        loop.call_soon_threadsafe(lambda: asyncio.create_task(_emit()))

    return on_progress


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
        graph = normalize_execution_graph(raw_graph)
        try:
            from jiuwenswarm.server.runtime.designer.orchestration import SupervisorAgent

            SupervisorAgent().onboard_user_added_nodes(graph)
        except Exception:
            logger.debug("Supervisor user-node onboard on save failed", exc_info=True)
        saved = _store.save_graph(graph)
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
        # Mark upserted nodes as user-added when the patch did not already set it
        # (canvas dock / successor add paths stamp this on the client).
        if isinstance(raw_patch, dict):
            upsert = raw_patch.get("upsert_nodes")
            if isinstance(upsert, list):
                stamped = []
                for item in upsert:
                    if not isinstance(item, dict):
                        stamped.append(item)
                        continue
                    node = dict(item)
                    cfg = dict(node.get("config") or {})
                    if "user_added" not in cfg:
                        cfg["user_added"] = True
                    node["config"] = cfg
                    stamped.append(node)
                raw_patch = {**raw_patch, "upsert_nodes": stamped}
        next_graph = apply_graph_patch(graph, raw_patch)
        try:
            from jiuwenswarm.server.runtime.designer.orchestration import SupervisorAgent

            SupervisorAgent().onboard_user_added_nodes(next_graph)
        except Exception:
            logger.debug("Supervisor user-node onboard on patch failed", exc_info=True)
        saved = _store.save_graph(next_graph)
    except DesignerGraphValidationError as exc:
        return None, str(exc), "BAD_REQUEST"
    return {"graph": dict(saved)}, None, None


def _bootstrap_graph(
    params: dict[str, Any],
    channel_id: str,
    analysis: dict[str, Any] | None = None,
    on_progress: Any | None = None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    prompt = str(params.get("prompt") or "").strip()
    raw_references = params.get("references")
    if raw_references is None:
        raw_references = params.get("user_references")
    has_references = isinstance(raw_references, list) and len(raw_references) > 0
    if not prompt and not has_references:
        return None, "prompt is required", "BAD_REQUEST"
    if not prompt:
        prompt = "根据参考素材创作"

    project_id = str(params.get("project_id") or "").strip()
    if is_default_project_id(project_id):
        project_id = ""
    project_payload: dict[str, Any] | None = None
    resolved_project_dir = ""

    if project_id:
        project = project_store.get_project_by_id(project_id, cache_bust=True)
        if project is None or project.hidden:
            return None, "project not found", "NOT_FOUND"
        resolved_project_dir = str(getattr(project, "project_dir", "") or "")
    else:
        name = str(params.get("name") or "").strip()
        if not name:
            from jiuwenswarm.server.runtime.session.project_store import (
                sanitize_project_dir_name,
            )

            name = sanitize_project_dir_name(prompt[:80] or "Designer Project")
        else:
            from jiuwenswarm.server.runtime.session.project_store import (
                sanitize_project_dir_name,
            )

            name = sanitize_project_dir_name(name)
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
        resolved_project_dir = str(project.project_dir or "")
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
    from pathlib import Path as _Path

    from jiuwenswarm.server.runtime.designer.composer import (
        compose_execution_graph,
        detect_scenario,
    )
    from jiuwenswarm.server.runtime.designer.script_analysis import (
        heuristic_analysis,
        _llm_configured,
    )
    from jiuwenswarm.server.runtime.designer.skills_loader import attach_skills_metadata
    from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph
    from jiuwenswarm.server.runtime.designer.user_references import (
        UserReferenceError,
        analysis_prompt_with_references,
        attach_user_references_to_graph,
        normalize_user_references,
    )

    try:
        refs_dir = (
            _Path(resolved_project_dir) / ".designer" / "refs"
            if resolved_project_dir
            else _Path(".") / ".designer" / "refs"
        )
        user_refs = normalize_user_references(raw_references, dest_dir=refs_dir)
    except UserReferenceError as exc:
        return None, str(exc), exc.code
    analysis_prompt = analysis_prompt_with_references(prompt, user_refs)

    detected = scenario or detect_scenario(analysis_prompt)
    if callable(on_progress):
        on_progress("thinking", "Supervisor · Reading brief")
    # Never call LLM from this sync thread (asyncio.run breaks AsyncOpenAI).
    # Caller passes LLM analysis from the main event loop when available.
    if detected == "video":
        if analysis is None:
            if callable(on_progress):
                on_progress("stage", "Supervisor · Provisional cast skeleton")
            analysis = heuristic_analysis(analysis_prompt)
        if not isinstance(analysis, dict):
            analysis = heuristic_analysis(analysis_prompt)
        analysis_mode = str(analysis.get("source") or "heuristic")
        if callable(on_progress):
            on_progress(
                "tool_call",
                "Supervisor · Materializing graph",
                "build_smart_video_graph",
            )
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
        meta["script_analysis_mode"] = analysis_mode
        meta["pending_llm_analysis"] = analysis_mode != "llm"
        # One-pass Enter already authored the plan — no Play redesign pending.
        meta["pending_supervisor_graph"] = False if analysis_mode == "llm" else bool(
            _llm_configured()
        )
        meta["supervisor_composed_on_bootstrap"] = analysis_mode == "llm"
        meta["supervisor_owns_graph"] = True
        meta["freeze_shot_topology"] = False
        meta["one_pass"] = analysis_mode == "llm"
        meta["auto_accept_outputs"] = True
        if isinstance(analysis.get("scene_locks"), dict) and analysis["scene_locks"]:
            meta["scene_locks"] = analysis["scene_locks"]
        graph["metadata"] = meta
        graph = attach_skills_metadata(graph, prompt)
        if callable(on_progress):
            cast_n = sum(
                1
                for n in (graph.get("nodes") or [])
                if str(n.get("id") or "").startswith("n_character")
            )
            on_progress(
                "stage",
                f"Supervisor · Graph materialised ({cast_n} solo cards, source={analysis_mode})",
            )
    else:
        if callable(on_progress):
            on_progress(
                "tool_call",
                "Supervisor · Composing non-video workflow",
                "compose_execution_graph",
            )
        graph = _prefer_runtime_pipeline(
            compose_execution_graph(
                project_id=project_id,
                prompt=prompt,
                title=str(title).strip() if isinstance(title, str) else None,
                optimize_for=optimize_for,  # type: ignore[arg-type]
                scenario=detected,
            )
        )
    if user_refs:
        graph = attach_user_references_to_graph(graph, user_refs)
    if callable(on_progress):
        on_progress("stage", "Manager · Saving graph")
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
                role = node_pipeline(node) or node_role(node)
                break
    if role == NODE_ROLE_COMPOSE or node_id == "n_compose":
        return "请先让所有视频片段生成完成，再重新生成成片。"
    return message


def _start_run(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    graph_id = str(params.get("graph_id") or "").strip()
    run_id = str(params.get("run_id") or "").strip()
    node_id = str(params.get("node_id") or "").strip()
    graph = _store.get_graph(graph_id) if graph_id else None
    contribution_warning = ""
    if graph is not None:
        try:
            from jiuwenswarm.server.runtime.designer.orchestration import ManagerAgent

            audit = ManagerAgent().audit_contribution_for_run(graph)
            contribution_warning = str(audit.get("warning") or "")
            _store.save_graph(graph)
        except Exception:
            logger.debug("Manager contribution audit failed", exc_info=True)
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
    # Stamp warning onto the run record so the UI can show it without blocking.
    if contribution_warning:
        try:
            run_obj = _store.get_run(run_id)
            if isinstance(run_obj, dict):
                run_obj = dict(run_obj)
                run_obj["warning"] = contribution_warning
                warnings = list(run_obj.get("warnings") or [])
                if contribution_warning not in warnings:
                    warnings.append(contribution_warning)
                run_obj["warnings"] = warnings[:8]
                _store.save_run(run_obj)
        except Exception:
            logger.debug("Failed to stamp run contribution warning", exc_info=True)
    return {
        "run_id": run_id,
        "graph_id": graph["graph_id"],
        "warning": contribution_warning or None,
        "warnings": [contribution_warning] if contribution_warning else [],
    }, None, None


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


async def _chat_graph(request: AgentRequest, params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    from jiuwenswarm.server.runtime.designer.leader_chat import run_leader_chat

    graph_id = str(params.get("graph_id") or "").strip()
    message = str(params.get("message") or params.get("prompt") or "").strip()
    if not graph_id:
        return None, "graph_id is required", "BAD_REQUEST"
    if not message:
        return None, "message is required", "BAD_REQUEST"
    graph = _store.get_graph(graph_id)
    if graph is None:
        return None, "graph not found", "NOT_FOUND"
    graph = _executor.reconcile_loaded_graph(graph)
    selected_node_id = str(params.get("selected_node_id") or params.get("node_id") or "").strip()
    run_new_nodes = bool(params.get("run_new_nodes") or params.get("runNewNodes"))
    progress = _leader_progress_callback(request)
    try:
        result = await run_leader_chat(
            graph,
            message,
            selected_node_id=selected_node_id,
            run_new_nodes=run_new_nodes,
            progress=progress,
        )
    except DesignerGraphValidationError as exc:
        return None, str(exc), "BAD_REQUEST"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DesignerAdapter] graph chat failed: %s", exc)
        return None, str(exc), "INTERNAL_ERROR"

    next_graph = result.get("graph") or graph
    saved = _store.save_graph(next_graph) if result.get("changed") else graph
    run_payload = None
    run_ids = list(result.get("run_node_ids") or [])
    if run_ids:
        start_params: dict[str, Any] = {"graph_id": saved["graph_id"], "node_id": run_ids[0]}
        latest = _store.get_latest_run_for_graph(saved["graph_id"])
        if latest is not None:
            start_params["run_id"] = str(latest.get("run_id") or "")
        payload, error, code = _start_run(start_params)
        if error is None and payload is not None:
            run = await _executor.start_run(
                str(payload["run_id"]),
                on_update=_run_update_callback(request),
                on_graph_update=_graph_update_callback(request),
            )
            run_payload = dict(run)
            await _push_designer_event(
                request=request,
                event_type=EventType.DESIGNER_RUN_UPDATED.value,
                payload={"run": run_payload},
            )
        elif error:
            result["summary"] = f"{result.get('summary') or ''} ({error})".strip()
    return {
        "graph": dict(saved),
        "summary": result.get("summary") or "",
        "intent": result.get("intent") or "answer",
        "run_node_ids": run_ids,
        "run": run_payload,
    }, None, None


async def _bootstrap_graph_with_supervisor(
    params: dict[str, Any],
    channel_id: str,
    on_progress: Any | None = None,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Enter: seed cast → Brief/Manager → Storyboard/Manager → Graph/Manager.

    Never runs AsyncOpenAI inside ``asyncio.to_thread`` / ``asyncio.run``.
    """
    from jiuwenswarm.server.runtime.designer.composer import detect_scenario
    from jiuwenswarm.server.runtime.designer.model_tools import llm_available
    from jiuwenswarm.server.runtime.designer.script_analysis import analyze_creative_brief
    from jiuwenswarm.server.runtime.designer.user_references import (
        analysis_prompt_with_references,
        normalize_user_references,
    )

    prompt = str(params.get("prompt") or "").strip() or "根据参考素材创作"
    scenario_raw = params.get("scenario")
    scenario = (
        str(scenario_raw).strip().lower()
        if isinstance(scenario_raw, str) and scenario_raw.strip()
        else None
    )
    raw_references = params.get("references")
    if raw_references is None:
        raw_references = params.get("user_references")
    analysis: dict[str, Any] | None = None
    try:
        user_refs_preview = (
            normalize_user_references(raw_references, dest_dir=None)
            if isinstance(raw_references, list) and raw_references
            else []
        )
    except Exception:  # noqa: BLE001
        user_refs_preview = []
    analysis_prompt = analysis_prompt_with_references(prompt, user_refs_preview)
    detected = scenario or detect_scenario(analysis_prompt)

    if detected == "video" and llm_available():
        if callable(on_progress):
            on_progress("thinking", "Supervisor · Extracting cast and scenes (LLM)")
        try:
            analysis = await analyze_creative_brief(
                analysis_prompt,
                use_llm=True,
                timeout_sec=120.0,
                reference_images=[
                    str(item.get("path") or "")
                    for item in user_refs_preview
                    if str(item.get("kind") or "") == "image"
                    and str(item.get("path") or "").strip()
                ],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Async LLM analysis failed: %s", exc, exc_info=True)
            analysis = None
        if isinstance(analysis, dict) and str(analysis.get("source") or "") == "llm":
            if callable(on_progress):
                n = len(analysis.get("characters") or [])
                on_progress(
                    "stage",
                    f"Supervisor · LLM cast locked ({n} characters)",
                )
        else:
            if callable(on_progress):
                on_progress(
                    "thinking",
                    "Supervisor · Analysis soft-failed; Manager chain will re-author via LLM",
                )
            if isinstance(analysis, dict) and analysis.get("llm_pending"):
                pass
            else:
                analysis = None

    payload, error, code = await asyncio.to_thread(
        _bootstrap_graph,
        params,
        channel_id,
        analysis,
        on_progress,
    )
    if error is not None or not isinstance(payload, dict):
        return payload, error, code
    graph = payload.get("graph")
    if not isinstance(graph, dict):
        return payload, error, code

    if not llm_available():
        return payload, None, None
    if str((graph.get("metadata") or {}).get("scenario") or "") != "video":
        return payload, None, None

    from jiuwenswarm.server.runtime.designer.orchestration import ManagerAgent, SupervisorAgent
    from jiuwenswarm.server.runtime.designer.smart_graph import apply_runtime_delegate

    optimize_for = str(
        params.get("optimize_for")
        or (graph.get("metadata") or {}).get("optimize_for")
        or "quality"
    )
    try:
        if callable(on_progress):
            on_progress("thinking", "Supervisor · Authoring brief (LLM)")
        await SupervisorAgent().author_creative_brief(graph, use_llm=True)
        if callable(on_progress):
            on_progress("thinking", "Manager · Reviewing / approving brief")
        await ManagerAgent().review_brief(graph, use_llm=True)
        if callable(on_progress):
            on_progress("thinking", "Supervisor · Designing storyboard (LLM)")
        await SupervisorAgent().author_storyboard(graph, use_llm=True)
        if callable(on_progress):
            on_progress("thinking", "Manager · Reviewing / approving storyboard")
        await ManagerAgent().review_storyboard(graph, use_llm=True)
        if callable(on_progress):
            on_progress(
                "tool_call",
                "Supervisor · Designing execution graph (LLM)",
                "design_execution_graph",
            )
        await SupervisorAgent().design_execution_graph(
            graph,
            use_llm=True,
            optimize_for=optimize_for,
        )
        if callable(on_progress):
            on_progress("thinking", "Manager · Validating / approving graph + locks")
        await ManagerAgent().validate_plan(graph, use_llm=True)
        graph = apply_runtime_delegate(graph)
        meta = dict(graph.get("metadata") or {})
        analysis_now = (
            meta.get("script_analysis")
            if isinstance(meta.get("script_analysis"), dict)
            else {}
        )
        cast_n = sum(
            1
            for n in (graph.get("nodes") or [])
            if str(n.get("id") or "").startswith("n_character")
        )
        analysis_source = str(analysis_now.get("source") or "")
        ack = meta.get("supervisor_graph_ack") if isinstance(meta.get("supervisor_graph_ack"), dict) else {}
        story_ack = (
            meta.get("supervisor_storyboard_ack")
            if isinstance(meta.get("supervisor_storyboard_ack"), dict)
            else {}
        )
        truly_llm = analysis_source == "llm" or str(ack.get("source") or "") == "llm" or (
            str(story_ack.get("source") or "") == "llm" and cast_n >= 1
        )
        meta["pending_llm_analysis"] = not truly_llm
        meta["pending_supervisor_graph"] = not truly_llm
        meta["supervisor_composed_on_bootstrap"] = truly_llm
        meta["script_analysis_mode"] = "llm" if truly_llm else (
            analysis_source or str(meta.get("script_analysis_mode") or "heuristic")
        )
        meta["agent_runtime_bootstrap"] = {
            "analysis": "analyze_creative_brief via call_model_tool",
            "orchestration": (
                "Supervisor brief → Manager approve → Supervisor storyboard → "
                "Manager approve → Supervisor graph → Manager validate/locks"
            ),
            "leaves": "NodeAgentHost / openjiuwen tools on Play",
        }
        meta["one_pass"] = False
        meta["enter_approval_chain"] = True
        graph["metadata"] = meta
        if callable(on_progress):
            on_progress(
                "stage",
                f"Manager · Approved — {cast_n} solo cards (mode={meta['script_analysis_mode']})",
            )
        saved = _store.save_graph(graph)
        payload = dict(payload)
        payload["graph"] = dict(saved)
        return payload, None, None
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "Supervisor bootstrap approval chain failed; keeping graph: %s",
            exc,
            exc_info=True,
        )
        meta = dict(graph.get("metadata") or {})
        meta["pending_supervisor_graph"] = False
        meta["supervisor_composed_on_bootstrap"] = True
        meta["enter_approval_chain"] = True
        graph["metadata"] = meta
        try:
            saved = _store.save_graph(graph)
            payload = dict(payload)
            payload["graph"] = dict(saved)
        except Exception:  # noqa: BLE001
            pass
        return payload, None, None


class DesignerAdapter(GatewayAdapter):
    """Designer execution graph adapter."""

    methods: frozenset[str] = frozenset(
        {
            ReqMethod.DESIGNER_GRAPH_GET.value,
            ReqMethod.DESIGNER_GRAPH_LIST.value,
            ReqMethod.DESIGNER_GRAPH_SAVE.value,
            ReqMethod.DESIGNER_GRAPH_BOOTSTRAP.value,
            ReqMethod.DESIGNER_GRAPH_PATCH.value,
            ReqMethod.DESIGNER_GRAPH_CHAT.value,
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
                # Enter: provisional graph, then Supervisor/Manager LLM approval chain.
                payload, error, code = await _bootstrap_graph_with_supervisor(
                    params,
                    request.channel_id,
                    _leader_progress_callback(request),
                )
            elif method == ReqMethod.DESIGNER_GRAPH_PATCH:
                payload, error, code = await asyncio.to_thread(_patch_graph, params)
            elif method == ReqMethod.DESIGNER_GRAPH_CHAT:
                payload, error, code = await _chat_graph(request, params)
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
                    run_out = dict(run)
                    warning = str(payload.get("warning") or "").strip()
                    warnings = [
                        str(x)
                        for x in (payload.get("warnings") or [])
                        if str(x).strip()
                    ]
                    if warning:
                        run_out["warning"] = warning
                        if warning not in warnings:
                            warnings = [warning, *warnings]
                    if warnings:
                        run_out["warnings"] = warnings[:8]
                    await _push_designer_event(
                        request=request,
                        event_type=EventType.DESIGNER_RUN_UPDATED.value,
                        payload={"run": run_out},
                    )
                    return _ok_response(
                        request,
                        {
                            "run": run_out,
                            "warning": warning or None,
                            "warnings": warnings,
                        },
                    )
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
            ReqMethod.DESIGNER_GRAPH_CHAT,
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
