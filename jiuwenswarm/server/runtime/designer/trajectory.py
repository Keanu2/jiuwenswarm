# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Detailed per-agent trajectory + feedback bundle for Designer runs."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Iterator

from jiuwenswarm.common.schema.designer_graph import utc_now_ms
from jiuwenswarm.server.runtime.designer.paths import (
    latest_run_bundle_path,
    run_bundle_path,
)

logger = logging.getLogger(__name__)

TRAJECTORY_SCHEMA = "designer-trajectory.v1"

_lock = threading.RLock()
_active: dict[str, "TrajectoryRecorder"] = {}


class TrajectoryRecorder:
    """In-memory run timeline persisted as JSON beside the catalog package."""

    def __init__(self, graph_id: str, run_id: str) -> None:
        self.graph_id = graph_id
        self.run_id = run_id
        self.started_at_ms = utc_now_ms()
        self.ended_at_ms: int | None = None
        self.events: list[dict[str, Any]] = []
        self.agents: dict[str, dict[str, Any]] = {}
        self.feedback: dict[str, Any] = {}
        self.meta: dict[str, Any] = {}

    def _agent(self, agent_id: str, *, role: str = "") -> dict[str, Any]:
        bucket = self.agents.setdefault(
            agent_id,
            {
                "agent_id": agent_id,
                "role": role,
                "total_ms": 0.0,
                "tool_calls": 0,
                "events": [],
            },
        )
        if role and not bucket.get("role"):
            bucket["role"] = role
        return bucket

    def record(
        self,
        *,
        agent_id: str,
        action: str,
        phase: str = "work",
        role: str = "",
        tool: str | None = None,
        duration_ms: float | None = None,
        detail: dict[str, Any] | None = None,
        status: str = "ok",
    ) -> None:
        event = {
            "ts_ms": utc_now_ms(),
            "agent_id": agent_id,
            "agent_role": role,
            "phase": phase,
            "action": action,
            "tool": tool,
            "duration_ms": duration_ms,
            "status": status,
            "detail": detail or {},
        }
        with _lock:
            self.events.append(event)
            bucket = self._agent(agent_id, role=role)
            bucket["events"].append(event)
            if duration_ms is not None:
                bucket["total_ms"] = float(bucket.get("total_ms") or 0.0) + float(duration_ms)
            if tool:
                bucket["tool_calls"] = int(bucket.get("tool_calls") or 0) + 1

    @contextmanager
    def span(
        self,
        *,
        agent_id: str,
        action: str,
        phase: str = "work",
        role: str = "",
        tool: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        started = time.perf_counter()
        payload: dict[str, Any] = dict(detail or {})
        status = "ok"
        try:
            yield payload
        except Exception as exc:  # noqa: BLE001
            status = "error"
            payload["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0
            self.record(
                agent_id=agent_id,
                action=action,
                phase=phase,
                role=role,
                tool=tool,
                duration_ms=duration_ms,
                detail=payload,
                status=status,
            )

    def set_feedback(self, feedback: dict[str, Any]) -> None:
        with _lock:
            self.feedback = deepcopy(feedback)

    def merge_feedback(self, patch: dict[str, Any]) -> None:
        with _lock:
            for key, value in patch.items():
                if isinstance(value, dict) and isinstance(self.feedback.get(key), dict):
                    merged = dict(self.feedback[key])
                    merged.update(value)
                    self.feedback[key] = merged
                else:
                    self.feedback[key] = value

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TRAJECTORY_SCHEMA,
            "graph_id": self.graph_id,
            "run_id": self.run_id,
            "started_at_ms": self.started_at_ms,
            "ended_at_ms": self.ended_at_ms or utc_now_ms(),
            "meta": deepcopy(self.meta),
            "agents": deepcopy(self.agents),
            "events": deepcopy(self.events),
            "feedback": deepcopy(self.feedback),
        }

    def save(self) -> str:
        self.ended_at_ms = utc_now_ms()
        path = run_bundle_path(self.graph_id, self.run_id)
        tmp = path.with_suffix(".json.tmp")
        body = self.to_dict()
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(body, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        latest = path.parent / "latest.json"
        with latest.open("w", encoding="utf-8") as fh:
            json.dump({"path": str(path), "run_id": self.run_id}, fh, ensure_ascii=False, indent=2)
        # Also mirror into agent feedback store for rerun helpers
        try:
            from jiuwenswarm.server.runtime.designer.feedback import save_feedback

            save_feedback(self.graph_id, self.run_id, body.get("feedback") or {"trajectory_path": str(path)})
        except Exception:  # noqa: BLE001
            logger.debug("mirror feedback failed", exc_info=True)
        return str(path)


def begin_trajectory(graph_id: str, run_id: str, *, meta: dict[str, Any] | None = None) -> TrajectoryRecorder:
    rec = TrajectoryRecorder(graph_id, run_id)
    if meta:
        rec.meta.update(meta)
    with _lock:
        _active[run_id] = rec
    return rec


def get_trajectory(run_id: str) -> TrajectoryRecorder | None:
    with _lock:
        return _active.get(run_id)


def end_trajectory(run_id: str) -> str | None:
    with _lock:
        rec = _active.pop(run_id, None)
    if rec is None:
        return None
    return rec.save()


def load_latest_bundle(graph_id: str) -> dict[str, Any] | None:
    path = latest_run_bundle_path(graph_id)
    if path is None or not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load trajectory bundle %s: %s", path, exc)
        return None


def load_prior_feedback(graph_id: str) -> dict[str, Any] | None:
    bundle = load_latest_bundle(graph_id)
    if not bundle:
        from jiuwenswarm.server.runtime.designer.feedback import load_latest_feedback

        return load_latest_feedback(graph_id)
    feedback = bundle.get("feedback")
    return feedback if isinstance(feedback, dict) else None
