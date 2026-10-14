# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistence for Designer execution graphs and run state.

Storage layout (2026-10-12, team-workspace co-location):

- Default (no project picked): each task's artifacts live with its owning
  team under ``<workspace>/.agent_teams/jiuwen_team_<session_id>/team-workspace/designer/``:
  - ``graphs/{graph_id}.json`` — domain graph (designer-execution-graph.v1)
  - ``runs/{run_id}.json`` — run state (designer-execution-run.v1)
- Project-picked tasks keep the same layout under that project's team workspace.
- The legacy central ``get_agent_root_dir()/designer/{graphs,runs}`` remains
  readable (older tasks) and is the fallback for graphs without a session id.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from jiuwenswarm.common.schema.designer_graph import (
    DesignerExecutionGraph,
    DesignerExecutionRun,
    DesignerGraphValidationError,
    normalize_execution_graph,
    normalize_execution_run,
    preserve_expanded_shot_nodes,
    preserve_nodes_added_since,
    preserve_node_output_refs,
    utc_now_ms,
)
from jiuwenswarm.common.utils import get_agent_root_dir, get_workspace_dir

logger = logging.getLogger(__name__)

_STORE_LOCK = threading.Lock()

# graph_id → the graphs directory that actually holds it (team or central).
# Saves run-frequency node-state writes from re-scanning team directories.
_GRAPH_DIR_CACHE: dict[str, Path] = {}


def _central_root() -> Path:
    root = get_agent_root_dir() / "designer"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _central_graphs_dir() -> Path:
    path = _central_root() / "graphs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _central_runs_dir() -> Path:
    path = _central_root() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


# Backward-compatible aliases for the pre-team-workspace layout helpers.
def _designer_root() -> Path:
    return _central_root()


def _graphs_dir() -> Path:
    return _central_graphs_dir()


def _runs_dir() -> Path:
    return _central_runs_dir()


def design_team_workspace(session_id: str) -> Path:
    """The team-workspace directory owning one design task.

    The real team name (persisted by the team runtime in session metadata)
    wins; before the team exists (task creation) fall back to the
    ``jiuwen_team_<session_id>`` convention used by the worker media root.
    """
    sid = str(session_id or "").strip()
    team_name = ""
    if sid:
        try:
            from jiuwenswarm.server.runtime.session.session_metadata import (
                get_session_metadata,
            )

            team_name = str(get_session_metadata(sid).get("team_name") or "").strip()
        except Exception:  # noqa: BLE001 — metadata is best-effort here
            team_name = ""
    name = team_name or f"jiuwen_team_{sid}"
    return get_workspace_dir() / ".agent_teams" / name / "team-workspace"


def _team_designer_root(session_id: str) -> Path:
    sid = str(session_id or "").strip()
    return design_team_workspace(sid) / "designer"


def _team_designer_dirs() -> list[Path]:
    """All existing team-workspace designer roots (one per design task team)."""
    try:
        teams_root = get_workspace_dir() / ".agent_teams"
        if not teams_root.is_dir():
            return []
        return [
            child / "team-workspace" / "designer"
            for child in teams_root.iterdir()
            if (child / "team-workspace" / "designer").is_dir()
        ]
    except OSError:
        return []


def _graphs_dir_for(graph: dict[str, Any]) -> Path:
    session_id = str((graph.get("metadata") or {}).get("session_id") or "").strip()
    if session_id:
        path = _team_designer_root(session_id) / "graphs"
        path.mkdir(parents=True, exist_ok=True)
        return path
    return _central_graphs_dir()


def _runs_dir_for_graph_id(graph_id: str) -> Path:
    cached = _GRAPH_DIR_CACHE.get(graph_id)
    if cached is not None:
        return cached.parent / "runs"
    graph = DesignerGraphStore().get_graph(graph_id)
    if graph is not None:
        graphs_dir = _GRAPH_DIR_CACHE.get(graph_id)
        if graphs_dir is not None:
            return graphs_dir.parent / "runs"
    return _central_runs_dir()


def _candidate_graph_paths(graph_id: str) -> list[Path]:
    paths: list[Path] = []
    for graphs_dir in _all_graphs_dirs():
        paths.append(graphs_dir / f"{graph_id}.json")
    return paths


