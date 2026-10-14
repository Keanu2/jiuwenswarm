# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""DesignerAdapter: designer.graph.* / designer.run.* executed in AgentServer."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import portalocker

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse

from jiuwenswarm.common.schema.designer_graph import (
    CONFIG_DELEGATE_HANDLER,
    DesignerGraphValidationError,
    apply_graph_patch,
    new_graph_id,
    normalize_execution_graph,
    utc_now_ms,
)
from jiuwenswarm.common.schema.message import EventType, ReqMethod
from jiuwenswarm.common.utils import get_agent_root_dir, get_agent_sessions_dir
from jiuwenswarm.common.work_mode import (
    DEFAULT_WEB_WORK_MODE,
    DESIGN_WORK_MODE,
    is_default_project_id,
)
from jiuwenswarm.server.runtime.design import RunCoordinator
from jiuwenswarm.server.runtime.design.context import get_graph_coordinator
from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore
from jiuwenswarm.server.runtime.designer.model_tools import DesignerLlmError
from jiuwenswarm.server.runtime.gateway_adapter.base import (
    GatewayAdapter,
    build_error_response,
)
from jiuwenswarm.server.runtime.session import project_store
from jiuwenswarm.server.runtime.session.work_mode import resolve_request_work_mode

logger = logging.getLogger(__name__)

_store = DesignerGraphStore()


def _coordinator_for(graph_id: str) -> RunCoordinator:
    """One RunCoordinator per graph (its run_id lives as long as the graph).

    Shared process-wide with DesignTeamBridge and the capability provider so
    every entry point observes the same in-flight trigger.
    """
    return get_graph_coordinator(_store, graph_id)


def _design_run_active(graph_id: str) -> bool:
    return _coordinator_for(graph_id).is_active()


def _workspace_receipt_path(token: str) -> Path:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return get_agent_root_dir() / "designer" / "workspace_receipts" / f"{digest}.json"


@asynccontextmanager
async def _workspace_create_lock(token: str):
    """Serialize one creation token across coroutines and AgentServer processes."""
    receipt_path = _workspace_receipt_path(token)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = receipt_path.with_suffix(".lock")
    handle = lock_path.open("a+", encoding="utf-8")
    acquired = False
    try:
        while not acquired:
            try:
                portalocker.lock(
                    handle,
                    portalocker.LOCK_EX | portalocker.LOCK_NB,
                )
                acquired = True
            except portalocker.exceptions.LockException:
                await asyncio.sleep(0.05)
        yield
    finally:
        if acquired:
            portalocker.unlock(handle)
        handle.close()


def _read_workspace_receipt(
    token: str,
    signature: str,
) -> tuple[dict[str, Any] | None, str | None, str | None] | None:
    path = _workspace_receipt_path(token)
    if not path.is_file():
        return None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(receipt, dict):
        return None
    if str(receipt.get("signature") or "") != signature:
        return None, "create_token was already used with different parameters", "CONFLICT"
    session_id = str(receipt.get("session_id") or "")
    if not session_id:
        # Compatibility for receipts written by the project-keyed implementation.
        project_id = str(receipt.get("project_id") or "")
        if not project_id:
            return None
        current, error, _ = _get_design_workspace({"project_id": project_id})
    else:
        current, error, _ = _get_design_workspace({"session_id": session_id})
    if error is not None or current is None:
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return current, None, None


def _write_workspace_receipt(
    token: str,
    signature: str,
    payload: dict[str, Any],
) -> None:
    path = _workspace_receipt_path(token)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    session = payload.get("session")
    session_id = str(session.get("session_id") or "") if isinstance(session, dict) else ""
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(
            {"signature": signature, "session_id": session_id},
            handle,
            ensure_ascii=False,
        )
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


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
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        if config.get("user_replaced_output"):
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
    project_id = str(graph.get("project_id") or "")
    project_dir: str | None = None
    project_name: str | None = None
    if project_id:
        project = project_store.get_project_by_id(project_id, cache_bust=True)
        if project is not None:
            project_dir = project.project_dir
            project_name = project.name
    return {
        "graph_id": graph.get("graph_id"),
        "session_id": str((graph.get("metadata") or {}).get("session_id") or ""),
        "project_id": project_id,
        "project_dir": project_dir,
        "project_name": project_name,
        "title": graph.get("title") or graph.get("graph_id"),
        "updated_at": graph.get("updated_at"),
        "run_id": run.get("run_id") if run else None,
        "run_status": run.get("status") if run else None,
        "has_video": has_video,
        "clip_label": clip_label,
    }


