# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for task artifacts over the registry's artifact relay.

「文件经 registry 中转」的两个方向各自钉死：

* 上传（交接方）：``upload_artifact_file`` 按协议 create → 分块 PUT，携带
  部署级 create token 与逐块 Content-Range；失败返回 None 而不是抛错。
* 下载（接手方）：``materialize_task_artifacts`` 按签名 URL 拉回文件、校验
  sha256、落进任务会话工作区；已存在且哈希一致则不重复下载。

全部流量走 ``httpx.MockTransport``——没有网络，也没有真 relay。
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from jiuwenswarm.agents.harness.team.tasks.artifacts import (
    archive_summary,
    collect_artifact_refs,
    extract_file_paths,
    materialize_task_artifacts,
    upload_artifact_file,
    upload_session_artifacts,
)
from jiuwenswarm.agents.harness.team.tasks.config import TasksConfig


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _config() -> TasksConfig:
    return TasksConfig(
        enabled=True,
        base_url="http://relay.test",
        api_key="test-key",
        timeout=5.0,
        artifact_base_url="http://relay.test",
        artifact_create_token="k" * 40,
        artifact_max_bytes=1024 * 1024,
        artifact_ttl_seconds=86_400,
    )


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_extract_file_paths_covers_both_file_tools() -> None:
    # 历史 arguments 是 JSON 文本：反斜杠在 JSON 里转义为 \\。
    assert extract_file_paths(
        "write_file", '{"file_path": "C:\\\\p\\\\a.html", "content": "…"}'
    ) == ["C:\\p\\a.html"]
    # abs_file_path_list 的三种形态：单串 / JSON 数组串 / 原生数组（内部再嵌
    # JSON 数组串也要展开）。
    assert extract_file_paths("send_file_to_user", '{"abs_file_path_list": "/tmp/a"}') == [
        "/tmp/a"
    ]
    assert extract_file_paths(
        "send_file_to_user", '{"abs_file_path_list": "[\\"/tmp/a\\", \\"/tmp/b\\"]"}'
    ) == ["/tmp/a", "/tmp/b"]
    # 非文件工具 / 坏 JSON / 空值：空列表，绝不抛错。
    assert extract_file_paths("search_tools", '{"query": "x"}') == []
    assert extract_file_paths("write_file", "not-json") == []
    assert extract_file_paths("write_file", "") == []
    assert extract_file_paths("", '{"file_path": "x"}') == []


def test_collect_artifact_refs_dedupes_by_url() -> None:
    history = [
        {
            "seq": 1,
            "payload": {
                "artifacts": [
                    {"name": "a.html", "size": 10, "download_url": "http://r/uri/1/t"},
                    # 同一条里重复 / 缺 URL / 非对象：忽略。
                    {"name": "a.html", "size": 10, "download_url": "http://r/uri/1/t"},
                    {"name": "bad"},
                    "junk",
                ]
            },
        },
        {
            "seq": 2,
            "payload": {
                "artifacts": [{"name": "b.png", "size": 20, "download_url": "http://r/uri/2/t"}]
            },
        },
        {"seq": 3, "payload": "plain text"},
        {"seq": 4},
    ]
    refs = collect_artifact_refs(history)
    assert [ref["name"] for ref in refs] == ["a.html", "b.png"]
    assert refs[0]["size"] == 10
    assert refs[1]["download_url"] == "http://r/uri/2/t"


def test_archive_summary_lists_name_size_and_url() -> None:
    text = archive_summary(
        [
            {
                "name": "gomoku.html",
                "size": 23753,
                "download_url": "http://r/uri/1/t",
            }
        ]
    )
    assert text.startswith("📎 已存档文件")
    assert "gomoku.html（23.2 KB）" in text
    assert "http://r/uri/1/t" in text
    assert archive_summary([]) == ""


# ── upload ───────────────────────────────────────────────────────────────────


