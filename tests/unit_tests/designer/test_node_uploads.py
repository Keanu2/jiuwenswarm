# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from jiuwenswarm.server.runtime.designer.handlers.clip import collect_clip_reference_images
from jiuwenswarm.server.runtime.designer.handlers.common import (
    apply_uploaded_outputs_to_run,
    node_output_image_paths,
)
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
from jiuwenswarm.server.runtime.gateway_adapter.designer_adapter import (
    hydrate_graph_node_outputs,
)


def test_uploaded_still_replaces_the_generated_one(tmp_path) -> None:
    old = tmp_path / "generated.png"
    uploaded = tmp_path / "uploaded.png"
    old.write_bytes(b"old")
    uploaded.write_bytes(b"new")
    uploaded_ref = {
        "kind": "image",
        "uri": uploaded.resolve().as_uri(),
        "mime_type": "image/png",
    }
    graph = {
        "nodes": [
            {
                "id": "n_frame_1",
                "type": "image",
                "config": {"role": "frame", "user_replaced_output": True},
                "output_ref": uploaded_ref,
            }
        ]
    }
    run = {
        "node_states": {
            "n_frame_1": {
                "status": "completed",
                "output_ref": {
                    "kind": "image",
                    "uri": old.resolve().as_uri(),
                    "mime_type": "image/png",
                },
            }
        }
    }
    ctx = NodeExecutionContext(graph=graph, run_id="run_1", node_id="n_clip_1", run=run)

    assert node_output_image_paths(ctx, "n_frame_1") == [uploaded.resolve()]
    assert apply_uploaded_outputs_to_run(run, graph) is True
    assert run["node_states"]["n_frame_1"]["output_ref"]["uri"] == uploaded_ref["uri"]
    assert run["node_states"]["n_frame_1"]["status"] == "completed"

    hydrated = hydrate_graph_node_outputs(
        graph,
        {
            "node_states": {
                "n_frame_1": {
                    "output_ref": {
                        "kind": "image",
                        "uri": old.resolve().as_uri(),
                        "mime_type": "image/png",
                    }
                }
            }
        },
    )
    assert hydrated["nodes"][0]["output_ref"]["uri"] == uploaded_ref["uri"]


def test_upload_uri_wins_over_a_stale_generated_output(tmp_path) -> None:
    old = tmp_path / "generated.png"
    uploaded = tmp_path / "uploaded.jpg"
    old.write_bytes(b"old")
    uploaded.write_bytes(b"new")
    graph = {
        "nodes": [
            {
                "id": "n_character_1",
                "type": "image",
                "config": {
                    "role": "character_design",
                    "user_replaced_output": True,
                    "upload": {
                        "filename": "uploaded.jpg",
                        "mime_type": "image/jpeg",
                        "uri": uploaded.resolve().as_uri(),
                    },
                },
                "output_ref": {
                    "kind": "image",
                    "uri": old.resolve().as_uri(),
                    "mime_type": "image/png",
                },
            }
        ]
    }
    run = {
        "node_states": {
            "n_character_1": {
                "status": "completed",
                "output_ref": graph["nodes"][0]["output_ref"],
            }
        }
    }
    ctx = NodeExecutionContext(graph=graph, run_id="run_1", node_id="n_clip_1", run=run)

    assert node_output_image_paths(ctx, "n_character_1") == [uploaded.resolve()]
    assert apply_uploaded_outputs_to_run(run, graph) is True
    assert run["node_states"]["n_character_1"]["output_ref"]["uri"] == uploaded.resolve().as_uri()


def test_material_upload_is_the_clip_input(tmp_path) -> None:
    keyframe = tmp_path / "keyframe.png"
    material = tmp_path / "material.png"
    keyframe.write_bytes(b"kf")
    material.write_bytes(b"mat")
    graph = {
        "nodes": [
            {
                "id": "n_frame_1",
                "type": "image",
                "config": {"role": "frame", "shot_index": 1},
                "output_ref": {
                    "kind": "image",
                    "uri": keyframe.resolve().as_uri(),
                    "mime_type": "image/png",
                },
            },
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {
                    "role": "clip",
                    "shot_index": 1,
                    "materials": [
                        {
                            "id": "mat_1",
                            "filename": "material.png",
                            "mime_type": "image/png",
                            "uri": material.resolve().as_uri(),
                        }
                    ],
                },
            },
        ]
    }
    run = {
        "node_states": {
            "n_frame_1": {
                "status": "completed",
                "output_ref": graph["nodes"][0]["output_ref"],
            }
        }
    }
    ctx = NodeExecutionContext(graph=graph, run_id="run_1", node_id="n_clip_1", run=run)
    clip = graph["nodes"][1]

    refs = collect_clip_reference_images(ctx, 1, clip)
    assert material.resolve() in refs
    assert keyframe.resolve() in refs
    assert refs[-1] == keyframe.resolve()