def _all_graphs_dirs() -> list[Path]:
    # Team dirs come first: graph rebuilds (director/shot-topology passes) used
    # to construct fresh metadata without session_id, landing those copies in
    # the central dir. The session-tagged team copy is the authority; scanning
    # it first makes the dedup in list/get shadow the stale central twin.
    dirs: list[Path] = []
    for team_root in _team_designer_dirs():
        graphs_dir = team_root / "graphs"
        if graphs_dir.is_dir():
            dirs.append(graphs_dir)
    dirs.append(_central_graphs_dir())
    return dirs


def _all_runs_dirs() -> list[Path]:
    dirs: list[Path] = []
    for team_root in _team_designer_dirs():
        runs_dir = team_root / "runs"
        if runs_dir.is_dir():
            dirs.append(runs_dir)
    dirs.append(_central_runs_dir())
    return dirs


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _read_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"expected object JSON in {path}")
    return data


def reload_graph_inplace(graph: dict[str, Any] | None) -> bool:
    """Replace ``graph`` with the copy last saved from the canvas.

    Node agents keep the dict they were started with. Canvas edits land in the
    store file, so tools re-read that file into the same dict before they run.
    """
    if not isinstance(graph, dict):
        return False
    graph_id = str(graph.get("graph_id") or "").strip()
    if not graph_id:
        return False
    fresh = DesignerGraphStore().get_graph(graph_id)
    if not isinstance(fresh, dict):
        return False
    graph.clear()
    graph.update(fresh)
    return True


