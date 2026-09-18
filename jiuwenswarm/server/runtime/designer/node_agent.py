# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-node DeepAgent host for Designer: AgentTemplate persona + graph tools."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from jiuwenswarm.common.schema.designer_graph import (
    DESIGNER_AGENT_GROUP_NAME,
    AssetRef,
    DesignerExecutionGraph,
    DesignerGraphNode,
    data_predecessors,
    node_agent_template,
    node_pipeline,
    node_role,
)
from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.server.runtime.designer.handlers.common import (
    file_output_ref,
    graph_prompt,
    write_workspace_text,
)
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext, NodeResult

logger = logging.getLogger(__name__)

# Roles that must emit real media (handlers are the generation backends).
_MEDIA_MATERIALIZE_ROLES = {
    "character",
    "character_design",
    "scene",
    "frame",
    "keyframe",
    "clip",
    "compose",
    "speech",
    "music",
}
_IMAGE_URI_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".jfif")
_VIDEO_URI_SUFFIXES = (".mp4", ".webm", ".mov", ".mkv")
_AUDIO_URI_SUFFIXES = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")
_MEDIA_URI_SUFFIXES = _IMAGE_URI_SUFFIXES + _VIDEO_URI_SUFFIXES + _AUDIO_URI_SUFFIXES

# Role → required media family for handler materialization.
# Clip/compose must not treat a keyframe PNG as "done" (agents often attach stills).
_ROLE_REQUIRED_MEDIA: dict[str, str] = {
    "character": "image",
    "character_design": "image",
    "scene": "image",
    "frame": "image",
    "keyframe": "image",
    "clip": "video",
    "compose": "video",
    "speech": "audio",
    "music": "audio",
}

NodeAgentRunner = Callable[
    [DesignerGraphNode, NodeExecutionContext, "DesignerGraphToolkit"],
    Awaitable[NodeResult],
]


def _ref_media_family(ref: Any) -> str | None:
    """Return 'image' | 'video' | 'audio' if ref is real media on disk/URI, else None.

    Agents often complete with ``kind=video`` / ``mime_type=video/mp4`` while the
    URI is still a ``.md`` stub. Extension (and existing file type) must win over
    kind/mime so handler materialization actually runs.
    """
    if not isinstance(ref, dict):
        return None
    uri = str(ref.get("uri") or "").strip()
    uri_l = uri.lower()
    # Markdown / text / json never count as raster or film.
    if any(uri_l.endswith(suf) for suf in (".md", ".markdown", ".txt", ".json", ".html")):
        return None
    if any(uri_l.endswith(suf) for suf in _IMAGE_URI_SUFFIXES):
        return "image"
    if any(uri_l.endswith(suf) for suf in _VIDEO_URI_SUFFIXES):
        return "video"
    if any(uri_l.endswith(suf) for suf in _AUDIO_URI_SUFFIXES):
        return "audio"
    # workspace:// or extension-less: fall back to kind only if not a known text kind
    kind = str(ref.get("kind") or "").lower()
    mime = str(ref.get("mime_type") or ref.get("mimeType") or "").lower()
    if mime.startswith("text/") or kind in {"text", "file", "markdown", "document"}:
        return None
    # If URI points at an on-disk file, sniff by suffix after resolving
    path = _path_from_file_uri(uri) if uri.startswith("file:") else None
    if path is not None and path.is_file():
        suf = path.suffix.lower()
        if suf in _IMAGE_URI_SUFFIXES:
            return "image"
        if suf in _VIDEO_URI_SUFFIXES:
            return "video"
        if suf in _AUDIO_URI_SUFFIXES:
            return "audio"
        if suf in {".md", ".markdown", ".txt", ".json"}:
            return None
    if mime.startswith("image/") or kind == "image":
        return "image"
    if mime.startswith("video/") or kind == "video":
        # Refuse mime-only video claims without a video URI/path.
        return None
    if mime.startswith("audio/") or kind == "audio":
        return None
    return None


def _ref_looks_like_media(ref: Any) -> bool:
    return _ref_media_family(ref) is not None


def _result_has_media(result: NodeResult | None, *, required: str | None = None) -> bool:
    """If required is set ('image'|'video'|'audio'), only that family counts."""
    if result is None:
        return False
    refs: list[Any] = []
    if result.output_ref is not None:
        refs.append(result.output_ref)
    refs.extend(list(result.output_refs or []))
    for ref in refs:
        family = _ref_media_family(ref)
        if family is None:
            continue
        if required is None or family == required:
            return True
    return False


def _upstream_image_uri_keys(ctx: NodeExecutionContext | None) -> set[str]:
    """Character/scene sheet URIs — must not satisfy a frame/keyframe node."""
    if ctx is None:
        return set()
    keys: set[str] = set()
    try:
        from jiuwenswarm.server.runtime.designer.handlers.common import (
            role_output_image_paths,
        )
    except Exception:  # noqa: BLE001
        return set()
    for role in ("character_design", "character", "scene"):
        try:
            paths = role_output_image_paths(ctx, role)
        except Exception:  # noqa: BLE001
            continue
        for path in paths or []:
            try:
                resolved = Path(path).resolve()
            except OSError:
                continue
            keys.add(resolved.as_uri().lower())
            keys.add(str(resolved).replace("\\", "/").lower())
            keys.add(resolved.name.lower())
    return keys


def _result_satisfies_required_media(
    result: NodeResult | None,
    node: DesignerGraphNode,
    ctx: NodeExecutionContext | None = None,
) -> bool:
    """Role-aware media check: frames need their own keyframe, not cast/scene refs."""
    required = _required_media_family(node)
    if not _result_has_media(result, required=required):
        return False
    role = str(node_pipeline(node) or node_role(node) or "").strip().lower()
    # Scene / character sheets: primary output_ref must itself be the image.
    # Agents often attach upstream master PNGs as extra_uris while primary is .md —
    # that must NOT count as this node producing an image.
    if required == "image" and role in {
        "scene",
        "character",
        "character_design",
        "frame",
        "keyframe",
    }:
        primary = result.output_ref if result is not None else None
        if _ref_media_family(primary) != "image":
            return False
    if required != "image" or role not in {"frame", "keyframe"}:
        return True
    refs: list[Any] = []
    if result is not None and result.output_ref is not None:
        refs.append(result.output_ref)
    if result is not None:
        refs.extend(list(result.output_refs or []))
    upstream = _upstream_image_uri_keys(ctx)
    for ref in refs:
        if _ref_media_family(ref) != "image" or not isinstance(ref, dict):
            continue
        uri = str(ref.get("uri") or "").strip()
        if not uri:
            continue
        uri_l = uri.lower()
        label = str(ref.get("label") or "").lower()
        if "designer_frame_" in uri_l or "designer_frame_" in label:
            return True
        name = Path(uri_l.replace("\\", "/").split("/")[-1]).name
        keys = {uri_l, uri_l.replace("\\", "/"), name}
        if upstream and keys & upstream:
            continue
        # Non-upstream image counts as this node's keyframe.
        return True
    return False


