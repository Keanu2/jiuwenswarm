# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from pathlib import Path

from jiuwenswarm.agents.harness.common.tools.video_tools import (
    _align_dashscope_video_api_base,
    _as_dashscope_file_url,
    _as_dashscope_media_url,
    _build_dashscope_video_call,
    _INTL_DASHSCOPE_API_BASE,
    video_generation_message_content,
)


def test_local_keyframe_becomes_data_uri(tmp_path: Path) -> None:
    frame = tmp_path / "shot1.png"
    frame.write_bytes(b"png")
    url = _as_dashscope_media_url(str(frame))
    assert url is not None
    assert url.startswith("data:image/png;base64,")


def test_build_call_uses_reference_urls_when_identity_references_exist(tmp_path: Path) -> None:
    frame = tmp_path / "shot1.png"
    extra = tmp_path / "character.png"
    frame.write_bytes(b"png")
    extra.write_bytes(b"png-extra")
    params = _build_dashscope_video_call(
        "wan3.0-video",
        first_frame=str(frame),
        reference_images=[str(frame), str(extra)],
    )
    assert params["model"] == "wan3.0-video"
    assert "img_url" not in params
    assert "shot_type" not in params
    assert params["size"] == "1280*720"
    assert "resolution" not in params
    media = params.get("media") or []
    assert len([item for item in media if item.get("type") == "reference_image"]) == 2


def test_build_call_uses_img_url_for_lone_first_frame(tmp_path: Path) -> None:
    frame = tmp_path / "shot1.png"
    frame.write_bytes(b"png")
    params = _build_dashscope_video_call(
        "wan3.0-video",
        first_frame=str(frame),
    )
    assert params["model"] == "wan3.0-video"
    assert str(params["img_url"]).startswith("data:image/png;base64,")
    assert "shot_type" not in params


def test_wan3_keeps_unified_model_without_shot_type(tmp_path: Path) -> None:
    frame = tmp_path / "shot1.png"
    frame.write_bytes(b"png")
    params = _build_dashscope_video_call(
        "wan3.0-video",
        first_frame=str(frame),
    )
    assert params["model"] == "wan3.0-video"
    assert str(params["img_url"]).startswith("data:image/png;base64,")
    assert "shot_type" not in params
    assert params["resolution"] == "720P"
    assert params["audio"] is False
    assert "size" not in params
    assert "ratio" not in params


def test_wan3_img_url_uses_requested_portrait_frame(tmp_path: Path) -> None:
    frame = tmp_path / "shot1.png"
    frame.write_bytes(b"png")
    params = _build_dashscope_video_call(
        "wan3.0-video",
        size="480*854",
        resolution="1080P",
        first_frame=str(frame),
    )
    assert "size" not in params
    assert "ratio" not in params
    assert params["resolution"] == "1080P"


def test_wan3_uses_media_for_character_scene_and_storyboard(tmp_path: Path) -> None:
    character = tmp_path / "character.png"
    scene = tmp_path / "scene.png"
    frame = tmp_path / "keyframe.png"
    story = tmp_path / "storyboard.md"
    character.write_bytes(b"png-c")
    scene.write_bytes(b"png-s")
    frame.write_bytes(b"png-f")
    story.write_text("| Shot | Action |\n| 1 | walk |\n", encoding="utf-8")
    params = _build_dashscope_video_call(
        "wan3.0-video",
        first_frame=None,
        reference_images=[str(frame), str(character), str(scene)],
        reference_file=str(story),
    )
    assert params["model"] == "wan3.0-video"
    assert "img_url" not in params
    assert "reference_urls" not in params
    assert "shot_type" not in params
    assert params["audio"] is False
    media = params["media"]
    assert [item["type"] for item in media] == [
        "reference_image",
        "reference_image",
        "reference_image",
        "file",
    ]
    assert all(item["url"].startswith("data:image/png;base64,") for item in media[:-1])
    assert media[-1]["url"] == str(story.resolve())
    assert _as_dashscope_file_url(str(story)) == str(story.resolve())


