# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Designer execution graph domain schema (canonical Python contract).

Shared between the React Flow frontend, gateway RPC handlers, and the graph
executor.  Frontend mirrors live in
``channels/web/frontend/src/features/designer/executionGraphTypes.ts``;
cross-layer literals are pinned by
``tests/unit_tests/test_designer_execution_graph_contract.py``.
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Any, TypedDict

# ── Schema version ────────────────────────────────────────────────────────────

SCHEMA_VERSION = "designer-execution-graph.v1"
"""Domain graph payload version."""

RUN_SCHEMA_VERSION = "designer-execution-run.v1"
"""Execution run state payload version."""

# ── Node types (modality, contract-pinned) ────────────────────────────────────

NODE_TYPE_TEXT = "text"
NODE_TYPE_TABLE = "table"
NODE_TYPE_IMAGE = "image"
NODE_TYPE_VIDEO = "video"
NODE_TYPE_AUDIO = "audio"

NODE_TYPES: frozenset[str] = frozenset(
    {
        NODE_TYPE_TEXT,
        NODE_TYPE_TABLE,
        NODE_TYPE_IMAGE,
        NODE_TYPE_VIDEO,
        NODE_TYPE_AUDIO,
    }
)

# ── Node roles (handler dispatch; modality type stays on node.type) ───────────

NODE_ROLE_BRIEF = "brief"
NODE_ROLE_CHARACTER_DESIGN = "character_design"
NODE_ROLE_SCENE = "scene"
NODE_ROLE_STORYBOARD = "storyboard"
NODE_ROLE_FRAME = "frame"
NODE_ROLE_CLIP = "clip"
NODE_ROLE_COMPOSE = "compose"
NODE_ROLE_MUSIC = "music"
NODE_ROLE_SPEECH = "speech"

NODE_ROLES: frozenset[str] = frozenset(
    {
        NODE_ROLE_BRIEF,
        NODE_ROLE_CHARACTER_DESIGN,
        NODE_ROLE_SCENE,
        NODE_ROLE_STORYBOARD,
        NODE_ROLE_FRAME,
        NODE_ROLE_CLIP,
        NODE_ROLE_COMPOSE,
        NODE_ROLE_MUSIC,
        NODE_ROLE_SPEECH,
    }
)

_LEGACY_ROLE_LABELS = {
    NODE_ROLE_BRIEF: (
        "项目 brief",
        "Brief",
        "brief",
    ),
    NODE_ROLE_CHARACTER_DESIGN: (
        "角色图",
        "Character",
        "character",
    ),
    NODE_ROLE_SCENE: (
        "场景图",
        "场景",
        "Scene",
        "scene",
    ),
    NODE_ROLE_STORYBOARD: (
        "分镜表",
        "Storyboard",
        "storyboard",
    ),
    NODE_ROLE_COMPOSE: (
        "成片",
        "Film",
        "Compose",
        "compose",
    ),
}
_INDEXED_LABEL_RE = re.compile(
    r"^(?:关键帧|视频片段|Keyframe|Clip)\s*(\d+)?$",
    re.IGNORECASE,
)


def pipeline_node_label(role: str, shot_index: int = 1) -> str:
    """English-first canvas title for a Designer pipeline role."""
    index = max(1, int(shot_index or 1))
    if role == NODE_ROLE_BRIEF:
        return "Brief"
    if role == NODE_ROLE_CHARACTER_DESIGN:
        return "Character"
    if role == NODE_ROLE_SCENE:
        return "Scene"
    if role == NODE_ROLE_STORYBOARD:
        return "Storyboard"
    if role == NODE_ROLE_FRAME:
        return f"Keyframe {index}"
    if role == NODE_ROLE_CLIP:
        return f"Clip {index}"
    if role == NODE_ROLE_COMPOSE:
        return "Film"
    return ""


def english_pipeline_label(label: str, role: str, shot_index: int = 1) -> str:
    """Rewrite known Chinese/legacy titles; leave custom names alone."""
    desired = pipeline_node_label(role, shot_index)
    if not desired:
        return label
    text = (label or "").strip()
    if not text:
        return desired
    aliases = _LEGACY_ROLE_LABELS.get(role, ())
    if text in aliases or text.casefold() in {item.casefold() for item in aliases}:
        return desired
    indexed = _INDEXED_LABEL_RE.match(text)
    if indexed and role in {NODE_ROLE_FRAME, NODE_ROLE_CLIP}:
        return desired
    return text

# ── Node config (role-discriminated; modality stays on node.type) ─────────────

CONFIG_KEY_ROLE = "role"
CONFIG_KEY_PROMPT = "prompt"
CONFIG_KEY_INPUTS = "inputs"
CONFIG_KEY_DELEGATE = "delegate"
CONFIG_KEY_AGENT_TEMPLATE = "agent_template"
CONFIG_KEY_COLLABORATE = "collaborate"
CONFIG_KEY_GENERATE = "generate"
CONFIG_KEY_UPLOAD = "upload"
CONFIG_KEY_EDIT = "edit"
CONFIG_KEY_INTERACTION_MODE = "interaction_mode"
CONFIG_KEY_MATERIALS = "materials"

CONFIG_KEYS: frozenset[str] = frozenset(
    {
        CONFIG_KEY_ROLE,
        CONFIG_KEY_PROMPT,
        CONFIG_KEY_INPUTS,
        CONFIG_KEY_DELEGATE,
        CONFIG_KEY_AGENT_TEMPLATE,
        CONFIG_KEY_COLLABORATE,
        CONFIG_KEY_GENERATE,
        CONFIG_KEY_UPLOAD,
        CONFIG_KEY_EDIT,
        CONFIG_KEY_INTERACTION_MODE,
        CONFIG_KEY_MATERIALS,
    }
)

CONFIG_INTERACTION_MODES: frozenset[str] = frozenset({"generate", "upload", "edit"})

GENERATE_PROMPT_ORIGIN_STORYBOARD = "storyboard"
GENERATE_PROMPT_ORIGIN_USER = "user"

CONFIG_DELEGATE_HANDLER = "handler"
CONFIG_DELEGATE_SUBAGENT = "subagent"
CONFIG_DELEGATE_AGENT = "agent"

CONFIG_DELEGATES: frozenset[str] = frozenset(
    {
        CONFIG_DELEGATE_HANDLER,
        CONFIG_DELEGATE_SUBAGENT,
        CONFIG_DELEGATE_AGENT,
    }
)

DESIGNER_AGENT_GROUP_NAME = "designer"

ROLE_DEFAULT_TEMPLATES: dict[str, str] = {
    NODE_ROLE_BRIEF: f"{DESIGNER_AGENT_GROUP_NAME}/leader",
    NODE_ROLE_CHARACTER_DESIGN: f"{DESIGNER_AGENT_GROUP_NAME}/character",
    NODE_ROLE_SCENE: f"{DESIGNER_AGENT_GROUP_NAME}/scene",
    NODE_ROLE_STORYBOARD: f"{DESIGNER_AGENT_GROUP_NAME}/storyboard",
    NODE_ROLE_FRAME: f"{DESIGNER_AGENT_GROUP_NAME}/frame",
    NODE_ROLE_CLIP: f"{DESIGNER_AGENT_GROUP_NAME}/clip",
    NODE_ROLE_COMPOSE: f"{DESIGNER_AGENT_GROUP_NAME}/clip",
}

