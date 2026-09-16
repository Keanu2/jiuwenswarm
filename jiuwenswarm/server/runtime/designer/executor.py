# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Designer graph executor — wave scheduler or agent-owned graph."""

from __future__ import annotations

import asyncio
import logging
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Protocol

from jiuwenswarm.common.schema.designer_graph import (
    CONFIG_DELEGATE_AGENT,
    CONFIG_DELEGATE_HANDLER,
    DesignerExecutionGraph,
    DesignerExecutionRun,
    DesignerGraphNode,
    DesignerNodeState,
    MAX_SHOT_CLIP_NODES,
    NODE_ROLE_CLIP,
    NODE_ROLE_COMPOSE,
    NODE_ROLE_FRAME,
    NODE_ROLE_STORYBOARD,
    NODE_STATUS_CANCELLED,
    NODE_STATUS_COMPLETED,
    NODE_STATUS_FAILED,
    NODE_STATUS_PENDING,
    NODE_STATUS_RUNNING,
    NODE_TYPE_IMAGE,
    NODE_TYPE_VIDEO,
    RUN_STATUS_CANCELLED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_DRAFT,
    RUN_STATUS_FAILED,
    RUN_STATUS_PAUSED,
    RUN_STATUS_RUNNING,
    apply_graph_patch,
    apply_shot_generate_prompts,
    clip_node_id,
    data_predecessors,
    expand_shot_nodes,
    frame_node_id,
    graph_uses_agent_scheduler,
    initial_node_states,
    new_run_id,
    node_pipeline,
    node_uses_agent_runtime,
    sync_groups,
    utc_now_ms,
)
from jiuwenswarm.common.schema.message import EventType
from jiuwenswarm.server.runtime.designer.activity import (
    emit_run_activity,
    graph_node_states,
)
from jiuwenswarm.server.runtime.designer.a2a_collab import collaborate_ready_wave
from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore
from jiuwenswarm.server.runtime.designer.handlers import (
    NodeExecutionContext,
    get_node_handler,
)
from jiuwenswarm.server.runtime.designer.node_agent import NodeAgentHost, NodeAgentRunner

logger = logging.getLogger(__name__)

GraphUpdateCallback = Callable[[DesignerExecutionGraph], None]


class RunUpdateCallback(Protocol):
    def __call__(
        self,
        run: DesignerExecutionRun,
        node_id: str | None = None,
    ) -> None: ...

_MOCK_NODE_DELAY_SECONDS = 0.35
_MAX_CONCURRENT_NODE_AGENTS = 3
_TERMINAL_NODE_STATUSES = {
    NODE_STATUS_COMPLETED,
    NODE_STATUS_FAILED,
    NODE_STATUS_CANCELLED,
}


@dataclass(frozen=True)
class NodeEvent:
    event: str
    run: DesignerExecutionRun
    node_id: str | None = None