def _node_expects_media(node: DesignerGraphNode) -> bool:
    role = str(node_pipeline(node) or node_role(node) or "").strip().lower()
    if role in _MEDIA_MATERIALIZE_ROLES:
        return True
    ntype = str(node.get("type") or "").strip().lower()
    return ntype in {"image", "video", "audio"}


def _required_media_family(node: DesignerGraphNode) -> str | None:
    role = str(node_pipeline(node) or node_role(node) or "").strip().lower()
    if role in _ROLE_REQUIRED_MEDIA:
        return _ROLE_REQUIRED_MEDIA[role]
    ntype = str(node.get("type") or "").strip().lower()
    if ntype in {"image", "video", "audio"}:
        return ntype
    return None

def _path_from_file_uri(uri: str) -> Path | None:
    raw = str(uri or "").strip()
    if not raw:
        return None
    if raw.startswith("file:"):
        from urllib.parse import unquote, urlparse
        from urllib.request import url2pathname

        parsed = urlparse(raw)
        try:
            path = Path(url2pathname(unquote(parsed.path)))
        except Exception:  # noqa: BLE001
            path = Path(unquote(parsed.path.lstrip("/")))
        return path if str(path) else None
    path = Path(raw)
    return path


def _agent_text_from_result(result: NodeResult | None) -> str:
    if result is None:
        return ""
    refs: list[Any] = []
    if result.output_ref:
        refs.append(result.output_ref)
    refs.extend(result.output_refs or [])
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        uri = str(ref.get("uri") or "").strip()
        if not uri:
            continue
        low = uri.lower()
        mime = str(ref.get("mime_type") or "").lower()
        if not (low.endswith((".md", ".txt")) or "text" in mime or "markdown" in mime):
            continue
        path = _path_from_file_uri(uri)
        if path is None:
            continue
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")[:8000]
        except OSError:
            continue
    return str(getattr(result, "message", "") or "")[:2000]


def _seed_handler_prompt(node: DesignerGraphNode, agent_result: NodeResult | None) -> None:
    """Push agent-authored creative text into node.config.prompt for handlers."""
    text = _agent_text_from_result(agent_result).strip()
    if not text:
        return
    cfg = node.get("config")
    if not isinstance(cfg, dict):
        cfg = {}
        node["config"] = cfg
    # Prefer agent brief/spec when handler prompt is empty or generic.
    existing = str(cfg.get("prompt") or "").strip()
    if not existing or len(text) > len(existing):
        cfg["prompt"] = text[:6000]


class DesignerNodeSpawner(Protocol):
    async def spawn_node_agent(self, run_id: str, node_id: str) -> str: ...

    def apply_agent_graph_patch(self, graph_id: str, patch: dict[str, Any]) -> DesignerExecutionGraph: ...

    def load_graph_snapshot(self, graph_id: str, run_id: str) -> dict[str, Any]: ...


def designer_agent_group_dir() -> Path:
    """Return the built-in Designer AgentGroup directory."""
    current = Path(__file__).resolve()
    for parent in current.parents:
        candidate = (
            parent
            / "resources"
            / "agent"
            / "workspace"
            / "plugins"
            / "agent_groups"
            / DESIGNER_AGENT_GROUP_NAME
        )
        if candidate.is_dir() and (candidate / "manifest.json").is_file():
            return candidate
    raise FileNotFoundError("built-in designer AgentGroup not found")


def parse_agent_template_ref(ref: str) -> tuple[str | None, str]:
    value = str(ref or "").strip()
    if "/" in value:
        group, _, member = value.partition("/")
        group = group.strip()
        member = member.strip()
        if group and member:
            return group, member
    return None, value


def flatten_template_prompt(template: Any, *, language: str = "cn") -> str:
    from jiuwenswarm.agents.swarm.assembly import (
        _render_prompt_text,
        _select_prompt_content,
    )

    sections = list(getattr(template, "prompt_sections", None) or [])
    rendered: list[str] = []
    workspace = str(get_agent_workspace_dir())
    for section in sorted(sections, key=lambda item: getattr(item, "priority", 0)):
        content = getattr(section, "content", None) or {}
        if not isinstance(content, dict):
            continue
        params = {
            "language": language,
            "workspace": workspace,
            **dict(getattr(section, "render_params", None) or {}),
        }
        text = _render_prompt_text(
            _select_prompt_content(content, language),
            params,
        ).strip()
        if text:
            rendered.append(text)
    return "\n\n".join(rendered)


_DESIGNER_MEMBERS = frozenset(
    {"leader", "character", "scene", "storyboard", "frame", "clip"}
)


def _resolve_group_package_dir(group_name: str) -> Path | None:
    if group_name == DESIGNER_AGENT_GROUP_NAME:
        try:
            return designer_agent_group_dir()
        except FileNotFoundError:
            pass
    from jiuwenswarm.server.runtime.extension_package_manager import (
        get_equipment_resources_agent_groups_dir,
        resolve_agent_group_dir,
    )

    resources = get_equipment_resources_agent_groups_dir()
    if resources is not None:
        candidate = resources / group_name
        if candidate.is_dir() and (candidate / "manifest.json").is_file():
            return candidate
    try:
        return resolve_agent_group_dir(group_name)
    except ValueError:
        return None