def _design_title(prompt: str) -> str:
    from jiuwenswarm.server.runtime.session.project_store import sanitize_project_dir_name

    return sanitize_project_dir_name(prompt, max_len=80)


_DESIGN_PLACEHOLDER_TITLE = "设计项目"


def _stamp_canvas_title(graph: dict[str, Any], prompt: str) -> str:
    """Set the canvas title from the model's short name, never the raw prompt."""
    from jiuwenswarm.server.runtime.designer.node_labels import usable_story_title

    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    name = usable_story_title(analysis.get("story_name"), prompt)
    if not name:
        current = "".join(ch for ch in str(graph.get("title") or "") if ch.isalnum())
        reference = "".join(ch for ch in str(prompt or "") if ch.isalnum())
        # Creation stamps a sanitized copy of the prompt as the title (quotes
        # and punctuation dropped); any alphanumeric prefix of the prompt is
        # that echo, not a user-chosen name.
        if not (current and reference.startswith(current)):
            name = usable_story_title(graph.get("title"), prompt)
    graph["title"] = name or _DESIGN_PLACEHOLDER_TITLE
    return name


_retitled_design_projects: set[str] = set()


def _replacement_display_title(current: str, prompt: str, story_name: str) -> str:
    """Short model title when the current label is still the user prompt."""
    from jiuwenswarm.server.runtime.designer.node_labels import usable_story_title

    reference = prompt or current
    summary = usable_story_title(story_name, reference)
    if not summary:
        return ""
    current_flat = " ".join(str(current or "").split())
    if usable_story_title(current, reference) and len(current_flat) <= 40:
        return ""
    if prompt or len(current_flat) > 40:
        return summary
    return ""


def _graph_story_prompt(graph: dict[str, Any]) -> tuple[str, str]:
    meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    return str(analysis.get("story_name") or ""), str(graph.get("description") or "")


def _graph_session_id(graph: dict[str, Any]) -> str:
    metadata = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
    return str((metadata or {}).get("session_id") or "").strip()


def _design_sessions(project_id: str) -> list[dict[str, Any]]:
    from jiuwenswarm.server.runtime.session.session_metadata import (
        collect_all_sessions_metadata,
    )

    return [
        item
        for item in collect_all_sessions_metadata()
        if str(item.get("project_id") or "") == project_id
        and str(item.get("work_mode") or "") == DESIGN_WORK_MODE
    ]