class GraphExecutor:
    """Wave scheduler for handler graphs; agent scheduler for node Agents."""

    def __init__(
        self,
        store: DesignerGraphStore | None = None,
        *,
        runner: NodeAgentRunner | None = None,
    ) -> None:
        self._store = store or DesignerGraphStore()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._node_workers: dict[str, dict[str, asyncio.Task[None]]] = {}
        self._pause_flags: dict[str, asyncio.Event] = {}
        self._cancel_flags: dict[str, asyncio.Event] = {}
        self._state_locks: dict[str, asyncio.Lock] = {}
        self._on_updates: dict[str, RunUpdateCallback] = {}
        self._on_graph_updates: dict[str, GraphUpdateCallback] = {}
        self._live_runs: dict[str, DesignerExecutionRun] = {}
        self._host = NodeAgentHost(self, runner=runner)

    def create_run(self, graph: DesignerExecutionGraph) -> DesignerExecutionRun:
        now = utc_now_ms()
        run: DesignerExecutionRun = {
            "schema_version": "designer-execution-run.v1",
            "run_id": new_run_id(),
            "graph_id": graph["graph_id"],
            "project_id": graph["project_id"],
            "status": RUN_STATUS_DRAFT,
            "node_states": initial_node_states(graph),
            "current_node_ids": [],
            "created_at": now,
            "updated_at": now,
        }
        return self._store.save_run(run)

    def create_rerun(
        self,
        graph: DesignerExecutionGraph,
        *,
        source_run: DesignerExecutionRun,
        node_id: str,
    ) -> DesignerExecutionRun:
        """Copy a finished run and reset one node so only that node executes again."""
        node_ids = {node["id"] for node in graph.get("nodes", [])}
        if node_id not in node_ids:
            raise KeyError(f"node not found: {node_id}")
        incoming = data_predecessors(graph)
        groups = sync_groups(graph)
        source_states = source_run.get("node_states") or {}
        for pred in incoming.get(node_id, []):
            members = groups.get(pred, frozenset({pred}))
            for member in members:
                if (source_states.get(member) or {}).get("status") != NODE_STATUS_COMPLETED:
                    raise ValueError(f"upstream not ready: {member}")
        now = utc_now_ms()
        states = deepcopy(source_states)
        for node in graph.get("nodes", []):
            states.setdefault(node["id"], {"status": NODE_STATUS_PENDING})
        previous = states.get(node_id) or {}
        kept_ref = previous.get("output_ref") if _usable_ref(previous.get("output_ref")) else None
        kept_refs = [
            ref for ref in (previous.get("output_refs") or []) if _usable_ref(ref)
        ]
        if kept_ref is not None and not kept_refs:
            kept_refs = [kept_ref]
        target_node = _node_by_id(graph, node_id)
        target_type = str(target_node.get("type") or "")
        if (
            target_type in {NODE_TYPE_IMAGE, NODE_TYPE_VIDEO}
            and _is_fallback_text_ref(kept_ref)
        ) or node_pipeline(target_node) == NODE_ROLE_COMPOSE:
            kept_ref = None
            kept_refs = []
        states[node_id] = {
            "status": NODE_STATUS_PENDING,
            "started_at": None,
            "completed_at": None,
            "output_ref": kept_ref,
            "output_refs": kept_refs,
            "error": None,
            "blocked_by": [],
        }
        run: DesignerExecutionRun = {
            "schema_version": "designer-execution-run.v1",
            "run_id": new_run_id(),
            "graph_id": graph["graph_id"],
            "project_id": graph["project_id"],
            "status": RUN_STATUS_DRAFT,
            "node_states": states,
            "current_node_ids": [],
            "created_at": now,
            "updated_at": now,
            "metadata": {"use_prior_feedback": True},
        }
        # Opt-in: Run again may apply prior report constraints (still one-pass, no loop).
        meta = dict(graph.get("metadata") or {})
        meta["use_prior_feedback"] = True
        graph["metadata"] = meta
        self._store.save_graph(graph)
        return self._store.save_run(run)

    async def start_run(
        self,
        run_id: str,
        *,
        on_update: RunUpdateCallback | None = None,
        on_graph_update: GraphUpdateCallback | None = None,
    ) -> DesignerExecutionRun:
        run = self._require_run(run_id)
        if run["status"] == RUN_STATUS_RUNNING:
            return run
        # Resume one-pass execution when a prior wave ended early with pending nodes.
        if run["status"] == RUN_STATUS_COMPLETED:
            pending = any(
                (state or {}).get("status") == NODE_STATUS_PENDING
                for state in graph_node_states(run).values()
            )
            if not pending:
                return run
        graph = self._require_graph(run["graph_id"])
        from jiuwenswarm.server.runtime.designer.model_tools import llm_available

        # Framework: every node is an LLM agent with tools when models exist.
        use_agents = llm_available()
        for node in graph.get("nodes") or []:
            cfg = node.setdefault("config", {})
            if not isinstance(cfg, dict):
                continue
            if use_agents:
                cfg.pop("force_handler", None)
                cfg["delegate"] = CONFIG_DELEGATE_AGENT
                cfg["kind"] = "agent"
                if cfg.get("skip_llm"):
                    cfg["skip_llm"] = False
                if cfg.get("prewritten") and not cfg.get("draft_prewritten"):
                    cfg["draft_prewritten"] = cfg.pop("prewritten")
                else:
                    cfg.pop("prewritten", None)
            else:
                current = str(cfg.get("delegate") or "").strip()
                if cfg.get("force_handler") or current == CONFIG_DELEGATE_HANDLER:
                    cfg["delegate"] = CONFIG_DELEGATE_HANDLER
                elif not current:
                    cfg["delegate"] = CONFIG_DELEGATE_HANDLER
        meta = dict(graph.get("metadata") or {})
        meta["ai_agent_pipeline"] = use_agents
        meta["all_nodes_agents"] = use_agents
        graph["metadata"] = meta
        run["status"] = RUN_STATUS_RUNNING
        run["updated_at"] = utc_now_ms()
        run = self._store.save_run(run)
        self._live_runs[run_id] = run
        self._pause_flags[run_id] = asyncio.Event()
        self._pause_flags[run_id].set()
        self._cancel_flags[run_id] = asyncio.Event()
        self._state_locks[run_id] = asyncio.Lock()
        self._node_workers[run_id] = {}
        if on_update is not None:
            self._on_updates[run_id] = on_update
        if on_graph_update is not None:
            self._on_graph_updates[run_id] = on_graph_update
        task = asyncio.create_task(
            self._execute_run(graph, run, on_update=on_update),
            name=f"designer-run-{run_id}",
        )
        self._tasks[run_id] = task
        return run

    async def run(self, graph: DesignerExecutionGraph, run_id: str) -> AsyncIterator[NodeEvent]:
        """Drive a run and yield node/run events as they happen."""
        queue: asyncio.Queue[NodeEvent] = asyncio.Queue()

        def on_update(updated: DesignerExecutionRun, node_id: str | None = None) -> None:
            event = (
                EventType.DESIGNER_NODE_UPDATED.value
                if node_id
                else EventType.DESIGNER_RUN_UPDATED.value
            )
            queue.put_nowait(
                NodeEvent(
                    event=event,
                    run=deepcopy(updated),
                    node_id=node_id,
                )
            )

        existing = self._tasks.get(run_id)
        if existing is None or existing.done():
            started = await self.start_run(run_id, on_update=on_update)
            queue.put_nowait(
                NodeEvent(event=EventType.DESIGNER_RUN_UPDATED.value, run=deepcopy(started))
            )
        task = self._tasks.get(run_id)
        if task is None:
            return
        while True:
            if task.done() and queue.empty():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.05)
            except TimeoutError:
                continue
            yield event
        await task

    def pause_run(self, run_id: str) -> DesignerExecutionRun:
        run = self._require_run(run_id)
        pause_flag = self._pause_flags.get(run_id)
        if pause_flag is not None:
            pause_flag.clear()
        run["status"] = RUN_STATUS_PAUSED
        run["updated_at"] = utc_now_ms()
        return self._store.save_run(run)

    def choose_output(
        self,
        run_id: str,
        node_id: str,
        choice: str,
    ) -> DesignerExecutionRun:
        """Keep the original output or promote the regenerated candidate."""
        run = self._require_run(run_id)
        states = run.setdefault("node_states", {})
        state = states.get(node_id)
        if not isinstance(state, dict):
            raise KeyError(f"node not found: {node_id}")
        if state.get("status") == NODE_STATUS_RUNNING:
            raise ValueError("node is still running")
        decided = str(choice or "").strip()
        if decided not in {"original", "new"}:
            raise ValueError("choice must be original or new")
        candidate = state.get("candidate_output_ref")
        if not _usable_ref(candidate):
            raise ValueError("no pending revision")
        if decided == "new":
            refs = [ref for ref in (state.get("candidate_output_refs") or []) if _usable_ref(ref)]
            state["output_ref"] = candidate
            state["output_refs"] = refs or [candidate]
        state["candidate_output_ref"] = None
        state["candidate_output_refs"] = []
        run["updated_at"] = utc_now_ms()
        return self._store.save_run(run)

    def cancel_run(self, run_id: str) -> DesignerExecutionRun:
        run = self._require_run(run_id)
        cancel_flag = self._cancel_flags.get(run_id)
        if cancel_flag is not None:
            cancel_flag.set()
        workers = self._node_workers.pop(run_id, {})
        for worker in workers.values():
            if not worker.done():
                worker.cancel()
        self._host.drop_run(run_id)
        task = self._tasks.pop(run_id, None)
        if task is not None and not task.done():
            task.cancel()
        for node_id, state in run.get("node_states", {}).items():
            if state.get("status") in {NODE_STATUS_PENDING, NODE_STATUS_RUNNING}:
                state["status"] = NODE_STATUS_CANCELLED
        run["status"] = RUN_STATUS_CANCELLED
        run["current_node_ids"] = []
        run["updated_at"] = utc_now_ms()
        return self._store.save_run(run)

    async def spawn_node_agent(self, run_id: str, node_id: str) -> str:
        """Start one node's Agent. Allowed even if data-predecessors are incomplete."""
        target = str(node_id or "").strip()
        if not target:
            return "node_id is required"
        if self._is_cancelled(run_id):
            return "run cancelled"
        run = self._require_run(run_id)
        if run.get("status") not in {RUN_STATUS_RUNNING, RUN_STATUS_PAUSED}:
            return f"run is {run.get('status')}"
        graph = self._require_graph(str(run.get("graph_id") or ""))
        try:
            _node_by_id(graph, target)
        except KeyError:
            return f"node not found: {target}"
        workers = self._node_workers.setdefault(run_id, {})
        existing = workers.get(target)
        if existing is not None and not existing.done():
            return f"already running: {target}"
        running_count = sum(1 for item in workers.values() if not item.done())
        if running_count >= _MAX_CONCURRENT_NODE_AGENTS:
            return "too many concurrent node agents"
        lock = self._state_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            run = self._require_run(run_id)
            states = run.setdefault("node_states", {})
            current = dict(states.get(target) or {"status": NODE_STATUS_PENDING})
            if current.get("status") == NODE_STATUS_RUNNING:
                return f"already running: {target}"
            if current.get("status") in _TERMINAL_NODE_STATUSES:
                current["status"] = NODE_STATUS_PENDING
                current["error"] = None
                current["completed_at"] = None
            states[target] = current
            current_ids = list(run.get("current_node_ids") or [])
            if target not in current_ids:
                current_ids.append(target)
                run["current_node_ids"] = current_ids
            run["updated_at"] = utc_now_ms()
            self._store.save_run(run)
        on_update = self._on_updates.get(run_id)
        task = asyncio.create_task(
            self._run_spawned_node(run_id, target, on_update=on_update),
            name=f"designer-node-{run_id}-{target}",
        )
        workers[target] = task
        return f"started {target}"

    def apply_agent_graph_patch(
        self,
        graph_id: str,
        patch: dict[str, Any],
    ) -> DesignerExecutionGraph:
        graph = self._require_graph(graph_id)
        saved = self._store.save_graph(apply_graph_patch(graph, patch))
        node_ids = {node["id"] for node in saved.get("nodes") or []}
        for run_id, run in list(self._live_runs.items()):
            if run.get("graph_id") != graph_id:
                continue
            states = run.setdefault("node_states", {})
            for node_id in node_ids:
                states.setdefault(node_id, {"status": NODE_STATUS_PENDING})
            run["updated_at"] = utc_now_ms()
            self._store.save_run(run)
            callback = self._on_graph_updates.get(run_id)
            if callback is not None:
                callback(deepcopy(saved))
        return saved

    def load_graph_snapshot(self, graph_id: str, run_id: str) -> dict[str, Any]:
        graph = self._require_graph(graph_id)
        run = self._store.get_run(run_id)
        return {
            "graph": deepcopy(graph),
            "run": deepcopy(run) if run else None,
        }

    def _require_run(self, run_id: str) -> DesignerExecutionRun:
        live = self._live_runs.get(run_id)
        if live is not None:
            return live
        run = self._store.get_run(run_id)
        if run is None:
            raise KeyError(f"designer run not found: {run_id}")
        return run

    def _require_graph(self, graph_id: str) -> DesignerExecutionGraph:
        graph = self._store.get_graph(graph_id)
        if graph is None:
            raise KeyError(f"designer graph not found: {graph_id}")
        return graph

    async def _execute_run(
        self,
        graph: DesignerExecutionGraph,
        run: DesignerExecutionRun,
        *,
        on_update: RunUpdateCallback | None,
    ) -> None:
        # Always use the continuous wave scheduler. The agent-spawn scheduler
        # dropped ready nodes past the concurrency cap (spawn returned
        # "too many concurrent" and was ignored), which left the run stalled
        # with pending nodes — UI showed Continue. Leaf agents still run via
        # config.delegate=agent inside _run_single_node.
        _ = graph_uses_agent_scheduler  # retained import for callers/tests
        await self._execute_wave_run(graph, run, on_update=on_update)

    async def _execute_agent_run(
        self,
        graph: DesignerExecutionGraph,
        run: DesignerExecutionRun,
        *,
        on_update: RunUpdateCallback | None,
    ) -> None:
        """Run all ready waves to completion (same one-pass contract as wave executor)."""
        run_id = run["run_id"]
        try:
            while True:
                await self._await_pause(run_id)
                if self._is_cancelled(run_id):
                    return
                run = self._require_run(run_id)
                graph = self._require_graph(str(run.get("graph_id") or graph.get("graph_id") or ""))
                incoming = data_predecessors(graph)
                groups = sync_groups(graph)
                ready_ids = [
                    node["id"]
                    for node in graph.get("nodes") or []
                    if _is_ready(node["id"], run, incoming, groups)
                ]
                if not ready_ids:
                    pending = any(
                        (run.get("node_states") or {}).get(node["id"], {}).get("status")
                        == NODE_STATUS_PENDING
                        for node in graph.get("nodes") or []
                    )
                    run["status"] = RUN_STATUS_FAILED if pending else RUN_STATUS_COMPLETED
                    run["current_node_ids"] = []
                    run["updated_at"] = utc_now_ms()
                    self._publish(run, on_update)
                    self._store.save_run(run)
                    return
                run["current_node_ids"] = list(ready_ids)
                run["updated_at"] = utc_now_ms()
                self._publish(run, on_update)
                self._store.save_run(run)
                for node_id in ready_ids:
                    await self.spawn_node_agent(run_id, node_id)
                await self._wait_agent_workers(run_id)
                if self._is_cancelled(run_id):
                    return
                run = self._require_run(run_id)
                if NODE_STATUS_FAILED in {
                    state.get("status") for state in graph_node_states(run).values()
                }:
                    run["status"] = RUN_STATUS_FAILED
                    run["current_node_ids"] = []
                    run["updated_at"] = utc_now_ms()
                    self._publish(run, on_update)
                    self._store.save_run(run)
                    return
        except asyncio.CancelledError:
            run = self.cancel_run(run_id)
            self._publish(run, on_update)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Designer agent run %s failed: %s", run_id, exc)
            run["status"] = RUN_STATUS_FAILED
            run["updated_at"] = utc_now_ms()
            self._publish(run, on_update)
            self._store.save_run(run)
        finally:
            self._cleanup_run(run_id)

    async def _execute_wave_run(
        self,
        graph: DesignerExecutionGraph,
        run: DesignerExecutionRun,
        *,
        on_update: RunUpdateCallback | None,
    ) -> None:
        run_id = run["run_id"]
        graph_id = str(graph.get("graph_id") or "")
        from jiuwenswarm.server.runtime.designer.orchestration import (
            ManagerAgent,
            SupervisorAgent,
            SupervisorReviewer,
            write_run_feedback,
        )
        from jiuwenswarm.server.runtime.designer.trajectory import (
            begin_trajectory,
            end_trajectory,
            get_trajectory,
            load_prior_feedback,
        )

        optimize_for = str((graph.get("metadata") or {}).get("optimize_for") or "quality")
        # One-pass: never consume prior ratings/feedback unless this is an explicit Run again.
        use_prior = bool(
            (graph.get("metadata") or {}).get("use_prior_feedback")
            or (run.get("metadata") or {}).get("use_prior_feedback")
        )
        # Supervisor LLM analysis when pending / heuristic bootstrap — rebuild once, no loop.
        meta0 = dict(graph.get("metadata") or {})
        from jiuwenswarm.server.runtime.designer.model_tools import llm_available

        use_llm_orch = llm_available()
        # Stamp how agents run for this Play (docs + trajectory).
        meta0 = dict(graph.get("metadata") or {})
        meta0["ai_agent_pipeline"] = bool(use_llm_orch)
        meta0["agent_runtime"] = {
            "mode": "ai" if use_llm_orch else "heuristic",
            "orch": (
                "SupervisorAgent/ManagerAgent via jiuwenswarm model_tools.call_model_tool "
                "(Settings chat models + orchestration skills)"
                if use_llm_orch
                else "SupervisorAgent/ManagerAgent plan_fast / validate_plan_fast heuristics"
            ),
            "leaves": (
                "NodeAgentHost → openjiuwen create_deep_agent (jiuwenswarm Settings model) "
                "when config.delegate=agent; handler fallback on agent failure"
                if use_llm_orch
                else "role handlers / direct image-video APIs only"
            ),
            "force_handler": (
                "music/speech stay on MusicNodeHandler/SpeechNodeHandler until TTS/music "
                "backends exist; if audio not requested, nodes are omitted"
            ),
            "heuristic_when": "llm_available() is False (no Settings chat models)",
        }
        graph["metadata"] = meta0
        # One-pass: skip Play-time redesign only when bootstrap truly LLM-composed.
        already_composed = bool(meta0.get("supervisor_composed_on_bootstrap")) and not bool(
            meta0.get("pending_llm_analysis")
        )
        if already_composed:
            meta0["pending_llm_analysis"] = False
            meta0["pending_supervisor_graph"] = False
            graph["metadata"] = meta0
        elif (
            str(meta0.get("scenario") or "") == "video"
            and use_llm_orch
            and (
                meta0.get("pending_llm_analysis")
                or str(meta0.get("script_analysis_mode") or "") != "llm"
            )
            and not already_composed
        ):
            try:
                from jiuwenswarm.server.runtime.designer.script_analysis import (
                    analyze_creative_brief,
                )
                from jiuwenswarm.server.runtime.designer.smart_graph import (
                    apply_runtime_delegate,
                    build_smart_video_graph,
                )
                from jiuwenswarm.server.runtime.designer.skills_loader import (
                    attach_skills_metadata,
                )

                prompt_text = str(graph.get("description") or "")
                if prompt_text:
                    analysis = await analyze_creative_brief(
                        prompt_text, use_llm=True, timeout_sec=45.0
                    )
                    if str(analysis.get("source") or "") == "llm":
                        # Cast shrink guard: never replace a richer solo cast with fewer humans.
                        old_solos = sum(
                            1
                            for n in (graph.get("nodes") or [])
                            if str(n.get("id") or "").startswith("n_character")
                        )
                        new_chars = [
                            c
                            for c in (analysis.get("characters") or [])
                            if isinstance(c, dict)
                            and c.get("id")
                            and not c.get("is_prop")
                            and str(c.get("cast_kind") or "") not in {"brand_mascot", "prop"}
                        ]
                        if old_solos > 1 and len(new_chars) < old_solos:
                            logger.info(
                                "Play rebuild rejected: would shrink cast %s → %s",
                                old_solos,
                                len(new_chars),
                            )
                            meta0["pending_llm_analysis"] = False
                            meta0["script_analysis_mode"] = str(
                                meta0.get("script_analysis_mode") or "heuristic"
                            )
                            graph["metadata"] = meta0
                            graph = self._store.save_graph(graph)
                        else:
                            old_id = str(graph.get("graph_id") or "")
                            project_id = str(graph.get("project_id") or "")
                            rebuilt = build_smart_video_graph(
                                project_id=project_id,
                                prompt=prompt_text,
                                analysis=analysis,
                                title=str(graph.get("title") or "") or None,
                                optimize_for=optimize_for,
                                ai_mode=True,
                            )
                            rebuilt["graph_id"] = old_id
                            rebuilt["project_id"] = project_id
                            rebuilt["created_at"] = graph.get("created_at") or rebuilt.get(
                                "created_at"
                            )
                            rebuilt = apply_runtime_delegate(rebuilt)
                            rebuilt = attach_skills_metadata(rebuilt, prompt_text)
                            meta_r = dict(rebuilt.get("metadata") or {})
                            meta_r["script_analysis"] = analysis
                            meta_r["script_analysis_mode"] = "llm"
                            meta_r["pending_llm_analysis"] = False
                            meta_r["ai_agent_pipeline"] = True
                            meta_r["supervisor_analyzed"] = True
                            meta_r["supervisor_composed_on_bootstrap"] = True
                            rebuilt["metadata"] = meta_r
                            graph = self._store.save_graph(rebuilt)
                    else:
                        meta0["script_analysis"] = analysis
                        meta0["script_analysis_mode"] = str(
                            analysis.get("source") or "heuristic"
                        )
                        meta0["pending_llm_analysis"] = False
                        graph["metadata"] = meta0
                        graph = self._store.save_graph(graph)
            except Exception:  # noqa: BLE001
                logger.info("Play-time supervisor LLM analysis skipped", exc_info=True)
                meta0 = dict(graph.get("metadata") or {})
                meta0["pending_llm_analysis"] = False
                graph["metadata"] = meta0
                try:
                    graph = self._store.save_graph(graph)
                except Exception:  # noqa: BLE001
                    pass
        elif meta0.get("pending_llm_analysis"):
            meta0["pending_llm_analysis"] = False
            graph["metadata"] = meta0
            try:
                graph = self._store.save_graph(graph)
            except Exception:  # noqa: BLE001
                pass
        prior: dict[str, Any] | None = None
        if use_prior:
            prior = load_prior_feedback(graph_id)
            if prior:
                meta = dict(graph.get("metadata") or {})
                meta["prior_feedback"] = prior
                meta["last_improvement_plan"] = str(
                    ((prior.get("final") or {}).get("improvement_plan"))
                    or meta.get("last_improvement_plan")
                    or ""
                )
                graph["metadata"] = meta
        else:
            # Strip stale prior so node agents do not re-apply old constraints mid-pass.
            meta = dict(graph.get("metadata") or {})
            meta.pop("prior_feedback", None)
            graph["metadata"] = meta
        traj = begin_trajectory(
            graph_id,
            run_id,
            meta={
                "scenario": (graph.get("metadata") or {}).get("scenario"),
                "optimize_for": optimize_for,
                "skill_guided": bool((graph.get("metadata") or {}).get("skill_guided")),
                "audio_intent": (graph.get("metadata") or {}).get("audio_intent"),
                "one_pass": True,
                "use_prior_feedback": use_prior,
            },
        )
        agent_feedback: dict[str, dict[str, Any]] = {}
        try:
            with traj.span(
                agent_id="manager",
                action="decide_capabilities",
                phase="orchestration",
                role="manager",
                tool="heuristic",
            ):
                cap_plan = ManagerAgent().decide_capabilities(graph)
                traj.record(
                    agent_id="manager",
                    action="decide_capabilities_result",
                    phase="orchestration",
                    role="manager",
                    detail={
                        "rating_modality": cap_plan.get("global_rating_modality"),
                        "can_vision": cap_plan.get("can_vision"),
                        "can_video": cap_plan.get("can_video"),
                        "reason": str(cap_plan.get("reason") or "")[:400],
                    },
                )
                graph = self._store.save_graph(graph)

            # Quality path: Brief+Storyboard redesign ONLY when Enter did not compose
            # or user explicitly asked Run again with prior feedback.
            scenario0 = str((graph.get("metadata") or {}).get("scenario") or "")
            meta_play = dict(graph.get("metadata") or {})
            already_composed = bool(meta_play.get("supervisor_composed_on_bootstrap")) and not bool(
                meta_play.get("pending_llm_analysis")
            )
            run_enter_redesign = scenario0 == "video" and use_llm_orch and (
                use_prior or not already_composed
            )
            if run_enter_redesign:
                with traj.span(
                    agent_id="supervisor",
                    action="author_creative_brief",
                    phase="orchestration",
                    role="supervisor",
                    tool="llm",
                ):
                    brief_ack = await SupervisorAgent().author_creative_brief(
                        graph, use_llm=True
                    )
                    traj.record(
                        agent_id="supervisor",
                        action="author_creative_brief_result",
                        phase="orchestration",
                        role="supervisor",
                        detail={
                            "source": brief_ack.get("source"),
                            "chars": brief_ack.get("chars"),
                            "notes": str(brief_ack.get("notes") or "")[:400],
                        },
                    )
                    graph = self._store.save_graph(graph)

                with traj.span(
                    agent_id="manager",
                    action="review_brief",
                    phase="orchestration",
                    role="manager",
                    tool="llm",
                ):
                    mgr_brief = await ManagerAgent().review_brief(graph, use_llm=True)
                    traj.record(
                        agent_id="manager",
                        action="review_brief_result",
                        phase="orchestration",
                        role="manager",
                        detail={
                            "source": mgr_brief.get("source"),
                            "patched": list(mgr_brief.get("patched") or [])[:20],
                            "notes": str(mgr_brief.get("notes") or "")[:400],
                        },
                    )
                    graph = self._store.save_graph(graph)

                with traj.span(
                    agent_id="supervisor",
                    action="author_storyboard",
                    phase="orchestration",
                    role="supervisor",
                    tool="llm",
                ):
                    sb_ack = await SupervisorAgent().author_storyboard(
                        graph, use_llm=True
                    )
                    traj.record(
                        agent_id="supervisor",
                        action="author_storyboard_result",
                        phase="orchestration",
                        role="supervisor",
                        detail={
                            "source": sb_ack.get("source"),
                            "shot_count": sb_ack.get("shot_count"),
                            "notes": str(sb_ack.get("notes") or "")[:400],
                        },
                    )
                    graph = self._store.save_graph(graph)

                with traj.span(
                    agent_id="manager",
                    action="review_storyboard_pre",
                    phase="orchestration",
                    role="manager",
                    tool="llm",
                ):
                    mgr_sb = await ManagerAgent().review_storyboard(
                        graph, use_llm=True
                    )
                    traj.record(
                        agent_id="manager",
                        action="review_storyboard_pre_result",
                        phase="orchestration",
                        role="manager",
                        detail={
                            "source": mgr_sb.get("source"),
                            "patched": list(mgr_sb.get("patched") or [])[:20],
                            "notes": str(mgr_sb.get("notes") or "")[:400],
                        },
                    )
                    graph = self._store.save_graph(graph)

                # Supervisor designs flexible multi-shot graph from locked Brief+Storyboard.
                with traj.span(
                    agent_id="supervisor",
                    action="design_execution_graph",
                    phase="orchestration",
                    role="supervisor",
                    tool=("llm" if use_llm_orch else "deterministic"),
                ):
                    graph_ack = await SupervisorAgent().design_execution_graph(
                        graph,
                        use_llm=use_llm_orch,
                        optimize_for=optimize_for,
                    )
                    graph = self._store.save_graph(graph)
                    # Topology changed — resync run states and push canvas update now.
                    self._resync_run_after_graph_redesign(run, graph)
                    self._publish(run, on_update)
                    self._publish_graph(run, graph)
                    traj.record(
                        agent_id="supervisor",
                        action="design_execution_graph_result",
                        phase="orchestration",
                        role="supervisor",
                        detail={
                            "source": graph_ack.get("source"),
                            "shot_count": graph_ack.get("shot_count"),
                            "frame_nodes": graph_ack.get("frame_nodes"),
                            "node_count": len(graph.get("nodes") or []),
                            "notes": str(graph_ack.get("notes") or "")[:400],
                        },
                    )
            elif scenario0 == "video":
                traj.record(
                    agent_id="supervisor",
                    action="skip_enter_redesign",
                    phase="orchestration",
                    role="supervisor",
                    detail={
                        "reason": "supervisor_composed_on_bootstrap",
                        "use_prior_feedback": use_prior,
                    },
                )

            with traj.span(
                agent_id="supervisor",
                action="plan",
                phase="orchestration",
                role="supervisor",
                tool=("llm" if use_llm_orch else "deterministic"),
                detail={"optimize_for": optimize_for, "has_prior_feedback": bool(prior)},
            ):
                supervisor_skill = str(
                    (graph.get("metadata") or {}).get("supervisor_skill_excerpt") or ""
                )
                if supervisor_skill:
                    meta = dict(graph.get("metadata") or {})
                    meta["active_supervisor_skill"] = supervisor_skill[:3000]
                    graph["metadata"] = meta
                plan = await SupervisorAgent().plan(
                    graph,
                    optimize_for=optimize_for,
                    prior_feedback=prior,
                    use_llm=use_llm_orch,
                )
                traj.record(
                    agent_id="supervisor",
                    action="plan_result",
                    phase="orchestration",
                    role="supervisor",
                    detail={
                        "notes": str((plan or {}).get("notes") or "")[:500],
                        "rating_modality": (plan or {}).get("rating_modality"),
                        "use_llm": use_llm_orch,
                    },
                )
                self._store.save_graph(graph)

            with traj.span(
                agent_id="manager",
                action="validate_plan",
                phase="orchestration",
                role="manager",
                tool=("llm" if use_llm_orch else "heuristic"),
            ):
                manager_ack = await ManagerAgent().validate_plan(
                    graph, use_llm=use_llm_orch
                )
                traj.record(
                    agent_id="manager",
                    action="validate_plan_result",
                    phase="orchestration",
                    role="manager",
                    detail={
                        "patched": list(manager_ack.get("patched") or [])[:20],
                        "rating_modality": manager_ack.get("rating_modality"),
                        "can_vision": manager_ack.get("can_vision"),
                        "use_llm": use_llm_orch,
                    },
                )
                graph = self._store.save_graph(graph)
                self._resync_run_after_graph_redesign(run, graph)
                self._publish(run, on_update)
                self._publish_graph(run, graph)

            remaining = {
                node["id"]
                for node in graph.get("nodes", [])
                if (run.get("node_states") or {}).get(node["id"], {}).get("status")
                not in _TERMINAL_NODE_STATUSES
            }
            incoming = data_predecessors(graph)
            groups = sync_groups(graph)
            graph, remaining, incoming, groups = self._expand_clips_if_needed(
                graph, run, remaining, on_update=on_update
            )
            # Continuous scheduling: start each node as soon as graph deps are met
            # (no wave barrier). Cap concurrency like the agent-scheduler path.
            in_flight: dict[str, asyncio.Task[None]] = {}
            sem = asyncio.Semaphore(_MAX_CONCURRENT_NODE_AGENTS)

            async def _run_guarded(node_id: str) -> None:
                async with sem:
                    await self._run_single_node(
                        graph,
                        run,
                        _node_by_id(graph, node_id),
                        on_update=on_update,
                        agent_feedback=agent_feedback,
                    )

            def _maybe_adjust_clips() -> None:
                nonlocal graph
                meta_live = dict(graph.get("metadata") or {})
                if meta_live.get("clips_adjusted_after_keyframes"):
                    return
                frame_nodes = [
                    n
                    for n in (graph.get("nodes") or [])
                    if node_pipeline(n) == NODE_ROLE_FRAME
                ]
                clip_pending = [
                    n
                    for n in (graph.get("nodes") or [])
                    if node_pipeline(n) == NODE_ROLE_CLIP
                    and (run.get("node_states") or {}).get(str(n.get("id") or ""), {}).get(
                        "status"
                    )
                    not in _TERMINAL_NODE_STATUSES
                ]
                frames_done = bool(frame_nodes) and all(
                    (run.get("node_states") or {}).get(str(n.get("id") or ""), {}).get("status")
                    in _TERMINAL_NODE_STATUSES
                    for n in frame_nodes
                )
                if not (frames_done and clip_pending):
                    return
                with traj.span(
                    agent_id="supervisor",
                    action="adjust_after_keyframes",
                    phase="orchestration",
                    role="supervisor",
                    tool="heuristic",
                ):
                    adj_notes = SupervisorAgent().adjust_clips_after_keyframes(
                        graph,
                        node_states=run.get("node_states"),
                        agent_feedback=agent_feedback,
                    )
                    traj.record(
                        agent_id="supervisor",
                        action="adjust_after_keyframes_result",
                        phase="orchestration",
                        role="supervisor",
                        detail={"notes": adj_notes[:20], "rating_modality": "text_only"},
                    )
                with traj.span(
                    agent_id="manager",
                    action="ack_keyframe_adjustment",
                    phase="orchestration",
                    role="manager",
                    tool="heuristic",
                ):
                    ManagerAgent().ack_keyframe_adjustment(graph, adj_notes)
                graph = self._store.save_graph(graph)

            async def _maybe_review_storyboard() -> None:
                nonlocal graph
                meta_live = dict(graph.get("metadata") or {})
                if meta_live.get("storyboard_reviewed"):
                    return
                sb_state = (run.get("node_states") or {}).get("n_storyboard") or {}
                if sb_state.get("status") not in _TERMINAL_NODE_STATUSES:
                    return
                with traj.span(
                    agent_id="manager",
                    action="review_storyboard",
                    phase="orchestration",
                    role="manager",
                    tool=("llm" if use_llm_orch else "heuristic"),
                ):
                    sb_ack = await ManagerAgent().review_storyboard_once(
                        graph,
                        node_states=run.get("node_states"),
                        use_llm=use_llm_orch,
                    )
                    traj.record(
                        agent_id="manager",
                        action="review_storyboard_result",
                        phase="orchestration",
                        role="manager",
                        detail={
                            "patched": list(sb_ack.get("patched") or [])[:20],
                            "source": sb_ack.get("source"),
                        },
                    )
                graph = self._store.save_graph(graph)

            while remaining or in_flight:
                await self._await_pause(run_id)
                if self._is_cancelled(run_id):
                    for task in in_flight.values():
                        task.cancel()
                    break

                newly_ready = [
                    node_id
                    for node_id in list(remaining)
                    if node_id not in in_flight and _is_ready(node_id, run, incoming, groups)
                ]
                if newly_ready:
                    enable_a2a = bool((graph.get("metadata") or {}).get("enable_a2a_collab"))
                    if enable_a2a:
                        with traj.span(
                            agent_id="a2a",
                            action="collaborate_ready_wave",
                            phase="collaboration",
                            role="peer",
                            detail={"ready_ids": list(newly_ready)},
                        ):
                            await collaborate_ready_wave(graph, run, newly_ready)
                    for node_id in newly_ready:
                        remaining.discard(node_id)
                        in_flight[node_id] = asyncio.create_task(
                            _run_guarded(node_id),
                            name=f"designer-node-{node_id}",
                        )
                    run["current_node_ids"] = list(in_flight.keys())
                    self._publish(run, on_update)

                if not in_flight:
                    if remaining:
                        run["status"] = RUN_STATUS_FAILED
                        run["updated_at"] = utc_now_ms()
                        self._publish(run, on_update)
                        self._store.save_run(run)
                        return
                    break

                done, _pending = await asyncio.wait(
                    set(in_flight.values()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                finished_ids: list[str] = []
                for task in done:
                    for nid, t in list(in_flight.items()):
                        if t is task:
                            finished_ids.append(nid)
                            in_flight.pop(nid, None)
                            break
                    exc = task.exception() if not task.cancelled() else None
                    if exc is not None:
                        logger.info("Node task error: %s", exc, exc_info=exc)

                for node_id in finished_ids:
                    state = (run.get("node_states") or {}).get(node_id) or {}
                    traj.record(
                        agent_id="supervisor",
                        action="node_report",
                        phase="orchestration",
                        role="supervisor",
                        detail={
                            "node_id": node_id,
                            "status": state.get("status"),
                            "has_output": bool(state.get("output_ref")),
                        },
                    )
                    if state.get("status") == NODE_STATUS_COMPLETED:
                        self._handoff_scene_prompt_after_frame(graph, node_id)

                run["current_node_ids"] = list(in_flight.keys())
                await _maybe_review_storyboard()
                _maybe_adjust_clips()
                graph, remaining, incoming, groups = self._expand_clips_if_needed(
                    graph, run, remaining, on_update=on_update
                )
                # Re-queue any newly expanded clip ids that are not in-flight.
                for node in graph.get("nodes") or []:
                    nid = str(node.get("id") or "")
                    if not nid or nid in in_flight:
                        continue
                    st = (run.get("node_states") or {}).get(nid) or {}
                    if st.get("status") not in _TERMINAL_NODE_STATUSES:
                        remaining.add(nid)
                if run.get("status") == RUN_STATUS_FAILED or self._is_cancelled(run_id):
                    break
            if self._is_cancelled(run_id):
                return
            statuses = {state.get("status") for state in graph_node_states(run).values()}
            if NODE_STATUS_FAILED in statuses or remaining:
                run["status"] = RUN_STATUS_FAILED
            else:
                run["status"] = RUN_STATUS_COMPLETED
            run["current_node_ids"] = []
            run["updated_at"] = utc_now_ms()

            # Write-only reports: supervisor rates nodes → manager rates all + supervisor.
            # Never feed ratings back into this run (no loop).
            supervisor_plan = (graph.get("metadata") or {}).get("supervisor_plan") or {}
            with traj.span(
                agent_id="supervisor",
                action="finalize",
                phase="orchestration",
                role="supervisor",
                tool=("llm" if use_llm_orch else "heuristic"),
            ):
                supervisor_final = await SupervisorReviewer().finalize(
                    graph,
                    agent_feedback=agent_feedback,
                    manager_review={},
                    optimize_for=optimize_for,
                    use_llm=use_llm_orch,
                    node_states=run.get("node_states"),
                )
            with traj.span(
                agent_id="manager",
                action="review",
                phase="orchestration",
                role="manager",
                tool=("llm" if use_llm_orch else "heuristic"),
            ):
                manager_review = await ManagerAgent().review(
                    graph,
                    agent_feedback=agent_feedback,
                    supervisor_plan=supervisor_plan if isinstance(supervisor_plan, dict) else {},
                    prior_feedback=None,
                    optimize_for=optimize_for,
                    use_llm=use_llm_orch,
                    supervisor_report=supervisor_final,
                    node_states=run.get("node_states"),
                )
            with traj.span(
                agent_id="manager",
                action="dual_rate_final",
                phase="orchestration",
                role="manager",
                tool=("llm" if use_llm_orch else "heuristic"),
            ):
                manager_review = await ManagerAgent().assign_dual_raters(
                    graph,
                    agent_feedback=agent_feedback,
                    supervisor_final=supervisor_final,
                    manager_review=manager_review,
                    node_states=run.get("node_states"),
                    use_llm=use_llm_orch,
                )
                traj.record(
                    agent_id="manager",
                    action="dual_rate_final_result",
                    phase="orchestration",
                    role="manager",
                    detail={
                        "aggregated_overall": manager_review.get("aggregated_overall"),
                        "raters": len((manager_review.get("dual_raters") or {}).get("raters") or []),
                        "recommendations": len(
                            manager_review.get("aggregated_recommendations") or []
                        ),
                    },
                )
            feedback_path = await write_run_feedback(
                graph=graph,
                run_id=run_id,
                agent_feedback=agent_feedback,
                supervisor_plan=supervisor_plan if isinstance(supervisor_plan, dict) else {},
                manager_review=manager_review,
                supervisor_final=supervisor_final,
                optimize_for=optimize_for,
            )
            traj.set_feedback(
                {
                    "agents": agent_feedback,
                    "supervisor": {
                        "plan": supervisor_plan,
                        "scores": supervisor_final.get("scores"),
                        "node_reports": supervisor_final.get("node_reports"),
                        "summary": supervisor_final.get("summary"),
                        "suggestions": supervisor_final.get("suggestions"),
                    },
                    "manager": manager_review,
                    "final": {
                        "improvement_plan": supervisor_final.get("improvement_plan"),
                        "aggregated_score": supervisor_final.get("aggregated_score"),
                        "summary": supervisor_final.get("summary"),
                        "apply_on": "run_again_only",
                    },
                    "feedback_path": str(feedback_path) if feedback_path else None,
                }
            )
            meta = dict(graph.get("metadata") or {})
            meta["last_feedback_run_id"] = run_id
            meta["last_trajectory_run_id"] = run_id
            meta["last_aggregated_score"] = supervisor_final.get("aggregated_score")
            meta["last_improvement_plan"] = supervisor_final.get("improvement_plan")
            meta["use_prior_feedback"] = False
            graph["metadata"] = meta
            self._store.save_graph(graph)
            self._publish(run, on_update)
            self._store.save_run(run)
        except asyncio.CancelledError:
            run = self.cancel_run(run_id)
            self._publish(run, on_update)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Designer run %s failed: %s", run_id, exc)
            run["status"] = RUN_STATUS_FAILED
            run["updated_at"] = utc_now_ms()
            self._publish(run, on_update)
            self._store.save_run(run)
            rec = get_trajectory(run_id)
            if rec is not None:
                rec.record(
                    agent_id="executor",
                    action="run_failed",
                    phase="system",
                    status="error",
                    detail={"error": str(exc)},
                )
        finally:
            end_trajectory(run_id)
            self._cleanup_run(run_id)

    def reconcile_loaded_graph(self, graph: DesignerExecutionGraph) -> DesignerExecutionGraph:
        """Expand per-shot keyframe nodes when a completed storyboard already exists."""
        graph_id = str(graph.get("graph_id") or "")
        run = None
        for live in self._live_runs.values():
            if str(live.get("graph_id") or "") == graph_id:
                run = live
                break
        if run is None:
            run = self._store.get_latest_run_for_graph(graph_id)
        if run is None:
            return graph
        expanded, _, _, _ = self._expand_clips_if_needed(
            graph, run, set(), on_update=None
        )
        return expanded

    def _expand_clips_if_needed(
        self,
        graph: DesignerExecutionGraph,
        run: DesignerExecutionRun,
        remaining: set[str],
        *,
        on_update: RunUpdateCallback | None,
    ) -> tuple[
        DesignerExecutionGraph,
        set[str],
        dict[str, list[str]],
        dict[str, frozenset[str]],
    ]:
        shot_rows = self._completed_storyboard_shots(graph, run)
        if shot_rows is None:
            return graph, remaining, data_predecessors(graph), sync_groups(graph)
        shot_count = max(1, min(len(shot_rows) or 1, MAX_SHOT_CLIP_NODES))
        from jiuwenswarm.server.runtime.designer.handlers.text_nodes import shot_generate_prompt

        prompts = [shot_generate_prompt(shot) for shot in shot_rows]
        has_clip_pipeline = any(
            node_pipeline(node) in {NODE_ROLE_CLIP, NODE_ROLE_COMPOSE}
            or str(node.get("id") or "") in {"n_clip", "n_compose"}
            or str(node.get("id") or "").startswith("n_clip_")
            for node in graph.get("nodes") or []
        )
        if not has_clip_pipeline:
            return graph, remaining, data_predecessors(graph), sync_groups(graph)
        # Flexible Supervisor graphs: if storyboard shot count differs from frame
        # nodes, rebuild from analysis (do NOT use expand_shot_nodes — that dumps
        # all cast into every frame and breaks identity wiring).
        meta = graph.get("metadata") or {}
        current_frame_ids = {
            str(node.get("id") or "")
            for node in graph.get("nodes") or []
            if str(node.get("id") or "").startswith("n_frame_")
            or node_pipeline(node) == NODE_ROLE_FRAME
        }
        if len(current_frame_ids) != shot_count and not bool(meta.get("freeze_shot_topology")):
            from jiuwenswarm.server.runtime.designer.smart_graph import (
                apply_runtime_delegate,
                build_smart_video_graph,
            )

            analysis = dict(meta.get("script_analysis") or {})
            analysis["shots"] = [
                {
                    "shot_index": i,
                    "timeline": getattr(row, "timeline", None)
                    or (row.get("timeline") if isinstance(row, dict) else "")
                    or f"{(i - 1) * 5:.1f}-{i * 5:.1f}s",
                    "camera": getattr(row, "camera", None)
                    or (row.get("camera") if isinstance(row, dict) else "")
                    or "medium / eye-level",
                    "action": getattr(row, "character_action", None)
                    or getattr(row, "action", None)
                    or (row.get("action") if isinstance(row, dict) else "")
                    or "",
                    "character_ids": list(
                        getattr(row, "character_ids", None)
                        or (row.get("character_ids") if isinstance(row, dict) else [])
                        or []
                    ),
                    "keyframe_prompt": getattr(row, "keyframe_prompt", None)
                    or (row.get("keyframe_prompt") if isinstance(row, dict) else "")
                    or "",
                    "setting_id": (row.get("setting_id") if isinstance(row, dict) else None)
                    or "set_1",
                }
                for i, row in enumerate(shot_rows, start=1)
            ]
            analysis["target_shot_count"] = shot_count
            rebuilt = build_smart_video_graph(
                project_id=str(graph.get("project_id") or "project"),
                prompt=str(graph.get("description") or ""),
                analysis=analysis,
                title=str(graph.get("title") or "") or None,
                optimize_for=str(meta.get("optimize_for") or "quality"),
                ai_mode=True,
            )
            rebuilt["graph_id"] = graph.get("graph_id") or rebuilt.get("graph_id")
            rebuilt["project_id"] = graph.get("project_id") or rebuilt.get("project_id")
            rmeta = dict(rebuilt.get("metadata") or {})
            for key in (
                "approved_brief",
                "approved_storyboard",
                "user_prompt",
                "manager_lock_ack",
                "supervisor_owns_graph",
            ):
                if key in meta and meta.get(key) is not None:
                    rmeta[key] = meta.get(key)
            rmeta["freeze_shot_topology"] = False
            rmeta["script_analysis"] = analysis
            rebuilt["metadata"] = rmeta
            saved = self._store.save_graph(apply_runtime_delegate(rebuilt))
            live_ids = {str(node.get("id") or "") for node in saved.get("nodes") or []}
            states = run.setdefault("node_states", {})
            for node_id in live_ids:
                states.setdefault(node_id, {"status": NODE_STATUS_PENDING})
            remaining = {
                node_id
                for node_id in live_ids
                if (states.get(node_id) or {}).get("status") not in _TERMINAL_NODE_STATUSES
            }
            run["updated_at"] = utc_now_ms()
            self._store.save_run(run)
            self._publish(run, on_update)
            callback = self._on_graph_updates.get(str(run.get("run_id") or ""))
            if callback is not None:
                callback(deepcopy(saved))
            return saved, remaining, data_predecessors(saved), sync_groups(saved)

        if bool(meta.get("freeze_shot_topology") or meta.get("lean_pipeline")):
            synced = apply_shot_generate_prompts(graph, prompts)
            if synced is graph:
                return graph, remaining, data_predecessors(graph), sync_groups(graph)
            saved = self._store.save_graph(synced)
            callback = self._on_graph_updates.get(str(run.get("run_id") or ""))
            if callback is not None:
                callback(deepcopy(saved))
            return saved, remaining, data_predecessors(saved), sync_groups(saved)
        current_clip_ids = {
            str(node.get("id") or "")
            for node in graph.get("nodes") or []
            if node_pipeline(node) == NODE_ROLE_CLIP
        }
        current_frame_ids = {
            str(node.get("id") or "")
            for node in graph.get("nodes") or []
            if node_pipeline(node) == NODE_ROLE_FRAME
        }
        wanted_clip_ids = {clip_node_id(index) for index in range(1, shot_count + 1)}
        wanted_frame_ids = {frame_node_id(index) for index in range(1, shot_count + 1)}
        has_compose = any(
            node_pipeline(node) == NODE_ROLE_COMPOSE or str(node.get("id") or "") == "n_compose"
            for node in graph.get("nodes") or []
        )
        topology_matches = (
            current_clip_ids == wanted_clip_ids
            and current_frame_ids == wanted_frame_ids
            and has_compose
        )
        bundled_frames = any(
            len(_image_output_refs((run.get("node_states") or {}).get(node_id) or {})) > 1
            for node_id in (current_frame_ids | {"n_frame"})
        )
        if topology_matches and not bundled_frames:
            synced = apply_shot_generate_prompts(graph, prompts)
            if synced is graph:
                return graph, remaining, data_predecessors(graph), sync_groups(graph)
            saved = self._store.save_graph(synced)
            callback = self._on_graph_updates.get(str(run.get("run_id") or ""))
            if callback is not None:
                callback(deepcopy(saved))
            return saved, remaining, data_predecessors(saved), sync_groups(saved)

        # Prefer Supervisor-style rebuild over expand_shot_nodes (identity-safe).
        from jiuwenswarm.server.runtime.designer.smart_graph import (
            apply_runtime_delegate,
            build_smart_video_graph,
        )

        analysis = dict(meta.get("script_analysis") or {})
        analysis["target_shot_count"] = shot_count
        rebuilt = build_smart_video_graph(
            project_id=str(graph.get("project_id") or "project"),
            prompt=str(graph.get("description") or ""),
            analysis=analysis,
            title=str(graph.get("title") or "") or None,
            optimize_for=str(meta.get("optimize_for") or "quality"),
            ai_mode=True,
        )
        rebuilt["graph_id"] = graph.get("graph_id") or rebuilt.get("graph_id")
        rebuilt["project_id"] = graph.get("project_id") or rebuilt.get("project_id")
        rmeta = dict(rebuilt.get("metadata") or {})
        for key in (
            "approved_brief",
            "approved_storyboard",
            "user_prompt",
            "manager_lock_ack",
            "supervisor_owns_graph",
        ):
            if key in meta and meta.get(key) is not None:
                rmeta[key] = meta.get(key)
        rmeta["freeze_shot_topology"] = False
        rebuilt["metadata"] = rmeta
        saved = self._store.save_graph(
            apply_shot_generate_prompts(apply_runtime_delegate(rebuilt), prompts)
        )
        live_ids = {str(node.get("id") or "") for node in saved.get("nodes") or []}
        states = run.setdefault("node_states", {})
        for node_id in live_ids:
            states.setdefault(node_id, {"status": NODE_STATUS_PENDING})
        redistribute_frame_node_states(run, shot_count)
        remaining = {
            node_id
            for node_id in live_ids
            if (states.get(node_id) or {}).get("status") not in _TERMINAL_NODE_STATUSES
        }
        run["updated_at"] = utc_now_ms()
        self._store.save_run(run)
        self._publish(run, on_update)
        callback = self._on_graph_updates.get(str(run.get("run_id") or ""))
        if callback is not None:
            callback(deepcopy(saved))
        return saved, remaining, data_predecessors(saved), sync_groups(saved)

    def _handoff_scene_prompt_after_frame(
        self,
        graph: DesignerExecutionGraph,
        frame_id: str,
    ) -> None:
        """Pass scene-master prompt text to later same-setting keyframes (compose path).

        Visual refs stay solo character sheets; only the deterministic scene bible /
        generate prompt is forwarded so later agents keep architecture consistent.
        """
        by_id = {
            str(n.get("id") or ""): n
            for n in (graph.get("nodes") or [])
            if isinstance(n, dict) and n.get("id")
        }
        src = by_id.get(str(frame_id or "").strip())
        if not src or node_pipeline(src) != NODE_ROLE_FRAME:
            return
        scfg = src.get("config") if isinstance(src.get("config"), dict) else {}
        if not bool(scfg.get("is_scene_master")):
            return
        setting_id = str(scfg.get("setting_id") or "").strip()
        if not setting_id:
            return
        gen = scfg.get("generate") if isinstance(scfg.get("generate"), dict) else {}
        master_prompt = str(gen.get("prompt") or scfg.get("prompt") or "").strip()
        bible = scfg.get("scene_bible") if isinstance(scfg.get("scene_bible"), dict) else None
        if not bible:
            meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
            locks = meta.get("scene_locks") if isinstance(meta.get("scene_locks"), dict) else {}
            maybe = locks.get(setting_id) if isinstance(locks, dict) else None
            if isinstance(maybe, dict):
                bible = maybe
        if not master_prompt and not bible:
            return
        changed = False
        for node in graph.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            nid = str(node.get("id") or "")
            if nid == frame_id or node_pipeline(node) != NODE_ROLE_FRAME:
                continue
            cfg = dict(node.get("config") or {})
            if str(cfg.get("setting_id") or "").strip() != setting_id:
                continue
            if bool(cfg.get("is_scene_master")):
                continue
            cfg["scene_prompt_handoff_from"] = frame_id
            if bible:
                cfg["scene_bible"] = dict(bible)
            if master_prompt:
                cfg["scene_master_prompt"] = master_prompt[:2400]
            gen2 = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
            prompt = str(gen2.get("prompt") or "")
            marker = "SCENE PROMPT HANDOFF"
            if marker not in prompt and master_prompt:
                handoff_bit = (
                    f"{marker} from {frame_id} (same setting `{setting_id}`): keep this "
                    f"architecture/lighting/crowd/objects; change ONLY camera view + "
                    f"on-screen cast/actions.\nMASTER SCENE PROMPT:\n{master_prompt[:1200]}"
                )
                gen2["prompt"] = (prompt + "\n" + handoff_bit).strip()[:2200]
                cfg["generate"] = gen2
            irefs = dict(cfg.get("identity_refs") or {})
            irefs["scene_prompt_handoff_from"] = frame_id
            irefs["keyframe_strategy"] = "compose_from_solo_refs"
            cfg["identity_refs"] = irefs
            cfg["keyframe_strategy"] = "compose_from_solo_refs"
            node["config"] = cfg
            changed = True
        if changed:
            self._store.save_graph(graph)

    def _completed_storyboard_shots(
        self,
        graph: DesignerExecutionGraph,
        run: DesignerExecutionRun,
    ) -> list | None:
        from jiuwenswarm.server.runtime.designer.handlers.common import role_output_text
        from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
            storyboard_shots_or_default,
        )
        from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext

        storyboard = next(
            (
                node
                for node in graph.get("nodes") or []
                if node_pipeline(node) == NODE_ROLE_STORYBOARD
            ),
            None,
        )
        if storyboard is None:
            return []
        node_id = str(storyboard.get("id") or "")
        status = ((run.get("node_states") or {}).get(node_id) or {}).get("status")
        if status != NODE_STATUS_COMPLETED:
            return None
        ctx = NodeExecutionContext(
            graph=graph,
            run_id=str(run.get("run_id") or ""),
            node_id=node_id,
            run=run,
        )
        text = role_output_text(ctx, NODE_ROLE_STORYBOARD)
        shots = storyboard_shots_or_default(
            text,
            str(graph.get("description") or graph.get("title") or ""),
        )
        return shots[:MAX_SHOT_CLIP_NODES]

    def _completed_storyboard_shot_count(
        self,
        graph: DesignerExecutionGraph,
        run: DesignerExecutionRun,
    ) -> int | None:
        shots = self._completed_storyboard_shots(graph, run)
        if shots is None:
            return None
        return max(1, min(len(shots) or 1, MAX_SHOT_CLIP_NODES))

    async def _wait_agent_workers(self, run_id: str) -> None:
        while True:
            if self._is_cancelled(run_id):
                return
            await self._await_pause(run_id)
            workers = self._node_workers.get(run_id) or {}
            pending = [task for task in workers.values() if not task.done()]
            if not pending:
                return
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

    async def _run_spawned_node(
        self,
        run_id: str,
        node_id: str,
        *,
        on_update: RunUpdateCallback | None,
    ) -> None:
        try:
            await self._await_pause(run_id)
            if self._is_cancelled(run_id):
                return
            run = self._require_run(run_id)
            graph = self._require_graph(str(run.get("graph_id") or ""))
            node = _node_by_id(graph, node_id)
            await self._run_single_node(graph, run, node, on_update=on_update)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Designer spawned node %s failed in run %s", node_id, run_id)
        finally:
            workers = self._node_workers.get(run_id)
            if workers is not None:
                workers.pop(node_id, None)

    def _cleanup_run(self, run_id: str) -> None:
        self._tasks.pop(run_id, None)
        self._node_workers.pop(run_id, None)
        self._pause_flags.pop(run_id, None)
        self._cancel_flags.pop(run_id, None)
        self._state_locks.pop(run_id, None)
        self._on_updates.pop(run_id, None)
        self._on_graph_updates.pop(run_id, None)
        self._live_runs.pop(run_id, None)
        self._host.drop_run(run_id)

    async def _run_single_node(
        self,
        graph: DesignerExecutionGraph,
        run: DesignerExecutionRun,
        node: DesignerGraphNode,
        *,
        on_update: RunUpdateCallback | None,
        agent_feedback: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        node_id = node["id"]
        started_at = utc_now_ms()
        blocked_by = list(sync_groups(graph).get(node_id, frozenset()) - {node_id})
        lock = self._state_locks.setdefault(run["run_id"], asyncio.Lock())
        from jiuwenswarm.common.schema.designer_graph import (
            node_pipeline as _node_pipeline_fn,
            node_role as _node_role_fn,
        )
        from jiuwenswarm.server.runtime.designer.skills_loader import (
            load_agent_skill,
            load_subject_skill,
        )
        from jiuwenswarm.server.runtime.designer.trajectory import get_trajectory

        role = str(
            _node_pipeline_fn(node)
            or (node.get("config") or {}).get("pipeline")
            or _node_role_fn(node)
            or node_id
        )
        traj = get_trajectory(run["run_id"])
        # Leaf nodes: task skill only (already on config). Do not dump scenario/subjects.
        skill = str((node.get("config") or {}).get("skill_excerpt") or "").strip()
        if not skill:
            skill = (load_agent_skill(role) or load_agent_skill(node_id) or "")[:1200]
            if skill:
                cfg = dict(node.get("config") or {})
                cfg["skill_excerpt"] = skill
                node["config"] = cfg
        # Optional tiny subject hint for character/scene only (not full encyclopedia).
        if role in {"character", "character_design", "scene"}:
            subjects = list((graph.get("metadata") or {}).get("subject_keys") or [])[:1]
            if subjects:
                bit = load_subject_skill(subjects[0])
                if bit:
                    cfg = dict(node.get("config") or {})
                    cfg["skill_excerpt"] = (
                        str(cfg.get("skill_excerpt") or "") + "\n\n" + bit[:600]
                    ).strip()[:1800]
                    node["config"] = cfg
        if (graph.get("metadata") or {}).get("use_prior_feedback"):
            cfg = dict(node.get("config") or {})
            prior_plan = str((graph.get("metadata") or {}).get("last_improvement_plan") or "")
            suggestions = ((graph.get("metadata") or {}).get("prior_feedback") or {}).get(
                "supervisor"
            ) or {}
            if isinstance(suggestions, dict):
                node_suggestion = (suggestions.get("suggestions") or {}).get(node_id)
                if node_suggestion:
                    cfg["rerun_suggestion"] = str(node_suggestion)
            if prior_plan and not cfg.get("rerun_suggestion"):
                cfg["rerun_suggestion"] = prior_plan[:1500]
            node["config"] = cfg

        # Manager leaf prompt gate + prior-shot handoff for frame/clip media.
        role_for_gate = str(
            _node_pipeline_fn(node)
            or (node.get("config") or {}).get("role")
            or ""
        ).lower()
        if role_for_gate in {"frame", "keyframe", "clip", "character", "character_design", "scene"}:
            from jiuwenswarm.server.runtime.designer.orchestration import ManagerAgent

            live_graph = self._require_graph(
                str(run.get("graph_id") or graph.get("graph_id") or "")
            )
            gate = ManagerAgent().review_leaf_media_prompt(live_graph, node)
            # Always persist Manager lock-gate stamps (even when prompt text unchanged).
            graph = live_graph
            self._store.save_graph(graph)
            for n in graph.get("nodes") or []:
                if str(n.get("id") or "") == node_id:
                    node = n
                    break
            _ = gate

        async with lock:
            self._set_node_state(
                run,
                node_id,
                {
                    "status": NODE_STATUS_RUNNING,
                    "started_at": started_at,
                    "error": None,
                    "blocked_by": blocked_by,
                },
            )
            self._store.save_run(run)
            self._publish(run, on_update, node_id)
        tool_name = "node_agent" if node_uses_agent_runtime(node) else "handler"
        span_cm = (
            traj.span(
                agent_id=node_id,
                action="execute",
                phase="node",
                role=role,
                tool=tool_name,
                detail={
                    "label": node.get("label"),
                    "type": node.get("type"),
                    "has_skill": bool((node.get("config") or {}).get("skill_excerpt")),
                },
            )
            if traj is not None
            else nullcontext()
        )
        try:
            with span_cm as span_detail:
                def emit_activity(
                    kind: str,
                    text: str,
                    tool: str = "",
                    *,
                    force: bool = False,
                ) -> None:
                    emit_run_activity(
                        run,
                        node_id,
                        kind=kind,
                        text=text,
                        tool=tool,
                        on_update=on_update,
                        force=force,
                    )

                from jiuwenswarm.common.schema.designer_graph import (
                    ACTIVITY_KIND_STAGE,
                    ACTIVITY_KIND_TOOL_CALL,
                    ACTIVITY_KIND_THINKING,
                )
                from jiuwenswarm.server.runtime.designer.activity import stage_text_for_node

                uses_agent = node_uses_agent_runtime(node)
                emit_activity(
                    ACTIVITY_KIND_THINKING,
                    f"starting {node.get('label') or node_id}",
                    force=True,
                )
                emit_activity(
                    ACTIVITY_KIND_TOOL_CALL if uses_agent else ACTIVITY_KIND_STAGE,
                    stage_text_for_node(node),
                    tool="node_agent" if uses_agent else "handler",
                    force=True,
                )
                ctx = NodeExecutionContext(
                    graph=graph,
                    run_id=run["run_id"],
                    node_id=node_id,
                    run=run,
                    emit_activity=emit_activity,
                )
                if uses_agent:
                    result = await self._host.execute(node, ctx)
                    handler_name = "NodeAgentHost"
                else:
                    handler = get_node_handler(node)
                    result = await asyncio.wait_for(
                        handler.execute(node, ctx),
                        timeout=_node_execute_timeout_sec(node),
                    )
                    handler_name = type(handler).__name__
                    if _MOCK_NODE_DELAY_SECONDS:
                        await asyncio.sleep(_MOCK_NODE_DELAY_SECONDS)
                if isinstance(span_detail, dict):
                    span_detail["message"] = str(getattr(result, "message", "") or "")[:300]
                    span_detail["handler"] = handler_name
                if agent_feedback is not None:
                    agent_feedback[node_id] = {
                        "agent_id": (node.get("config") or {}).get("agent_id") or node_id,
                        "agent_name": (node.get("config") or {}).get("agent_name")
                        or node.get("label")
                        or node_id,
                        "role": role,
                        "message": str(getattr(result, "message", "") or ""),
                        "self_score": 7,
                        "notes": str(getattr(result, "message", "") or "")[:500],
                        "suggestion_for_next": str(
                            (node.get("config") or {}).get("rerun_suggestion") or ""
                        ),
                        "tool": tool_name,
                    }
            if self._is_cancelled(run["run_id"]):
                return
            refs = [ref for ref in (result.output_refs or []) if ref]
            primary = result.output_ref or (refs[0] if refs else None)
            if primary is not None and not refs:
                refs = [primary]
            if node_pipeline(node) == NODE_ROLE_FRAME:
                image_refs = [ref for ref in refs if _ref_kind(ref) == "image"]
                if image_refs:
                    primary = image_refs[0]
                    refs = [image_refs[0]]
            current = (run.get("node_states") or {}).get(node_id) or {}
            kept = current.get("output_ref") if _usable_ref(current.get("output_ref")) else None
            kept_refs = [ref for ref in (current.get("output_refs") or []) if _usable_ref(ref)]
            if kept is not None and not kept_refs:
                kept_refs = [kept]
            incoming_uri = str((primary or {}).get("uri") or "") if primary else ""
            kept_uri = str((kept or {}).get("uri") or "") if kept else ""
            # Auto-accept new outputs — never pause the pipeline for one-by-one approval.
            auto_accept = bool(
                (graph.get("metadata") or {}).get("auto_accept_outputs", True)
            )
            pending = bool(
                not auto_accept
                and kept
                and primary
                and incoming_uri
                and incoming_uri != kept_uri
            )
            if pending and (
                node_pipeline(node) == NODE_ROLE_COMPOSE or _should_auto_promote(kept, primary)
            ):
                pending = False
            async with lock:
                self._set_node_state(
                    run,
                    node_id,
                    {
                        "status": NODE_STATUS_COMPLETED,
                        "started_at": started_at,
                        "completed_at": utc_now_ms(),
                        "output_ref": kept if pending else primary,
                        "output_refs": kept_refs if pending else refs,
                        "candidate_output_ref": primary if pending else None,
                        "candidate_output_refs": refs if pending else [],
                        "error": None,
                        "blocked_by": [],
                    },
                )
            if node_pipeline(node) == NODE_ROLE_STORYBOARD:
                live_graph = self._require_graph(str(run.get("graph_id") or graph.get("graph_id") or ""))
                self._expand_clips_if_needed(live_graph, run, set(), on_update=on_update)
            # Stamp prior-shot prompt handoff after frame/clip media completes.
            if node_pipeline(node) in {NODE_ROLE_FRAME, NODE_ROLE_CLIP}:
                live_graph = self._require_graph(
                    str(run.get("graph_id") or graph.get("graph_id") or "")
                )
                cfg_done = dict(node.get("config") or {})
                approved = str(
                    cfg_done.get("last_approved_prompt")
                    or (cfg_done.get("generate") or {}).get("prompt")
                    or ""
                ).strip()
                shot_index = int(cfg_done.get("shot_index") or 0)
                if approved:
                    for n in live_graph.get("nodes") or []:
                        if str(n.get("id") or "") != node_id:
                            continue
                        c = dict(n.get("config") or {})
                        c["last_approved_prompt"] = approved[:4000]
                        if node_pipeline(node) == NODE_ROLE_CLIP:
                            c["last_wan_prompt"] = approved[:4000]
                            c["clip_prompt_preview"] = approved[:1200]
                        n["config"] = c
                        break
                    if node_pipeline(node) == NODE_ROLE_FRAME and shot_index >= 1:
                        next_frame = f"n_frame_{shot_index + 1}"
                        next_clip = f"n_clip_{shot_index + 1}"
                        for n in live_graph.get("nodes") or []:
                            nid = str(n.get("id") or "")
                            if nid not in {next_frame, next_clip}:
                                continue
                            c = dict(n.get("config") or {})
                            c["previous_keyframe_prompt"] = approved[:3500]
                            c["previous_keyframe_node_id"] = node_id
                            n["config"] = c
                    if node_pipeline(node) == NODE_ROLE_CLIP and shot_index >= 1:
                        from jiuwenswarm.server.runtime.designer.experiments.clip_prompt_handoff import (
                            stamp_wan_prompt_handoff,
                        )

                        stamp_wan_prompt_handoff(
                            live_graph,
                            shot_index=shot_index,
                            prompt=approved,
                            node_id=node_id,
                        )
                    graph = self._store.save_graph(live_graph)
        except Exception as exc:  # noqa: BLE001
            async with lock:
                self._set_node_state(
                    run,
                    node_id,
                    {
                        "status": NODE_STATUS_FAILED,
                        "started_at": started_at,
                        "completed_at": utc_now_ms(),
                        "error": _exception_text(exc),
                    },
                )
                run["status"] = RUN_STATUS_FAILED
            if traj is not None:
                traj.record(
                    agent_id=node_id,
                    action="execute_failed",
                    phase="node",
                    role=role,
                    tool=tool_name,
                    status="error",
                    detail={"error": _exception_text(exc)},
                )
            if agent_feedback is not None:
                agent_feedback[node_id] = {
                    "agent_id": node_id,
                    "role": role,
                    "self_score": 2,
                    "notes": _exception_text(exc),
                    "suggestion_for_next": "Retry with adjusted params from manager plan",
                    "tool": tool_name,
                }
        finally:
            async with lock:
                run["updated_at"] = utc_now_ms()
                self._publish(run, on_update, node_id)
                self._store.save_run(run)

    async def _await_pause(self, run_id: str) -> None:
        pause_flag = self._pause_flags.get(run_id)
        if pause_flag is None:
            return
        await pause_flag.wait()

    def _is_cancelled(self, run_id: str) -> bool:
        cancel_flag = self._cancel_flags.get(run_id)
        return cancel_flag is not None and cancel_flag.is_set()

    def _resync_run_after_graph_redesign(
        self,
        run: DesignerExecutionRun,
        graph: DesignerExecutionGraph,
    ) -> None:
        """Keep run.node_states aligned after Supervisor redesigns topology."""
        live_ids = {
            str(node.get("id") or "")
            for node in (graph.get("nodes") or [])
            if isinstance(node, dict) and node.get("id")
        }
        states = dict(run.get("node_states") or {})
        # Drop states for removed nodes; seed pending for new ones.
        states = {nid: st for nid, st in states.items() if nid in live_ids}
        for nid in live_ids:
            if nid not in states:
                states[nid] = {"status": NODE_STATUS_PENDING}
        run["node_states"] = states
        run["updated_at"] = utc_now_ms()
        self._store.save_run(run)
        self._live_runs[str(run.get("run_id") or "")] = run

    def _publish_graph(
        self,
        run: DesignerExecutionRun,
        graph: DesignerExecutionGraph,
    ) -> None:
        callback = self._on_graph_updates.get(str(run.get("run_id") or ""))
        if callback is not None:
            callback(deepcopy(graph))

    @staticmethod
    def _set_node_state(
        run: DesignerExecutionRun,
        node_id: str,
        patch: DesignerNodeState,
    ) -> None:
        states = run.setdefault("node_states", {})
        current = dict(states.get(node_id) or {"status": NODE_STATUS_PENDING})
        current.update(patch)
        states[node_id] = current

    @staticmethod
    def _publish(
        run: DesignerExecutionRun,
        on_update: RunUpdateCallback | None,
        node_id: str | None = None,
    ) -> None:
        if on_update is not None:
            on_update(run, node_id)


def _image_output_refs(state: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_refs = list(state.get("output_refs") or [])
    primary = state.get("output_ref")
    if primary is not None:
        raw_refs = [primary, *raw_refs]
    for ref in raw_refs:
        if not _usable_ref(ref) or _ref_kind(ref) != "image":
            continue
        uri = str((ref or {}).get("uri") or "")
        if not uri or uri in seen:
            continue
        seen.add(uri)
        refs.append(ref)
    return refs


def redistribute_frame_node_states(run: DesignerExecutionRun, shot_count: int) -> None:
    """Split a bundled keyframe node (many PNGs) into one image per n_frame_i."""
    count = max(1, min(int(shot_count or 1), MAX_SHOT_CLIP_NODES))
    states = run.setdefault("node_states", {})
    bundled: list[dict[str, Any]] = []
    source_status = NODE_STATUS_PENDING
    for node_id in ("n_frame", *[frame_node_id(index) for index in range(1, count + 1)]):
        state = states.get(node_id) or {}
        images = _image_output_refs(state)
        if len(images) > len(bundled):
            bundled = images
            source_status = str(state.get("status") or NODE_STATUS_PENDING)
    for index in range(1, count + 1):
        node_id = frame_node_id(index)
        state = dict(states.get(node_id) or {"status": NODE_STATUS_PENDING})
        images = _image_output_refs(state)
        if index <= len(bundled):
            ref = bundled[index - 1]
            state["output_ref"] = ref
            state["output_refs"] = [ref]
            if source_status == NODE_STATUS_COMPLETED and state.get("status") == NODE_STATUS_PENDING:
                state["status"] = NODE_STATUS_COMPLETED
                state["error"] = None
        elif len(images) > 1:
            state["output_ref"] = images[0]
            state["output_refs"] = [images[0]]
        elif images:
            state["output_ref"] = images[0]
            state["output_refs"] = [images[0]]
        states[node_id] = state


def _usable_ref(ref: object) -> bool:
    if not isinstance(ref, dict):
        return False
    uri = str(ref.get("uri") or "").strip()
    return bool(uri) and not uri.startswith("designer://")


_TEXT_FALLBACK_KINDS = {"text", "table"}
_MEDIA_KINDS = {"image", "video", "audio"}
_MEDIA_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".mp4",
    ".webm",
    ".mov",
    ".m4v",
)


def _ref_kind(ref: object) -> str:
    if not isinstance(ref, dict):
        return ""
    kind = str(ref.get("kind") or "").strip().lower()
    if kind:
        return kind
    mime = str(ref.get("mime_type") or "").strip().lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("text/"):
        return "text"
    label = f"{ref.get('label') or ''} {ref.get('uri') or ''}".lower()
    if ".md" in label:
        return "text"
    if any(ext in label for ext in _MEDIA_SUFFIXES):
        return "video" if any(ext in label for ext in (".mp4", ".webm", ".mov", ".m4v")) else "image"
    return ""


def _is_fallback_text_ref(ref: object) -> bool:
    return _ref_kind(ref) in _TEXT_FALLBACK_KINDS


def _is_media_ref(ref: object) -> bool:
    return _ref_kind(ref) in _MEDIA_KINDS


def _should_auto_promote(kept: object, primary: object) -> bool:
    """Replace fallback notes with a newly generated image/video instead of asking."""
    return _is_fallback_text_ref(kept) and _is_media_ref(primary)


def _node_by_id(graph: DesignerExecutionGraph, node_id: str) -> DesignerGraphNode:
    for node in graph.get("nodes", []):
        if node.get("id") == node_id:
            return node
    raise KeyError(f"node not found: {node_id}")


def _exception_text(exc: BaseException) -> str:
    text = str(exc).strip()
    if text:
        return text
    if isinstance(exc, TimeoutError):
        return "timed out"
    return type(exc).__name__


def _node_execute_timeout_sec(node: DesignerGraphNode) -> float:
    """Hard cap so one leaf cannot hang the ready-queue forever.

    Agent + media materialization share this budget (DeepAgent turns then
    image/video backends), so keep it generous for Wan / image_gen latency.
    """
    pipeline = node_pipeline(node)
    if pipeline in {NODE_ROLE_CLIP, NODE_ROLE_COMPOSE}:
        return 1800.0
    if pipeline in {NODE_ROLE_FRAME, "character", "character_design", "scene"}:
        return 1200.0
    if pipeline in {"speech", "music"}:
        return 600.0
    return 900.0


def _is_ready(
    node_id: str,
    run: DesignerExecutionRun,
    incoming: dict[str, list[str]],
    groups: dict[str, frozenset[str]],
) -> bool:
    state = run.get("node_states", {}).get(node_id) or {}
    if state.get("status") != NODE_STATUS_PENDING:
        return False
    preds = incoming.get(node_id, [])
    for pred in preds:
        group = groups.get(pred, frozenset({pred}))
        for member in group:
            # Sync groups often include this node (Align edges). Requiring it
            # completed before it can start deadlocks storyboard forever.
            if member == node_id:
                continue
            member_status = (run.get("node_states", {}).get(member) or {}).get("status")
            if member_status != NODE_STATUS_COMPLETED:
                return False
    return True