def _make_relay_transport(
    *,
    body: bytes,
    chunk_bytes: int = 4,
    create_status: int = 201,
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """A tiny fake artifact relay: one create + N sequential chunk PUTs."""
    seen: list[httpx.Request] = []
    sha = hashlib.sha256(body).hexdigest()
    received = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal received
        seen.append(request)
        if request.url.path == "/api/artifact-relay/transfers" and request.method == "POST":
            if create_status >= 400:
                return httpx.Response(create_status, json={"error": {"code": "x"}})
            return httpx.Response(
                create_status,
                json={
                    "transferId": "f" * 32,
                    "state": "uploading",
                    "chunkBytes": chunk_bytes,
                    "expiresAt": "2026-10-14T00:00:00Z",
                    "upload": {
                        "url": "http://relay.test/api/artifact-relay/transfers/" + "f" * 32,
                        "token": "upload-token",
                        "method": "PUT",
                    },
                    "download": {
                        "url": "http://relay.test/api/artifact-relay/transfers/" + "f" * 32,
                        "token": "download-token",
                        "signedUrl": "http://relay.test/api/artifact-relay/uri/" + "f" * 32 + "/uri-token",
                    },
                },
            )
        if request.url.path.endswith("/transfers/" + "f" * 32) and request.method == "PUT":
            received += len(request.content)
            done = received == len(body)
            return httpx.Response(
                200,
                json={"transferId": "f" * 32, "state": "ready" if done else "uploading", "sha256": sha},
            )
        return httpx.Response(404, json={"error": {"code": "nope"}})

    return httpx.MockTransport(handler), seen


@pytest.mark.anyio
async def test_upload_artifact_file_chunks_and_returns_ref(tmp_path) -> None:  # noqa: ANN001
    body = ("五子棋" * 3).encode("utf-8")  # 27 bytes → 4-byte chunks → 7 块
    source = tmp_path / "gomoku.html"
    source.write_bytes(body)
    transport, seen = _make_relay_transport(body=body)

    async with httpx.AsyncClient(transport=transport) as client:
        ref = await upload_artifact_file(str(source), config=_config(), client=client)

    assert ref is not None
    assert ref["name"] == "gomoku.html"
    assert ref["size"] == len(body)
    assert ref["sha256"] == hashlib.sha256(body).hexdigest()
    assert ref["media_type"] == "text/html"
    assert ref["download_url"].endswith("/uri/" + "f" * 32 + "/uri-token")

    create = seen[0]
    assert create.method == "POST"
    assert create.headers["X-Artifact-Relay-Key"] == "k" * 40
    sent = eval_create_body(create)
    assert sent["filename"] == "gomoku.html"
    assert sent["byteLength"] == len(body)
    assert sent["sha256"] == ref["sha256"]
    assert sent["ttlSeconds"] == 86_400

    puts = [r for r in seen if r.method == "PUT"]
    assert len(puts) == 7
    ranges = [r.headers["Content-Range"] for r in puts]
    assert ranges[0] == f"bytes 0-3/{len(body)}"
    assert ranges[-1] == f"bytes 24-26/{len(body)}"
    assert all(r.headers["Authorization"] == "Bearer upload-token" for r in puts)
    # 分块拼回去等于原文件。
    assert b"".join(r.content for r in puts) == body


def eval_create_body(request: httpx.Request) -> dict:
    return json.loads(request.content.decode("utf-8"))


@pytest.mark.anyio
async def test_upload_artifact_file_returns_none_on_relay_rejection(tmp_path) -> None:  # noqa: ANN001
    source = tmp_path / "a.txt"
    source.write_bytes(b"hello")
    transport, _ = _make_relay_transport(body=b"hello", create_status=404)

    async with httpx.AsyncClient(transport=transport) as client:
        assert await upload_artifact_file(str(source), config=_config(), client=client) is None


@pytest.mark.anyio
async def test_upload_artifact_file_skips_missing_and_oversized(tmp_path) -> None:  # noqa: ANN001
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await upload_artifact_file(tmp_path / "missing.txt", config=_config(), client=client) is None
        big = tmp_path / "big.bin"
        big.write_bytes(b"x" * 11)
        small_config = TasksConfig(
            enabled=True,
            base_url="http://relay.test",
            api_key=None,
            timeout=5.0,
            artifact_base_url="http://relay.test",
            artifact_create_token="k" * 40,
            artifact_max_bytes=10,
        )
        assert await upload_artifact_file(str(big), config=small_config, client=client) is None
    assert calls == [], "missing/oversized files must not touch the relay"


@pytest.mark.anyio
async def test_upload_session_artifacts_requires_configuration(tmp_path) -> None:  # noqa: ANN001
    source = tmp_path / "a.txt"
    source.write_text("hello", encoding="utf-8")
    unconfigured = TasksConfig(
        enabled=True,
        base_url="http://relay.test",
        api_key=None,
        timeout=5.0,
        artifact_base_url="http://relay.test",
        artifact_create_token=None,
    )
    assert await upload_session_artifacts([str(source)], config=unconfigured) == {}
    assert await upload_session_artifacts([], config=_config()) == {}


# ── download（接手方物化）────────────────────────────────────────────────────


def _artifact_history(body: bytes, name: str = "gomoku.html") -> list[dict]:
    return [
        {
            "seq": 3,
            "kind": "tool_result",
            "payload": {
                "tool_name": "write_file",
                "text": "[工具 write_file] success",
                "artifacts": [
                    {
                        "name": name,
                        "size": len(body),
                        "media_type": "text/html",
                        "sha256": hashlib.sha256(body).hexdigest(),
                        "download_url": f"http://relay.test/api/artifact-relay/uri/{'f'*32}/t",
                    }
                ],
            },
        }
    ]


@pytest.mark.anyio
async def test_materialize_task_artifacts_downloads_and_verifies(tmp_path) -> None:  # noqa: ANN001
    body = "<html>gomoku</html>".encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/artifact-relay/uri/" + "f" * 32 + "/t"
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materialized = await materialize_task_artifacts(
            _artifact_history(body),
            "task_7",
            config=_config(),
            client=client,
            target_dir=tmp_path,
        )

    assert materialized == {"gomoku.html": str(tmp_path / "gomoku.html")}
    assert (tmp_path / "gomoku.html").read_bytes() == body


@pytest.mark.anyio
async def test_materialize_task_artifacts_reuses_matching_existing_file(tmp_path) -> None:  # noqa: ANN001
    body = b"same-bytes"
    (tmp_path / "gomoku.html").write_bytes(body)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("existing matching file must not be re-downloaded")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materialized = await materialize_task_artifacts(
            _artifact_history(body),
            "task_7",
            config=_config(),
            client=client,
            target_dir=tmp_path,
        )
    assert materialized == {"gomoku.html": str(tmp_path / "gomoku.html")}


@pytest.mark.anyio
async def test_materialize_task_artifacts_rejects_hash_mismatch(tmp_path) -> None:  # noqa: ANN001
    body = b"actual-bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    bad_ref_history = _artifact_history(b"declared-but-different")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materialized = await materialize_task_artifacts(
            bad_ref_history,
            "task_7",
            config=_config(),
            client=client,
            target_dir=tmp_path,
        )
    assert materialized == {}
    assert not (tmp_path / "gomoku.html").exists(), "a mismatched file must not stay on disk"


@pytest.mark.anyio
async def test_materialize_task_artifacts_survives_download_failure(tmp_path) -> None:  # noqa: ANN001
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410, json={"error": {"code": "artifact_expired"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        materialized = await materialize_task_artifacts(
            _artifact_history(b"x"),
            "task_7",
            config=_config(),
            client=client,
            target_dir=tmp_path,
        )
    assert materialized == {}
