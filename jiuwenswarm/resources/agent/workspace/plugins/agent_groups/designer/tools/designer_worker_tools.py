# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Designer worker tools — the designer AgentGroup's shared tool module.

Terminal-state contract (user ruling 2026-10-12): ALL worker logic lives in
the manifest package; the claw repo only compiles graph scripts (node →
``agent(options={'agent_template': role})``) and reads events. These tools
follow the colleague's ticket model (manifest 样例 §3.5): the task prompt
carries ONE opaque ticket line the tools self-parse — no injected ctx, no
host-side state:

    node_ticket: design:{graph_id}:{run_id}:{node_id}

Each tool resolves graph/run state from the DesignerGraphStore by ticket at
call time, so it always sees the latest persisted canvas state (including
edits/replace uploads that landed mid-run — same semantics read_shot_context
documents).

Media generation hard-codes the claw handler module functions for now
(generate_designer_image / generate_clip_video are pure async functions);
the follow-up crew will fully strip NodeAgentHost logic out of the claw repo
and externalize the pipelines into this package.

Tool class shape satisfies the ``harness.tool.file`` provider contract
(``cls(**params)`` → a live Tool instance).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# The ticket slot lives in the installed claw package (stable single module
# instance across every canonical load of THIS file — the extension loader
# execs each load as a fresh module object, so module state here would not
# be shared between the rail and the tools). Async-flow scoped: parallel
# workers in one process never bleed tickets.
from jiuwenswarm.server.runtime.design.context import DESIGN_NODE_TICKET as _TICKET

_TICKET_RE = re.compile(
    r"^\s*node_ticket:\s*design:(?P<graph_id>[^:\s]+):(?P<run_id>[^:\s]+):(?P<node_id>[^:\s]+)",
    re.MULTILINE,
)


def parse_node_ticket(text: str) -> dict[str, str] | None:
    """Extract the design node ticket from the worker's task prompt."""
    match = _TICKET_RE.search(str(text or ""))
    if match is None:
        return None
    return {
        "graph_id": match.group("graph_id"),
        "run_id": match.group("run_id"),
        "node_id": match.group("node_id"),
    }


def _resolve_ticket_env(ticket: dict[str, str] | None) -> tuple[Any, Any, dict]:
    """Load (store, graph, run) for a ticket; raises ValueError when unknown."""
    if ticket is None:
        raise ValueError(
            "no node_ticket in this task — cannot resolve design context"
        )
    from jiuwenswarm.server.runtime.designer.graph_store import DesignerGraphStore

    store = DesignerGraphStore()
    graph = store.get_graph(ticket["graph_id"])
    if graph is None:
        raise ValueError(f"ticket graph not found: {ticket['graph_id']!r}")
    run = store.get_run(ticket["run_id"])
    return store, graph, run


class _TicketContext:
    """Per-async-flow ticket state over the process-wide ContextVar.

    All tool instances in one worker share the flow's ticket (bound by the
    NodeTicketBindingRail from the task prompt); parallel workers in the
    same process each carry their own ContextVar value, so tickets never
    bleed across a parallel wave.
    """

    @property
    def ticket(self) -> dict[str, str] | None:
        return _TICKET.get()

    def bind(self, prompt_text: str) -> None:
        ticket = parse_node_ticket(prompt_text)
        if ticket is not None:
            _TICKET.set(ticket)

    def env(self) -> tuple[Any, Any, Any]:
        return _resolve_ticket_env(self.ticket)

    @property
    def node_id(self) -> str:
        ticket = self.ticket
        if ticket is None:
            raise ValueError("no node_ticket bound")
        return ticket["node_id"]


# Shared per-flow ticket holder (see _TICKET above).
_CTX = _TicketContext()


def _bind_ctx_from_prompt(prompt_text: str | None) -> None:
    if prompt_text:
        _CTX.bind(prompt_text)


def _emit_stage(text: str, tool: str) -> None:
    """Push one handler-level stage line to the node's activity peek.

    Resolves the trigger's activity sink from the design context (the old
    executor's ctx.emit_activity("stage", ...) contract); a no-op outside
    a trigger or when the ticket is unbound.
    """
    ticket = _CTX.ticket
    if ticket is None:
        return
    try:
        from jiuwenswarm.server.runtime.design.context import (
            get_design_context_registry,
        )

        sink = get_design_context_registry().node_activity_sink_for(
            ticket["graph_id"]
        )
    except Exception:  # noqa: BLE001 — observability only
        return
    if sink is None:
        return
    try:
        emit = sink(ticket["node_id"])
        if emit is not None:
            emit("stage", text, tool)
    except Exception:  # noqa: BLE001 — observability only
        pass


# ─── context read (read_shot_context 同型) ───────────────────────────────


