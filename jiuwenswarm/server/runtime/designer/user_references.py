# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Register user-attached image / video / audio as Designer bootstrap sources.

Original files stay the visual/audio authority. Analysis and prompts only
assign ordered slots (image 1 / video 1 / audio 1); they do not replace the
media with a text summary.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_TYPE_AUDIO,
    NODE_TYPE_IMAGE,
    NODE_TYPE_VIDEO,
    node_pipeline,
)
from jiuwenswarm.server.runtime.attachments.upload_storage import (
    safe_upload_filename,
    unique_upload_path,
)

logger = logging.getLogger(__name__)

DEFAULT_ROLE = "reference"
REFERENCE_NODE_PREFIX = "n_ref_"
# Roles that describe look-and-feel only. Everything else may carry a subject
# whose identity the cast cards must preserve instead of redesigning.
STYLE_ONLY_ROLES = frozenset(
    {"style", "composition", "mood", "palette", "background", "environment", "scene"}
)
KIND_IMAGE = "image"
KIND_VIDEO = "video"
KIND_AUDIO = "audio"
SUPPORTED_KINDS = frozenset({KIND_IMAGE, KIND_VIDEO, KIND_AUDIO})
MAX_REFS_BY_KIND = {KIND_IMAGE: 3, KIND_VIDEO: 1, KIND_AUDIO: 1}
MAX_INLINE_BYTES = 6 * 1024 * 1024

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".jfif"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
_AUDIO_SUFFIXES = {".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a"}
_MIME_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
}
_DATA_URI_RE = re.compile(r"^data:([^;,]+);base64,(.+)$", re.DOTALL | re.IGNORECASE)


class UserReferenceError(ValueError):
    """Raised when bootstrap references cannot be materialized."""

    def __init__(self, message: str, *, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.code = code


def graph_user_references(graph: dict[str, Any] | None) -> list[dict[str, Any]]:
    meta = (graph or {}).get("metadata") if isinstance(graph, dict) else None
    raw = (meta or {}).get("user_references") if isinstance(meta, dict) else None
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def prompt_slot_roster(refs: list[dict[str, Any]] | None) -> str:
    """Ordered slot labels for prompts; never include paths or internal ids."""
    counts = {KIND_IMAGE: 0, KIND_VIDEO: 0, KIND_AUDIO: 0}
    lines: list[str] = []
    for item in refs or []:
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in SUPPORTED_KINDS:
            continue
        counts[kind] += 1
        filename = str(item.get("filename") or f"{kind}-{counts[kind]}").strip()
        lines.append(
            f"{kind} {counts[kind]} = {filename}, generic reference "
            "(visual/audio authority; take compatible visible traits)"
        )
    return "\n".join(lines)


def analysis_prompt_with_references(prompt: str, refs: list[dict[str, Any]] | None) -> str:
    text = (prompt or "").strip()
    roster = prompt_slot_roster(refs)
    if not roster:
        return text
    prefix = text or "根据参考素材创作"
    return (
        f"{prefix}\n\n"
        "REFERENCE_MEDIA (not a story beat; do not turn into a shot):\n"
        "Original files are the authority; do not reduce them to a style-only summary. "
        "Slots in attachment order:\n"
        f"{roster}"
    )


def user_reference_paths(graph: dict[str, Any] | None, *, kind: str) -> list[Path]:
    wanted = str(kind or "").strip().lower()
    suffixes = {
        KIND_IMAGE: _IMAGE_SUFFIXES,
        KIND_VIDEO: _VIDEO_SUFFIXES,
        KIND_AUDIO: _AUDIO_SUFFIXES,
    }.get(wanted, set())
    paths: list[Path] = []
    seen: set[str] = set()
    for item in graph_user_references(graph):
        if str(item.get("kind") or "").strip().lower() != wanted:
            continue
        path = _existing_file(str(item.get("path") or item.get("uri") or ""))
        if path is None or path.suffix.lower() not in suffixes:
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        paths.append(path.resolve())
    return paths


def user_reference_image_paths(graph: dict[str, Any] | None) -> list[Path]:
    return user_reference_paths(graph, kind=KIND_IMAGE)


def identity_reference_image_paths(graph: dict[str, Any] | None) -> list[Path]:
    """Uploads that may depict a subject, so cast cards must not redesign them."""
    style_only: set[str] = set()
    for item in graph_user_references(graph):
        if str(item.get("kind") or "").strip().lower() != KIND_IMAGE:
            continue
        if str(item.get("role") or DEFAULT_ROLE).strip().lower() not in STYLE_ONLY_ROLES:
            continue
        candidate = _existing_file(str(item.get("path") or item.get("uri") or ""))
        if candidate is not None:
            style_only.add(str(candidate.resolve()))
    return [p for p in user_reference_image_paths(graph) if str(p) not in style_only]


def user_reference_video_path(graph: dict[str, Any] | None) -> Path | None:
    paths = user_reference_paths(graph, kind=KIND_VIDEO)
    return paths[0] if paths else None


def user_reference_audio_path(graph: dict[str, Any] | None) -> Path | None:
    paths = user_reference_paths(graph, kind=KIND_AUDIO)
    return paths[0] if paths else None


def attach_user_references_to_graph(
    graph: dict[str, Any],
    refs: list[dict[str, Any]],
) -> dict[str, Any]:
    stored = [_public_record(item, index) for index, item in enumerate(refs, start=1)]
    meta = dict(graph.get("metadata") or {})
    meta["user_references"] = stored
    graph["metadata"] = meta
    roster = prompt_slot_roster(stored)
    ids = [str(item["id"]) for item in stored]
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict) or node_pipeline(node) != NODE_ROLE_BRIEF:
            continue
        cfg = dict(node.get("config") or {})
        cfg["user_reference_ids"] = ids
        task = str(cfg.get("supervisor_task") or "").strip()
        if roster:
            extra = (
                " User attached reference media; original files are visual/audio "
                f"authority. Slots:\n{roster}"
            )
            cfg["supervisor_task"] = f"{task}{extra}".strip() if task else extra.strip()
        node["config"] = cfg
        break
    attach_user_reference_nodes(graph)
    return graph


