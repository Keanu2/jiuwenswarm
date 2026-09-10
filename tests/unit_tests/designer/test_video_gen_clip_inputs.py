# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from pathlib import Path

from jiuwenswarm.agents.harness.common.tools.video_tools import (
    _align_dashscope_video_api_base,
    _as_dashscope_file_url,
    _as_dashscope_media_url,
    _build_dashscope_video_call,
    _INTL_DASHSCOPE_API_BASE,
    _switch_wan_task,
    video_generation_message_content,
)


def test_switch_wan_task_keeps_flash_suffix() -> None:
    assert _switch_wan_task("wan2.6-t2v", "i2v") == "wan2.6-i2v"
    assert _switch_wan_task("wan2.6-i2v-flash", "i2v") == "wan2.6-i2v-flash"
    assert _switch_wan_task("wan2.6-t2v", "r2v") == "wan2.6-r2v"


def test_local_keyframe_becomes_data_uri(tmp_path: Path) -> None:
    frame = tmp_path / "shot1.png"
    frame.write_bytes(b"png")
    url = _as_dashscope_media_url(str(frame))
    assert url is not None
    assert url.startswith("data:image/png;base64,")


def test_build_call_uses_r2v_when_identity_references_exist(tmp_path: Path) -> None:
    frame = tmp_path / "shot1.png"
    extra = tmp_path / "character.png"
    frame.write_bytes(b"png")
    extra.write_bytes(b"png-extra")
    params = _build_dashscope_video_call(
        "wan2.6-t2v",
        first_frame=str(frame),
        reference_images=[str(frame), str(extra)],
    )
    assert params["model"] == "wan2.6-r2v"
    assert "img_url" not in params
    assert params["shot_type"] == "multi"
    assert params["size"] == "1280*720"
    assert "resolution" not in params
    assert len(params.get("reference_urls") or []) == 2


def test_build_call_uses_single_shot_for_one_keyframe(tmp_path: Path) -> None:
    frame = tmp_path / "shot1.png"
    frame.write_bytes(b"png")
    params = _build_dashscope_video_call(
        "wan2.6-t2v",
        first_frame=str(frame),
    )
    assert params["model"] == "wan2.6-i2v"
    assert str(params["img_url"]).startswith("data:image/png;base64,")
    assert params["shot_type"] == "single"


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


def test_build_call_stays_text_to_video_without_images() -> None:
    params = _build_dashscope_video_call("wan2.6-t2v")
    assert params["model"] == "wan2.6-t2v"
    assert params["size"] == "1280*720"
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
