# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Task artifacts over the registry's artifact relay.

「交接为跨设备任务」导出的是*可见性*（截断后的工具记录）；本模块补上*字节*：
交接时把会话产出的文件（write_file 写出的、send_file_to_user 投递的）经
relay 的 artifact-relay 上传，任务日志里附上签名下载链接；接手设备的
bridge 再把引用的文件物化回本机任务工作区。全程只有「设备 → relay → 设备」
一条路——不存在任何设备到设备的直传，同一台机器上的联调也只是对真实跨设备
拓扑的模拟。

artifact-relay 协议（见 relay 侧 ``a2x_registry/artifact_relay``）：

* ``POST /api/artifact-relay/transfers``，header ``X-Artifact-Relay-Key`` 是
  部署级静态 create token；请求体 ``{filename, byteLength, sha256, mediaType,
  ttlSeconds}``，响应含 ``chunkBytes``、``upload.token``、
  ``download.signedUrl``。
* ``PUT /api/artifact-relay/transfers/{id}``，Bearer upload token +
  ``Content-Range: bytes START-END/TOTAL`` 顺序分块；最后一块落盘并校验
  sha256 后 state=ready。
* ``GET /api/artifact-relay/uri/{id}/{uri_token}`` 是免鉴权的签名下载 URL，
  面板可以直接 <a href>，接手方也可以直接 GET。

错误姿态：上传/下载都是任务的*增强*而不是前提——单个文件失败记日志后跳过，
绝不让交接或 agent 回合因此失败。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any, Iterable, Mapping

import httpx

from .config import TasksConfig, resolve_tasks_config

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_BYTES = 1024 * 1024
"""create 响应缺 ``chunkBytes`` 时的兜底（relay 默认 1MB）。"""

FILE_TOOLS: dict[str, tuple[str, ...]] = {
    # 工具名 → 参数里指明文件路径的字段。write_file 的路径在参数里；
    # send_file_to_user 的 abs_file_path_list 兼容单路径字符串、JSON 数组
    # 字符串与原生数组三种形态（见该工具的参数说明）。
    "write_file": ("file_path",),
    "send_file_to_user": ("abs_file_path_list",),
}

_HASH_CHUNK = 1024 * 1024


# ── pure helpers (unit-tested without IO) ─────────────────────────────────────


def extract_file_paths(tool_name: str, arguments: Any) -> list[str]:
    """Pull local file paths out of one tool call's arguments.

    ``arguments`` 是 runner 落进 history 的 JSON 字符串；解析失败（空、非
    JSON、非对象）时返回空列表而不是抛错——历史里完全可能有别的形态。
    """
    name = str(tool_name or "").strip()
    fields = FILE_TOOLS.get(name)
    if not fields or not isinstance(arguments, str) or not arguments.strip():
        return []
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return []
    if not isinstance(parsed, Mapping):
        return []

    paths: list[str] = []
    for field in fields:
        value = parsed.get(field)
        for item in _flatten_paths(value):
            if item not in paths:
                paths.append(item)
    return paths


def _flatten_paths(value: Any) -> list[str]:
    """Normalize one argument value into absolute-path strings."""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return _flatten_paths(parsed)
        if isinstance(parsed, str):
            return _flatten_paths(parsed)
        return [stripped]
    if isinstance(value, (list, tuple)):
        flattened: list[str] = []
        for item in value:
            for path in _flatten_paths(item):
                if path not in flattened:
                    flattened.append(path)
        return flattened
    return []