def carry_user_references(
    source_meta: dict[str, Any] | None,
    rebuilt: dict[str, Any] | None,
) -> list[str]:
    """Re-seed uploads onto a rebuilt graph.

    ``build_smart_video_graph`` starts from a fresh metadata dict, so a
    Supervisor redesign or shot re-expansion would otherwise drop the user's
    attachments and their canvas nodes.
    """
    if not isinstance(rebuilt, dict):
        return []
    refs = (source_meta or {}).get("user_references") if isinstance(source_meta, dict) else None
    refs = [item for item in refs if isinstance(item, dict)] if isinstance(refs, list) else []
    if not refs:
        return []
    attach_user_references_to_graph(rebuilt, refs)
    return user_reference_node_ids(rebuilt)


def user_reference_node_ids(graph: dict[str, Any] | None) -> list[str]:
    """Canvas nodes that stand for a user upload (never regenerated)."""
    out: list[str] = []
    for node in (graph or {}).get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        if str(cfg.get("user_reference_id") or "").strip():
            out.append(str(node.get("id") or ""))
    return [nid for nid in out if nid]


def is_user_reference_node(node: dict[str, Any] | None) -> bool:
    """True for immutable canvas nodes backed by a user upload."""
    if not isinstance(node, dict):
        return False
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    return bool(str(cfg.get("user_reference_id") or "").strip())


def user_reference_node_file(node: dict[str, Any] | None) -> Path | None:
    """Resolve the uploaded file behind a reference node."""
    cfg = (node or {}).get("config") if isinstance(node, dict) else None
    cfg = cfg if isinstance(cfg, dict) else {}
    candidate = _existing_file(str(cfg.get("user_reference_path") or ""))
    if candidate is not None:
        return candidate.resolve()
    ref_uri = ""
    output_ref = (node or {}).get("output_ref") if isinstance(node, dict) else None
    if isinstance(output_ref, dict):
        ref_uri = str(output_ref.get("uri") or "")
    candidate = _existing_file(ref_uri)
    return candidate.resolve() if candidate is not None else None