def _pair_design_canvases(
    sessions: list[dict[str, Any]],
    graphs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bind each design session to one canvas.

    A project may hold many sessions. Chat history stays on the session, and
    the canvas is the graph whose metadata.session_id matches it. Graphs saved
    before sessions were explicit fill sessions that do not yet have one.
    """
    ordered = sorted(sessions, key=lambda item: float(item.get("created_at") or 0))
    owned: dict[str, list[dict[str, Any]]] = {}
    unmatched: list[dict[str, Any]] = []
    for graph in graphs:
        session_id = _graph_session_id(graph)
        if session_id:
            owned.setdefault(session_id, []).append(graph)
        else:
            unmatched.append(graph)
    unmatched.sort(key=lambda item: int(item.get("created_at") or item.get("updated_at") or 0))
    pairs: list[dict[str, Any]] = []
    for session in ordered:
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            continue
        candidates = list(owned.get(session_id) or [])
        if not candidates and unmatched:
            candidates = [unmatched.pop(0)]
        if not candidates:
            continue
        graph = max(candidates, key=lambda item: int(item.get("updated_at") or 0))
        pairs.append({"session": session, "graph": graph})
    return pairs


def sync_design_display_name(project: Any) -> Any:
    """Rename a design project and its canvases from the stored model title."""
    project_id = str(getattr(project, "project_id", "") or "")
    if not project_id or project_id in _retitled_design_projects:
        return project
    if str(getattr(project, "work_mode", "") or "") != DESIGN_WORK_MODE:
        return project
    try:
        pairs = _pair_design_canvases(
            _design_sessions(project_id),
            _store.list_graphs_for_project(project_id),
        )
    except Exception:
        logger.debug("Skip design title sync for %s", project_id, exc_info=True)
        return project
    _retitled_design_projects.add(project_id)
    if not pairs:
        return project
    from jiuwenswarm.server.runtime.session.session_metadata import update_session_metadata

    ordered = sorted(pairs, key=lambda item: int(item["graph"].get("updated_at") or 0))
    project_summary = ""
    for pair in ordered:
        graph = pair["graph"]
        story_name, prompt = _graph_story_prompt(graph)
        if not project_summary:
            project_summary = _replacement_display_title(
                str(getattr(project, "name", "") or ""),
                prompt,
                story_name,
            )
        session = pair["session"]
        session_id = str(session.get("session_id") or "")
        session_title = _replacement_display_title(
            str(session.get("title") or ""),
            prompt,
            story_name,
        )
        if session_id and session_title and session_title != str(session.get("title") or ""):
            update_session_metadata(
                session_id=session_id,
                title=session_title,
                touch_last_message_at=False,
                sync_write=True,
            )
    if project_summary:
        renamed_name = _design_title(project_summary) or project_summary
        if renamed_name and renamed_name != project.name:
            try:
                renamed = project_store.rename_project(project_id, renamed_name)
                if renamed is not None:
                    return renamed
            except (project_store.ProjectNameConflict, ValueError):
                logger.debug(
                    "Design project %s kept its name; %r is taken",
                    project_id,
                    renamed_name,
                    exc_info=True,
                )
    return project


def sync_design_display_names(projects: list[Any]) -> list[Any]:
    return [sync_design_display_name(project) for project in projects]


def _design_workspace_messages(session_id: str) -> list[dict[str, Any]]:
    from jiuwenswarm.server.runtime.session.session_history import load_history_records

    messages: list[dict[str, Any]] = []
    for record in load_history_records(session_id):
        role = str(record.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        content = str(record.get("content") or "")
        if not content:
            continue
        event_type = str(record.get("event_type") or "")
        messages.append(
            {
                "id": str(record.get("id") or uuid.uuid4()),
                "role": role,
                "content": content,
                "kind": str(record.get("design_kind") or (
                    "user" if role == "user" else "chat_ack"
                )),
                "createdAt": int(float(record.get("timestamp") or 0) * 1000),
                **(
                    {"references": record["references"]}
                    if isinstance(record.get("references"), list)
                    else {}
                ),
                **({"graph_id": record["graph_id"]} if record.get("graph_id") else {}),
            }
        )
    return messages




def _get_design_workspace(
    params: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Load one design task workspace (session + graph) by session id.

    The design task unit is the session (one session = one graph = one
    canvas); ``project_id`` is only an optional grouping directory, exactly
    like work-mode conversations. ``project_id``-only lookups (legacy) fall
    back to the project's most recent design session.
    """
    from jiuwenswarm.server.runtime.session.session_metadata import (
        collect_all_sessions_metadata,
    )

    requested_session_id = str(params.get("session_id") or "").strip()
    project_id = str(params.get("project_id") or "").strip()
    if not requested_session_id and not project_id:
        return None, "session_id is required", "BAD_REQUEST"

    project = None
    if project_id:
        project = project_store.get_project_by_id(project_id, cache_bust=True)
        if project is None or project.hidden or project.work_mode != DESIGN_WORK_MODE:
            return None, "design project not found", "NOT_FOUND"

    sessions = [
        item
        for item in collect_all_sessions_metadata()
        if str(item.get("work_mode") or "") == DESIGN_WORK_MODE
        and (
            not project_id
            or str(item.get("project_id") or "") == project_id
        )
    ]
    if requested_session_id:
        session = next(
            (item for item in sessions if str(item.get("session_id")) == requested_session_id),
            None,
        )
        if session is None:
            return None, "design session not found", "NOT_FOUND"
    elif sessions:
        sessions.sort(key=lambda item: float(item.get("created_at") or 0))
        session = sessions[-1]
    else:
        return None, "design workspace has no sessions", "NOT_FOUND"
    session_id = str(session.get("session_id") or "")

    if project is None:
        session_project_id = str(session.get("project_id") or "")
        if session_project_id:
            project = project_store.get_project_by_id(session_project_id, cache_bust=True)

    graphs = _store.list_graphs_for_project(str(session.get("project_id") or "")) if str(
        session.get("project_id") or ""
    ) else [
        g
        for g in _store.list_graphs()
        if str((g.get("metadata") or {}).get("session_id") or "") == session_id
    ]
    graph = next(
        (g for g in graphs if str((g.get("metadata") or {}).get("session_id") or "") == session_id),
        None,
    )
    if graph is None and graphs:
        graphs.sort(key=lambda g: float(g.get("updated_at") or 0))
        graph = graphs[-1]
    if graph is None:
        return None, "design workspace has no graph", "NOT_FOUND"
    # Heal canvases whose title is still the raw creation prompt: the canvas
    # header is sized for the model's short story name, never the prompt.
    previous_title = str(graph.get("title") or "")
    _stamp_canvas_title(graph, str(graph.get("description") or ""))
    if str(graph.get("title") or "") != previous_title:
        graph = dict(_store.save_graph(graph))
    run = _store.get_latest_run_for_graph(str(graph.get("graph_id") or ""))
    return {
        "project": {
            "project_id": (project.project_id if project is not None else ""),
            "name": (project.name if project is not None else ""),
            "project_dir": (project.project_dir if project is not None else ""),
            "work_mode": DESIGN_WORK_MODE,
        },
        "session": {
            "session_id": session_id,
            "title": session.get("title"),
            "project_id": (project.project_id if project is not None else ""),
            "project_dir": (project.project_dir if project is not None else ""),
            "work_mode": DESIGN_WORK_MODE,
        },
        "graph": dict(hydrate_graph_node_outputs(graph, run)),
        "messages": _design_workspace_messages(session_id),
    }, None, None


def _design_team_workspace(session_id: str) -> Path:
    """The owning team's team-workspace for one design task.

    Design task artifacts (graph.json, runs, references, generated media)
    live under the team workspace unless the user picked a project directory.
    Resolves the real team name from session metadata (see graph_store).
    """
    from jiuwenswarm.server.runtime.designer.graph_store import design_team_workspace

    return design_team_workspace(session_id)


def _resolve_design_task_dir(params: dict[str, Any], session_id: str) -> tuple[str, str, str, str]:
    """Resolve ``(project_id, project_name, refs_dir, leader_project_dir)``.

    Work-mode semantics: a task is a session, never a directory. Directories
    (projects) exist only when the user picked one in the landing selector —
    artifacts then stay under that project. Without a project everything lands
    in the owning team's team-workspace (same root the team rails use).
    """
    requested_project_id = str(params.get("project_id") or "").strip()
    if requested_project_id and not is_default_project_id(requested_project_id):
        project = project_store.get_project_by_id(requested_project_id, cache_bust=True)
        if project is None or project.hidden or project.work_mode != DESIGN_WORK_MODE:
            raise ValueError("design project not found")
        project_dir = str(project.project_dir or "")
        refs_dir = str(Path(project_dir) / ".designer" / "refs")
        return project.project_id, project.name, refs_dir, project_dir
    team_ws = _design_team_workspace(session_id)
    refs_dir = team_ws / "designer" / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    return "", "", str(refs_dir), str(team_ws)


async def _create_design_workspace_once(
    request: AgentRequest,
    params: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Create one design task (session + empty graph); no leader round here.

    The task unit is the session — no project/directory is created (the
    work-mode conversation semantics); an explicitly selected ``project_id``
    only groups the task under that directory. The first user message arrives
    as a standard ``chat.send(mode='team.design')`` — the whole leader turn
    (streaming, interact, history persistence) rides the standard team
    pipeline; the canvas gets its graph via the tools' designer.* pushes.
    """
    from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata
    from jiuwenswarm.server.runtime.designer.user_references import (
        UserReferenceError,
        attach_user_references_to_graph,
        normalize_user_references,
    )

    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        return None, "prompt is required", "BAD_REQUEST"
    model_name = str(params.get("model_name") or "").strip()
    title = _design_title(prompt)
    session_id = f"design_{uuid.uuid4().hex}"
    graph_id = ""
    committed = False
    try:
        try:
            project_id, project_name, refs_dir, leader_project_dir = _resolve_design_task_dir(
                params, session_id
            )
        except ValueError as exc:
            return None, str(exc), "NOT_FOUND"
        init_session_metadata(
            session_id=session_id,
            channel_id=request.channel_id or "web",
            user_id=str(request.user_id or ""),
            title=title,
            mode="designer",
            project_dir=leader_project_dir if project_id else "",
            project_id=project_id,
            persist_session=True,
            model=model_name,
            work_mode=DESIGN_WORK_MODE,
            # The standard team path resolves the AgentGroup from session
            # metadata — bind 'designer' here so every chat.send inherits it.
            agent_group_name="designer",
        )

        graph = normalize_execution_graph(
            {
                "schema_version": "designer-execution-graph.v1",
                "graph_id": new_graph_id(),
                "project_id": project_id,
                "title": title,
                "description": prompt[:200],
                "nodes": [],
                "edges": [],
                "metadata": {
                    "session_id": session_id,
                    "work_mode": DESIGN_WORK_MODE,
                    **({"model_name": model_name} if model_name else {}),
                },
            }
        )
        _stamp_canvas_title(graph, prompt)
        graph = dict(_store.save_graph(graph))
        graph_id = str(graph["graph_id"])

        raw_references = params.get("references")
        user_refs: list[dict[str, Any]] = []
        if isinstance(raw_references, list) and raw_references:
            try:
                user_refs = normalize_user_references(raw_references, dest_dir=Path(refs_dir))
            except UserReferenceError as exc:
                return None, str(exc), exc.code
            if user_refs:
                graph = attach_user_references_to_graph(graph, user_refs)
                graph = dict(_store.save_graph(graph))

        # No history write and no leader round here: the first user message is
        # a standard chat.send, which persists the user record and streams the
        # leader reply through the standard team pipeline.

        committed = True
        return {
            "project": {
                "project_id": project_id,
                "name": project_name,
                "project_dir": leader_project_dir if project_id else "",
                "work_mode": DESIGN_WORK_MODE,
            },
            "session": {
                "session_id": session_id,
                "title": title,
                "project_id": project_id,
                "project_dir": leader_project_dir if project_id else "",
                "work_mode": DESIGN_WORK_MODE,
            },
            "graph": graph,
            "messages": _design_workspace_messages(session_id),
        }, None, None
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to create design workspace")
        return None, str(exc), "INTERNAL_ERROR"
    finally:
        # Only the task-local resources (session + graph) roll back; a user
        # project directory is never touched.
        if not committed:
            if graph_id:
                _store.delete_graph(graph_id)
            if session_id:
                shutil.rmtree(get_agent_sessions_dir() / session_id, ignore_errors=True)


async def _create_design_workspace(
    request: AgentRequest,
    params: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    token = str(params.get("create_token") or "").strip()
    prompt = str(params.get("prompt") or "").strip()
    if not token:
        return None, "create_token is required", "BAD_REQUEST"
    signature = hashlib.sha256(
        json.dumps(
            {
                "prompt": prompt,
                "references": params.get("references") or [],
                "model_name": str(params.get("model_name") or "").strip(),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    async with _workspace_create_lock(token):
        receipt = _read_workspace_receipt(token, signature)
        if receipt is not None:
            return receipt
        result = await _create_design_workspace_once(request, params)
        payload, error, _ = result
        if payload is None or error is not None:
            return result
        try:
            _write_workspace_receipt(token, signature, payload)
        except OSError as exc:
            # The shared design project must survive; roll back only the
            # task-local resources (session + graph).
            session = payload.get("session")
            graph = payload.get("graph")
            if isinstance(graph, dict) and graph.get("graph_id"):
                _store.delete_graph(str(graph["graph_id"]))
            if isinstance(session, dict) and session.get("session_id"):
                shutil.rmtree(
                    get_agent_sessions_dir() / str(session["session_id"]), ignore_errors=True
                )
            logger.exception("Failed to persist Design idempotency receipt")
            return None, f"failed to persist workspace receipt: {exc}", "INTERNAL_ERROR"
        return result


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
    # Whole-graph bodies blow the WebSocket send budget once the store grows;
    # callers load the one graph they need through designer.graph.get.
    payload: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    latest_runs = _store.latest_runs_by_graph()
    project_cache: dict[str, tuple[str, str] | None] = {}

    def _project_dir_name(pid: str) -> tuple[str, str] | None:
        if pid not in project_cache:
            project = project_store.get_project_by_id(pid, cache_bust=True) if pid else None
            project_cache[pid] = (
                (project.project_dir, project.name) if project is not None else None
            )
        return project_cache[pid]

    for graph in graphs:
        item = dict(graph)
        run = latest_runs.get(str(item.get("graph_id") or ""))
        summaries.append(_summarize_graph(item, run))
        # Do not hydrate outputs or ship full node bodies here — Agent WS
        # send budget is 6MB; a page of Designer graphs with images exceeds it.
        project_id = str(item.get("project_id") or "")
        resolved = _project_dir_name(project_id)
        project_dir = resolved[0] if resolved else None
        project_name = resolved[1] if resolved else None
        payload.append(
            {
                "graph_id": item.get("graph_id"),
                "project_id": project_id,
                "project_dir": project_dir,
                "project_name": project_name,
                "title": item.get("title"),
                "updated_at": item.get("updated_at"),
                "schema_version": item.get("schema_version"),
            }
        )
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
        graph = normalize_execution_graph(raw_graph)
        try:
            from jiuwenswarm.server.runtime.designer.orchestration import Director

            Director().onboard_user_added_nodes(graph)
        except Exception:
            logger.debug("Director user-node onboard on save failed", exc_info=True)
        saved = _store.save_graph(graph)
        _persist_uploaded_outputs(saved)
    except DesignerGraphValidationError as exc:
        return None, str(exc), "BAD_REQUEST"
    return {"graph": dict(saved)}, None, None


def _persist_uploaded_outputs(graph: dict[str, Any]) -> None:
    """Keep the latest run aligned with a still the user uploaded onto a node."""
    from jiuwenswarm.server.runtime.designer.handlers.common import (
        apply_uploaded_outputs_to_run,
    )

    run = _store.get_latest_run_for_graph(str(graph.get("graph_id") or ""))
    if run is None or not apply_uploaded_outputs_to_run(run, graph):
        return
    run["updated_at"] = utc_now_ms()
    _store.save_run(run)


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
            from jiuwenswarm.server.runtime.designer.orchestration import Director

            Director().onboard_user_added_nodes(next_graph)
        except Exception:
            logger.debug("Director user-node onboard on patch failed", exc_info=True)
        saved = _store.save_graph(next_graph)
        _persist_uploaded_outputs(saved)
    except DesignerGraphValidationError as exc:
        return None, str(exc), "BAD_REQUEST"
    return {"graph": dict(saved)}, None, None


def _media_capabilities(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Model limits and canvas media settings for the settings menu.

    Video figures come from the configured model's capacity entry, image tiers
    from the image capacity table; ``settings`` echoes the graph's stored
    canvas-wide video/image settings when a graph is requested. Per-node video
    durations are not derived on this line (no storyboard timeline), so the
    menu falls back to each node's ``duration_sec`` and the model default.
    """
    from jiuwenswarm.server.runtime.designer.media_generation import slot_settings
    from jiuwenswarm.server.runtime.designer.pipeline.image_capacity import (
        DEFAULT_IMAGE_RATIO,
        IMAGE_RATIOS,
        active_image_capacity,
    )
    from jiuwenswarm.server.runtime.designer.pipeline.model_capacity import (
        active_video_capacity,
    )

    video_slot = slot_settings("video")
    video_cap = active_video_capacity()
    image_slot = slot_settings("image")
    image_cap = active_image_capacity()
    caps: dict[str, Any] = {
        "video": {
            "configured_model": video_slot.model,
            "backend": video_slot.backend,
            "model_id": video_cap.model_id,
            "provider": video_cap.provider,
            "ratios": list(video_cap.ratios),
            "resolutions": list(video_cap.resolutions),
            "default_ratio": "16:9" if "16:9" in video_cap.ratios else video_cap.ratios[0],
            "default_resolution": video_cap.default_resolution,
            "min_sec": video_cap.min_sec,
            "max_sec": video_cap.max_sec,
            "default_sec": video_cap.default_sec,
        },
        "image": {
            "configured_model": image_slot.model,
            "backend": image_slot.backend,
            "ratios": list(IMAGE_RATIOS),
            "resolutions": list(image_cap.resolutions),
            "default_ratio": DEFAULT_IMAGE_RATIO,
            "default_resolution": image_cap.default_resolution,
        },
    }
    graph_id = str(params.get("graph_id") or "").strip()
    if graph_id:
        graph = _store.get_graph(graph_id)
        if graph is None:
            return None, "graph not found", "NOT_FOUND"
        metadata = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
        caps["settings"] = {
            "video": dict(metadata.get("video_settings") or {}),
            "image": dict(metadata.get("image_settings") or {}),
        }
    return caps, None, None



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


def _launch_run_trigger(
    coordinator: RunCoordinator,
    payload: dict[str, Any],
    on_update: Any,
) -> None:
    """Run the trigger on a background task; failures land on the run record.

    The trigger's own error handling already writes a failed run and pushes it
    through ``on_update``; a task-level exception (outside the coordinator)
    is logged — the record keeps its last persisted state.
    """

    async def _run() -> None:
        try:
            await coordinator.start(
                payload["graph"],
                node_ids=payload["node_ids"],
                cascade=bool(payload.get("cascade")),
                on_update=on_update,
            )
        except Exception:  # noqa: BLE001 — background task must never raise
            logger.exception(
                "[DesignerAdapter] run trigger crashed: graph_id=%s",
                payload.get("graph_id"),
            )

    asyncio.create_task(_run())


def _initial_running_run(payload: dict[str, Any]) -> dict[str, Any]:
    """The 'running' snapshot the run.start response answers with.

    Mirrors the first ``on_update`` the coordinator will emit once the lock is
    taken (status running, the trigger's nodes back to pending) without
    racing the background task: read the minted run record, or synthesize a
    draft-shaped one when the run_id is brand new.
    """
    graph = payload["graph"]
    graph_id = str(graph.get("graph_id") or "")
    meta = dict(graph.get("metadata") or {})
    run_id = str(meta.get("team_design_run_id") or "").strip()
    run = _store.get_run(run_id) if run_id else None
    if run is not None:
        return dict(run)
    node_ids = payload.get("node_ids") or []
    return {
        "schema_version": "designer-execution-run.v1",
        "run_id": run_id,
        "graph_id": graph_id,
        "project_id": str(graph.get("project_id") or ""),
        "status": "running",
        "node_states": {
            node_id: {
                "status": "pending",
                "started_at": None,
                "completed_at": None,
            }
            for node_id in (node_ids or [])
        },
        "current_node_ids": [],
        "created_at": utc_now_ms(),
        "updated_at": utc_now_ms(),
    }


def _start_run(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Resolve the run target (graph + optional node); no execution here.

    team.design semantics: ``node_id`` present → single-node trigger (only
    that node re-executes, downstream frozen; ``cascade=true`` turns it into
    "rerun from X" — X plus its downstream closure re-execute with fresh
    outputs, upstream stays frozen at current values); absent → full-graph
    trigger (unfrozen nodes execute in topological order). The run_id is the
    graph's lifelong identity, minted by the coordinator on first trigger.
    """
    graph_id = str(params.get("graph_id") or "").strip()
    run_id = str(params.get("run_id") or "").strip()
    node_id = str(params.get("node_id") or "").strip()
    cascade = bool(params.get("cascade"))
    graph = _store.get_graph(graph_id) if graph_id else None
    if graph is None and run_id:
        existing = _store.get_run(run_id)
        if existing is None:
            return None, "run not found", "NOT_FOUND"
        graph = _store.get_graph(str(existing.get("graph_id") or ""))
    if graph is None:
        return None, "graph_id or run_id is required", "BAD_REQUEST"
    node_ids: list[str] | None = None
    if node_id:
        if not any(str(n.get("id") or "") == node_id for n in graph.get("nodes") or []):
            return None, "node not found", "NOT_FOUND"
        node_ids = [node_id]
    elif cascade:
        return None, "cascade requires node_id", "BAD_REQUEST"
    return {
        "graph": dict(graph),
        "graph_id": graph["graph_id"],
        "node_ids": node_ids,
        "cascade": cascade,
    }, None, None


def _pause_run(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    run_id = str(params.get("run_id") or "").strip()
    if not run_id:
        return None, "run_id is required", "BAD_REQUEST"
    return (
        None,
        "team.design runs do not support pause; cancel the trigger and re-run",
        "NOT_IMPLEMENTED",
    )


def _cancel_run(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    run_id = str(params.get("run_id") or "").strip()
    if not run_id:
        return None, "run_id is required", "BAD_REQUEST"
    run = _store.get_run(run_id)
    if run is None:
        return None, "run not found", "NOT_FOUND"
    graph_id = str(run.get("graph_id") or "")
    if graph_id:
        _coordinator_for(graph_id).cancel(run_id)
    return {"run": dict(run)}, None, None


def _choose_output(params: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    run_id = str(params.get("run_id") or "").strip()
    node_id = str(params.get("node_id") or "").strip()
    choice = str(params.get("choice") or "").strip()
    if not run_id:
        return None, "run_id is required", "BAD_REQUEST"
    if not node_id:
        return None, "node_id is required", "BAD_REQUEST"
    run = _store.get_run(run_id)
    if run is None:
        return None, "run or node not found", "NOT_FOUND"
    graph_id = str(run.get("graph_id") or "")
    if not graph_id:
        return None, "run or node not found", "NOT_FOUND"
    try:
        updated = _coordinator_for(graph_id).choose_output(run_id, node_id, choice)
    except ValueError as exc:
        return None, str(exc), "BAD_REQUEST"
    return {"run": dict(updated)}, None, None










class DesignerAdapter(GatewayAdapter):
    """Designer execution graph adapter."""

    # designer.graph.bootstrap / designer.graph.chat are RETIRED (2026-10-12,
    # F01 §8): all leader conversation — including the first message — rides
    # the standard chat.send(mode='team.design') pipeline.
    methods: frozenset[str] = frozenset(
        {
            ReqMethod.DESIGNER_WORKSPACE_CREATE.value,
            ReqMethod.DESIGNER_WORKSPACE_GET.value,
            ReqMethod.DESIGNER_GRAPH_GET.value,
            ReqMethod.DESIGNER_GRAPH_LIST.value,
            ReqMethod.DESIGNER_GRAPH_SAVE.value,
            ReqMethod.DESIGNER_GRAPH_PATCH.value,
            ReqMethod.DESIGNER_MEDIA_CAPABILITIES.value,
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
            if method == ReqMethod.DESIGNER_WORKSPACE_CREATE:
                payload, error, code = await _create_design_workspace(request, params)
            elif method == ReqMethod.DESIGNER_WORKSPACE_GET:
                payload, error, code = await asyncio.to_thread(
                    _get_design_workspace, params
                )
            elif method == ReqMethod.DESIGNER_GRAPH_GET:
                payload, error, code = await asyncio.to_thread(_get_graph, params)
            elif method == ReqMethod.DESIGNER_GRAPH_LIST:
                payload, error, code = await asyncio.to_thread(_list_graphs, params)
            elif method == ReqMethod.DESIGNER_GRAPH_SAVE:
                payload, error, code = await asyncio.to_thread(_save_graph, params)
            elif method == ReqMethod.DESIGNER_GRAPH_PATCH:
                payload, error, code = await asyncio.to_thread(_patch_graph, params)
            elif method == ReqMethod.DESIGNER_MEDIA_CAPABILITIES:
                payload, error, code = await asyncio.to_thread(_media_capabilities, params)
            elif method == ReqMethod.DESIGNER_RUN_GET:
                payload, error, code = await asyncio.to_thread(_get_run, params)
            elif method == ReqMethod.DESIGNER_RUN_START:
                payload, error, code = await asyncio.to_thread(_start_run, params)
                if error is None and payload is not None:
                    coordinator = _coordinator_for(str(payload["graph_id"]))
                    if coordinator.is_active():
                        return build_error_response(
                            request,
                            "a trigger is already executing on this graph; wait for "
                            "it to finish (the canvas shows its progress)",
                            code="CONFLICT",
                        )
                    on_update = _run_update_callback(request)
                    # Accept-and-trigger: schedule the run and answer with the
                    # initial 'running' record at once. A real run spans many
                    # worker LLM turns — awaiting it here would blow both the
                    # gateway wait and the web client's 15s request timeout
                    # (the canvas follows progress via the designer.run.updated
                    # / designer.node.updated pushes and designer.run.get).
                    _launch_run_trigger(coordinator, payload, on_update)
                    run_out = _initial_running_run(payload)
                    await _push_designer_event(
                        request=request,
                        event_type=EventType.DESIGNER_RUN_UPDATED.value,
                        payload={"run": dict(run_out)},
                    )
                    return _ok_response(request, {"run": run_out})
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
        except DesignerLlmError as exc:
            logger.warning("[DesignerAdapter] %s LLM failed: %s", method, exc)
            return build_error_response(request, exc.user_message, code=exc.code)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DesignerAdapter] %s failed: %s", method, exc)
            return build_error_response(request, str(exc), code="INTERNAL_ERROR")

        if error is not None:
            return build_error_response(request, error, code=code or "BAD_REQUEST")
        if isinstance(payload, dict) and payload.get("graph") is not None and method in {
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
