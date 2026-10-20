# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Enterprise upload ingress; identity is validated by the shared access guard."""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from minio.error import S3Error

from jiuwenswarm.common.obs_files import (
    AttachmentStore,
    MAX_FILE_BYTES,
    OBJECT_ASSET_KIND,
    object_prefix,
)
from jiuwenswarm.common.utils import build_tenant_key
from jiuwenswarm.server.runtime.attachments.upload_storage import safe_upload_filename


async def serve_object_download(
    request: Request, token: str, identity: dict
) -> Response:
    from jiuwenswarm.agents.harness.common.tools.web_file_download import (
        WebFileDownloadManager,
    )
    from jiuwenswarm.gateway.channel_manager.web.container_file_http import (
        _parse_single_byte_range,
    )

    payload = WebFileDownloadManager.get_instance().validate_token(
        token, session_id=request.query_params.get("session_id")
    )
    tenant = build_tenant_key(
        identity["group_id"], identity["bot_id"], identity["user_id"]
    )
    if (
        not payload
        or payload.get("kind") != OBJECT_ASSET_KIND
        or payload.get("tenant_key") != tenant
    ):
        return JSONResponse({"error": "invalid_download_token"}, status_code=403)
    try:
        store = AttachmentStore.from_env()
        if payload["bucket"] != store.bucket:
            return JSONResponse({"error": "invalid_download_bucket"}, status_code=403)
        info = await asyncio.to_thread(
            store.client.stat_object, store.bucket, payload["object_key"]
        )
        if (
            info.size != payload["size"]
            or info.etag != payload["etag"]
            or info.metadata.get("x-amz-meta-sha256") != payload["digest"]
        ):
            return JSONResponse({"error": "download_object_changed"}, status_code=403)
        size = payload["size"]
        range_header = request.headers.get("range", "").strip()
        byte_range = (
            _parse_single_byte_range(range_header, size) if range_header else None
        )
        if range_header and byte_range is None:
            return Response(
                status_code=416,
                headers={
                    "Content-Range": f"bytes */{size}",
                    "Content-Length": "0",
                    "Accept-Ranges": "bytes",
                    "Cache-Control": "no-store",
                },
            )
        start, end = byte_range or (0, max(size - 1, 0))
        length = end - start + 1 if size else 0
        name = payload["name"]
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        inline = request.query_params.get("inline", "").lower() in {"true", "1"}
        safe_inline = (
            mime.startswith(("image/", "audio/", "video/")) and mime != "image/svg+xml"
        )
        disposition = "inline" if inline and safe_inline else "attachment"
        headers = {
            "Content-Length": str(length),
            "Accept-Ranges": "bytes",
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name, safe='')}",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        }
        status = 206 if byte_range else 200
        if byte_range:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        if request.method == "HEAD" or not size:
            return Response(status_code=status, headers=headers, media_type=mime)
        upstream = await asyncio.to_thread(
            store.client.get_object,
            store.bucket,
            payload["object_key"],
            offset=start,
            length=length,
            request_headers={"If-Match": f'"{info.etag}"'},
        )
    except KeyError:
        return JSONResponse({"error": "invalid_download_token"}, status_code=403)
    except S3Error as exc:
        status = 404 if exc.code in {"NoSuchKey", "NoSuchObject"} else 503
        return JSONResponse(
            {"error": "download_object_unavailable"}, status_code=status
        )
    except (OSError, ValueError, RuntimeError):
        return JSONResponse({"error": "object_store_unavailable"}, status_code=503)

    async def stream():
        remaining = length
        try:
            while remaining:
                chunk = await asyncio.to_thread(upstream.read, min(65536, remaining))
                if not chunk:
                    raise RuntimeError("download object truncated")
                remaining -= len(chunk)
                yield chunk
        finally:
            try:
                upstream.close()
            finally:
                upstream.release_conn()

    return StreamingResponse(
        stream(), status_code=status, headers=headers, media_type=mime
    )


def register_enterprise_file_routes(app: FastAPI, channel) -> None:
    @app.post("/file-api/upload-obs")
    async def upload(request: Request) -> JSONResponse:
        identity = getattr(request.state, "enterprise_identity", None)
        if not identity:
            return JSONResponse(
                {"ok": False, "error": "identity_not_wired"}, status_code=503
            )
        # Bound the body before JSON/base64 decoding, including chunked requests.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > (MAX_FILE_BYTES * 4 // 3) + 8192:
                return JSONResponse(
                    {"ok": False, "error": "file_too_large"}, status_code=413
                )
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("JSON object required")
            filename = safe_upload_filename(
                str(payload.get("filename") or ""), fallback="upload"
            )
            content = base64.b64decode(
                payload.get("content_base64") or "", validate=True
            )
            prefix = object_prefix(
                identity["group_id"],
                identity["bot_id"],
                identity["user_id"],
            )
            if not content or len(content) > MAX_FILE_BYTES:
                raise ValueError("file must contain 1..50 MiB")
        except (ValueError, TypeError):
            return JSONResponse(
                {"ok": False, "error": "invalid_upload"}, status_code=400
            )
        try:
            store = AttachmentStore.from_env()
            result = await asyncio.to_thread(store.upload, content, filename, prefix)
        except Exception:
            # Never return object-store credentials, signed URLs or upstream error details.
            return JSONResponse(
                {"ok": False, "error": "object_store_unavailable"}, status_code=503
            )
        return JSONResponse({"ok": True, **result})

    @app.delete("/file-api/upload-obs")
    async def remove_upload(request: Request) -> JSONResponse:
        identity = getattr(request.state, "enterprise_identity", None)
        if not identity:
            return JSONResponse(
                {"ok": False, "error": "identity_not_wired"}, status_code=503
            )
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                return JSONResponse(
                    {"ok": False, "error": "invalid_upload"}, status_code=400
                )
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict) or not isinstance(payload.get("url"), str):
                raise ValueError("attachment URL required")
            prefix = object_prefix(
                identity["group_id"], identity["bot_id"], identity["user_id"]
            )
            store = AttachmentStore.from_env()
            await asyncio.to_thread(store.remove_upload, payload["url"], prefix)
        except (ValueError, TypeError):
            return JSONResponse(
                {"ok": False, "error": "invalid_upload"}, status_code=400
            )
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "object_store_unavailable"}, status_code=503
            )
        return JSONResponse({"ok": True})

    @app.api_route("/file-api/download", methods=["GET", "HEAD"])
    async def download(request: Request) -> Response:
        from jiuwenswarm.gateway.channel_manager.web.container_file_http import (
            _decode_download_token_payload,
            serve_verified_download,
        )

        identity = getattr(request.state, "enterprise_identity", None)
        if not identity:
            return JSONResponse({"error": "identity_not_wired"}, status_code=503)
        token = request.query_params.get("token", "")
        payload = _decode_download_token_payload(token)
        if payload and payload.get("kind") == OBJECT_ASSET_KIND:
            return await serve_object_download(request, token, identity)
        if not payload or payload.get("kind") != "verified_asset_v1":
            return JSONResponse({"error": "invalid_download_token"}, status_code=403)
        return await serve_verified_download(
            request, channel, identity["user_id"], token, payload
        )