def attach_user_reference_nodes(graph: dict[str, Any]) -> list[str]:
    """Show each upload as its own canvas node feeding Brief (and cast cards).

    The node is a passthrough asset: the original file is the authority, so the
    runner must never regenerate it. Images also reach character cards, which
    lets the cast be derived from the upload instead of invented.
    """
    refs = graph_user_references(graph)
    if not refs:
        return []
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
    existing = {str(n.get("id") or "") for n in nodes}
    edge_ids = {str(e.get("id") or "") for e in edges}
    brief_ids = [
        str(n.get("id"))
        for n in nodes
        if node_pipeline(n) == NODE_ROLE_BRIEF and str(n.get("id") or "")
    ]
    cast_ids = [
        str(n.get("id"))
        for n in nodes
        if node_pipeline(n) == NODE_ROLE_CHARACTER_DESIGN and str(n.get("id") or "")
    ]
    node_type_by_kind = {
        KIND_IMAGE: NODE_TYPE_IMAGE,
        KIND_VIDEO: NODE_TYPE_VIDEO,
        KIND_AUDIO: NODE_TYPE_AUDIO,
    }
    added: list[str] = []
    for order, item in enumerate(refs, start=1):
        kind = str(item.get("kind") or KIND_IMAGE).strip().lower()
        node_type = node_type_by_kind.get(kind)
        path = _existing_file(str(item.get("path") or item.get("uri") or ""))
        if node_type is None or path is None:
            continue
        ref_id = str(item.get("id") or f"ref_{order:02d}")
        node_id = f"{REFERENCE_NODE_PREFIX}{order:02d}"
        if node_id in existing:
            continue
        resolved = path.resolve()
        filename = str(item.get("filename") or resolved.name)
        mime = str(item.get("mime_type") or _guess_mime(kind, resolved.suffix))
        nodes.append(
            {
                "id": node_id,
                "type": node_type,
                "label": f"Reference {order}: {filename}",
                "config": {
                    "role": node_type,
                    "user_reference_id": ref_id,
                    "user_reference_kind": kind,
                    "user_reference_path": str(resolved),
                    "interaction_mode": "upload",
                    "upload": {
                        "filename": filename,
                        "asset_id": ref_id,
                        "mime_type": mime,
                    },
                    "materials": [
                        {
                            "id": ref_id,
                            "filename": filename,
                            "mime_type": mime,
                            "uri": resolved.as_uri(),
                        }
                    ],
                    "inputs": [],
                    "delegate": "handler",
                    "force_handler": True,
                    "skip_llm": True,
                    "read_only": True,
                    "immutable_source": True,
                    "supervisor_task": (
                        "User-attached reference. Keep the original file as the "
                        "visual/audio authority; never regenerate or restyle it."
                    ),
                },
                "layout": {
                    "x": -300.0,
                    "y": float(120 + (order - 1) * 180),
                    "width": 220,
                    "height": 140,
                },
                "output_ref": {
                    "kind": node_type,
                    "uri": resolved.as_uri(),
                    "mime_type": mime,
                    "label": filename,
                },
            }
        )
        existing.add(node_id)
        added.append(node_id)
        targets = list(brief_ids)
        if kind == KIND_IMAGE:
            targets.extend(cast_ids)
        for target in targets:
            edge_id = f"e_{node_id}_{target}"
            if edge_id in edge_ids:
                continue
            edges.append({"id": edge_id, "source": node_id, "target": target})
            edge_ids.add(edge_id)
            for node in nodes:
                if str(node.get("id") or "") != target:
                    continue
                cfg = dict(node.get("config") or {})
                inputs = [str(x) for x in (cfg.get("inputs") or [])]
                if node_id not in inputs:
                    inputs.append(node_id)
                cfg["inputs"] = inputs
                if target in cast_ids:
                    cfg["character_source_reference"] = ref_id
                    task = str(cfg.get("supervisor_task") or "").strip()
                    hint = (
                        " A user reference image is wired into this card: if it "
                        "depicts this character, derive the sheet from it "
                        "(face, wardrobe, palette) instead of inventing a look."
                    )
                    if hint.strip() not in task:
                        cfg["supervisor_task"] = f"{task}{hint}".strip()
                node["config"] = cfg
    if not added:
        return []
    graph["nodes"] = nodes
    graph["edges"] = edges
    meta = dict(graph.get("metadata") or {})
    meta["user_reference_nodes"] = user_reference_node_ids(graph)
    graph["metadata"] = meta
    return added