def test_wan3_storyboard_file_uses_reference_image_not_img_url(tmp_path: Path) -> None:
    frame = tmp_path / "keyframe.png"
    story = tmp_path / "storyboard.md"
    frame.write_bytes(b"png-f")
    story.write_text("| Shot | Action |\n| 1 | walk |\n", encoding="utf-8")
    params = _build_dashscope_video_call(
        "wan3.0-video",
        first_frame=str(frame),
        reference_images=[str(frame)],
        reference_file=str(story),
    )
    assert "img_url" not in params
    assert [item["type"] for item in params["media"]] == ["reference_image", "file"]
    assert params["audio"] is False


def test_wan3_compose_score_can_enable_audio() -> None:
    params = _build_dashscope_video_call("wan3.0-video", audio=True)
    assert params["audio"] is True
    assert "img_url" not in params


def test_legacy_model_keeps_img_url_when_user_video_file_is_attached(tmp_path: Path) -> None:
    """Non-wan3 models ignore reference_file for mode selection; lone first_frame → img_url."""
    frame = tmp_path / "shot1.png"
    video = tmp_path / "motion.mp4"
    frame.write_bytes(b"png")
    video.write_bytes(b"mp4")
    params = _build_dashscope_video_call(
        "custom-video-model",
        first_frame=str(frame),
        reference_file=str(video),
    )
    assert params["model"] == "custom-video-model"
    assert "img_url" in params
    assert "reference_urls" not in params
    assert "media" not in params


def test_build_call_stays_text_only_without_images() -> None:
    params = _build_dashscope_video_call("wan3.0-video")
    assert params["model"] == "wan3.0-video"
    assert params["size"] == "1280*720"
    assert "img_url" not in params


def test_force_reference_480p_uses_size_not_resolution(tmp_path: Path) -> None:
    extra = tmp_path / "character.png"
    extra.write_bytes(b"png-extra")
    params = _build_dashscope_video_call(
        "wan3.0-video",
        size="854*480",
        resolution="480P",
        first_frame=None,
        reference_images=[str(extra)],
        force_reference_mode=True,
    )
    assert params["model"] == "wan3.0-video"
    assert params["size"] == "832*480"
    assert "resolution" not in params
    assert "media" in params


def test_text_only_480p_uses_size_not_resolution() -> None:
    params = _build_dashscope_video_call(
        "wan3.0-video",
        size="854*480",
        resolution="480P",
    )
    assert params["model"] == "wan3.0-video"
    assert params["size"] == "832*480"
    assert "resolution" not in params
    assert "img_url" not in params


def test_align_video_api_base_to_image_gen_intl(monkeypatch) -> None:
    monkeypatch.setenv("IMAGE_GEN_API_BASE", _INTL_DASHSCOPE_API_BASE)
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "sk-test")
    aligned = _align_dashscope_video_api_base(
        "https://dashscope.aliyuncs.com/api/v1",
        "sk-test",
    )
    assert aligned == _INTL_DASHSCOPE_API_BASE


def test_video_message_content_lists_explicit_images(tmp_path: Path) -> None:
    character = tmp_path / "character.png"
    scene = tmp_path / "scene.png"
    frame = tmp_path / "keyframe.png"
    character.write_bytes(b"png-c")
    scene.write_bytes(b"png-s")
    frame.write_bytes(b"png-f")
    content = video_generation_message_content(
        "Create shot 1",
        first_frame=str(frame),
        reference_images=[str(character), str(scene), str(frame)],
    )
    assert isinstance(content, list)
    images = [item["image"] for item in content if "image" in item]
    assert len(images) == 3
    assert all(item.startswith("data:image/png;base64,") for item in images)
    assert content[-1]["text"] == "Create shot 1"