def load_node_agent_template(node: DesignerGraphNode) -> Any | None:
    """Load the AgentTemplateSpec for a node, or None if unavailable."""
    ref = node_agent_template(node)
    if not ref:
        return None
    group_name, member_id = parse_agent_template_ref(ref)
    if group_name is None and member_id in _DESIGNER_MEMBERS:
        group_name = DESIGNER_AGENT_GROUP_NAME
    if group_name:
        from jiuwenswarm.agents.swarm.agent_group import load_agent_group_package

        package_dir = _resolve_group_package_dir(group_name)
        if package_dir is None:
            return None
        try:
            return load_agent_group_package(package_dir).get(member_id)
        except (OSError, ValueError):
            logger.exception("failed to load AgentGroup %s", group_name)
            return None
    from openjiuwen.harness.resources import load_agent_template_package
    from jiuwenswarm.server.runtime.extension_package_manager import resolve_agent_template_dir

    try:
        package_dir = resolve_agent_template_dir(member_id)
    except ValueError:
        return None
    try:
        return load_agent_template_package(package_dir / "manifest.json")
    except (OSError, ValueError):
        logger.exception("failed to load AgentTemplate %s", member_id)
        return None


def build_node_user_query(node: DesignerGraphNode, ctx: NodeExecutionContext) -> str:
    graph = ctx.graph
    incoming = data_predecessors(graph)
    preds = incoming.get(node["id"], [])
    suggested = [
        edge.get("target")
        for edge in graph.get("edges") or []
        if edge.get("source") == node["id"]
    ]
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    identity = cfg.get("identity_refs") if isinstance(cfg.get("identity_refs"), dict) else {}
    spatial = cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else {}
    if not spatial:
        meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
        spatial = meta.get("spatial_lock") if isinstance(meta.get("spatial_lock"), dict) else {}
    locks = {
        "setting_id": cfg.get("setting_id") or identity.get("setting_id"),
        "keyframe_strategy": cfg.get("keyframe_strategy") or identity.get("keyframe_strategy"),
        "costume_lock": cfg.get("costume_lock") or identity.get("costume_lock"),
        "positioning_lock": cfg.get("positioning_lock"),
        "action_lock": cfg.get("action_lock"),
        "relationship_lock": cfg.get("relationship_lock"),
        "cast_actions": cfg.get("cast_actions"),
        "blocking": cfg.get("blocking"),
        "spatial_lock": spatial or None,
        "occupancy": cfg.get("occupancy") or identity.get("occupancy"),
        "character_node_ids": cfg.get("character_node_ids") or identity.get("character_node_ids"),
        "all_solo_node_ids": identity.get("all_solo_node_ids"),
        "prior_keyframe_node_id": cfg.get("prior_keyframe_node_id")
        or identity.get("prior_keyframe_node_id"),
        "manager_prompt_reviewed": bool(cfg.get("manager_prompt_reviewed")),
        "manager_lock_gate": cfg.get("manager_lock_gate"),
    }
    snapshot = {
        "node_id": node["id"],
        "role": node_pipeline(node) or node_role(node),
        "label": node.get("label"),
        "type": node.get("type"),
        "prompt": graph_prompt(graph, node),
        "shot_index": cfg.get("shot_index"),
        "shot_action": str(cfg.get("shot_action") or "")[:500],
        "camera": str(cfg.get("camera") or "")[:120],
        "agent_template": node_agent_template(node),
        "upstream_node_ids": preds,
        "suggested_next_node_ids": [item for item in suggested if isinstance(item, str)],
        "upstream_outputs": _upstream_outputs(ctx, preds),
        "setting_id": locks["setting_id"],
        "keyframe_strategy": locks["keyframe_strategy"],
        "occupancy": locks["occupancy"],
        "already_done": cfg.get("already_done"),
        "previous_keyframe_prompt": str(cfg.get("previous_keyframe_prompt") or "")[:800],
        "previous_keyframe_action": str(cfg.get("previous_keyframe_action") or "")[:300],
        "previous_clip_wan_prompt": str(cfg.get("previous_clip_wan_prompt") or "")[:800],
        "previous_clip_action": str(cfg.get("previous_clip_action") or "")[:300],
        "scene_architecture_clause": str(cfg.get("scene_architecture_clause") or "")[:900],
        "locks": locks,
        "manager_prompt_reviewed": locks["manager_prompt_reviewed"],
    }
    return (
        "执行当前设计节点。先看 JSON 上下文，用工具完成本节点产物，最后 "
        "designer_node_complete。\n"
        "CRITICAL: Call designer_node_complete before finishing, then STOP. "
        "Do not designer_node_run graph nodes that already exist — the scheduler "
        "starts them after this node completes. Only designer_node_run a node you "
        "just added with designer_graph_patch.\n"
        "TOOLS (not heuristics): Use call_model for text reasoning; call_image_model "
        "for stills (configured image model); call_video_model for clips (configured "
        "video model); ffmpeg_compose / mix_audio for film assemble; read_upstream "
        "for prior outputs. Prefer tool-produced file URIs in designer_node_complete.\n"
        "LOCKS (must keep in every tool prompt before image/video calls): "
        "obey locks.keyframe_strategy (compose_from_solo_refs for first KF of a setting; "
        "edit_prior_keyframe for later same-setting KFs), costume_lock, spatial_lock, "
        "occupancy, and solo identity sheets — never invent new faces/wardrobe/architecture.\n"
        "CONTINUITY: When previous_* prompts are present, read them fully and continue — "
        "do not restage finished onsets/exits/dialogue. Keep character consistency "
        "(same faces/costumes) on every clip call. Advance this shot only unless the "
        "storyboard asks for an explicit repeat.\n\n"
        f"```json\n{json.dumps(snapshot, ensure_ascii=False, indent=2)}\n```"
    )


def _upstream_outputs(ctx: NodeExecutionContext, pred_ids: list[str]) -> list[dict[str, Any]]:
    run = ctx.run or {}
    states = run.get("node_states") or {}
    items: list[dict[str, Any]] = []
    for node_id in pred_ids:
        state = states.get(node_id) or {}
        ref = state.get("output_ref") or {}
        items.append(
            {
                "node_id": node_id,
                "status": state.get("status"),
                "output_ref": ref if isinstance(ref, dict) else {},
            }
        )
    return items