async def _read_node_context(ticket_text: str = "") -> str:
    """Read this node's execution context snapshot from the Store.

    Returns the same JSON shape the old build_node_user_query assembled:
    node config, upstream outputs, run status, and the canvas snapshot.
    """
    _bind_ctx_from_prompt(ticket_text)
    store, graph, run = _CTX.env()
    from jiuwenswarm.common.schema.designer_graph import data_predecessors
    from jiuwenswarm.server.runtime.designer.node_agent import _upstream_outputs

    states = (run or {}).get("node_states") or {}
    node = next(
        (n for n in graph.get("nodes") or [] if str(n.get("id")) == _CTX.node_id),
        None,
    )
    if node is None:
        raise ValueError(f"ticket node not on graph: {_CTX.node_id!r}")
    preds = data_predecessors(graph).get(_CTX.node_id, [])
    snapshot = {
        "node": {
            "id": node.get("id"),
            "type": node.get("type"),
            "label": node.get("label"),
            "config": node.get("config") or {},
        },
        "upstream_outputs": _upstream_outputs(
            type(
                "C",
                (),
                {
                    "graph": graph,
                    "run": run,
                    "node_id": _CTX.node_id,
                },
            )(),
            preds,
        ),
        "run": {
            "run_id": (run or {}).get("run_id"),
            "status": (run or {}).get("status"),
        },
        "graph_title": graph.get("title"),
        "graph_description": graph.get("description"),
        # Advisory media prompt budgets (colleague PR #14 semantics): the
        # worker self-limits its tool prompts to these; nothing truncates.
        "media_prompt_limits": _media_limit_packet(),
    }
    return json.dumps(snapshot, ensure_ascii=False, indent=2)


def _media_limit_packet() -> dict[str, Any]:
    try:
        from jiuwenswarm.server.runtime.designer.pipeline.media_prompt_limits import (
            media_prompt_limit_packet,
        )

        return media_prompt_limit_packet()
    except Exception:  # noqa: BLE001 — advisory only, context read must not die
        return {}


# ─── media generation (硬编 claw handler；后续同事剥离) ──────────────────

def _media_save_dir(run_id: str) -> Path:
    """Team-owned media dir for one run; falls back to the legacy layout."""
    from jiuwenswarm.server.runtime.designer.node_agent import (
        _media_dir_from_team_workspace,
    )

    root = _media_dir_from_team_workspace(run_id)
    if root is not None:
        return root
    from jiuwenswarm.common.utils import get_agent_workspace_dir

    root = get_agent_workspace_dir() / "designer_media" / run_id
    root.mkdir(parents=True, exist_ok=True)
    return root


def _ticket_graph() -> Any:
    """Graph for the bound ticket, or None outside a node run (defaults apply)."""
    try:
        return _CTX.env()[1]
    except Exception:  # noqa: BLE001 — settings fall back to model defaults
        return None


async def _call_image_model(
    prompt: str,
    reference_images: list[str] | None = None,
    ticket_text: str = "",
) -> str:
    """Generate one image via the Settings image model.

    Size is server-decided (canvas image_settings snapped onto the active
    model — colleague PR #9 semantics); the LLM never passes a size.
    """
    _bind_ctx_from_prompt(ticket_text)
    if not str(prompt or "").strip():
        return "prompt is required"
    _emit_stage("calling image model", "image_gen")
    run_id = _CTX.ticket["run_id"] if _CTX.ticket else "designer"
    from jiuwenswarm.server.runtime.designer.handlers.common import (
        generate_designer_image,
    )
    from jiuwenswarm.server.runtime.designer.media_settings import image_settings

    settings = image_settings(_ticket_graph())
    try:
        generated = await generate_designer_image(
            prompt,
            size=settings.size,
            reference_images=[str(r) for r in (reference_images or []) if str(r).strip()]
            or None,
            max_tries=2,
            save_dir=str(_media_save_dir(run_id)),
        )
    except Exception as exc:  # noqa: BLE001 — surface to the worker, fail-closed at submit
        return f"image generation failed: {exc}"
    path = str((generated or {}).get("image_path") or "").strip()
    if not path:
        detail = str((generated or {}).get("error") or "") if isinstance(generated, dict) else ""
        return f"image generation produced no file{': ' + detail if detail else ''}"
    uri = Path(path).resolve().as_uri()
    return f"image_ready uri={uri} path={path}"