def normalize_user_references(
    raw: Any,
    *,
    dest_dir: Path | None,
) -> list[dict[str, Any]]:
    """Copy / decode attachments into dest_dir and return ordered source records.

    When ``dest_dir`` is None, return lightweight preview records (no disk copy)
    suitable for LLM analysis before the project directory exists.
    """
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise UserReferenceError("references must be an array")
    preview_only = dest_dir is None
    if not preview_only:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
    counts = {KIND_IMAGE: 0, KIND_VIDEO: 0, KIND_AUDIO: 0}
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        kind = _infer_kind(item)
        if kind is None:
            raise UserReferenceError(
                f"reference {index} must be an image, video, or audio file"
            )
        limit = MAX_REFS_BY_KIND[kind]
        if counts[kind] >= limit:
            raise UserReferenceError(
                f"at most {limit} {kind} reference(s) can be attached"
            )
        if preview_only:
            record = _preview_reference(item, kind=kind, index=index)
        else:
            record = _materialize_reference(
                item, kind=kind, dest_dir=dest_dir, index=index
            )
        counts[kind] += 1
        out.append(record)
    return out


def _preview_reference(item: dict[str, Any], *, kind: str, index: int) -> dict[str, Any]:
    """In-memory reference slot for analysis (no materialize / mkdir)."""
    path_raw = str(item.get("path") or "").strip()
    uri = str(item.get("uri") or "").strip()
    filename = str(item.get("filename") or Path(path_raw or uri).name or f"{kind}-{index}")
    path = Path(path_raw) if path_raw else Path()
    if not path_raw and uri.startswith("file:"):
        try:
            from urllib.parse import unquote, urlparse
            from urllib.request import url2pathname

            parsed = urlparse(uri)
            path = Path(url2pathname(unquote(parsed.path)))
            path_raw = str(path)
        except Exception:  # noqa: BLE001
            path_raw = ""
    return {
        "id": str(item.get("id") or f"ref_{index:02d}"),
        "kind": kind,
        "role": str(item.get("role") or DEFAULT_ROLE),
        "filename": filename,
        "mime_type": str(item.get("mime_type") or item.get("mimeType") or ""),
        "path": path_raw,
        "uri": uri or (path.resolve().as_uri() if path_raw and path.exists() else ""),
        "size_bytes": int(item.get("size_bytes") or 0),
        "order": index,
        "preview": True,
    }


def _public_record(item: dict[str, Any], index: int) -> dict[str, Any]:
    path = Path(str(item.get("path") or ""))
    uri = str(item.get("uri") or "").strip() or (path.resolve().as_uri() if path.exists() else "")
    return {
        "id": str(item.get("id") or f"ref_{index:02d}"),
        "kind": str(item.get("kind") or KIND_IMAGE),
        "role": str(item.get("role") or DEFAULT_ROLE),
        "filename": str(item.get("filename") or path.name or f"ref_{index:02d}"),
        "mime_type": str(item.get("mime_type") or ""),
        "path": str(path.resolve()) if str(path) else "",
        "uri": uri,
        "size_bytes": int(item.get("size_bytes") or 0),
        "order": index,
    }


