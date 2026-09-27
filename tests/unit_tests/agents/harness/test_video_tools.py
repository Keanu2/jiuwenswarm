# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from jiuwenswarm.agents.harness.common.tools import video_tools


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {
                "provider": "DashScope",
                "endpoint_profile": "",
                "vendor_key": "",
                "api_base": "",
                "model": "wan3.0-video",
            },
            "dashscope",
        ),
        (
            {
                "provider": "OpenAI",
                "endpoint_profile": "dashscope",
                "vendor_key": "alibaba",
                "api_base": "",
                "model": "wan3.0-video",
            },
            "dashscope",
        ),
        (
            {
                "provider": "OpenAI",
                "endpoint_profile": "minimax",
                "vendor_key": "minimax",
                "api_base": "",
                "model": "MiniMax-H3",
            },
            "minimax",
        ),
        (
            {
                "provider": "MiniMax",
                "endpoint_profile": "",
                "vendor_key": "",
                "api_base": "",
                "model": "x",
            },
            "minimax",
        ),
        (
            {
                "provider": "OpenAI",
                "endpoint_profile": "",
                "vendor_key": "",
                "api_base": "",
                "model": "MiniMax-H3-Max",
            },
            "minimax",
        ),
        (
            {
                "provider": "OpenAI",
                "endpoint_profile": "volcengine",
                "vendor_key": "volcengine",
                "api_base": "",
                "model": "x",
            },
            "volcengine",
        ),
        (
            {
                "provider": "VolcEngine",
                "endpoint_profile": "",
                "vendor_key": "",
                "api_base": "",
                "model": "x",
            },
            "volcengine",
        ),
        (
            {
                "provider": "OpenAI",
                "endpoint_profile": "vllm-omni",
                "vendor_key": "vllm-omni",
                "api_base": "http://127.0.0.1:8091/v1",
                "model": "",
            },
            "vllm-omni",
        ),
        (
            {
                "provider": "OpenAI",
                "endpoint_profile": "",
                "vendor_key": "",
                "api_base": "https://ark.cn-beijing.volces.com/api/v3",
                "model": "doubao-seedance-2-5-260628",
            },
            "volcengine",
        ),
    ],
)
def test_resolve_video_gen_backend(kwargs: dict, expected: str) -> None:
    assert video_tools._resolve_video_gen_backend(**kwargs) == expected


def test_size_to_ratio_and_resolution_helpers() -> None:
    assert video_tools._size_to_ratio("1280*720") == "16:9"
    assert video_tools._size_to_ratio("720x1280") == "9:16"
    assert video_tools._normalize_minimax_resolution(None, "1280*720") == "768P"
    assert video_tools._normalize_minimax_resolution("2K", "1280*720") == "2K"
    assert video_tools._normalize_ark_resolution(None, "1280*720") == "720p"
    assert video_tools._normalize_ark_resolution("1080P", None) == "1080p"


def test_minimax_and_ark_api_root_normalization() -> None:
    assert (
        video_tools._minimax_api_root("https://api.minimaxi.com/v1")
        == "https://api.minimaxi.com"
    )
    assert (
        video_tools._minimax_api_root("https://api.minimaxi.com")
        == "https://api.minimaxi.com"
    )
    assert (
        video_tools._ark_api_root("https://ark.cn-beijing.volces.com/api/coding/v3")
        == "https://ark.cn-beijing.volces.com/api/v3"
    )
    assert (
        video_tools._ark_api_root("https://ark.cn-beijing.volces.com/api/v3")
        == "https://ark.cn-beijing.volces.com/api/v3"
    )