async def _call_video_model(
    prompt: str,
    reference_images: list[str] | None = None,
    ticket_text: str = "",
) -> str:
    """Generate one video clip via the Settings video model.

    Format comes from canvas video_settings snapped onto the active model and
    duration from the node's own config.duration_sec (clamped to the model's
    min/max) — the LLM passes neither (colleague PR #9 semantics).
    """
    _bind_ctx_from_prompt(ticket_text)
    if not str(prompt or "").strip():
        return "prompt is required"
    # Same ordering as _call_image_model: stage line before the model call.
    _emit_stage("calling video model", "video_gen")
    run_id = _CTX.ticket["run_id"] if _CTX.ticket else "designer"
    from jiuwenswarm.server.runtime.designer.handlers.clip import (
        generate_clip_video,
    )
    from jiuwenswarm.server.runtime.designer.media_settings import video_settings
    from jiuwenswarm.server.runtime.designer.pipeline.model_capacity import (
        active_video_capacity,
        size_for_resolution,
        snap_duration,
    )

    graph = _ticket_graph()
    settings = video_settings(graph)
    video_size = size_for_resolution(settings.resolution, settings.aspect_ratio)
    duration = _node_duration_sec(graph)
    try:
        generated = await generate_clip_video(
            prompt,
            save_dir=str(_media_save_dir(run_id)),
            reference_images=[str(r) for r in (reference_images or []) if str(r).strip()]
            or None,
            duration=snap_duration(duration, active_video_capacity()),
            size=video_size,
            resolution=settings.resolution,
        )
    except Exception as exc:  # noqa: BLE001
        return f"video generation failed: {exc}"
    path = str((generated or {}).get("video_path") or "").strip()
    if not path:
        return "video generation produced no file"
    uri = Path(path).resolve().as_uri()
    return f"video_ready uri={uri} path={path}"


def _node_duration_sec(graph: Any) -> int:
    """The node's own duration_sec, else 5 — snap_duration clamps to the model."""
    if not isinstance(graph, dict) or _CTX.ticket is None:
        return 5
    node = next(
        (n for n in graph.get("nodes") or [] if str(n.get("id")) == _CTX.node_id),
        None,
    )
    config = node.get("config") if isinstance(node, dict) else None
    raw = config.get("duration_sec") if isinstance(config, dict) else None
    try:
        return int(raw) if raw else 5
    except (TypeError, ValueError):
        return 5


async def _call_model(prompt: str, ticket_text: str = "") -> str:
    """Plain text-model call (story/brief drafting aid)."""
    _bind_ctx_from_prompt(ticket_text)
    from jiuwenswarm.server.runtime.designer.node_agent import _plain_model_text

    _emit_stage("calling text model", "text_gen")
    try:
        return _plain_model_text(str(prompt or ""))
    except Exception as exc:  # noqa: BLE001
        return f"model call failed: {exc}"


# ─── submit (submit_shot_output 同型；事件化写回由 event_sink 落 Store) ──


async def _submit_node_output(
    uri: str = "",
    extra_uris: list[str] | None = None,
    text: str = "",
    kind: str = "",
    label: str = "",
    ticket_text: str = "",
) -> str:
    """Submit this node's output. Files must exist; text is written to the workspace."""
    _bind_ctx_from_prompt(ticket_text)
    from jiuwenswarm.server.runtime.designer.handlers.common import (
        file_output_ref,
        write_workspace_text,
    )

    refs: list[dict[str, Any]] = []
    if str(text or "").strip():
        path = write_workspace_text(
            f"designer_agent_{_CTX.ticket['run_id']}_{_CTX.node_id}", text
        )
        ref = file_output_ref(path, kind="text", mime_type="text/markdown")
        if str(label or "").strip():
            ref["label"] = str(label).strip()
        refs.append(ref)
    elif str(uri or "").strip():
        refs.append(
            {
                "kind": str(kind or "").strip() or "file",
                "uri": str(uri).strip(),
                "label": str(label or "").strip() or Path(uri).name,
            }
        )
    for extra in extra_uris or []:
        extra_uri = str(extra or "").strip()
        if extra_uri:
            refs.append({"kind": str(kind or "").strip() or "file", "uri": extra_uri})
    if not refs:
        return "submit requires uri or text"
    for ref in refs:
        if str(ref["uri"]).lower().endswith((".md", ".markdown")):
            ref.update(kind="text", mime_type="text/markdown")
    payload = {
        "text": str(text or ""),
        "output_ref": refs[0],
        "output_refs": refs,
    }
    # Event writeback: the coordinator's event_sink persists this via the
    # design bridge. The tool returns the ack; the run record is the store of
    # truth (single/cascade semantics decided by the trigger shape).
    from jiuwenswarm.server.runtime.design.context import get_design_context_registry

    registry = get_design_context_registry()
    sink = registry.node_result_sink_for(_CTX.ticket["graph_id"])
    if sink is None:
        return (
            "submitted (no active trigger sink — output not persisted; "
            "rerun from the canvas)"
        )
    try:
        sink(_CTX.node_id, payload)
    except Exception as exc:  # noqa: BLE001
        return f"submit failed: {exc}"
    return "completed"


# ─── Tool classes (harness.tool.file contract: cls(**params) → Tool) ─────