@dataclass
class DesignerGraphToolkit:
    """In-process graph tools bound to the active node/run."""

    spawner: DesignerNodeSpawner
    ctx: NodeExecutionContext
    completed: NodeResult | None = None
    spawned: list[str] = field(default_factory=list)

    def graph_get(self) -> dict[str, Any]:
        graph_id = str(self.ctx.graph.get("graph_id") or "")
        return self.spawner.load_graph_snapshot(graph_id, self.ctx.run_id)

    def graph_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        graph_id = str(self.ctx.graph.get("graph_id") or "")
        graph = self.spawner.apply_agent_graph_patch(graph_id, patch)
        return {
            "graph_id": graph.get("graph_id"),
            "nodes": [node.get("id") for node in graph.get("nodes") or []],
        }

    async def node_run(self, node_id: str) -> str:
        if self.completed is not None:
            return "already completed this node; scheduler will start remaining graph nodes"
        target = str(node_id or "").strip()
        if not target:
            return "node_id is required"
        if target == self.ctx.node_id:
            return "cannot run the current node"
        result = await self.spawner.spawn_node_agent(self.ctx.run_id, target)
        self.spawned.append(target)
        return result

    async def node_complete(
        self,
        *,
        uri: str = "",
        kind: str = "",
        mime_type: str = "",
        label: str = "",
        extra_uris: list[str] | None = None,
        text: str = "",
    ) -> str:
        refs: list[AssetRef] = []
        node = _node_from_ctx(self.ctx)
        # Prefer materializing `text` when present — agents often pass both a
        # workspace:// uri hint and the actual markdown body; the uri alone may
        # not exist on disk.
        if text.strip():
            path = write_workspace_text(
                f"designer_agent_{self.ctx.run_id}_{self.ctx.node_id}",
                text,
            )
            ref = file_output_ref(
                path,
                kind=kind.strip() or str(node.get("type") or "text"),
                mime_type=mime_type.strip() or "text/markdown",
            )
            if label.strip():
                ref["label"] = label.strip()
            refs.append(ref)
        elif uri.strip():
            refs.append(
                {
                    "kind": kind.strip() or "file",
                    "uri": uri.strip(),
                    "mime_type": mime_type.strip() or "application/octet-stream",
                    "label": label.strip() or Path(uri).name,
                }
            )
        for extra in extra_uris or []:
            extra_uri = str(extra or "").strip()
            if extra_uri:
                refs.append(
                    {
                        "kind": kind.strip() or "file",
                        "uri": extra_uri,
                        "label": Path(extra_uri).name,
                    }
                )
        if not refs:
            return "complete requires uri or text"
        agent_result = NodeResult(
            output_ref=refs[0],
            output_refs=refs,
            message="node completed",
        )
        self.completed = agent_result

        # Eager media materialization: agents often spawn compose right after
        # submitting a clip/image text spec. Generate the real asset here so
        # downstream nodes (and run state) see a media URI before that spawn.
        required_family = _required_media_family(node)
        if _node_expects_media(node) and not _result_satisfies_required_media(
            agent_result, node, self.ctx
        ):
            from jiuwenswarm.server.runtime.designer.handlers import get_node_handler

            _seed_handler_prompt(node, agent_result)
            try:
                media = await get_node_handler(node).execute(node, self.ctx)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Eager media materialization failed node=%s: %s",
                    self.ctx.node_id,
                    exc,
                )
                return f"completed text; media materialization deferred: {exc}"
            if media is not None:
                self.completed = media
                if isinstance(self.ctx.run, dict):
                    states = self.ctx.run.setdefault("node_states", {})
                    states[self.ctx.node_id] = {
                        **dict(states.get(self.ctx.node_id) or {}),
                        "output_ref": media.output_ref,
                        "output_refs": list(media.output_refs or []),
                        "message": media.message,
                    }
                return "completed with media"

        return "completed"

    async def call_model(self, *, prompt: str = "", system: str = "") -> str:
        from jiuwenswarm.server.runtime.designer.model_tools import call_model_tool

        node = _node_from_ctx(self.ctx)
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        optimize = str(
            (self.ctx.graph.get("metadata") or {}).get("optimize_for") or "quality"
        )
        result = await call_model_tool(
            prompt=str(prompt or graph_prompt(self.ctx.graph, node) or "")[:6000],
            system=str(system or cfg.get("supervisor_task") or "You are a Designer node agent.")[:4000],
            optimize_for=optimize,
            max_tokens=16384,
        )
        if not result.get("ok"):
            return f"call_model error: {result.get('error') or 'unknown'}"
        return str(result.get("text") or "")

    async def read_upstream(self) -> str:
        node = _node_from_ctx(self.ctx)
        preds = data_predecessors(self.ctx.graph).get(str(node.get("id") or ""), [])
        return json.dumps(_upstream_outputs(self.ctx, preds), ensure_ascii=False)

    def _media_save_dir(self) -> Path:
        root = get_agent_workspace_dir() / "designer_media" / self.ctx.run_id
        root.mkdir(parents=True, exist_ok=True)
        return root

    async def call_image_model(
        self,
        *,
        prompt: str = "",
        size: str = "1024x1024",
    ) -> str:
        from jiuwenswarm.agents.harness.common.tools.image_tools import generate_image

        node = _node_from_ctx(self.ctx)
        text = str(prompt or "").strip() or str(
            (node.get("config") or {}).get("prompt") or graph_prompt(self.ctx.graph, node) or ""
        )
        if not text:
            return "call_image_model error: prompt required"
        # Seed config so handler fallback uses the same creative direction.
        cfg = node.get("config")
        if not isinstance(cfg, dict):
            cfg = {}
            node["config"] = cfg
        cfg["prompt"] = text[:6000]
        try:
            out = await generate_image(
                prompt=text[:4000],
                size=str(size or "1024x1024"),
                save_dir=str(self._media_save_dir()),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("call_image_model failed: %s", exc, exc_info=True)
            return f"call_image_model error: {exc}"
        path = ""
        for line in str(out or "").splitlines():
            if "Saved to:" in line:
                path = line.split("Saved to:", 1)[-1].strip()
                break
        if not path:
            # generate_image may return path-only on some backends
            candidate = Path(str(out or "").strip())
            if candidate.is_file():
                path = str(candidate)
        if path and Path(path).is_file():
            uri = Path(path).resolve().as_uri()
            self.completed = NodeResult(
                output_ref=file_output_ref(Path(path), kind="image", mime_type="image/png"),
                output_refs=[
                    file_output_ref(Path(path), kind="image", mime_type="image/png")
                ],
                message="call_image_model",
            )
            return f"image_ready uri={uri} path={path}"
        return str(out or "call_image_model produced no file")

    async def call_video_model(
        self,
        *,
        prompt: str = "",
        duration: int = 5,
        first_frame: str = "",
    ) -> str:
        from jiuwenswarm.server.runtime.designer.handlers.clip import (
            _looks_like_contaminated_prompt,
            build_clip_prompt,
            generate_clip_video,
        )

        node = _node_from_ctx(self.ctx)
        # Always ground I2V in storyboard-aligned clip prompt (do not let the leaf
        # invent a freeform motion brief that ignores the shot row).
        structured = ""
        try:
            structured = str(build_clip_prompt(self.ctx.graph, node, self.ctx) or "").strip()
        except Exception:  # noqa: BLE001
            logger.debug("build_clip_prompt failed in call_video_model", exc_info=True)
        agent_text = str(prompt or "").strip()
        if structured:
            text = structured
            if (
                agent_text
                and len(agent_text) < 400
                and not _looks_like_contaminated_prompt(agent_text)
                and agent_text[:120].casefold() not in structured.casefold()
            ):
                text = f"{structured}\nAgent motion note: {agent_text[:300]}"
        else:
            text = agent_text or str(
                (node.get("config") or {}).get("prompt")
                or graph_prompt(self.ctx.graph, node)
                or ""
            )
        if not text:
            return "call_video_model error: prompt required"
        cfg = node.get("config")
        if not isinstance(cfg, dict):
            cfg = {}
            node["config"] = cfg
        cfg["prompt"] = text[:6000]
        frame = str(first_frame or "").strip() or None
        from jiuwenswarm.server.runtime.designer.audio_locks import (
            audio_lock_prompt_block,
            resolve_audio_intent_flags,
            resolve_video_audio_request,
        )

        meta = self.ctx.graph.get("metadata") if isinstance(self.ctx.graph, dict) else {}
        meta = meta if isinstance(meta, dict) else {}
        flags = resolve_audio_intent_flags(meta, cfg)
        block = audio_lock_prompt_block(
            language_lock=str(flags.get("language_lock") or ""),
            speech_by_character=flags.get("speech_by_character") or {},
            speech_line=str(flags.get("speech_line") or ""),
            bgm_lock=flags.get("bgm_lock") or {},
            include_speech=bool(flags.get("include_speech")),
            include_music=bool(flags.get("include_music")),
            clip_embedded=bool(flags.get("clip_embedded")),
        )
        if block and "LANGUAGE LOCK" not in text:
            text = (text + "\n" + block).strip()[:6000]
            cfg["prompt"] = text
        want_audio, _model_override = resolve_video_audio_request(cfg, meta)
        if callable(getattr(self.ctx, "on_prompt_artifact", None)):
            try:
                self.ctx.on_prompt_artifact(text)
            except Exception:  # noqa: BLE001
                logger.debug("call_video_model early prompt handoff failed", exc_info=True)
        # Always use the user-configured video model — never override.
        try:
            result = await generate_clip_video(
                prompt=text[:4000],
                save_dir=str(self._media_save_dir()),
                first_frame=frame,
                duration=max(2, min(10, int(duration or 5))),
                audio=True if want_audio else False,
                model=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("call_video_model failed: %s", exc, exc_info=True)
            return f"call_video_model error: {exc}"
        video_path = str(result.get("video_path") or "").strip()
        if not video_path or not Path(video_path).is_file():
            return f"call_video_model error: no video_path ({result!r})"
        path = Path(video_path)
        self.completed = NodeResult(
            output_ref=file_output_ref(path, kind="video", mime_type="video/mp4"),
            output_refs=[file_output_ref(path, kind="video", mime_type="video/mp4")],
            message="call_video_model" + ("_audio" if want_audio else ""),
        )
        return f"video_ready uri={path.resolve().as_uri()} path={path} audio={want_audio}"

    async def ffmpeg_compose(self, *, prompt: str = "") -> str:
        """Run the compose handler for this node (ffmpeg assemble)."""
        from jiuwenswarm.common.schema.designer_graph import (
            NODE_ROLE_CLIP,
            is_compose_sink_node,
            node_pipeline,
        )
        from jiuwenswarm.server.runtime.designer.handlers import get_node_handler

        node = _node_from_ctx(self.ctx)
        if is_compose_sink_node(node):
            states = (self.ctx.run or {}).get("node_states") or {}
            waiting: list[str] = []
            for n in self.ctx.graph.get("nodes") or []:
                if node_pipeline(n) != NODE_ROLE_CLIP:
                    continue
                nid = str(n.get("id") or "")
                st = states.get(nid) if isinstance(states.get(nid), dict) else {}
                status = str((st or {}).get("status") or "").strip().lower()
                if status not in {"completed", "complete", "done", "success"}:
                    waiting.append(f"{nid}:{status or 'pending'}")
            if waiting:
                return (
                    "ffmpeg_compose blocked: waiting for clips "
                    + ", ".join(waiting[:8])
                    + " — do not assemble an empty film"
                )
        if prompt.strip():
            cfg = node.get("config")
            if not isinstance(cfg, dict):
                cfg = {}
                node["config"] = cfg
            cfg["prompt"] = prompt.strip()[:4000]
        try:
            media = await get_node_handler(node).execute(node, self.ctx)
        except Exception as exc:  # noqa: BLE001
            return f"ffmpeg_compose error: {exc}"
        self.completed = media
        ref = media.output_ref if media else None
        uri = str((ref or {}).get("uri") or "") if isinstance(ref, dict) else ""
        return f"compose_ready uri={uri}" if uri else "compose_ready"

    async def mix_audio(self, *, prompt: str = "") -> str:
        return await self.ffmpeg_compose(prompt=prompt)


def _node_from_ctx(ctx: NodeExecutionContext) -> DesignerGraphNode:
    for node in ctx.graph.get("nodes") or []:
        if node.get("id") == ctx.node_id:
            return node
    return {"id": ctx.node_id, "type": "text", "label": ctx.node_id}


def _emit_ctx_activity(
    ctx: NodeExecutionContext,
    kind: str,
    text: str,
    tool: str = "",
    *,
    force: bool = False,
) -> None:
    emit = getattr(ctx, "emit_activity", None)
    if not callable(emit):
        return
    try:
        emit(kind, text, tool, force=force)
    except TypeError:
        emit(kind, text, tool)


def build_designer_tools(toolkit: DesignerGraphToolkit) -> list[Any]:
    from openjiuwen.core.foundation.tool import LocalFunction, ToolCard

    from jiuwenswarm.common.schema.designer_graph import (
        ACTIVITY_KIND_TOOL_CALL,
    )

    def make_tool(name: str, description: str, input_params: dict[str, Any], func: Any) -> Any:
        async def wrapped(**kwargs: Any) -> Any:
            if toolkit.completed is not None and name == "designer_node_run":
                return "already completed this node; scheduler will start remaining graph nodes"
            _emit_ctx_activity(
                toolkit.ctx,
                ACTIVITY_KIND_TOOL_CALL,
                f"calling {name}",
                tool=name,
                force=True,
            )
            result = await func(**kwargs)
            preview = str(result or "")
            _emit_ctx_activity(
                toolkit.ctx,
                ACTIVITY_KIND_TOOL_CALL,
                preview[:80] or f"{name} done",
                tool=name,
            )
            return result

        card = ToolCard(name=name, description=description, input_params=input_params)
        return LocalFunction(card=card, func=wrapped)

    def _payload(**kwargs: Any) -> dict[str, Any]:
        """LocalFunction invokes tools as func(**schema_fields)."""
        payload = dict(kwargs)
        nested = payload.pop("inputs", None)
        if isinstance(nested, dict):
            merged = dict(nested)
            merged.update(payload)
            return merged
        return payload

    async def graph_get(**_kwargs: Any) -> str:
        return json.dumps(toolkit.graph_get(), ensure_ascii=False)

    async def graph_patch(**kwargs: Any) -> str:
        payload = _payload(**kwargs)
        patch = payload.get("patch")
        if isinstance(patch, str):
            try:
                patch = json.loads(patch)
            except json.JSONDecodeError:
                return "patch must be a JSON object"
        if not isinstance(patch, dict):
            return "patch must be an object"
        return json.dumps(toolkit.graph_patch(patch), ensure_ascii=False)

    async def node_run(**kwargs: Any) -> str:
        payload = _payload(**kwargs)
        return await toolkit.node_run(str(payload.get("node_id") or ""))

    async def node_complete(**kwargs: Any) -> str:
        payload = _payload(**kwargs)
        extra = payload.get("extra_uris") or payload.get("extraUris") or []
        if isinstance(extra, str):
            extra = [item.strip() for item in extra.split(",") if item.strip()]
        return await toolkit.node_complete(
            uri=str(payload.get("uri") or ""),
            kind=str(payload.get("kind") or ""),
            mime_type=str(payload.get("mime_type") or payload.get("mimeType") or ""),
            label=str(payload.get("label") or ""),
            extra_uris=list(extra) if isinstance(extra, list) else None,
            text=str(payload.get("text") or ""),
        )

    async def call_model(**kwargs: Any) -> str:
        payload = _payload(**kwargs)
        return await toolkit.call_model(
            prompt=str(payload.get("prompt") or ""),
            system=str(payload.get("system") or ""),
        )

    async def read_upstream(**_kwargs: Any) -> str:
        return await toolkit.read_upstream()

    async def call_image_model(**kwargs: Any) -> str:
        payload = _payload(**kwargs)
        return await toolkit.call_image_model(
            prompt=str(payload.get("prompt") or ""),
            size=str(payload.get("size") or "1024x1024"),
        )

    async def call_video_model(**kwargs: Any) -> str:
        payload = _payload(**kwargs)
        try:
            duration = int(payload.get("duration") or 5)
        except (TypeError, ValueError):
            duration = 5
        return await toolkit.call_video_model(
            prompt=str(payload.get("prompt") or ""),
            duration=duration,
            first_frame=str(payload.get("first_frame") or payload.get("firstFrame") or ""),
        )

    async def ffmpeg_compose(**kwargs: Any) -> str:
        payload = _payload(**kwargs)
        return await toolkit.ffmpeg_compose(prompt=str(payload.get("prompt") or ""))

    async def mix_audio(**kwargs: Any) -> str:
        payload = _payload(**kwargs)
        return await toolkit.mix_audio(prompt=str(payload.get("prompt") or ""))

    # Always expose graph tools + role tools from node.config.tools (jiuwen agents).
    node = _node_from_ctx(toolkit.ctx)
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    wanted = {str(t).strip() for t in (cfg.get("tools") or []) if str(t).strip()}
    # Default capability set when config.tools empty — still prefer real tools.
    if not wanted:
        wanted = {
            "call_model",
            "read_upstream",
            "call_image_model",
            "call_video_model",
            "ffmpeg_compose",
            "mix_audio",
        }

    tools: list[Any] = [
        make_tool(
            "designer_graph_get",
            "读取当前设计图、运行态和各节点产出。",
            {"type": "object", "properties": {}},
            graph_get,
        ),
        make_tool(
            "designer_graph_patch",
            "按 Designer graph patch 增删节点或边。patch 可为对象或 JSON 字符串。",
            {
                "type": "object",
                "properties": {
                    "patch": {
                        "anyOf": [
                            {"type": "object"},
                            {"type": "string"},
                        ]
                    }
                },
                "required": ["patch"],
            },
            graph_patch,
        ),
        make_tool(
            "designer_node_run",
            "启动另一个节点上的 Agent。",
            {
                "type": "object",
                "properties": {"node_id": {"type": "string"}},
                "required": ["node_id"],
            },
            node_run,
        ),
        make_tool(
            "designer_node_complete",
            "提交当前节点产物。可给 file uri，或 text 写成工作区 Markdown。",
            {
                "type": "object",
                "properties": {
                    "uri": {"type": "string"},
                    "kind": {"type": "string"},
                    "mime_type": {"type": "string"},
                    "label": {"type": "string"},
                    "text": {"type": "string"},
                    "extra_uris": {"type": "array", "items": {"type": "string"}},
                },
            },
            node_complete,
        ),
    ]
    if "call_model" in wanted:
        tools.append(
            make_tool(
                "call_model",
                "Call the configured chat LLM (Settings model) for creative text/JSON.",
                {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "system": {"type": "string"},
                    },
                },
                call_model,
            )
        )
    if "read_upstream" in wanted:
        tools.append(
            make_tool(
                "read_upstream",
                "Read upstream node outputs for this node.",
                {"type": "object", "properties": {}},
                read_upstream,
            )
        )
    if "call_image_model" in wanted:
        tools.append(
            make_tool(
                "call_image_model",
                "Generate a still via the configured image model (Qwen/image gen).",
                {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "size": {"type": "string"},
                    },
                    "required": ["prompt"],
                },
                call_image_model,
            )
        )
    if "call_video_model" in wanted:
        tools.append(
            make_tool(
                "call_video_model",
                "Generate a clip via the configured video model (Wan).",
                {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "duration": {"type": "integer"},
                        "first_frame": {"type": "string"},
                    },
                    "required": ["prompt"],
                },
                call_video_model,
            )
        )
    if "ffmpeg_compose" in wanted:
        tools.append(
            make_tool(
                "ffmpeg_compose",
                "Assemble the final film with ffmpeg/compose handler.",
                {
                    "type": "object",
                    "properties": {"prompt": {"type": "string"}},
                },
                ffmpeg_compose,
            )
        )
    if "mix_audio" in wanted:
        tools.append(
            make_tool(
                "mix_audio",
                "Mix speech/music into the film (compose path).",
                {
                    "type": "object",
                    "properties": {"prompt": {"type": "string"}},
                },
                mix_audio,
            )
        )
    return tools


