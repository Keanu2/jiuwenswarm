# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.tools import image_tools


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {
                "provider": "DashScope",
                "endpoint_profile": "",
                "vendor_key": "",
                "api_base": "",
                "model": "wanx-v1",
            },
            "dashscope",
        ),
        (
            {
                "provider": "OpenAI",
                "endpoint_profile": "dashscope",
                "vendor_key": "alibaba",
                "api_base": "",
                "model": "wanx-v1",
            },
            "dashscope",
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
                "endpoint_profile": "",
                "vendor_key": "",
                "api_base": "",
                "model": "doubao-seedream-5-0-260128",
            },
            "volcengine",
        ),
        (
            {
                "provider": "OpenAI",
                "endpoint_profile": "minimax",
                "vendor_key": "minimax",
                "api_base": "",
                "model": "image-01",
            },
            "minimax",
        ),
        (
            {
                "provider": "MiniMax",
                "endpoint_profile": "",
                "vendor_key": "",
                "api_base": "",
                "model": "image-01-live",
            },
            "minimax",
        ),
        (
            {
                "provider": "OpenAI",
                "endpoint_profile": "vllm-omni",
                "vendor_key": "vllm-omni",
                "api_base": "http://127.0.0.1:8000/v1",
                "model": "",
            },
            "vllm-omni",
        ),
    ],
)
def test_resolve_image_gen_backend(kwargs: dict, expected: str) -> None:
    assert image_tools._resolve_image_gen_backend(**kwargs) == expected


def test_normalize_seedream_size_and_ark_root() -> None:
    assert image_tools._normalize_seedream_size("1024*1024") == "1024x1024"
    assert image_tools._normalize_seedream_size("2k") == "2K"
    assert image_tools._normalize_seedream_size("") == "2048x2048"
    assert image_tools._normalize_seedream_size("1K", 3_686_400) == "2K"
    assert image_tools._normalize_seedream_size("1024x576") == "1280x720"
    assert (
        image_tools._ark_image_api_root("https://ark.cn-beijing.volces.com/api/coding/v3")
        == "https://ark.cn-beijing.volces.com/api/v3"
    )
    assert image_tools._size_to_minimax_aspect_ratio("1280x720") == "16:9"
    assert image_tools._size_to_minimax_aspect_ratio("1024*1024") == "1:1"
    assert (
        image_tools._minimax_image_api_url("https://api.minimaxi.com/v1")
        == "https://api.minimaxi.com/v1/image_generation"
    )
    assert (
        image_tools._minimax_image_api_url("https://api.minimaxi.com")
        == "https://api.minimaxi.com/v1/image_generation"
    )


