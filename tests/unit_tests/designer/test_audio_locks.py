# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.audio_locks import (
    resolve_video_audio_request,
    video_model_supports_native_audio,
)


def test_seedance_supports_native_audio() -> None:
    assert video_model_supports_native_audio("doubao-seedance-2-5-260628")
    assert video_model_supports_native_audio("wan3.0-video")
    assert not video_model_supports_native_audio("wan2.6-t2v")


def test_seedance_does_not_override_to_wan3(monkeypatch) -> None:
    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "doubao-seedance-2-5-260628")
    want, override = resolve_video_audio_request(
        {"clip_embedded_audio": True, "prefer_wan3_clip_audio": True},
        {"prefer_wan3_clip_audio": True},
    )
    assert want is True
    assert override is None


def test_video_gen_family_label_follows_configured_model(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.audio_locks import video_gen_family_label

    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "doubao-seedance-2-5-260628")
    assert video_gen_family_label() == "Seedance"
    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "wan2.6-i2v")
    assert video_gen_family_label() == "Wan"
    monkeypatch.delenv("VIDEO_GEN_MODEL_NAME", raising=False)
    assert video_gen_family_label("MiniMax-Hailuo-02") == "MiniMax"


def test_image_gen_family_label_follows_configured_model(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.audio_locks import image_gen_family_label

    monkeypatch.setenv("IMAGE_GEN_MODEL_NAME", "doubao-seedream-4-5-251128")
    assert image_gen_family_label() == "Seedream"
    monkeypatch.setenv("IMAGE_GEN_MODEL_NAME", "qwen-image-3.0")
    assert image_gen_family_label() == "Qwen"
    monkeypatch.delenv("IMAGE_GEN_MODEL_NAME", raising=False)
    assert image_gen_family_label("wan2.7-image") == "Wan"


def test_clip_playbook_uses_configured_video_family(monkeypatch) -> None:
    from jiuwenswarm.server.runtime.designer.media_model_playbook import playbook_for_role

    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "doubao-seedance-2-5-260628")
    monkeypatch.setenv("IMAGE_GEN_MODEL_NAME", "doubao-seedream-4-5-251128")
    text = playbook_for_role("clip")
    assert "Seedance" in text
    assert "Qwen-Image 3.0" not in text
    assert "Prefer 480P" not in text
    assert "aspect_lock" in text


def test_wan2_promotes_to_wan3_on_same_stack(monkeypatch) -> None:
    monkeypatch.setenv("VIDEO_GEN_MODEL_NAME", "wan2.6-t2v")
    want, override = resolve_video_audio_request(
        {"clip_embedded_audio": True},
        {"prefer_wan3_clip_audio": True},
    )
    assert want is True
    assert override == "wan3.0-video"