def collect_artifact_refs(messages: Iterable[Any]) -> list[dict[str, Any]]:
    """Gather artifact references from a relay task log.

    Handover attaches ``payload.artifacts`` to ``tool_result`` entries; this
    walks any payload shape (defensive against foreign writers) and dedupes by
    ``download_url`` so a file referenced by several entries downloads once.
    """
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        payload = message.get("payload")
        if not isinstance(payload, Mapping):
            continue
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list):
            continue
        for ref in artifacts:
            if not isinstance(ref, Mapping):
                continue
            url = str(ref.get("download_url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            refs.append(
                {
                    "name": str(ref.get("name") or "artifact.bin"),
                    "size": _non_negative_int(ref.get("size")),
                    "media_type": str(ref.get("media_type") or ""),
                    "sha256": str(ref.get("sha256") or ""),
                    "download_url": url,
                }
            )
    return refs


def _non_negative_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def format_size(size: int) -> str:
    """Human-readable byte size for the log/payload summaries."""
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def archive_summary(refs: list[dict[str, Any]]) -> str:
    """One-line ``📎`` summary appended to a tool entry's ``payload.text``.

    Old panels/bridges that only read ``text`` still learn that the file exists
    and where to fetch it; the signed URL is the whole capability.
    """
    parts = []
    for ref in refs:
        size = format_size(ref.get("size") or 0)
        name = ref.get("name") or "artifact"
        parts.append(f"{name}（{size}）：{ref.get('download_url')}")
    if not parts:
        return ""
    return "📎 已存档文件（经 registry 中转，可直接下载）：" + "；".join(parts)


# ── upload (handover side) ────────────────────────────────────────────────────


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_ref(
    *,
    name: str,
    size: int,
    media_type: str,
    sha256: str,
    created: Mapping[str, Any],
) -> dict[str, Any]:
    download = created.get("download")
    signed_url = ""
    if isinstance(download, Mapping):
        signed_url = str(download.get("signedUrl") or "").strip()
    return {
        "name": name,
        "size": size,
        "media_type": media_type,
        "sha256": sha256,
        "transfer_id": str(created.get("transferId") or ""),
        "download_url": signed_url,
        "expires_at": str(created.get("expiresAt") or ""),
    }


async def upload_artifact_file(
    path: str | Path,
    *,
    config: TasksConfig,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Upload one local file through the artifact relay.

    Returns the reference dict (see ``_artifact_ref``) or ``None`` when the
    file is missing/too large or the relay rejects any step — callers treat
    ``None`` as "skip this file", never as a handover failure.
    """
    source = Path(path)
    try:
        if not source.is_file():
            logger.debug("[TaskArtifacts] not a file, skipping: %s", source)
            return None
        size = source.stat().st_size
    except OSError as exc:
        logger.warning("[TaskArtifacts] stat failed: %s: %s", source, exc)
        return None
    if size <= 0:
        logger.debug("[TaskArtifacts] empty file, skipping: %s", source)
        return None
    if size > config.artifact_max_bytes:
        logger.warning(
            "[TaskArtifacts] file exceeds artifact_max_bytes (%s > %s), skipping: %s",
            size,
            config.artifact_max_bytes,
            source,
        )
        return None

    sha256 = await asyncio.to_thread(_hash_file, source)
    media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"

    owned_client = client is None
    http = client or httpx.AsyncClient(timeout=config.timeout)
    try:
        try:
            created = await _create_transfer(
                http,
                config,
                filename=source.name,
                byte_length=size,
                sha256=sha256,
                media_type=media_type,
            )
        except (_RelayRejected, httpx.HTTPError) as exc:
            logger.warning(
                "[TaskArtifacts] create transfer failed: %s: %s", source, exc
            )
            return None

        ref = _artifact_ref(
            name=source.name,
            size=size,
            media_type=media_type,
            sha256=sha256,
            created=created,
        )
        if not ref["download_url"]:
            logger.warning(
                "[TaskArtifacts] relay returned no signed URL: %s", source
            )
            return None

        try:
            await _upload_chunks(http, created, source, size)
        except (_RelayRejected, httpx.HTTPError, OSError) as exc:
            logger.warning("[TaskArtifacts] upload failed: %s: %s", source, exc)
            return None
        return ref
    finally:
        if owned_client:
            await http.aclose()


class _RelayRejected(Exception):
    """The artifact relay answered a non-2xx or an unusable body."""


async def _create_transfer(
    http: httpx.AsyncClient,
    config: TasksConfig,
    *,
    filename: str,
    byte_length: int,
    sha256: str,
    media_type: str,
) -> dict[str, Any]:
    base = config.artifact_base_url.rstrip("/")
    resp = await http.post(
        f"{base}/api/artifact-relay/transfers",
        headers={"X-Artifact-Relay-Key": config.artifact_create_token or ""},
        json={
            "filename": filename,
            "byteLength": byte_length,
            "sha256": sha256,
            "mediaType": media_type,
            "ttlSeconds": config.artifact_ttl_seconds,
        },
    )
    if resp.status_code >= 400:
        raise _RelayRejected(f"HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise _RelayRejected("non-JSON response") from exc
    if not isinstance(body, dict):
        raise _RelayRejected("unexpected response shape")
    return body


async def _upload_chunks(
    http: httpx.AsyncClient,
    created: Mapping[str, Any],
    source: Path,
    size: int,
) -> None:
    upload = created.get("upload")
    if not isinstance(upload, Mapping):
        raise _RelayRejected("create response lacks upload target")
    url = str(upload.get("url") or "")
    token = str(upload.get("token") or "")
    if not url or not token:
        raise _RelayRejected("create response lacks upload url/token")
    try:
        chunk_bytes = int(created.get("chunkBytes") or DEFAULT_CHUNK_BYTES)
    except (TypeError, ValueError):
        chunk_bytes = DEFAULT_CHUNK_BYTES
    chunk_bytes = max(1, chunk_bytes)

    state = ""
    with source.open("rb") as handle:
        offset = 0
        while offset < size:
            chunk = handle.read(min(chunk_bytes, size - offset))
            if not chunk:
                raise _RelayRejected(f"file shrank mid-upload: {source}")
            end = offset + len(chunk) - 1
            resp = await http.put(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Range": f"bytes {offset}-{end}/{size}",
                    "Content-Length": str(len(chunk)),
                },
                content=chunk,
            )
            if resp.status_code >= 400:
                raise _RelayRejected(
                    f"chunk {offset}-{end} HTTP {resp.status_code}: {resp.text[:200]}"
                )
            try:
                body = resp.json()
                if isinstance(body, dict):
                    state = str(body.get("state") or state)
            except ValueError:
                pass
            offset = end + 1
    if state and state != "ready":
        # The relay streams metadata back on every chunk; anything but "ready"
        # after the final one means the upload never completed.
        raise _RelayRejected(f"transfer state is {state!r}, not ready")


async def upload_session_artifacts(
    file_paths: Iterable[str],
    *,
    config_base: Mapping[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
    config: TasksConfig | None = None,
) -> dict[str, dict[str, Any]]:
    """Upload every uploadable path once; returns ``{path: artifact_ref}``.

    Missing files and relay rejections drop out of the map (logged); the map
    only ever contains files that are fully stored on the relay.
    """
    resolved = config or resolve_tasks_config(config_base)
    if not resolved.artifacts_configured:
        return {}

    unique: list[str] = []
    for raw in file_paths:
        path = str(raw or "").strip()
        if path and path not in unique:
            unique.append(path)

    refs: dict[str, dict[str, Any]] = {}
    if not unique:
        return refs
    owned_client = client is None
    http = client or httpx.AsyncClient(timeout=resolved.timeout)
    try:
        for path in unique:
            ref = await upload_artifact_file(path, config=resolved, client=http)
            if ref is not None:
                refs[path] = ref
    finally:
        if owned_client:
            await http.aclose()
    if refs:
        logger.info(
            "[TaskArtifacts] uploaded %d/%d session artifact(s)",
            len(refs),
            len(unique),
        )
    return refs


# ── download (taker side) ─────────────────────────────────────────────────────


def _safe_leaf_name(name: str) -> str:
    leaf = Path(str(name).replace("\\", "/")).name
    leaf = leaf.replace("\r", "").replace("\n", "").strip()
    return leaf[:255] or "artifact.bin"


def _sha256_matches(path: Path, expected: str) -> bool:
    if not expected:
        return False
    try:
        return _hash_file(path) == expected.lower()
    except OSError:
        return False


async def materialize_task_artifacts(
    history: Iterable[Any],
    session_id: str,
    *,
    config_base: Mapping[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
    target_dir: str | Path | None = None,
    config: TasksConfig | None = None,
) -> dict[str, str]:
    """Download referenced artifacts into the task session's workspace.

    Returns ``{filename: local_path}`` for everything now on this machine.
    Best-effort by design: each file is fetched independently and a failure
    only removes that file from the map — a turn must never die because the
    artifact relay is down. Files already present with a matching sha256 are
    reused as-is, so repeated turns don't re-download.
    """
    refs = collect_artifact_refs(history)
    if not refs:
        return {}

    if target_dir is None:
        try:
            from jiuwenswarm.common.utils import (
                get_default_project_session_workspace_dir,
            )

            target_dir = get_default_project_session_workspace_dir(session_id)
        except Exception as exc:  # noqa: BLE001 - bridge must stay importable
            logger.warning(
                "[TaskArtifacts] cannot resolve task workspace: %s", exc
            )
            return {}
    root = Path(target_dir)

    resolved = config or resolve_tasks_config(config_base)
    owned_client = client is None and resolved.artifacts_configured
    http = client or httpx.AsyncClient(timeout=resolved.timeout)
    try:
        materialized: dict[str, str] = {}
        for ref in refs:
            name = _safe_leaf_name(ref.get("name") or "")
            destination = root / name
            try:
                if destination.is_file() and (
                    not ref.get("sha256")
                    or await asyncio.to_thread(
                        _sha256_matches, destination, ref["sha256"]
                    )
                ):
                    materialized[name] = str(destination)
                    continue
                root.mkdir(parents=True, exist_ok=True)
                body = await _fetch_artifact(http, ref["download_url"])
                destination.write_bytes(body)
                expected = str(ref.get("sha256") or "").lower()
                if expected:
                    actual = hashlib.sha256(body).hexdigest()
                    if actual != expected:
                        destination.unlink(missing_ok=True)
                        logger.warning(
                            "[TaskArtifacts] sha256 mismatch for %s: expected %s got %s",
                            name,
                            expected,
                            actual,
                        )
                        continue
                materialized[name] = str(destination)
                logger.info(
                    "[TaskArtifacts] materialized %s -> %s",
                    ref.get("download_url"),
                    destination,
                )
            except (httpx.HTTPError, OSError, _RelayRejected) as exc:
                logger.warning(
                    "[TaskArtifacts] download failed (%s): %s", name, exc
                )
    finally:
        if owned_client:
            await http.aclose()
    return materialized


async def _fetch_artifact(http: httpx.AsyncClient, url: str) -> bytes:
    """GET one signed URL. Non-2xx is a download failure, not a crash."""
    resp = await http.get(url)
    if resp.status_code >= 400:
        raise _RelayRejected(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.content