def test_invoke_minimax_image_generation_sync(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    posts: list[dict] = []

    class _Resp:
        def __init__(self, ok: bool, payload: dict | None = None, content: bytes = b"", status_code: int = 200):
            self.ok = ok
            self.status_code = status_code
            self._payload = payload or {}
            self.content = content
            self.text = str(self._payload)

        def json(self):
            return self._payload

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError(f"http {self.status_code}")

    def fake_post(url, **kwargs):
        assert url.endswith("/v1/image_generation")
        posts.append(kwargs.get("json") or {})
        return _Resp(
            True,
            {
                "data": {"image_urls": ["https://cdn.example/out.png"]},
                "base_resp": {"status_code": 0, "status_msg": "success"},
            },
        )

    def fake_get(url, **kwargs):
        del kwargs
        assert url.endswith(".png")
        return _Resp(True, content=b"png-bytes")

    monkeypatch.setattr(image_tools.requests, "post", fake_post)
    monkeypatch.setattr(image_tools.requests, "get", fake_get)
    monkeypatch.setattr(image_tools, "get_agent_workspace_dir", lambda: tmp_path)

    result = image_tools._invoke_minimax_image_generation_sync(
        "a cat",
        api_key="k",
        api_base="https://api.minimaxi.com",
        model="image-01",
        size="1024x1024",
    )
    assert "image_path" in result
    assert result["original_url"] == "https://cdn.example/out.png"
    assert posts[0]["model"] == "image-01"
    assert posts[0]["aspect_ratio"] == "1:1"


def test_invoke_volcengine_image_generation_sync(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    posts: list[dict] = []

    class _Resp:
        def __init__(self, ok: bool, payload: dict | None = None, content: bytes = b"", status_code: int = 200):
            self.ok = ok
            self.status_code = status_code
            self._payload = payload or {}
            self.content = content
            self.text = str(self._payload)

        def json(self):
            return self._payload

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError(f"http {self.status_code}")

    def fake_post(url, **kwargs):
        assert url.endswith("/images/generations")
        posts.append(kwargs.get("json") or {})
        return _Resp(
            True,
            {
                "data": [{"url": "https://ark.example/out.png"}],
            },
        )

    def fake_get(url, **kwargs):
        del kwargs
        assert url.endswith(".png")
        return _Resp(True, content=b"png-bytes")

    monkeypatch.setattr(image_tools.requests, "post", fake_post)
    monkeypatch.setattr(image_tools.requests, "get", fake_get)
    monkeypatch.setattr(image_tools, "get_agent_workspace_dir", lambda: tmp_path)

    result = image_tools._invoke_volcengine_image_generation_sync(
        "a cat",
        api_key="k",
        api_base="https://ark.cn-beijing.volces.com/api/v3",
        model="doubao-seedream-5-0-260128",
        size="1024x1024",
    )
    assert "image_path" in result
    assert result["original_url"] == "https://ark.example/out.png"
    assert posts[0]["model"] == "doubao-seedream-5-0-260128"
    assert posts[0]["size"] == "1024x1024"
    assert posts[0]["watermark"] is False


def test_volcengine_retries_when_ark_rejects_1k(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    posts: list[dict] = []
    model = "doubao-seedream-test-1k"

    class _Resp:
        def __init__(self, ok: bool, payload: dict | None = None, content: bytes = b"", status_code: int = 200):
            self.ok = ok
            self.status_code = status_code
            self._payload = payload or {}
            self.content = content
            self.text = str(self._payload)

        def json(self):
            return self._payload

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError(f"http {self.status_code}")

    def fake_post(url, **kwargs):
        del url
        payload = kwargs.get("json") or {}
        posts.append(payload)
        if payload.get("size") == "1K":
            return _Resp(
                False,
                {
                    "error": {
                        "message": (
                            "The parameter `size` specified in the request is not valid: "
                            "image size must be at least 3686400 pixels."
                        )
                    }
                },
                status_code=400,
            )
        return _Resp(True, {"data": [{"url": "https://ark.example/out.png"}]})

    monkeypatch.setattr(image_tools.requests, "post", fake_post)
    monkeypatch.setattr(image_tools.requests, "get", lambda url, **kw: _Resp(True, content=b"png-bytes"))
    monkeypatch.setattr(image_tools, "get_agent_workspace_dir", lambda: tmp_path)
    image_tools._SEEDREAM_LEARNED_MIN_AREA.pop(model, None)

    result = image_tools._invoke_volcengine_image_generation_sync(
        "a cat",
        api_key="k",
        api_base="https://ark.cn-beijing.volces.com/api/v3",
        model=model,
        size="1K",
    )
    assert "image_path" in result
    assert [item["size"] for item in posts] == ["1K", "2K"]


@pytest.mark.asyncio
async def test_invoke_model_image_generation_routes_to_volcengine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = SimpleNamespace(hit=False)

    def fake_sync(*args, **kwargs):
        del args, kwargs
        called.hit = True
        return {"image_path": "/tmp/z.png", "revised_prompt": "p"}

    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: {})
    monkeypatch.setattr(
        image_tools,
        "_get_model_config",
        lambda *_: {
            "api_key": "k",
            "api_base": "https://ark.cn-beijing.volces.com/api/v3",
            "model_name": "doubao-seedream-5-0-260128",
            "client_provider": "OpenAI",
            "endpoint_profile": "volcengine",
            "vendor_key": "volcengine",
        },
    )
    monkeypatch.setattr(image_tools, "_invoke_volcengine_image_generation_sync", fake_sync)

    result = await image_tools._invoke_model_image_generation("hello", size="1024x1024")
    assert called.hit is True
    assert result["image_path"] == "/tmp/z.png"


@pytest.mark.asyncio
async def test_invoke_model_image_generation_routes_to_minimax(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = SimpleNamespace(hit=False)

    def fake_sync(*args, **kwargs):
        del args, kwargs
        called.hit = True
        return {"image_path": "/tmp/mm.png", "revised_prompt": "p"}

    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: {})
    monkeypatch.setattr(
        image_tools,
        "_get_model_config",
        lambda *_: {
            "api_key": "k",
            "api_base": "https://api.minimaxi.com",
            "model_name": "image-01",
            "client_provider": "OpenAI",
            "endpoint_profile": "minimax",
            "vendor_key": "minimax",
        },
    )
    monkeypatch.setattr(image_tools, "_invoke_minimax_image_generation_sync", fake_sync)

    result = await image_tools._invoke_model_image_generation("hello", size="1024x1024")
    assert called.hit is True
    assert result["image_path"] == "/tmp/mm.png"


@pytest.mark.asyncio
async def test_invoke_model_image_generation_routes_to_vllm_omni_without_key_or_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: dict = {}

    def fake_sync(*args, **kwargs):
        called["args"] = args
        called["kwargs"] = kwargs
        return {"image_path": "/tmp/vo.png", "revised_prompt": "p"}

    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: {})
    monkeypatch.setattr(
        image_tools,
        "_get_model_config",
        lambda *_: {
            # Self-deployed vLLM-Omni: key and model name are both optional.
            "api_key": "",
            "api_base": "http://127.0.0.1:8000/v1",
            "model_name": "",
            "client_provider": "OpenAI",
            "endpoint_profile": "vllm-omni",
            "vendor_key": "vllm-omni",
        },
    )
    monkeypatch.setattr(image_tools, "invoke_vllm_omni_image_generation_sync", fake_sync)

    result = await image_tools._invoke_model_image_generation(
        "hello", size="1024x1024", reference_images=["/tmp/r.png"]
    )
    assert result["image_path"] == "/tmp/vo.png"
    assert called["kwargs"]["api_key"] == ""
    assert called["kwargs"]["model"] == ""
    assert called["kwargs"]["reference_images"] == ["/tmp/r.png"]


@pytest.mark.asyncio
async def test_invoke_model_image_generation_still_requires_key_for_hosted_vendors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jiuwenswarm.common.config.get_config", lambda: {})
    monkeypatch.setattr(
        image_tools,
        "_get_model_config",
        lambda *_: {
            "api_key": "",
            "api_base": "https://api.minimaxi.com",
            "model_name": "image-01",
            "client_provider": "OpenAI",
            "endpoint_profile": "minimax",
            "vendor_key": "minimax",
        },
    )

    result = await image_tools._invoke_model_image_generation("hello", size="1024x1024")
    assert "error" in result