def _infer_kind(item: dict[str, Any]) -> str | None:
    explicit = str(item.get("kind") or item.get("type") or "").strip().lower()
    if explicit in SUPPORTED_KINDS:
        return explicit
    mime = str(item.get("mime_type") or item.get("mimeType") or "").strip().lower()
    if mime.startswith("image/"):
        return KIND_IMAGE
    if mime.startswith("video/"):
        return KIND_VIDEO
    if mime.startswith("audio/"):
        return KIND_AUDIO
    name = str(item.get("filename") or item.get("path") or item.get("uri") or "").lower()
    suffix = Path(name.split("?", 1)[0]).suffix
    if suffix in _IMAGE_SUFFIXES:
        return KIND_IMAGE
    if suffix in _VIDEO_SUFFIXES:
        return KIND_VIDEO
    if suffix in _AUDIO_SUFFIXES:
        return KIND_AUDIO
    return None


def _materialize_reference(
    item: dict[str, Any],
    *,
    kind: str,
    dest_dir: Path,
    index: int,
) -> dict[str, Any]:
    filename = str(item.get("filename") or f"{kind}-{index}").strip() or f"{kind}-{index}"
    mime = str(item.get("mime_type") or item.get("mimeType") or "").strip()
    source = _existing_file(
        str(item.get("path") or item.get("uri") or item.get("local_path") or "")
    )
    payload = _decode_inline_bytes(item)
    if source is None and payload is None:
        raise UserReferenceError(f"reference {index} is missing a readable file")
    suffix = Path(filename).suffix.lower()
    if not suffix:
        suffix = _MIME_SUFFIX.get(mime.lower()) or {
            KIND_IMAGE: ".png",
            KIND_VIDEO: ".mp4",
            KIND_AUDIO: ".mp3",
        }[kind]
        filename = f"{filename}{suffix}"
    safe_name = safe_upload_filename(filename, fallback=f"{kind}-{index}{suffix}")
    dest = unique_upload_path(dest_dir / safe_name)
    if payload is not None:
        dest.write_bytes(payload)
    else:
        assert source is not None
        dest.write_bytes(source.read_bytes())
    resolved = dest.resolve()
    return {
        "id": f"ref_{index:02d}",
        "kind": kind,
        "role": str(item.get("role") or DEFAULT_ROLE).strip() or DEFAULT_ROLE,
        "filename": resolved.name,
        "mime_type": mime or _guess_mime(kind, resolved.suffix),
        "path": str(resolved),
        "uri": resolved.as_uri(),
        "size_bytes": resolved.stat().st_size,
        "order": index,
    }


def _existing_file(raw: str) -> Path | None:
    value = (raw or "").strip()
    if not value or value.startswith("blob:") or value.startswith("designer://"):
        return None
    if value.startswith("file:"):
        parsed = urlparse(value)
        path = unquote(parsed.path)
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        candidate = Path(path)
    else:
        candidate = Path(value)
    try:
        if candidate.is_file():
            return candidate
    except OSError:
        return None
    return None


def _decode_inline_bytes(item: dict[str, Any]) -> bytes | None:
    raw = item.get("base64_data") or item.get("base64Data") or item.get("data")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    match = _DATA_URI_RE.match(text)
    if match:
        text = match.group(2)
    try:
        payload = base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise UserReferenceError("reference payload is not valid base64") from exc
    if len(payload) > MAX_INLINE_BYTES:
        raise UserReferenceError(
            f"inline reference exceeds {MAX_INLINE_BYTES // (1024 * 1024)}MB; "
            "upload a local file path instead"
        )
    return payload or None


def _guess_mime(kind: str, suffix: str) -> str:
    lowered = (suffix or "").lower()
    for mime, mapped in _MIME_SUFFIX.items():
        if mapped == lowered:
            return mime
    return {
        KIND_IMAGE: "image/png",
        KIND_VIDEO: "video/mp4",
        KIND_AUDIO: "audio/mpeg",
    }.get(kind, "application/octet-stream")