class ReadNodeContextTool:
    """read_node_context — read this node's ticket-scoped execution context."""

    def __new__(cls, **params: Any) -> Any:
        from openjiuwen.core.foundation.tool import LocalFunction, ToolCard

        async def _invoke(**kwargs: Any) -> str:
            return await _read_node_context(str(kwargs.get("ticket_text") or ""))

        card = ToolCard(
            name="read_node_context",
            description=(
                "读取当前设计节点的执行上下文：节点配置、上游产出、运行状态。"
                "任务输入里的 node_ticket 票据自动绑定，无需传参。"
            ),
            input_params={
                "type": "object",
                "properties": {},
            },
        )
        return LocalFunction(card=card, func=_invoke)


class GenerateImageTool:
    """generate_image — one image via the Settings image model."""

    def __new__(cls, **params: Any) -> Any:
        from openjiuwen.core.foundation.tool import LocalFunction, ToolCard

        async def _invoke(**kwargs: Any) -> str:
            return await _call_image_model(
                prompt=str(kwargs.get("prompt") or ""),
                reference_images=kwargs.get("reference_images"),
                ticket_text=str(kwargs.get("ticket_text") or ""),
            )

        card = ToolCard(
            name="generate_image",
            description=(
                "生成一张图片。prompt 必填；reference_images 可传上游图片文件 uri 列表。"
                "尺寸由画布媒体设置决定（服务端解析），无需传参。"
            ),
            input_params={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "图片生成提示词"},
                    "reference_images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "参考图 uri 列表（可选）",
                    },
                },
                "required": ["prompt"],
            },
        )
        return LocalFunction(card=card, func=_invoke)


class GenerateVideoTool:
    """generate_video — one clip via the Settings video model."""

    def __new__(cls, **params: Any) -> Any:
        from openjiuwen.core.foundation.tool import LocalFunction, ToolCard

        async def _invoke(**kwargs: Any) -> str:
            return await _call_video_model(
                prompt=str(kwargs.get("prompt") or ""),
                reference_images=kwargs.get("reference_images"),
                ticket_text=str(kwargs.get("ticket_text") or ""),
            )

        card = ToolCard(
            name="generate_video",
            description=(
                "生成一段视频。prompt 必填；reference_images 传参考图 uri 列表。"
                "时长/分辨率由节点时长与画布媒体设置决定（服务端解析），无需传参。"
            ),
            input_params={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "视频生成提示词"},
                    "reference_images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "参考图 uri 列表",
                    },
                },
                "required": ["prompt"],
            },
        )
        return LocalFunction(card=card, func=_invoke)


class CallModelTool:
    """call_model — plain text-model call."""

    def __new__(cls, **params: Any) -> Any:
        from openjiuwen.core.foundation.tool import LocalFunction, ToolCard

        async def _invoke(**kwargs: Any) -> str:
            return await _call_model(str(kwargs.get("prompt") or ""))

        card = ToolCard(
            name="call_model",
            description="调用文本模型生成一段文本（大纲/设定/文案辅助）。",
            input_params={
                "type": "object",
                "properties": {"prompt": {"type": "string"}},
                "required": ["prompt"],
            },
        )
        return LocalFunction(card=card, func=_invoke)


class SubmitNodeOutputTool:
    """submit_node_output — submit this node's output (fail-closed)."""

    def __new__(cls, **params: Any) -> Any:
        from openjiuwen.core.foundation.tool import LocalFunction, ToolCard

        async def _invoke(**kwargs: Any) -> str:
            return await _submit_node_output(
                uri=str(kwargs.get("uri") or ""),
                extra_uris=kwargs.get("extra_uris"),
                text=str(kwargs.get("text") or ""),
                kind=str(kwargs.get("kind") or ""),
                label=str(kwargs.get("label") or ""),
                ticket_text=str(kwargs.get("ticket_text") or ""),
            )

        card = ToolCard(
            name="submit_node_output",
            description=(
                "提交当前节点产物并结束。text 写为工作区 Markdown；或 uri 指向工具产出的文件；"
                "extra_uris 收集本节点生成的每个文件。提交后立即停止，不要再调用生成工具。"
            ),
            input_params={
                "type": "object",
                "properties": {
                    "uri": {"type": "string", "description": "主产物文件 uri"},
                    "extra_uris": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "全部产物文件 uri（多产物必填）",
                    },
                    "text": {"type": "string", "description": "文本产物正文"},
                    "kind": {
                        "type": "string",
                        "description": "产物类型 image/video/text/file",
                    },
                    "label": {"type": "string", "description": "产物显示名"},
                },
            },
        )
        return LocalFunction(card=card, func=_invoke)


__all__ = [
    "CallModelTool",
    "GenerateImageTool",
    "GenerateVideoTool",
    "ReadNodeContextTool",
    "SubmitNodeOutputTool",
    "parse_node_ticket",
]