class NodeAgentHost:
    """Run one Designer node as a DeepAgent, with handler fallback."""

    def __init__(
        self,
        spawner: DesignerNodeSpawner,
        *,
        runner: NodeAgentRunner | None = None,
    ) -> None:
        self._spawner = spawner
        self._runner = runner
        self._agents: dict[str, Any] = {}

    def agent_key(self, run_id: str, node_id: str) -> str:
        return f"designer:{run_id}:{node_id}"

    def drop_run(self, run_id: str) -> None:
        prefix = f"designer:{run_id}:"
        for key in [item for item in self._agents if item.startswith(prefix)]:
            self._agents.pop(key, None)

    async def execute(
        self,
        node: DesignerGraphNode,
        ctx: NodeExecutionContext,
    ) -> NodeResult:
        toolkit = DesignerGraphToolkit(self._spawner, ctx)
        from jiuwenswarm.server.runtime.designer.executor import _node_execute_timeout_sec

        timeout = float(_node_execute_timeout_sec(node))

        async def _body() -> NodeResult:
            if self._runner is not None:
                return await self._runner(node, ctx, toolkit)
            return await self._run_deep_agent(node, ctx, toolkit)

        try:
            try:
                agent_result = await asyncio.wait_for(_body(), timeout=timeout)
            except TimeoutError:
                if toolkit.completed is not None:
                    logger.warning(
                        "Designer node agent timed out after complete; keeping output. node=%s",
                        ctx.node_id,
                    )
                    agent_result = toolkit.completed
                else:
                    raise TimeoutError(
                        f"node {ctx.node_id} timed out after {int(timeout)}s"
                    ) from None
        except Exception:
            if toolkit.completed is not None:
                logger.warning(
                    "Designer node agent failed after complete; keeping output. node=%s",
                    ctx.node_id,
                    exc_info=True,
                )
                agent_result = toolkit.completed
            elif self._runner is not None:
                raise
            else:
                logger.exception(
                    "Designer node agent failed; falling back to handler. node=%s",
                    ctx.node_id,
                )
                from jiuwenswarm.server.runtime.designer.handlers import get_node_handler

                return await get_node_handler(node).execute(node, ctx)

        # Agents author creative direction via designer_* tools, but media
        # generation backends live on handlers. If the agent only submitted
        # text/markdown (or the wrong media family, e.g. PNG on a clip node),
        # materialize the expected media while keeping the AI creative path.
        required_family = _required_media_family(node)
        if _node_expects_media(node) and not _result_satisfies_required_media(
            agent_result, node, ctx
        ):
            from jiuwenswarm.server.runtime.designer.handlers import get_node_handler

            _seed_handler_prompt(node, agent_result)
            logger.info(
                "Materializing media via handler after agent text output. "
                "node=%s role=%s required=%s",
                ctx.node_id,
                node_pipeline(node) or node_role(node),
                required_family,
            )
            try:
                media = await get_node_handler(node).execute(node, ctx)
            except Exception:
                logger.exception(
                    "Media materialization failed; not keeping markdown stub. node=%s",
                    ctx.node_id,
                )
                raise
            if not _result_satisfies_required_media(media, node, ctx):
                raise RuntimeError(
                    f"node {ctx.node_id} handler did not produce required "
                    f"{required_family or 'media'}"
                )
            refs: list[AssetRef] = []
            if isinstance(media.output_ref, dict):
                refs.append(media.output_ref)
            for ref in media.output_refs or []:
                if isinstance(ref, dict) and ref not in refs:
                    refs.append(ref)
            if isinstance(agent_result.output_ref, dict) and agent_result.output_ref not in refs:
                refs.append(agent_result.output_ref)
            for ref in agent_result.output_refs or []:
                if isinstance(ref, dict) and ref not in refs:
                    refs.append(ref)
            return NodeResult(
                output_ref=media.output_ref or (refs[0] if refs else None),
                output_refs=refs,
                message=f"agent+handler: {media.message or 'media materialized'}",
            )
        return agent_result

    async def _run_deep_agent(
        self,
        node: DesignerGraphNode,
        ctx: NodeExecutionContext,
        toolkit: DesignerGraphToolkit,
    ) -> NodeResult:
        from openjiuwen.core.foundation.llm import Model, ModelClientConfig
        from openjiuwen.core.single_agent import AgentCard
        from openjiuwen.harness.factory import create_deep_agent
        from openjiuwen.harness.schema.config import Workspace

        from jiuwenswarm.common.config import get_config, get_default_models

        template = load_node_agent_template(node)
        persona = flatten_template_prompt(template) if template else ""
        system_prompt = persona or "你是设计画布上的节点 Agent。用工具完成任务并提交产物。"
        query = build_node_user_query(node, ctx)
        from jiuwenswarm.common.schema.designer_graph import ACTIVITY_KIND_THINKING

        _emit_ctx_activity(
            ctx,
            ACTIVITY_KIND_THINKING,
            f"planning {node.get('label') or ctx.node_id}",
            force=True,
        )
        tools = build_designer_tools(toolkit)

        key = self.agent_key(ctx.run_id, ctx.node_id)
        agent = self._agents.get(key)
        if agent is None:
            entries = get_default_models(get_config())
            entry = next((item for item in entries if item.get("is_default") is True), None)
            if entry is None and entries:
                entry = entries[0]
            client = (entry or {}).get("model_client_config") if isinstance(entry, dict) else {}
            mco = (entry or {}).get("model_config_obj") if isinstance(entry, dict) else {}
            if not isinstance(client, dict):
                raise RuntimeError("no model configured for designer node agent")
            if not isinstance(mco, dict):
                mco = {}
            api_key = str(client.get("api_key") or "").strip()
            model_name = str(client.get("model_name") or "").strip()
            if not api_key or not model_name:
                raise RuntimeError("incomplete model config for designer node agent")
            kwargs: dict[str, object] = {
                "api_key": api_key,
                "api_base": str(client.get("api_base") or "").strip(),
                "client_provider": str(client.get("client_provider") or "").strip(),
            }
            profile = str(client.get("endpoint_profile") or "").strip()
            if profile:
                kwargs["endpoint_profile"] = profile
            from openjiuwen.core.foundation.llm.schema.config import ModelRequestConfig

            request = ModelRequestConfig(
                model_name=model_name,
                temperature=float(mco.get("temperature", 0.95) or 0.95),
                top_p=float(mco.get("top_p", 0.95) or 0.95),
            )
            model = Model(
                model_client_config=ModelClientConfig(**kwargs),
                model_config=request,
            )
            workspace_dir = get_agent_workspace_dir()
            workspace_dir.mkdir(parents=True, exist_ok=True)
            card_name = str(
                getattr(getattr(template, "agent_card", None), "name", "")
                or node.get("label")
                or ctx.node_id
            )
            agent = create_deep_agent(
                model=model,
                card=AgentCard(name=card_name, id=key, description=card_name),
                system_prompt=system_prompt,
                tools=tools,
                workspace=Workspace(root_path=str(workspace_dir)),
                # Task loop needs a Session; we pass one on invoke below.
                enable_task_loop=True,
                max_iterations=14,
                add_general_purpose_agent=False,
            )
            ensure = getattr(agent, "ensure_initialized", None)
            if callable(ensure):
                maybe = ensure()
                if hasattr(maybe, "__await__"):
                    await maybe
            self._agents[key] = agent

        invoke = getattr(agent, "invoke", None)
        if not callable(invoke):
            raise RuntimeError("DeepAgent has no invoke")
        # openjiuwen DeepAgent requires an explicit Session for task-loop mode.
        from openjiuwen.core.session.agent import Session

        session = Session(session_id=key, card=getattr(agent, "card", None))
        result = invoke({"query": query, "conversation_id": key}, session=session)
        if hasattr(result, "__await__"):
            result = await result
        if toolkit.completed is not None:
            completed = toolkit.completed
            # Guarantee required media family even when agent completed with text/PNG only.
            required_family = _required_media_family(node)
            if _node_expects_media(node) and not _result_satisfies_required_media(
                completed, node, ctx
            ):
                from jiuwenswarm.server.runtime.designer.handlers import get_node_handler

                _seed_handler_prompt(node, completed)
                try:
                    media = await get_node_handler(node).execute(node, ctx)
                except Exception:
                    logger.exception(
                        "Post-complete media materialization failed. node=%s", ctx.node_id
                    )
                    media = None
                if media is not None and _result_satisfies_required_media(media, node, ctx):
                    refs: list[AssetRef] = []
                    if isinstance(media.output_ref, dict):
                        refs.append(media.output_ref)
                    for ref in media.output_refs or []:
                        if isinstance(ref, dict) and ref not in refs:
                            refs.append(ref)
                    if isinstance(completed.output_ref, dict):
                        refs.append(completed.output_ref)
                    completed = NodeResult(
                        output_ref=media.output_ref or (refs[0] if refs else None),
                        output_refs=refs,
                        message=f"agent+handler: {media.message or 'media materialized'}",
                    )
            if _node_expects_media(node):
                required_family = _required_media_family(node)
                if not _result_satisfies_required_media(completed, node, ctx):
                    raise RuntimeError(
                        f"node {ctx.node_id} finished without required {required_family} media"
                    )
            return completed
        text = ""
        if isinstance(result, dict):
            text = str(result.get("output") or result.get("content") or "")
        elif isinstance(result, str):
            text = result
        else:
            text = str(getattr(result, "content", "") or result)
        if not text.strip():
            # Empty agent reply on media nodes → handler path (no silent hang).
            if _node_expects_media(node):
                from jiuwenswarm.server.runtime.designer.handlers import get_node_handler

                return await get_node_handler(node).execute(node, ctx)
            raise RuntimeError("node agent returned empty output")
        path = write_workspace_text(f"designer_agent_{ctx.run_id}_{ctx.node_id}", text)
        agent_result = NodeResult(
            output_ref=file_output_ref(
                path,
                kind=str(node.get("type") or "text"),
                mime_type="text/markdown",
            ),
            message="node agent text fallback",
        )
        required_family = _required_media_family(node)
        if _node_expects_media(node):
            from jiuwenswarm.server.runtime.designer.handlers import get_node_handler

            _seed_handler_prompt(node, agent_result)
            media = await get_node_handler(node).execute(node, ctx)
            if not _result_satisfies_required_media(media, node, ctx):
                raise RuntimeError(
                    f"node {ctx.node_id} handler did not produce required {required_family}"
                )
            refs = []
            if isinstance(media.output_ref, dict):
                refs.append(media.output_ref)
            for ref in media.output_refs or []:
                if isinstance(ref, dict) and ref not in refs:
                    refs.append(ref)
            refs.append(agent_result.output_ref)  # type: ignore[arg-type]
            return NodeResult(
                output_ref=media.output_ref or refs[0],
                output_refs=refs,
                message=f"agent-text+handler: {media.message or 'media materialized'}",
            )
        return agent_result