class DesignerGraphStore:
    """File-backed store for Designer graphs and execution runs."""

    def save_graph(self, graph: DesignerExecutionGraph) -> DesignerExecutionGraph:
        normalized = normalize_execution_graph(graph)
        # Identity carry: rebuild paths (director / shot-topology passes)
        # construct fresh metadata and can drop session_id, which would land
        # the graph in the central dir and orphan it from its session. The
        # existing on-disk copy is the authority for graph identity.
        if not str((normalized.get("metadata") or {}).get("session_id") or "").strip():
            existing = self.get_graph(str(normalized.get("graph_id") or ""))
            carried = str(((existing or {}).get("metadata") or {}).get("session_id") or "").strip()
            if carried:
                meta = dict(normalized.get("metadata") or {})
                meta["session_id"] = carried
                normalized["metadata"] = meta
        graphs_dir = _graphs_dir_for(normalized)
        path = graphs_dir / f"{normalized['graph_id']}.json"
        with _STORE_LOCK:
            if path.is_file():
                try:
                    existing = normalize_execution_graph(_read_json(path))
                    normalized = preserve_expanded_shot_nodes(normalized, existing)
                    normalized = preserve_nodes_added_since(normalized, existing)
                    normalized = preserve_node_output_refs(normalized, existing)
                    from jiuwenswarm.server.runtime.designer.user_references import (
                        reapply_user_reference_routes,
                    )

                    normalized = reapply_user_reference_routes(normalized, existing) or normalized
                except (DesignerGraphValidationError, ValueError, json.JSONDecodeError, OSError):
                    pass
            normalized["updated_at"] = utc_now_ms()
            _atomic_write_json(path, dict(normalized))
            _GRAPH_DIR_CACHE[str(normalized["graph_id"])] = graphs_dir
        return normalized

    def get_graph(self, graph_id: str) -> DesignerExecutionGraph | None:
        graph_id = str(graph_id or "").strip()
        if not graph_id:
            return None
        cached = _GRAPH_DIR_CACHE.get(graph_id)
        paths = [cached / f"{graph_id}.json"] if cached is not None else _candidate_graph_paths(graph_id)
        with _STORE_LOCK:
            raw = None
            for path in paths:
                if not path.is_file():
                    continue
                try:
                    raw = _read_json(path)
                    _GRAPH_DIR_CACHE[graph_id] = path.parent
                    break
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    logger.warning("Failed to read designer graph %s: %s", graph_id, exc)
        if raw is None:
            return None
        try:
            return normalize_execution_graph(raw)
        except DesignerGraphValidationError as exc:
            logger.warning("Invalid designer graph %s: %s", graph_id, exc)
            return None

    def list_graphs(self, project_id: str | None = None) -> list[DesignerExecutionGraph]:
        wanted = str(project_id or "").strip()
        graphs: list[DesignerExecutionGraph] = []
        seen: set[str] = set()
        with _STORE_LOCK:
            for graphs_dir in _all_graphs_dirs():
                for path in sorted(graphs_dir.glob("*.json")):
                    try:
                        raw = _read_json(path)
                        graph = normalize_execution_graph(raw)
                    except (DesignerGraphValidationError, ValueError, json.JSONDecodeError):
                        continue
                    if graph["graph_id"] in seen:
                        continue
                    if wanted and graph.get("project_id") != wanted:
                        continue
                    seen.add(graph["graph_id"])
                    _GRAPH_DIR_CACHE[graph["graph_id"]] = path.parent
                    graphs.append(graph)
        graphs.sort(key=lambda item: int(item.get("updated_at") or 0), reverse=True)
        return graphs

    def list_graphs_for_project(self, project_id: str) -> list[DesignerExecutionGraph]:
        project_id = str(project_id or "").strip()
        if not project_id:
            return []
        return self.list_graphs(project_id)

    def get_latest_graph_for_session(
        self, session_id: str
    ) -> DesignerExecutionGraph | None:
        """The session's freshest graph (one design session owns one graph).

        The chat path resolves the graph to bind from the session id alone —
        the registry is in-process state that a server restart clears, the
        store is durable.
        """
        session_id = str(session_id or "").strip()
        if not session_id:
            return None
        for graph in self.list_graphs():
            if str((graph.get("metadata") or {}).get("session_id") or "") == session_id:
                return graph
        return None

    def delete_graph(self, graph_id: str) -> bool:
        """Delete a graph created by an uncommitted workspace transaction."""
        graph_id = str(graph_id or "").strip()
        if not graph_id:
            return False
        with _STORE_LOCK:
            deleted = False
            for path in _candidate_graph_paths(graph_id):
                try:
                    path.unlink()
                    deleted = True
                except FileNotFoundError:
                    continue
            _GRAPH_DIR_CACHE.pop(graph_id, None)
        return deleted

    def save_run(self, run: DesignerExecutionRun) -> DesignerExecutionRun:
        normalized = normalize_execution_run(run)
        normalized["updated_at"] = utc_now_ms()
        runs_dir = _runs_dir_for_graph_id(str(normalized.get("graph_id") or ""))
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / f"{normalized['run_id']}.json"
        with _STORE_LOCK:
            _atomic_write_json(path, dict(normalized))
        return normalized

    def get_run(self, run_id: str) -> DesignerExecutionRun | None:
        run_id = str(run_id or "").strip()
        if not run_id:
            return None
        with _STORE_LOCK:
            raw = None
            for runs_dir in _all_runs_dirs():
                path = runs_dir / f"{run_id}.json"
                if not path.is_file():
                    continue
                try:
                    raw = _read_json(path)
                    break
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    logger.warning("Failed to read designer run %s: %s", run_id, exc)
        if raw is None:
            return None
        try:
            return normalize_execution_run(raw)
        except DesignerGraphValidationError as exc:
            logger.warning("Invalid designer run %s: %s", run_id, exc)
            return None

    def get_latest_run_for_graph(self, graph_id: str) -> DesignerExecutionRun | None:
        graph_id = str(graph_id or "").strip()
        if not graph_id:
            return None
        latest: DesignerExecutionRun | None = None
        latest_ts = -1
        with _STORE_LOCK:
            for runs_dir in _all_runs_dirs():
                for path in runs_dir.glob("*.json"):
                    try:
                        raw = _read_json(path)
                        run = normalize_execution_run(raw)
                    except (DesignerGraphValidationError, ValueError, json.JSONDecodeError):
                        continue
                    if run.get("graph_id") != graph_id:
                        continue
                    ts = int(run.get("updated_at") or 0)
                    if ts >= latest_ts:
                        latest = run
                        latest_ts = ts
        return latest

    def latest_runs_by_graph(self) -> dict[str, DesignerExecutionRun]:
        """Latest run per graph in one scan over the run files.

        List views need the latest run of every graph at once; calling
        ``get_latest_run_for_graph`` per graph re-parses every run file each
        time (O(graphs x runs)). One pass keeps the landing page flat.
        """
        latest: dict[str, DesignerExecutionRun] = {}
        with _STORE_LOCK:
            for runs_dir in _all_runs_dirs():
                for path in runs_dir.glob("*.json"):
                    try:
                        run = normalize_execution_run(_read_json(path))
                    except (DesignerGraphValidationError, ValueError, json.JSONDecodeError):
                        continue
                    graph_id = str(run.get("graph_id") or "")
                    if not graph_id:
                        continue
                    ts = int(run.get("updated_at") or 0)
                    held = latest.get(graph_id)
                    if held is None or ts >= int(held.get("updated_at") or 0):
                        latest[graph_id] = run
        return latest
