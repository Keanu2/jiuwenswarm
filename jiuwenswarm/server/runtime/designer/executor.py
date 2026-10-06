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
    node_role,
    node_uses_agent_runtime,
    sync_groups,
    utc_now_ms,
)
from jiuwenswarm.common.schema.message import EventType
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
        ) or node_role(target_node) == NODE_ROLE_COMPOSE:
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
                for state in (run.get("node_states") or {}).values()
            )
            if not pending:
                return run
        graph = self._require_graph(run["graph_id"])
        from jiuwenswarm.server.runtime.designer.model_tools import llm_available

        # AI-first: keep agent delegate when models exist; handlers only as no-LLM fallback.
        # force_handler nodes (music/speech beds) stay on the fast handler path.
        use_agents = llm_available()
        for node in graph.get("nodes") or []:
            cfg = node.setdefault("config", {})
            if isinstance(cfg, dict):
                if cfg.get("force_handler"):
                    cfg["delegate"] = "handler"
                else:
                    cfg["delegate"] = "agent" if use_agents else "handler"
                    if use_agents and cfg.get("skip_llm"):
                        cfg["skip_llm"] = False
        meta = dict(graph.get("metadata") or {})
        meta["ai_agent_pipeline"] = use_agents
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
        if graph_uses_agent_scheduler(graph):
            await self._execute_agent_run(graph, run, on_update=on_update)
            return
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
                    state.get("status") for state in (run.get("node_states") or {}).values()
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
        if (
            str(meta0.get("scenario") or "") == "video"
            and use_llm_orch
            and (
                meta0.get("pending_llm_analysis")
                or str(meta0.get("script_analysis_mode") or "") != "llm"
            )
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
                    if node_role(n) == NODE_ROLE_FRAME
                ]
                clip_pending = [
                    n
                    for n in (graph.get("nodes") or [])
                    if node_role(n) == NODE_ROLE_CLIP
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
            statuses = {state.get("status") for state in run["node_states"].values()}
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
                manager_review = await ManagerAgent().dual_rate_final(
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
            node_role(node) in {NODE_ROLE_CLIP, NODE_ROLE_COMPOSE}
            or str(node.get("id") or "") in {"n_clip", "n_compose"}
            or str(node.get("id") or "").startswith("n_clip_")
            for node in graph.get("nodes") or []
        )
        if not has_clip_pipeline:
            return graph, remaining, data_predecessors(graph), sync_groups(graph)
        current_clip_ids = {
            str(node.get("id") or "")
            for node in graph.get("nodes") or []
            if node_role(node) == NODE_ROLE_CLIP
        }
        current_frame_ids = {
            str(node.get("id") or "")
            for node in graph.get("nodes") or []
            if node_role(node) == NODE_ROLE_FRAME
        }
        wanted_clip_ids = {clip_node_id(index) for index in range(1, shot_count + 1)}
        wanted_frame_ids = {frame_node_id(index) for index in range(1, shot_count + 1)}
        has_compose = any(
            node_role(node) == NODE_ROLE_COMPOSE or str(node.get("id") or "") == "n_compose"
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

        saved = self._store.save_graph(
            apply_shot_generate_prompts(expand_shot_nodes(graph, shot_count), prompts)
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
                if node_role(node) == NODE_ROLE_STORYBOARD
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
        from jiuwenswarm.common.schema.designer_graph import node_role as _node_role_fn
        from jiuwenswarm.server.runtime.designer.skills_loader import (
            load_agent_skill,
            load_subject_skill,
        )
        from jiuwenswarm.server.runtime.designer.trajectory import get_trajectory

        role = str(
            (node.get("config") or {}).get("role")
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
                ctx = NodeExecutionContext(
                    graph=graph,
                    run_id=run["run_id"],
                    node_id=node_id,
                    run=run,
                )
                if node_uses_agent_runtime(node):
                    result = await asyncio.wait_for(
                        self._host.execute(node, ctx),
                        timeout=_node_execute_timeout_sec(node),
                    )
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
            if node_role(node) == NODE_ROLE_FRAME:
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
                node_role(node) == NODE_ROLE_COMPOSE or _should_auto_promote(kept, primary)
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
            if node_role(node) == NODE_ROLE_STORYBOARD:
                live_graph = self._require_graph(str(run.get("graph_id") or graph.get("graph_id") or ""))
                self._expand_clips_if_needed(live_graph, run, set(), on_update=on_update)
        except Exception as exc:  # noqa: BLE001
            async with lock:
                self._set_node_state(
                    run,
                    node_id,
                    {
                        "status": NODE_STATUS_FAILED,
                        "started_at": started_at,
                        "completed_at": utc_now_ms(),
                        "error": str(exc),
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
                    detail={"error": str(exc)},
                )
            if agent_feedback is not None:
                agent_feedback[node_id] = {
                    "agent_id": node_id,
                    "role": role,
                    "self_score": 2,
                    "notes": str(exc),
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


def _node_execute_timeout_sec(node: DesignerGraphNode) -> float:
    """Hard cap so one leaf cannot hang the ready-queue forever."""
    role = node_role(node)
    if role in {NODE_ROLE_CLIP, NODE_ROLE_COMPOSE}:
        return 480.0
    if role in {NODE_ROLE_FRAME, "character", "character_design", "scene"}:
        return 300.0
    if role in {"speech", "music"}:
        return 120.0
    return 180.0


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