# ── Edge kinds ────────────────────────────────────────────────────────────────

EDGE_KIND_DATA = "data"
EDGE_KIND_SYNC = "sync"

EDGE_KINDS: frozenset[str] = frozenset({EDGE_KIND_DATA, EDGE_KIND_SYNC})

# ── Graph sources ─────────────────────────────────────────────────────────────

GRAPH_SOURCE_PROMPT = "prompt"
GRAPH_SOURCE_MANUAL = "manual"

GRAPH_SOURCES: frozenset[str] = frozenset(
    {
        GRAPH_SOURCE_PROMPT,
        GRAPH_SOURCE_MANUAL,
    }
)

# ── Node / run statuses ───────────────────────────────────────────────────────

NODE_STATUS_PENDING = "pending"
NODE_STATUS_RUNNING = "running"
NODE_STATUS_COMPLETED = "completed"
NODE_STATUS_FAILED = "failed"
NODE_STATUS_CANCELLED = "cancelled"

NODE_STATUSES: frozenset[str] = frozenset(
    {
        NODE_STATUS_PENDING,
        NODE_STATUS_RUNNING,
        NODE_STATUS_COMPLETED,
        NODE_STATUS_FAILED,
        NODE_STATUS_CANCELLED,
    }
)

RUN_STATUS_DRAFT = "draft"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_PAUSED = "paused"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"

RUN_STATUSES: frozenset[str] = frozenset(
    {
        RUN_STATUS_DRAFT,
        RUN_STATUS_RUNNING,
        RUN_STATUS_PAUSED,
        RUN_STATUS_COMPLETED,
        RUN_STATUS_FAILED,
        RUN_STATUS_CANCELLED,
    }
)

# ── ID helpers ────────────────────────────────────────────────────────────────

_GRAPH_ID_PREFIX = "graph_"
_RUN_ID_PREFIX = "run_"
_ID_HEX_LEN = 8


def new_graph_id() -> str:
    return f"{_GRAPH_ID_PREFIX}{secrets.token_hex(_ID_HEX_LEN)}"


def new_run_id() -> str:
    return f"{_RUN_ID_PREFIX}{secrets.token_hex(_ID_HEX_LEN)}"


def utc_now_ms() -> int:
    return int(time.time() * 1000)


# ── Typed payloads ──────────────────────────────────────────────────────────────


class AssetRef(TypedDict, total=False):
    kind: str
    uri: str
    mime_type: str
    label: str


class NodeLayout(TypedDict, total=False):
    x: float
    y: float
    width: float
    height: float


class DesignerNodeConfig(TypedDict, total=False):
    """Typed node config. ``role`` is the handler discriminator."""

    role: str
    prompt: str
    inputs: list[str]
    delegate: str
    agent_template: str
    collaborate: bool
    generate: dict[str, Any]
    upload: dict[str, Any]
    edit: dict[str, Any]
    interaction_mode: str
    materials: list[Any]
    shot_index: int


class DesignerGraphPatch(TypedDict, total=False):
    title: str
    description: str
    upsert_nodes: list[Any]
    upsert_edges: list[Any]
    remove_node_ids: list[str]
    remove_edge_ids: list[str]


class DesignerGraphNode(TypedDict, total=False):
    id: str
    type: str
    label: str
    config: DesignerNodeConfig
    layout: NodeLayout
    output_ref: AssetRef | None


class DesignerGraphEdge(TypedDict, total=False):
    id: str
    source: str
    target: str
    kind: str
    label: str


class DesignerExecutionGraph(TypedDict, total=False):
    schema_version: str
    graph_id: str
    project_id: str
    title: str
    description: str
    source: str
    nodes: list[DesignerGraphNode]
    edges: list[DesignerGraphEdge]
    metadata: dict[str, Any]
    created_at: int
    updated_at: int


class DesignerNodeState(TypedDict, total=False):
    status: str
    started_at: int | None
    completed_at: int | None
    output_ref: AssetRef | None
    output_refs: list[AssetRef]
    candidate_output_ref: AssetRef | None
    candidate_output_refs: list[AssetRef]
    error: str | None
    blocked_by: list[str]


class DesignerExecutionRun(TypedDict, total=False):
    schema_version: str
    run_id: str
    graph_id: str
    project_id: str
    status: str
    node_states: dict[str, DesignerNodeState]
    current_node_ids: list[str]
    created_at: int
    updated_at: int
    metadata: dict[str, Any]


# ── Validation / normalization ────────────────────────────────────────────────


class DesignerGraphValidationError(ValueError):
    """Raised when an execution graph payload fails schema validation."""


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DesignerGraphValidationError(f"{field} must be a non-empty string")
    return value.strip()


def normalize_layout(raw: Any) -> NodeLayout:
    layout: NodeLayout = {}
    if not isinstance(raw, dict):
        return layout
    for key in ("x", "y", "width", "height"):
        val = raw.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            layout[key] = float(val)  # type: ignore[literal-required]
    return layout


def normalize_asset_ref(raw: Any) -> AssetRef | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("output_ref must be an object")
    kind = raw.get("kind")
    uri = raw.get("uri")
    if not isinstance(kind, str) or not kind.strip():
        raise DesignerGraphValidationError("output_ref.kind must be a non-empty string")
    if not isinstance(uri, str) or not uri.strip():
        raise DesignerGraphValidationError("output_ref.uri must be a non-empty string")
    ref: AssetRef = {"kind": kind.strip(), "uri": uri.strip()}
    mime_type = raw.get("mime_type")
    if isinstance(mime_type, str) and mime_type.strip():
        ref["mime_type"] = mime_type.strip()
    label = raw.get("label")
    if isinstance(label, str) and label.strip():
        ref["label"] = label.strip()
    return ref