def test_invoke_minimax_video_generation_create_and_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls: list[tuple[str, str]] = []

    class _Resp:
        def __init__(self, ok: bool, payload: dict, status_code: int = 200):
            self.ok = ok
            self.status_code = status_code
            self._payload = payload
            self.content = b"fake-mp4"
            self.text = str(payload)

        def json(self):
            return self._payload

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError(f"http {self.status_code}")

    def fake_request(method: str, url: str, **kwargs):
        del kwargs
        calls.append((method, url))
        if method == "POST" and url.endswith("/v2/video_generation"):
            return _Resp(True, {"task_id": "tid-1"})
        if method == "GET" and "/v2/query/video_generation/" in url:
            return _Resp(
                True,
                {
                    "task": {
                        "id": "tid-1",
                        "status": "succeeded",
                        "content": {"url": "https://cdn.example/out.mp4"},
                    }
                },
            )
        if method == "GET" and url.endswith(".mp4"):
            return _Resp(True, {})
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(video_tools, "_http_request", fake_request)
    monkeypatch.setattr(video_tools, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(video_tools.time, "sleep", lambda *_: None)

    result = video_tools._invoke_minimax_video_generation_sync(
        "a cat runs",
        api_key="k",
        api_base="https://api.minimaxi.com/v1",
        model="MiniMax-H3",
        size="1280*720",
        duration=5,
        resolution=None,
    )
    assert "video_path" in result
    assert result["original_url"] == "https://cdn.example/out.mp4"
    assert any(m == "POST" and u.endswith("/v2/video_generation") for m, u in calls)


def test_invoke_volcengine_video_generation_create_and_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    posts: list[dict] = []

    class _Resp:
        def __init__(
            self, ok: bool, payload: dict, status_code: int = 200, content: bytes = b""
        ):
            self.ok = ok
            self.status_code = status_code
            self._payload = payload
            self.content = content or b"fake-mp4"
            self.text = str(payload)

        def json(self):
            return self._payload

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError(f"http {self.status_code}")

    def fake_request(method: str, url: str, **kwargs):
        if method == "POST":
            posts.append(kwargs.get("json") or {})
            return _Resp(True, {"id": "cgt-1"})
        if method == "GET" and url.endswith("/contents/generations/tasks/cgt-1"):
            return _Resp(
                True,
                {
                    "id": "cgt-1",
                    "status": "succeeded",
                    "content": {"video_url": "https://ark.example/out.mp4"},
                },
            )
        if method == "GET" and url.endswith(".mp4"):
            return _Resp(True, {}, content=b"bytes")
        raise AssertionError(f"unexpected request {method} {url}")

    monkeypatch.setattr(video_tools, "_http_request", fake_request)
    monkeypatch.setattr(video_tools, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(video_tools.time, "sleep", lambda *_: None)

    result = video_tools._invoke_volcengine_video_generation_sync(
        "a dog runs",
        api_key="k",
        api_base="https://ark.cn-beijing.volces.com/api/coding/v3",
        model="doubao-seedance-2-5-260628",
        size="1280*720",
        duration=5,
        resolution=None,
    )
    assert "video_path" in result
    assert posts[0]["model"] == "doubao-seedance-2-5-260628"
    assert posts[0]["ratio"] == "16:9"
    assert posts[0]["resolution"] == "720p"


@pytest.mark.asyncio
async def test_invoke_model_video_generation_routes_to_minimax(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {}

    def fake_sync(*args, **kwargs):
        called["args"] = args
        called["kwargs"] = kwargs
        return {"video_path": "/tmp/x.mp4", "revised_prompt": args[0]}

    monkeypatch.setattr(video_tools, "get_config", lambda: {})
    monkeypatch.setattr(
        video_tools,
        "_get_model_config",
        lambda *_: {
            "api_key": "k",
            "api_base": "https://api.minimaxi.com",
            "model_name": "MiniMax-H3",
            "client_provider": "OpenAI",
            "endpoint_profile": "minimax",
            "vendor_key": "minimax",
        },
    )
    monkeypatch.setattr(video_tools, "_invoke_minimax_video_generation_sync", fake_sync)

    result = await video_tools._invoke_model_video_generation(
        "hello", size="1280*720", duration=5
    )
    assert result["video_path"] == "/tmp/x.mp4"
    assert called["args"][0] == "hello"


@pytest.mark.asyncio
async def test_invoke_model_video_generation_routes_to_volcengine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = SimpleNamespace(hit=False)

    def fake_sync(*args, **kwargs):
        del args, kwargs
        called.hit = True
        return {"video_path": "/tmp/y.mp4", "revised_prompt": "p"}

    monkeypatch.setattr(video_tools, "get_config", lambda: {})
    monkeypatch.setattr(
        video_tools,
        "_get_model_config",
        lambda *_: {
            "api_key": "k",
            "api_base": "https://ark.cn-beijing.volces.com/api/v3",
            "model_name": "doubao-seedance-2-5-260628",
            "client_provider": "OpenAI",
            "endpoint_profile": "volcengine",
            "vendor_key": "volcengine",
        },
    )
    monkeypatch.setattr(
        video_tools, "_invoke_volcengine_video_generation_sync", fake_sync
    )

    result = await video_tools._invoke_model_video_generation(
        "hello", size="1280*720", duration=5
    )
    assert called.hit is True
    assert result["video_path"] == "/tmp/y.mp4"


def test_minimax_and_seedance_send_reference_stills(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    plate = tmp_path / "plate.png"
    plate.write_bytes(b"\x89PNG\r\n\x1a\n")
    posts: list[dict] = []

    class _Resp:
        def __init__(self, payload: dict):
            self.ok = True
            self.status_code = 200
            self._payload = payload
            self.content = b"fake-mp4"
            self.text = str(payload)

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    def fake_request(method: str, url: str, **kwargs):
        if method == "POST":
            posts.append(kwargs.get("json") or {})
            if "minimaxi" in url:
                return _Resp({"task_id": "tid-1"})
            return _Resp({"id": "cgt-1"})
        if method == "GET" and "tid-1" in url:
            return _Resp(
                {
                    "task": {
                        "status": "succeeded",
                        "content": {"url": "https://cdn.example/out.mp4"},
                    }
                }
            )
        if method == "GET" and "cgt-1" in url:
            return _Resp(
                {
                    "status": "succeeded",
                    "content": {"video_url": "https://ark.example/out.mp4"},
                }
            )
        if method == "GET":
            return _Resp({})
        raise AssertionError(url)

    monkeypatch.setattr(video_tools, "_http_request", fake_request)
    monkeypatch.setattr(video_tools, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(video_tools.time, "sleep", lambda *_: None)

    video_tools._invoke_minimax_video_generation_sync(
        "father sits",
        api_key="k",
        api_base="https://api.minimaxi.com/v1",
        model="MiniMax-H3",
        size="832*480",
        duration=5,
        resolution="480P",
        reference_images=[str(plate)],
        audio=True,
        force_reference_mode=True,
    )
    video_tools._invoke_volcengine_video_generation_sync(
        "father sits",
        api_key="k",
        api_base="https://ark.cn-beijing.volces.com/api/v3",
        model="doubao-seedance-2-0-260128",
        size="832*480",
        duration=5,
        resolution="480P",
        reference_images=[str(plate)],
        audio=True,
        force_reference_mode=True,
    )
    for body in posts:
        roles = [
            item.get("role")
            for item in body["content"]
            if item.get("type") == "image_url"
        ]
        assert roles == ["reference_image"]
        assert body["generate_audio"] is True


def test_dropped_status_poll_still_saves_finished_video(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    task_id = "00a35677-8280-4e6e-8ec0-23726b5df68c"

    class _Resp:
        def __init__(self, payload: dict, content: bytes = b"mp4-bytes"):
            self.ok = True
            self.status_code = 200
            self._payload = payload
            self.content = content
            self.text = str(payload)

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    def fake_request(method: str, url: str, **kwargs):
        del kwargs
        if method == "GET" and url.endswith(f"/tasks/{task_id}"):
            return _Resp(
                {
                    "output": {
                        "task_id": task_id,
                        "task_status": "SUCCEEDED",
                        "video_url": "https://cdn.example/saved.mp4",
                    }
                }
            )
        if method == "GET" and url.endswith(".mp4"):
            return _Resp({}, content=b"mp4-bytes")
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(video_tools, "_http_request", fake_request)
    monkeypatch.setattr(video_tools, "get_agent_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(video_tools.time, "sleep", lambda *_: None)

    exc = requests.exceptions.SSLError(
        f"Max retries exceeded with url: /api/v1/tasks/{task_id}"
    )
    assert video_tools._task_id_from_error(exc) == task_id
    saved = video_tools._recover_provider_task_file(
        api_base="https://example.maas.aliyuncs.com/api/v1",
        api_key="k",
        task_id=task_id,
        prompt="clip",
    )
    assert Path(saved["video_path"]).read_bytes() == b"mp4-bytes"
    assert saved["original_url"] == "https://cdn.example/saved.mp4"


@pytest.mark.asyncio
async def test_invoke_model_video_generation_routes_to_vllm_omni_without_key_or_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict = {}

    def fake_sync(*args, **kwargs):
        called["args"] = args
        called["kwargs"] = kwargs
        return {"video_path": "/tmp/vo.mp4", "revised_prompt": args[0]}

    monkeypatch.setattr(video_tools, "get_config", lambda: {})
    monkeypatch.setattr(
        video_tools,
        "_get_model_config",
        lambda *_: {
            # Self-deployed vLLM-Omni: key and model name are both optional.
            "api_key": "",
            "api_base": "http://127.0.0.1:8091/v1",
            "model_name": "",
            "client_provider": "OpenAI",
            "endpoint_profile": "vllm-omni",
            "vendor_key": "vllm-omni",
        },
    )
    monkeypatch.setattr(
        video_tools, "invoke_vllm_omni_video_generation_sync", fake_sync
    )

    result = await video_tools._invoke_model_video_generation(
        "hello", size="1280*720", duration=5, first_frame="/tmp/f.png"
    )
    assert result["video_path"] == "/tmp/vo.mp4"
    assert called["args"][0] == "hello"
    assert called["kwargs"]["api_key"] == ""
    assert called["kwargs"]["model"] == ""
    assert called["kwargs"]["first_frame"] == "/tmp/f.png"


@pytest.mark.asyncio
async def test_invoke_model_video_generation_still_requires_key_for_hosted_vendors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(video_tools, "get_config", lambda: {})
    monkeypatch.setattr(
        video_tools,
        "_get_model_config",
        lambda *_: {
            "api_key": "",
            "api_base": "https://api.minimaxi.com",
            "model_name": "MiniMax-H3",
            "client_provider": "OpenAI",
            "endpoint_profile": "minimax",
            "vendor_key": "minimax",
        },
    )

    result = await video_tools._invoke_model_video_generation(
        "hello", size="1280*720", duration=5
    )
    assert "error" in result