def test_user_stills_join_r2v_refs_without_replacing_them(tmp_path) -> None:
    solo = tmp_path / "solo.png"
    scene = tmp_path / "scene.png"
    first = tmp_path / "extra-a.png"
    second = tmp_path / "extra-b.png"
    for path in (solo, scene, first, second):
        path.write_bytes(b"img")
    graph = {
        "nodes": [
            {
                "id": "n_character_1",
                "type": "image",
                "config": {
                    "role": "character_design",
                    "character_ids": ["char_1"],
                },
                "output_ref": {
                    "kind": "image",
                    "uri": solo.resolve().as_uri(),
                    "mime_type": "image/png",
                },
            },
            {
                "id": "n_scene_1",
                "type": "image",
                "config": {"role": "scene", "setting_id": "set_1"},
                "output_ref": {
                    "kind": "image",
                    "uri": scene.resolve().as_uri(),
                    "mime_type": "image/png",
                },
            },
            {
                "id": "n_clip_1",
                "type": "video",
                "config": {
                    "role": "clip",
                    "shot_index": 1,
                    "scene_node_id": "n_scene_1",
                    "on_screen": ["char_1"],
                    "character_node_ids": ["n_character_1"],
                    "materials": [
                        {
                            "filename": "extra-a.png",
                            "mime_type": "image/png",
                            "uri": first.resolve().as_uri(),
                        },
                        {
                            "filename": "extra-b.png",
                            "mime_type": "image/png",
                            "uri": second.resolve().as_uri(),
                        },
                    ],
                },
            },
        ]
    }
    run = {
        "node_states": {
            "n_character_1": {
                "status": "completed",
                "output_ref": graph["nodes"][0]["output_ref"],
            },
            "n_scene_1": {
                "status": "completed",
                "output_ref": graph["nodes"][1]["output_ref"],
            },
        }
    }
    ctx = NodeExecutionContext(graph=graph, run_id="run_1", node_id="n_clip_1", run=run)

    assert collect_clip_reference_images(ctx, 1, graph["nodes"][2]) == [
        solo.resolve(),
        first.resolve(),
        second.resolve(),
        scene.resolve(),
    ]


def test_upstream_images_keep_attachments_and_graph_inputs(tmp_path) -> None:
    from jiuwenswarm.server.runtime.designer.handlers.media_nodes import _upstream_images

    files = [tmp_path / f"in_{index}.png" for index in range(4)]
    extra = tmp_path / "attached.png"
    for path in [*files, extra]:
        path.write_bytes(b"img")
    nodes = []
    states = {}
    for index, path in enumerate(files, start=1):
        node_id = f"n_src_{index}"
        ref = {
            "kind": "image",
            "uri": path.resolve().as_uri(),
            "mime_type": "image/png",
        }
        nodes.append({"id": node_id, "type": "image", "output_ref": ref})
        states[node_id] = {"status": "completed", "output_ref": ref}
    clip = {
        "id": "n_clip_1",
        "type": "video",
        "config": {
            "role": "clip",
            "inputs": [f"n_src_{index}" for index in range(1, 5)],
            "materials": [
                {
                    "filename": "attached.png",
                    "mime_type": "image/png",
                    "uri": extra.resolve().as_uri(),
                }
            ],
        },
    }
    nodes.append(clip)
    ctx = NodeExecutionContext(
        graph={"nodes": nodes, "edges": []},
        run_id="run_1",
        node_id="n_clip_1",
        run={"node_states": states},
    )

    refs = _upstream_images(ctx, clip)
    assert refs[0] == extra.resolve()
    assert [path.name for path in refs[1:]] == [path.name for path in files]
    assert len(refs) == 5