def normalize_node_config(raw: Any) -> DesignerNodeConfig:
    """Validate role-discriminated node config; keep unknown keys for forward compat."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("node.config must be an object")
    config: dict[str, Any] = {
        key: value for key, value in raw.items() if key not in CONFIG_KEYS
    }
    role = raw.get(CONFIG_KEY_ROLE)
    if role is not None and not (isinstance(role, str) and not role.strip()):
        if not isinstance(role, str) or not role.strip():
            raise DesignerGraphValidationError("node.config.role must be a non-empty string")
        role = role.strip()
        if role not in NODE_ROLES:
            raise DesignerGraphValidationError(f"unsupported node role: {role!r}")
        config[CONFIG_KEY_ROLE] = role
    prompt = raw.get(CONFIG_KEY_PROMPT)
    if prompt is not None:
        if not isinstance(prompt, str):
            raise DesignerGraphValidationError("node.config.prompt must be a string")
        config[CONFIG_KEY_PROMPT] = prompt
    inputs = raw.get(CONFIG_KEY_INPUTS)
    if inputs is not None:
        if not isinstance(inputs, list) or not all(
            isinstance(item, str) and item.strip() for item in inputs
        ):
            raise DesignerGraphValidationError(
                "node.config.inputs must be a non-empty-string array"
            )
        config[CONFIG_KEY_INPUTS] = [str(item).strip() for item in inputs]
    delegate = raw.get(CONFIG_KEY_DELEGATE)
    if delegate is not None and not (isinstance(delegate, str) and not delegate.strip()):
        if not isinstance(delegate, str) or delegate.strip() not in CONFIG_DELEGATES:
            raise DesignerGraphValidationError(
                "node.config.delegate must be 'handler', 'agent', or 'subagent'"
            )
        config[CONFIG_KEY_DELEGATE] = delegate.strip()
    agent_template = raw.get(CONFIG_KEY_AGENT_TEMPLATE)
    if agent_template is not None and not (
        isinstance(agent_template, str) and not agent_template.strip()
    ):
        if not isinstance(agent_template, str) or not agent_template.strip():
            raise DesignerGraphValidationError(
                "node.config.agent_template must be a non-empty string"
            )
        config[CONFIG_KEY_AGENT_TEMPLATE] = agent_template.strip()
    collaborate = raw.get(CONFIG_KEY_COLLABORATE)
    if collaborate is not None:
        if not isinstance(collaborate, bool):
            raise DesignerGraphValidationError("node.config.collaborate must be a boolean")
        config[CONFIG_KEY_COLLABORATE] = collaborate
    for object_key in (CONFIG_KEY_GENERATE, CONFIG_KEY_UPLOAD, CONFIG_KEY_EDIT):
        value = raw.get(object_key)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise DesignerGraphValidationError(f"node.config.{object_key} must be an object")
        config[object_key] = dict(value)
    interaction_mode = raw.get(CONFIG_KEY_INTERACTION_MODE)
    if interaction_mode is not None:
        if (
            not isinstance(interaction_mode, str)
            or interaction_mode.strip() not in CONFIG_INTERACTION_MODES
        ):
            raise DesignerGraphValidationError(
                "node.config.interaction_mode must be generate, upload, or edit"
            )
        config[CONFIG_KEY_INTERACTION_MODE] = interaction_mode.strip()
    materials = raw.get(CONFIG_KEY_MATERIALS)
    if materials is not None:
        if not isinstance(materials, list):
            raise DesignerGraphValidationError("node.config.materials must be an array")
        config[CONFIG_KEY_MATERIALS] = list(materials)
    return config  # type: ignore[return-value]


def normalize_node(raw: Any) -> DesignerGraphNode:
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("node must be an object")
    node_id = _require_str(raw.get("id"), "node.id")
    node_type = _require_str(raw.get("type"), "node.type")
    if node_type not in NODE_TYPES:
        raise DesignerGraphValidationError(f"unsupported node type: {node_type!r}")
    label = raw.get("label")
    config = normalize_node_config(raw.get("config"))
    role = str(config.get(CONFIG_KEY_ROLE) or "").strip()
    shot_raw = config.get("shot_index")
    try:
        shot_index = int(shot_raw) if shot_raw is not None else 1
    except (TypeError, ValueError):
        shot_index = 1
    current_label = str(label).strip() if isinstance(label, str) and label.strip() else node_id
    node: DesignerGraphNode = {
        "id": node_id,
        "type": node_type,
        "label": english_pipeline_label(current_label, role, shot_index),
        "config": config,
        "layout": normalize_layout(raw.get("layout")),
    }
    if "output_ref" in raw:
        node["output_ref"] = normalize_asset_ref(raw.get("output_ref"))
    return node


def normalize_edge(raw: Any) -> DesignerGraphEdge:
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("edge must be an object")
    edge_id = _require_str(raw.get("id"), "edge.id")
    source = _require_str(raw.get("source"), "edge.source")
    target = _require_str(raw.get("target"), "edge.target")
    kind_raw = raw.get("kind")
    kind = EDGE_KIND_DATA
    if isinstance(kind_raw, str) and kind_raw.strip():
        kind = kind_raw.strip()
    if kind not in EDGE_KINDS:
        raise DesignerGraphValidationError(f"unsupported edge kind: {kind!r}")
    edge: DesignerGraphEdge = {
        "id": edge_id,
        "source": source,
        "target": target,
        "kind": kind,
    }
    label = raw.get("label")
    if isinstance(label, str) and label.strip():
        text = label.strip()
        edge["label"] = "Align" if text in {"对齐", "Align", "align"} else text
    return edge


def normalize_execution_graph(raw: Any) -> DesignerExecutionGraph:
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("graph must be an object")
    schema_version = _require_str(raw.get("schema_version"), "schema_version")
    if schema_version != SCHEMA_VERSION:
        raise DesignerGraphValidationError(
            f"unsupported schema_version: {schema_version!r} (expected {SCHEMA_VERSION!r})"
        )
    graph_id = _require_str(raw.get("graph_id"), "graph_id")
    project_id = _require_str(raw.get("project_id"), "project_id")
    title = raw.get("title")
    description = raw.get("description")
    source = raw.get("source")
    graph_source = GRAPH_SOURCE_MANUAL
    if isinstance(source, str) and source.strip():
        graph_source = source.strip()
        if graph_source not in GRAPH_SOURCES:
            raise DesignerGraphValidationError(f"unsupported source: {graph_source!r}")
    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, list):
        raise DesignerGraphValidationError("nodes must be an array")
    raw_edges = raw.get("edges")
    if not isinstance(raw_edges, list):
        raise DesignerGraphValidationError("edges must be an array")
    nodes = [normalize_node(item) for item in raw_nodes]
    edges = [normalize_edge(item) for item in raw_edges]
    node_ids = {node["id"] for node in nodes}
    if len(node_ids) != len(nodes):
        raise DesignerGraphValidationError("node ids must be unique")
    for edge in edges:
        if edge["source"] not in node_ids or edge["target"] not in node_ids:
            raise DesignerGraphValidationError(
                f"edge {edge['id']!r} references unknown node id"
            )
    metadata = raw.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise DesignerGraphValidationError("metadata must be an object")
    now = utc_now_ms()
    created_at = raw.get("created_at")
    updated_at = raw.get("updated_at")
    graph: DesignerExecutionGraph = {
        "schema_version": SCHEMA_VERSION,
        "graph_id": graph_id,
        "project_id": project_id,
        "title": str(title).strip() if isinstance(title, str) and title.strip() else "Untitled",
        "description": str(description).strip() if isinstance(description, str) else "",
        "source": graph_source,
        "nodes": nodes,
        "edges": edges,
        "metadata": dict(metadata) if isinstance(metadata, dict) else {},
        "created_at": int(created_at) if isinstance(created_at, int) else now,
        "updated_at": int(updated_at) if isinstance(updated_at, int) else now,
    }
    return ensure_bootstrap_pipeline(graph)


def _append_unique_edge(
    edges: list[DesignerGraphEdge],
    *,
    edge_id: str,
    source: str,
    target: str,
    kind: str = EDGE_KIND_DATA,
    label: str | None = None,
) -> None:
    if any(edge.get("source") == source and edge.get("target") == target for edge in edges):
        return
    edge: DesignerGraphEdge = {"id": edge_id, "source": source, "target": target, "kind": kind}
    if label:
        edge["label"] = label
    edges.append(edge)


def _append_node_input(graph: DesignerExecutionGraph, node_id: str, input_id: str) -> None:
    for node in graph.get("nodes") or []:
        if node.get("id") != node_id:
            continue
        config = node.setdefault("config", {})
        if not isinstance(config, dict):
            return
        inputs = [str(item) for item in (config.get("inputs") or [])]
        if input_id not in inputs:
            inputs.append(input_id)
            config["inputs"] = inputs
        return


def ensure_bootstrap_pipeline(graph: DesignerExecutionGraph) -> DesignerExecutionGraph:
    """Keep old bootstrap graphs on the current clip / scene / keyframe pipeline."""
    metadata = graph.get("metadata") or {}
    if metadata.get("bootstrap") != "designer.graph.bootstrap.v1":
        return graph
    node_ids = {node["id"] for node in graph.get("nodes") or []}
    edges = graph.setdefault("edges", [])
    has_frame = "n_frame" in node_ids or any(
        str(item).startswith("n_frame_") for item in node_ids
    )
    if "n_brief" in node_ids and has_frame and "n_scene" not in node_ids:
        graph.setdefault("nodes", []).append(
            {
                "id": "n_scene",
                "type": NODE_TYPE_IMAGE,
                "label": "Scene",
                "config": {"role": NODE_ROLE_SCENE, "inputs": ["n_brief"]},
                "layout": {"x": 400, "y": 240, "width": 280, "height": 160},
            }
        )
        node_ids.add("n_scene")
    frame_ids = [
        str(node.get("id") or "")
        for node in graph.get("nodes") or []
        if node_role(node) == NODE_ROLE_FRAME
        or str(node.get("id") or "") == "n_frame"
        or str(node.get("id") or "").startswith("n_frame_")
    ]
    frame_ids = [item for item in frame_ids if item]
    if "n_scene" in node_ids:
        _append_unique_edge(edges, edge_id="e_brief_scene", source="n_brief", target="n_scene")
        for frame_id in frame_ids:
            _append_unique_edge(
                edges,
                edge_id=f"e_scene_{frame_id}",
                source="n_scene",
                target=frame_id,
            )
            _append_node_input(graph, frame_id, "n_scene")
    if "n_scene" in node_ids and "n_storyboard" in node_ids:
        _append_unique_edge(
            edges,
            edge_id="e_scene_storyboard",
            source="n_scene",
            target="n_storyboard",
            kind=EDGE_KIND_SYNC,
            label="Align",
        )
    if frame_ids:
        clip_nodes = [
            node
            for node in graph.get("nodes") or []
            if node_role(node) == NODE_ROLE_CLIP
            or str(node.get("id") or "") == "n_clip"
            or str(node.get("id") or "").startswith("n_clip_")
        ]
        if not clip_nodes and "n_clip" in node_ids:
            clip_nodes = [{"id": "n_clip"}]
        frame_by_shot = {node_shot_index(node): str(node.get("id") or "") for node in graph.get("nodes") or [] if str(node.get("id") or "") in frame_ids}
        for clip in clip_nodes:
            clip_id = str(clip.get("id") or "")
            if not clip_id:
                continue
            frame_id = frame_by_shot.get(node_shot_index(clip)) or frame_ids[0]
            _append_unique_edge(
                edges,
                edge_id=f"e_{frame_id}_{clip_id}",
                source=frame_id,
                target=clip_id,
            )
            _append_node_input(graph, clip_id, frame_id)
            if "n_scene" in node_ids:
                _append_unique_edge(
                    edges,
                    edge_id=f"e_scene_{clip_id}",
                    source="n_scene",
                    target=clip_id,
                )
                _append_node_input(graph, clip_id, "n_scene")
        clip_ids = [str(node.get("id") or "") for node in clip_nodes if node.get("id")]
        if clip_ids and "n_compose" not in node_ids:
            graph.setdefault("nodes", []).append(
                {
                    "id": "n_compose",
                    "type": NODE_TYPE_VIDEO,
                    "label": "Film",
                    "config": {"role": NODE_ROLE_COMPOSE, "inputs": list(clip_ids)},
                    "layout": compose_layout_right_of_clips(
                        [node.get("layout") for node in clip_nodes]
                    ),
                }
            )
            node_ids.add("n_compose")
        if "n_compose" in {node["id"] for node in graph.get("nodes") or []}:
            for clip_id in clip_ids:
                _append_unique_edge(
                    edges,
                    edge_id=f"e_{clip_id}_compose",
                    source=clip_id,
                    target="n_compose",
                )
                _append_node_input(graph, "n_compose", clip_id)
    return repair_overlapping_pipeline_layout(graph)


MAX_SHOT_CLIP_NODES = 6
COMPOSE_NODE_ID = "n_compose"
DEFAULT_NODE_WIDTH = 280.0
DEFAULT_NODE_HEIGHT = 160.0
LAYOUT_COL_GAP = 80.0
LAYOUT_ROW_GAP = 40.0
LAYOUT_ROW_STEP = DEFAULT_NODE_HEIGHT + LAYOUT_ROW_GAP


def _layout_box(layout: dict[str, Any] | None) -> tuple[float, float, float, float]:
    raw = layout if isinstance(layout, dict) else {}
    x = float(raw.get("x") or 0)
    y = float(raw.get("y") or 0)
    width = float(raw.get("width") or DEFAULT_NODE_WIDTH)
    height = float(raw.get("height") or DEFAULT_NODE_HEIGHT)
    return x, y, width, height


def compose_layout_right_of_clips(clip_layouts: list[dict[str, Any] | None]) -> dict[str, float]:
    """Place 成片 to the right of the clip column, vertically centered on that stack."""
    boxes = [_layout_box(layout) for layout in clip_layouts if layout is not None]
    if not boxes:
        return {
            "x": 1480.0,
            "y": 240.0,
            "width": DEFAULT_NODE_WIDTH,
            "height": DEFAULT_NODE_HEIGHT,
        }
    clip_right = max(x + width for x, _y, width, _h in boxes)
    stack_top = min(y for _x, y, _w, _h in boxes)
    stack_bottom = max(y + height for _x, y, _w, height in boxes)
    y = stack_top + max(0.0, (stack_bottom - stack_top - DEFAULT_NODE_HEIGHT) / 2.0)
    return {
        "x": clip_right + LAYOUT_COL_GAP,
        "y": y,
        "width": DEFAULT_NODE_WIDTH,
        "height": DEFAULT_NODE_HEIGHT,
    }


def _stacked_node_layout(
    *,
    existing: dict[str, Any],
    base: dict[str, Any],
    index: int,
    default_x: float,
    default_y: float,
) -> dict[str, float]:
    origin_y = float(base.get("y", default_y))
    return {
        "x": float(existing.get("x", base.get("x", default_x))),
        "y": origin_y + (index - 1) * LAYOUT_ROW_STEP,
        "width": float(existing.get("width", base.get("width", DEFAULT_NODE_WIDTH))),
        "height": float(existing.get("height", base.get("height", DEFAULT_NODE_HEIGHT))),
    }


def _boxes_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    ax, ay, aw, ah = _layout_box(left.get("layout") if isinstance(left, dict) else None)
    bx, by, bw, bh = _layout_box(right.get("layout") if isinstance(right, dict) else None)
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _restack_column(nodes: list[dict[str, Any]]) -> None:
    visible = [node for node in nodes if isinstance(node, dict)]
    if len(visible) < 2:
        return
    if not any(
        _boxes_overlap(left, right)
        for index, left in enumerate(visible)
        for right in visible[index + 1 :]
    ):
        return
    ordered = sorted(visible, key=lambda node: _layout_box(node.get("layout"))[1])
    origin_x, origin_y, _width, _height = _layout_box(ordered[0].get("layout"))
    for index, node in enumerate(ordered):
        _x, _y, node_w, node_h = _layout_box(node.get("layout"))
        node["layout"] = {
            "x": origin_x,
            "y": origin_y + index * LAYOUT_ROW_STEP,
            "width": node_w,
            "height": node_h,
        }


def repair_overlapping_pipeline_layout(graph: DesignerExecutionGraph) -> DesignerExecutionGraph:
    """Push 成片 to the right of clips and unstack overlapping bootstrap columns."""
    nodes = [node for node in (graph.get("nodes") or []) if isinstance(node, dict)]
    by_id = {str(node.get("id") or ""): node for node in nodes if node.get("id")}
    _restack_column(
        [by_id[key] for key in ("n_character", "n_scene", "n_storyboard") if key in by_id]
    )
    frames = sorted(
        [
            node
            for node in nodes
            if node_role(node) == NODE_ROLE_FRAME
            or str(node.get("id") or "").startswith("n_frame")
        ],
        key=node_shot_index,
    )
    clips = sorted(
        [
            node
            for node in nodes
            if node_role(node) == NODE_ROLE_CLIP
            or str(node.get("id") or "").startswith("n_clip")
        ],
        key=node_shot_index,
    )
    _restack_column(frames)
    _restack_column(clips)
    compose = by_id.get(COMPOSE_NODE_ID)
    if compose and clips:
        clip_right = max(
            _layout_box(node.get("layout"))[0] + _layout_box(node.get("layout"))[2]
            for node in clips
        )
        compose_x = _layout_box(compose.get("layout"))[0]
        overlaps = any(_boxes_overlap(compose, clip) for clip in clips)
        if overlaps or compose_x < clip_right:
            compose["layout"] = compose_layout_right_of_clips(
                [node.get("layout") for node in clips]
            )
    return graph


def clip_node_id(shot_index: int) -> str:
    return f"n_clip_{int(shot_index)}"


def frame_node_id(shot_index: int) -> str:
    return f"n_frame_{int(shot_index)}"


def node_shot_index(node: DesignerGraphNode) -> int:
    raw = node_config(node).get("shot_index")
    if isinstance(raw, bool):
        raw = None
    if isinstance(raw, int) and raw > 0:
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
        if value > 0:
            return value
    node_id = str(node.get("id") or "")
    if node_id in {"n_clip", "n_frame"}:
        return 1
    for prefix in ("n_clip_", "n_frame_"):
        if node_id.startswith(prefix):
            suffix = node_id.rsplit("_", 1)[-1]
            if suffix.isdigit() and int(suffix) > 0:
                return int(suffix)
    return 1


def _copied_delegate(graph: DesignerExecutionGraph) -> str | None:
    for node in graph.get("nodes") or []:
        delegate = str(node_config(node).get("delegate") or "").strip()
        if delegate:
            return delegate
    return None


def _is_shot_pipeline_id(node_id: str) -> bool:
    return (
        node_id in {"n_frame", "n_clip", COMPOSE_NODE_ID}
        or node_id.startswith("n_frame_")
        or node_id.startswith("n_clip_")
    )


def shot_pipeline_count(graph: DesignerExecutionGraph) -> int:
    """How many per-shot frame/clip slots the graph currently has."""
    frames = 0
    clips = 0
    for node in graph.get("nodes") or []:
        node_id = str(node.get("id") or "")
        role = node_role(node)
        if role == NODE_ROLE_FRAME or node_id == "n_frame" or node_id.startswith("n_frame_"):
            frames += 1
        elif role == NODE_ROLE_CLIP or node_id == "n_clip" or node_id.startswith("n_clip_"):
            clips += 1
    return max(frames, clips, 0)


def _asset_ref_uri(ref: object) -> str:
    if not isinstance(ref, dict):
        return ""
    return str(ref.get("uri") or "").strip()


def preserve_node_output_refs(
    incoming: DesignerExecutionGraph,
    existing: DesignerExecutionGraph | None,
) -> DesignerExecutionGraph:
    """Keep completed artifact URIs when a layout save omits output_ref."""
    if existing is None:
        return incoming
    existing_by_id = {
        str(node.get("id") or ""): node
        for node in existing.get("nodes") or []
        if str(node.get("id") or "")
    }
    nodes: list[DesignerGraphNode] = []
    changed = False
    for node in incoming.get("nodes") or []:
        prev = existing_by_id.get(str(node.get("id") or "")) or {}
        prev_ref = prev.get("output_ref") if isinstance(prev, dict) else None
        if _asset_ref_uri(prev_ref) and not _asset_ref_uri(node.get("output_ref")):
            nodes.append({**node, "output_ref": dict(prev_ref)})
            changed = True
            continue
        nodes.append(node)
    if not changed:
        return incoming
    next_graph = dict(incoming)
    next_graph["nodes"] = nodes
    return next_graph


def preserve_expanded_shot_nodes(
    incoming: DesignerExecutionGraph,
    existing: DesignerExecutionGraph | None,
) -> DesignerExecutionGraph:
    """Keep already-expanded keyframe/clip nodes when a stale save sends the bootstrap shape."""
    if existing is None:
        return incoming
    existing_count = shot_pipeline_count(existing)
    incoming_count = shot_pipeline_count(incoming)
    if existing_count <= incoming_count or existing_count <= 1:
        return incoming
    incoming_ids = {str(node.get("id") or "") for node in incoming.get("nodes") or []}
    grafted = dict(incoming)
    extra_nodes = [
        dict(node)
        for node in existing.get("nodes") or []
        if str(node.get("id") or "") not in incoming_ids
        and (
            node_role(node) in {NODE_ROLE_FRAME, NODE_ROLE_CLIP, NODE_ROLE_COMPOSE}
            or _is_shot_pipeline_id(str(node.get("id") or ""))
        )
    ]
    grafted["nodes"] = [dict(node) for node in incoming.get("nodes") or []] + extra_nodes
    return expand_shot_nodes(grafted, existing_count)


def expand_shot_nodes(
    graph: DesignerExecutionGraph,
    shot_count: int,
) -> DesignerExecutionGraph:
    """One keyframe node and one clip node per storyboard shot, plus compose."""
    count = max(1, min(int(shot_count or 1), MAX_SHOT_CLIP_NODES))
    raw = dict(graph)
    existing_by_id = {
        str(node.get("id") or ""): dict(node)
        for node in raw.get("nodes") or []
        if str(node.get("id") or "")
    }
    frame_template = existing_by_id.get("n_frame_1") or existing_by_id.get("n_frame") or {}
    clip_template = existing_by_id.get("n_clip_1") or existing_by_id.get("n_clip") or {}
    frame_base = dict(frame_template.get("layout") or {})
    clip_base = dict(clip_template.get("layout") or {})

    kept_nodes = [
        dict(node)
        for node in raw.get("nodes") or []
        if node_role(node) not in {NODE_ROLE_FRAME, NODE_ROLE_CLIP, NODE_ROLE_COMPOSE}
        and not _is_shot_pipeline_id(str(node.get("id") or ""))
    ]
    kept_edges = [
        dict(edge)
        for edge in raw.get("edges") or []
        if not _is_shot_pipeline_id(str(edge.get("source") or ""))
        and not _is_shot_pipeline_id(str(edge.get("target") or ""))
    ]
    kept_ids = {str(node.get("id") or "") for node in kept_nodes}

    def _role_node_ids(role: str) -> list[str]:
        ids: list[str] = []
        for node in kept_nodes:
            if node_role(node) == role and str(node.get("id") or ""):
                ids.append(str(node["id"]))
        return ids

    character_ids = _role_node_ids(NODE_ROLE_CHARACTER_DESIGN)
    scene_ids = _role_node_ids(NODE_ROLE_SCENE)
    storyboard_id = (_role_node_ids(NODE_ROLE_STORYBOARD) or [None])[0]
    delegate = _copied_delegate(graph)
    frame_nodes: list[DesignerGraphNode] = []
    clip_nodes: list[DesignerGraphNode] = []
    for index in range(1, count + 1):
        frame_id = frame_node_id(index)
        clip_id = clip_node_id(index)
        frame_inputs = [*character_ids, *scene_ids]
        if storyboard_id:
            frame_inputs.append(storyboard_id)
        clip_inputs = [*character_ids, *scene_ids]
        if storyboard_id:
            clip_inputs.append(storyboard_id)
        clip_inputs.append(frame_id)
        frame_config: dict[str, Any] = {
            "role": NODE_ROLE_FRAME,
            "shot_index": index,
            "inputs": frame_inputs,
        }
        clip_config: dict[str, Any] = {
            "role": NODE_ROLE_CLIP,
            "shot_index": index,
            "inputs": clip_inputs,
        }
        prev_frame_generate = _generate_config(existing_by_id.get(frame_id) or {})
        prev_clip_generate = _generate_config(existing_by_id.get(clip_id) or {})
        if prev_frame_generate:
            frame_config[CONFIG_KEY_GENERATE] = prev_frame_generate
        if prev_clip_generate:
            clip_config[CONFIG_KEY_GENERATE] = prev_clip_generate
        if delegate:
            frame_config["delegate"] = delegate
            clip_config["delegate"] = delegate
        prev_frame_layout = dict((existing_by_id.get(frame_id) or {}).get("layout") or {})
        prev_clip_layout = dict((existing_by_id.get(clip_id) or {}).get("layout") or {})
        frame_layout = _stacked_node_layout(
            existing=prev_frame_layout,
            base=frame_base,
            index=index,
            default_x=760.0,
            default_y=240.0,
        )
        clip_layout = _stacked_node_layout(
            existing=prev_clip_layout,
            base=clip_base,
            index=index,
            default_x=1120.0,
            default_y=240.0,
        )
        frame_nodes.append(
            {
                "id": frame_id,
                "type": NODE_TYPE_IMAGE,
                "label": f"Keyframe {index}",
                "config": frame_config,
                "layout": frame_layout,
            }
        )
        clip_nodes.append(
            {
                "id": clip_id,
                "type": NODE_TYPE_VIDEO,
                "label": f"Clip {index}",
                "config": clip_config,
                "layout": clip_layout,
            }
        )
        for source_id in character_ids:
            if source_id not in kept_ids:
                continue
            kept_edges.append(
                {
                    "id": f"e_{source_id}_{frame_id}",
                    "source": source_id,
                    "target": frame_id,
                    "kind": EDGE_KIND_DATA,
                }
            )
        for source_id in scene_ids:
            if source_id not in kept_ids:
                continue
            kept_edges.append(
                {
                    "id": f"e_{source_id}_{frame_id}",
                    "source": source_id,
                    "target": frame_id,
                    "kind": EDGE_KIND_DATA,
                }
            )
        if storyboard_id and storyboard_id in kept_ids:
            kept_edges.append(
                {
                    "id": f"e_storyboard_{frame_id}",
                    "source": storyboard_id,
                    "target": frame_id,
                    "kind": EDGE_KIND_DATA,
                }
            )
        for source_id in character_ids:
            if source_id not in kept_ids:
                continue
            kept_edges.append(
                {
                    "id": f"e_{source_id}_{clip_id}",
                    "source": source_id,
                    "target": clip_id,
                    "kind": EDGE_KIND_DATA,
                }
            )
        for source_id in scene_ids:
            if source_id not in kept_ids:
                continue
            kept_edges.append(
                {
                    "id": f"e_{source_id}_{clip_id}",
                    "source": source_id,
                    "target": clip_id,
                    "kind": EDGE_KIND_DATA,
                }
            )
        if storyboard_id and storyboard_id in kept_ids:
            kept_edges.append(
                {
                    "id": f"e_storyboard_{clip_id}",
                    "source": storyboard_id,
                    "target": clip_id,
                    "kind": EDGE_KIND_DATA,
                }
            )
        kept_edges.append(
            {
                "id": f"e_{frame_id}_{clip_id}",
                "source": frame_id,
                "target": clip_id,
                "kind": EDGE_KIND_DATA,
            }
        )
        kept_edges.append(
            {
                "id": f"e_{clip_id}_compose",
                "source": clip_id,
                "target": COMPOSE_NODE_ID,
                "kind": EDGE_KIND_DATA,
            }
        )
    compose_config: dict[str, Any] = {
        "role": NODE_ROLE_COMPOSE,
        "inputs": [clip_node_id(index) for index in range(1, count + 1)],
        "delegate": CONFIG_DELEGATE_HANDLER,
    }
    compose_node: DesignerGraphNode = {
        "id": COMPOSE_NODE_ID,
        "type": NODE_TYPE_VIDEO,
        "label": "Film",
        "config": compose_config,
        "layout": compose_layout_right_of_clips(
            [node.get("layout") for node in clip_nodes]
        ),
    }
    raw["nodes"] = kept_nodes + frame_nodes + clip_nodes + [compose_node]
    raw["edges"] = kept_edges
    return normalize_execution_graph(raw)


def _generate_config(node: DesignerGraphNode) -> dict[str, Any]:
    raw = node_config(node).get(CONFIG_KEY_GENERATE)
    return dict(raw) if isinstance(raw, dict) else {}


def apply_shot_generate_prompts(
    graph: DesignerExecutionGraph,
    prompts: list[str],
) -> DesignerExecutionGraph:
    """Fill frame/clip generate.prompt from storyboard rows unless the user edited them."""
    changed = False
    nodes: list[DesignerGraphNode] = []
    for node in graph.get("nodes") or []:
        role = node_role(node)
        if role not in {NODE_ROLE_FRAME, NODE_ROLE_CLIP}:
            nodes.append(node)
            continue
        index = node_shot_index(node)
        if index < 1 or index > len(prompts):
            nodes.append(node)
            continue
        prompt_text = str(prompts[index - 1] or "").strip()
        if not prompt_text:
            nodes.append(node)
            continue
        generate = _generate_config(node)
        existing = str(generate.get("prompt") or "").strip()
        origin = str(generate.get("prompt_origin") or "").strip()
        if origin == GENERATE_PROMPT_ORIGIN_USER and existing:
            nodes.append(node)
            continue
        if existing == prompt_text and origin == GENERATE_PROMPT_ORIGIN_STORYBOARD:
            nodes.append(node)
            continue
        next_config = dict(node_config(node))
        next_generate = dict(generate)
        next_generate["prompt"] = prompt_text
        next_generate["prompt_origin"] = GENERATE_PROMPT_ORIGIN_STORYBOARD
        next_config[CONFIG_KEY_GENERATE] = next_generate
        nodes.append({**node, "config": next_config})
        changed = True
    if not changed:
        return graph
    raw = dict(graph)
    raw["nodes"] = nodes
    return normalize_execution_graph(raw)


def expand_clip_nodes_for_shots(
    graph: DesignerExecutionGraph,
    shot_count: int,
) -> DesignerExecutionGraph:
    """Backward-compatible alias: expand keyframes and clips together."""
    return expand_shot_nodes(graph, shot_count)


def ensure_bootstrap_clip_waits_for_frame(
    graph: DesignerExecutionGraph,
) -> DesignerExecutionGraph:
    """Backward-compatible alias used by older tests."""
    return ensure_bootstrap_pipeline(graph)


def normalize_node_state(raw: Any) -> DesignerNodeState:
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("node_state must be an object")
    status = _require_str(raw.get("status"), "node_state.status")
    if status not in NODE_STATUSES:
        raise DesignerGraphValidationError(f"unsupported node status: {status!r}")
    state: DesignerNodeState = {"status": status}
    for key in ("started_at", "completed_at"):
        val = raw.get(key)
        if val is None:
            state[key] = None  # type: ignore[literal-required]
        elif isinstance(val, int) and not isinstance(val, bool):
            state[key] = val  # type: ignore[literal-required]
        else:
            raise DesignerGraphValidationError(f"node_state.{key} must be an integer or null")
    if "output_ref" in raw:
        state["output_ref"] = normalize_asset_ref(raw.get("output_ref"))
    if "output_refs" in raw:
        raw_refs = raw.get("output_refs")
        if not isinstance(raw_refs, list):
            raise DesignerGraphValidationError("node_state.output_refs must be an array")
        state["output_refs"] = [
            ref
            for ref in (normalize_asset_ref(item) for item in raw_refs)
            if ref is not None
        ]
    if "candidate_output_ref" in raw:
        state["candidate_output_ref"] = normalize_asset_ref(raw.get("candidate_output_ref"))
    if "candidate_output_refs" in raw:
        raw_candidates = raw.get("candidate_output_refs")
        if not isinstance(raw_candidates, list):
            raise DesignerGraphValidationError("node_state.candidate_output_refs must be an array")
        state["candidate_output_refs"] = [
            ref
            for ref in (normalize_asset_ref(item) for item in raw_candidates)
            if ref is not None
        ]
    error = raw.get("error")
    if isinstance(error, str):
        state["error"] = error
    blocked_by = raw.get("blocked_by")
    if blocked_by is not None:
        if not isinstance(blocked_by, list) or not all(
            isinstance(item, str) for item in blocked_by
        ):
            raise DesignerGraphValidationError("node_state.blocked_by must be a string array")
        state["blocked_by"] = list(blocked_by)
    return state


def normalize_execution_run(raw: Any) -> DesignerExecutionRun:
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("run must be an object")
    schema_version = _require_str(raw.get("schema_version"), "schema_version")
    if schema_version != RUN_SCHEMA_VERSION:
        raise DesignerGraphValidationError(
            f"unsupported run schema_version: {schema_version!r} "
            f"(expected {RUN_SCHEMA_VERSION!r})"
        )
    run_id = _require_str(raw.get("run_id"), "run_id")
    graph_id = _require_str(raw.get("graph_id"), "graph_id")
    project_id = _require_str(raw.get("project_id"), "project_id")
    status = _require_str(raw.get("status"), "status")
    if status not in RUN_STATUSES:
        raise DesignerGraphValidationError(f"unsupported run status: {status!r}")
    raw_states = raw.get("node_states")
    if not isinstance(raw_states, dict):
        raise DesignerGraphValidationError("node_states must be an object")
    node_states = {
        str(node_id): normalize_node_state(state)
        for node_id, state in raw_states.items()
    }
    current_node_ids = raw.get("current_node_ids")
    if current_node_ids is None:
        current_ids: list[str] = []
    elif not isinstance(current_node_ids, list) or not all(
        isinstance(item, str) for item in current_node_ids
    ):
        raise DesignerGraphValidationError("current_node_ids must be a string array")
    else:
        current_ids = list(current_node_ids)
    now = utc_now_ms()
    created_at = raw.get("created_at")
    updated_at = raw.get("updated_at")
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "graph_id": graph_id,
        "project_id": project_id,
        "status": status,
        "node_states": node_states,
        "current_node_ids": current_ids,
        "created_at": int(created_at) if isinstance(created_at, int) else now,
        "updated_at": int(updated_at) if isinstance(updated_at, int) else now,
    }


def initial_node_states(graph: DesignerExecutionGraph) -> dict[str, DesignerNodeState]:
    return {node["id"]: {"status": NODE_STATUS_PENDING} for node in graph["nodes"]}


def node_config(node: DesignerGraphNode) -> DesignerNodeConfig:
    config = node.get("config")
    return config if isinstance(config, dict) else {}


def node_role(node: DesignerGraphNode) -> str:
    role = str(node_config(node).get("role") or "").strip()
    return role


def node_delegate(node: DesignerGraphNode) -> str:
    delegate = str(node_config(node).get("delegate") or CONFIG_DELEGATE_AGENT).strip()
    if delegate == CONFIG_DELEGATE_SUBAGENT:
        return CONFIG_DELEGATE_AGENT
    return delegate if delegate in CONFIG_DELEGATES else CONFIG_DELEGATE_AGENT


def node_agent_template(node: DesignerGraphNode) -> str:
    """Resolve the AgentTemplate ref for a node (explicit, else role default)."""
    explicit = str(node_config(node).get(CONFIG_KEY_AGENT_TEMPLATE) or "").strip()
    if explicit:
        return explicit
    return ROLE_DEFAULT_TEMPLATES.get(node_role(node), "")


def node_uses_agent_runtime(node: DesignerGraphNode) -> bool:
    return node_delegate(node) != CONFIG_DELEGATE_HANDLER


def graph_uses_agent_scheduler(graph: DesignerExecutionGraph) -> bool:
    nodes = graph.get("nodes") or []
    if not nodes:
        return False
    return any(node_uses_agent_runtime(node) for node in nodes)


def apply_graph_patch(graph: DesignerExecutionGraph, patch: Any) -> DesignerExecutionGraph:
    """Merge upserts/removals into a domain graph, then re-normalize."""
    if patch is None:
        patch = {}
    if not isinstance(patch, dict):
        raise DesignerGraphValidationError("patch must be an object")
    raw = dict(graph)
    title = patch.get("title")
    if isinstance(title, str) and title.strip():
        raw["title"] = title.strip()
    if "description" in patch:
        description = patch.get("description")
        if description is not None and not isinstance(description, str):
            raise DesignerGraphValidationError("patch.description must be a string")
        raw["description"] = str(description or "")
    nodes_by_id = {node["id"]: dict(node) for node in raw.get("nodes") or []}
    upsert_nodes = patch.get("upsert_nodes") or []
    if not isinstance(upsert_nodes, list):
        raise DesignerGraphValidationError("patch.upsert_nodes must be an array")
    for item in upsert_nodes:
        node = normalize_node(item)
        nodes_by_id[node["id"]] = node
    remove_node_ids = patch.get("remove_node_ids") or []
    if not isinstance(remove_node_ids, list) or not all(
        isinstance(item, str) and item.strip() for item in remove_node_ids
    ):
        raise DesignerGraphValidationError("patch.remove_node_ids must be a string array")
    for node_id in remove_node_ids:
        nodes_by_id.pop(str(node_id).strip(), None)
    remaining_node_ids = set(nodes_by_id)
    edges_by_id = {edge["id"]: dict(edge) for edge in raw.get("edges") or []}
    upsert_edges = patch.get("upsert_edges") or []
    if not isinstance(upsert_edges, list):
        raise DesignerGraphValidationError("patch.upsert_edges must be an array")
    for item in upsert_edges:
        edge = normalize_edge(item)
        edges_by_id[edge["id"]] = edge
    remove_edge_ids = patch.get("remove_edge_ids") or []
    if not isinstance(remove_edge_ids, list) or not all(
        isinstance(item, str) and item.strip() for item in remove_edge_ids
    ):
        raise DesignerGraphValidationError("patch.remove_edge_ids must be a string array")
    for edge_id in remove_edge_ids:
        edges_by_id.pop(str(edge_id).strip(), None)
    raw["nodes"] = list(nodes_by_id.values())
    raw["edges"] = [
        edge
        for edge in edges_by_id.values()
        if edge.get("source") in remaining_node_ids and edge.get("target") in remaining_node_ids
    ]
    raw["updated_at"] = utc_now_ms()
    return normalize_execution_graph(raw)


def edge_kind(edge: DesignerGraphEdge) -> str:
    kind = str(edge.get("kind") or EDGE_KIND_DATA).strip()
    return kind if kind in EDGE_KINDS else EDGE_KIND_DATA


def data_predecessors(graph: DesignerExecutionGraph) -> dict[str, list[str]]:
    incoming: dict[str, list[str]] = {node["id"]: [] for node in graph.get("nodes", [])}
    for edge in graph.get("edges", []):
        if edge_kind(edge) != EDGE_KIND_DATA:
            continue
        source = edge.get("source")
        target = edge.get("target")
        if isinstance(source, str) and isinstance(target, str) and target in incoming:
            incoming[target].append(source)
    return incoming


def sync_groups(graph: DesignerExecutionGraph) -> dict[str, frozenset[str]]:
    """Union-find over undirected ``sync`` edges."""
    parent: dict[str, str] = {node["id"]: node["id"] for node in graph.get("nodes", [])}

    def find(node_id: str) -> str:
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for edge in graph.get("edges", []):
        if edge_kind(edge) != EDGE_KIND_SYNC:
            continue
        source = edge.get("source")
        target = edge.get("target")
        if isinstance(source, str) and isinstance(target, str) and source in parent and target in parent:
            union(source, target)

    groups: dict[str, set[str]] = {}
    for node_id in parent:
        root = find(node_id)
        groups.setdefault(root, set()).add(node_id)
    return {node_id: frozenset(groups[find(node_id)]) for node_id in parent}


def build_bootstrap_graph(
    *,
    project_id: str,
    prompt: str,
    title: str | None = None,
) -> DesignerExecutionGraph:
    """Create the default video-creation pipeline graph from an initial prompt."""
    graph_id = new_graph_id()
    now = utc_now_ms()
    prompt_text = prompt.strip()
    graph_title = title.strip() if isinstance(title, str) and title.strip() else prompt_text[:80]
    nodes: list[DesignerGraphNode] = [
        {
            "id": "n_brief",
            "type": NODE_TYPE_TEXT,
            "label": "Brief",
            "config": {"role": NODE_ROLE_BRIEF, "prompt": prompt_text},
            "layout": {"x": 40, "y": 240, "width": 280, "height": 160},
        },
        {
            "id": "n_character",
            "type": NODE_TYPE_IMAGE,
            "label": "Character",
            "config": {"role": NODE_ROLE_CHARACTER_DESIGN, "inputs": ["n_brief"]},
            "layout": {"x": 400, "y": 40, "width": 280, "height": 160},
        },
        {
            "id": "n_scene",
            "type": NODE_TYPE_IMAGE,
            "label": "Scene",
            "config": {"role": NODE_ROLE_SCENE, "inputs": ["n_brief"]},
            "layout": {"x": 400, "y": 240, "width": 280, "height": 160},
        },
        {
            "id": "n_storyboard",
            "type": NODE_TYPE_TABLE,
            "label": "Storyboard",
            "config": {"role": NODE_ROLE_STORYBOARD, "inputs": ["n_brief"]},
            "layout": {"x": 400, "y": 440, "width": 280, "height": 160},
        },
        {
            "id": "n_frame_1",
            "type": NODE_TYPE_IMAGE,
            "label": "Keyframe 1",
            "config": {
                "role": NODE_ROLE_FRAME,
                "shot_index": 1,
                "inputs": ["n_character", "n_scene", "n_storyboard"],
            },
            "layout": {"x": 760, "y": 240, "width": 280, "height": 160},
        },
        {
            "id": "n_clip_1",
            "type": NODE_TYPE_VIDEO,
            "label": "Clip 1",
            "config": {
                "role": NODE_ROLE_CLIP,
                "shot_index": 1,
                "inputs": ["n_character", "n_scene", "n_storyboard", "n_frame_1"],
            },
            "layout": {"x": 1120, "y": 240, "width": 280, "height": 160},
        },
        {
            "id": "n_compose",
            "type": NODE_TYPE_VIDEO,
            "label": "Film",
            "config": {"role": NODE_ROLE_COMPOSE, "inputs": ["n_clip_1"]},
            "layout": {"x": 1480, "y": 240, "width": 280, "height": 160},
        },
    ]
    edges: list[DesignerGraphEdge] = [
        {"id": "e_brief_character", "source": "n_brief", "target": "n_character", "kind": EDGE_KIND_DATA},
        {"id": "e_brief_scene", "source": "n_brief", "target": "n_scene", "kind": EDGE_KIND_DATA},
        {"id": "e_brief_storyboard", "source": "n_brief", "target": "n_storyboard", "kind": EDGE_KIND_DATA},
        {
            "id": "e_character_storyboard",
            "source": "n_character",
            "target": "n_storyboard",
            "kind": EDGE_KIND_SYNC,
            "label": "Align",
        },
        {
            "id": "e_scene_storyboard",
            "source": "n_scene",
            "target": "n_storyboard",
            "kind": EDGE_KIND_SYNC,
            "label": "Align",
        },
        {"id": "e_character_n_frame_1", "source": "n_character", "target": "n_frame_1", "kind": EDGE_KIND_DATA},
        {"id": "e_scene_n_frame_1", "source": "n_scene", "target": "n_frame_1", "kind": EDGE_KIND_DATA},
        {"id": "e_storyboard_n_frame_1", "source": "n_storyboard", "target": "n_frame_1", "kind": EDGE_KIND_DATA},
        {"id": "e_character_n_clip_1", "source": "n_character", "target": "n_clip_1", "kind": EDGE_KIND_DATA},
        {"id": "e_scene_n_clip_1", "source": "n_scene", "target": "n_clip_1", "kind": EDGE_KIND_DATA},
        {"id": "e_storyboard_n_clip_1", "source": "n_storyboard", "target": "n_clip_1", "kind": EDGE_KIND_DATA},
        {"id": "e_n_frame_1_n_clip_1", "source": "n_frame_1", "target": "n_clip_1", "kind": EDGE_KIND_DATA},
        {"id": "e_n_clip_1_compose", "source": "n_clip_1", "target": "n_compose", "kind": EDGE_KIND_DATA},
    ]
    graph: DesignerExecutionGraph = {
        "schema_version": SCHEMA_VERSION,
        "graph_id": graph_id,
        "project_id": project_id,
        "title": graph_title,
        "description": prompt_text,
        "source": GRAPH_SOURCE_PROMPT,
        "nodes": nodes,
        "edges": edges,
        "metadata": {"bootstrap": "designer.graph.bootstrap.v1"},
        "created_at": now,
        "updated_at": now,
    }
    return normalize_execution_graph(graph)
