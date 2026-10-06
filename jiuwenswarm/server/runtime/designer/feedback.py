# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persisted Designer pipeline feedback (self/supervisor/manager ratings)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_agent_root_dir

logger = logging.getLogger(__name__)

FEEDBACK_SCHEMA = "designer-feedback.v1"


def _feedback_dir(graph_id: str) -> Path:
    path = get_agent_root_dir() / "designer" / "feedback" / graph_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def feedback_path(graph_id: str, run_id: str) -> Path:
    return _feedback_dir(graph_id) / f"{run_id}.json"


def latest_feedback_path(graph_id: str) -> Path | None:
    directory = _feedback_dir(graph_id)
    files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def load_feedback(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load feedback %s: %s", path, exc)
        return None


def load_latest_feedback(graph_id: str) -> dict[str, Any] | None:
    return load_feedback(latest_feedback_path(graph_id))


def save_feedback(graph_id: str, run_id: str, payload: dict[str, Any]) -> Path:
    path = feedback_path(graph_id, run_id)
    tmp = path.with_suffix(".json.tmp")
    body = dict(payload)
    body.setdefault("schema_version", FEEDBACK_SCHEMA)
    body["graph_id"] = graph_id
    body["run_id"] = run_id
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(body, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    # Pointer for quick lookup
    latest = _feedback_dir(graph_id) / "latest.json"
    with latest.open("w", encoding="utf-8") as fh:
        json.dump({"path": str(path), "run_id": run_id}, fh, ensure_ascii=False, indent=2)
    return path


def suggestion_for_node(feedback: dict[str, Any] | None, node_id: str) -> str:
    if not feedback:
        return ""
    supervisor = feedback.get("supervisor") or {}
    suggestions = supervisor.get("suggestions") or {}
    if isinstance(suggestions, dict) and suggestions.get(node_id):
        return str(suggestions[node_id])
    agents = feedback.get("agents") or {}
    agent = agents.get(node_id) or {}
    if agent.get("suggestion_for_next"):
        return str(agent["suggestion_for_next"])
    final = feedback.get("final") or {}
    return str(final.get("improvement_plan") or "")
