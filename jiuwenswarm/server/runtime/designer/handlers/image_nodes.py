# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Image intermediate handlers: character sheet, scene, and keyframe."""

from __future__ import annotations

from pathlib import Path
from shutil import copy2

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_ROLE_SCENE,
    NODE_ROLE_STORYBOARD,
    NODE_TYPE_IMAGE,
    NODE_TYPE_TEXT,
    AssetRef,
    DesignerGraphNode,
    node_shot_index,
)
from jiuwenswarm.server.runtime.designer.handlers import common as handler_io
from jiuwenswarm.server.runtime.designer.handlers.common import (
    collect_frame_reference_images,
    file_output_ref,
    graph_workspace_dir,
    graph_prompt,
    role_output_image_path,
    role_output_image_paths,
    role_output_text,
    write_workspace_text,
)
from jiuwenswarm.server.runtime.designer.user_references import (
    graph_user_references,
    identity_reference_image_paths,
    prompt_slot_roster,
    user_reference_image_paths,
)
from jiuwenswarm.server.runtime.designer.a2a_collab import collaboration_card
from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
    StoryboardShot,
    storyboard_shots_or_default,
)
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext, NodeResult


def _resolve_image_size(cfg: dict, graph: dict | None = None) -> str:
    """Prefer Manager-stamped aspect_lock image_size (~1K), then node config."""
    aspect = cfg.get("aspect_lock") if isinstance(cfg.get("aspect_lock"), dict) else {}
    if not aspect and isinstance(graph, dict):
        meta = graph.get("metadata") if isinstance(graph.get("metadata"), dict) else {}
        aspect = meta.get("aspect_lock") if isinstance(meta.get("aspect_lock"), dict) else {}
        if not aspect:
            analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
            aspect = analysis.get("aspect_lock") if isinstance(analysis.get("aspect_lock"), dict) else {}
    return str(
        cfg.get("image_size")
        or (aspect or {}).get("image_size")
        or "1K"
    ).strip() or "1K"


def _frame_prompt_looks_contaminated(text: str) -> bool:
    raw = (text or "").upper()
    needles = (
        "PRIOR KEYFRAME PROMPT",
        "PREVIOUS KEYFRAME HAD",
        "PRIOR CLIP CONTINUITY",
        "PREVIOUS CLIP HAD",
        "YOUR ASSIGNMENT",
        "STAGING LOCK",
        "MASTER SCENE PROMPT",
        "CONTINUITY CARD (MANAGER)",
        "LANGUAGE LOCK",
        "ASPECT LOCK",
        "STYLE LOCK",
        "SCENE BIBLE",
        "SPEECH LOCK",
        "BGM LOCK",
    )
    if any(n in raw for n in needles):
        return True
    return len(text or "") > 500


def _character_prompt(
    source: str, *, combined_cast: bool = False, keep_subject: bool = False
) -> str:
    if keep_subject:
        # The upload is the subject. Text may re-dress it, never redesign it.
        return (
            "EDIT the attached reference image — do NOT invent a new character. "
            "Image 1 is this character: keep the exact same identity — face, head shape, "
            "species, skin/fur/scale texture, hair, eyes, age, sex, and body proportions. "
            "Change ONLY wardrobe, accessories, props, pose, framing, and lighting as "
            "described below. Single subject, three-quarter body, clean background. "
            "If the text disagrees with the image about who this is, the IMAGE wins; "
            "the text only governs clothing and accessories. "
            "FORBIDDEN: redesign the subject, swap species or breed, restyle the face, "
            "or replace it with a look described only in text.\n"
            f"{source}"
        )
    if combined_cast:
        return (
            "Combined cast postcard: all listed characters side-by-side on one sheet, "
            "full or three-quarter body each, consistent scale, clean background, "
            "cinematic lighting. Clear identity for each person. Be fast — one clear image. "
            "No subtitles, no storyboard grid, no dense text labels.\n"
            f"{source}"
        )
    return (
        "Character design sheet, single subject, full or three-quarter body, clean background, "
        "cinematic lighting. Be fast — one clear image. Follow the brief. "
        "No subtitles, no storyboard grid.\n"
        f"{source}"
    )


def _scene_prompt(source: str, *, derive_from_master: bool = False) -> str:
    if derive_from_master:
        return (
            "EDIT / REFRAME the provided master environment reference. "
            "Same landmarks, buildings, terrain, props, and lighting direction. "
            "Only change camera angle/framing for this shot. Environment only — no people. "
            "Do NOT invent a new location. Be fast — one clear image.\n"
            f"{source}"
        )
    return (
        "Cinematic establishing shot of the environment only, no people. "
        "Show space, weather, lighting, and ground so a character can be placed later. "
        "This is the CANONICAL plate — freeze geography. "
        "Be fast — one clear image. No people, no subtitles, no storyboard grid.\n"
        f"{source}"
    )


def _strip_markdown_tables(text: str) -> str:
    """Keep prose from Brief; drop markdown tables so the image model does not paint them."""
    lines: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.count("|") >= 2:
            continue
        if stripped.startswith("|") or stripped.endswith("|"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _shot_frame_prompt(
    shot: StoryboardShot,
    brief: str,
    *,
    has_character: bool,
    has_scene: bool,
    cast_names: list[str] | None = None,
    character_ref_count: int = 1,
    combined_cast_ref: bool = False,
    keyframe_strategy: str = "",
    costume_lock: str = "",
    staging_lock: str = "",
) -> str:
    timeline = f" ({shot['timeline']})" if shot["timeline"] else ""
    comment = str(shot.get("comment") or "").strip()
    names = [str(n).strip() for n in (cast_names or []) if str(n).strip()]
    who = ", ".join(names)
    lead = (
        f"Cinematic keyframe, one photoreal still for shot {shot['shot_no']}{timeline}. "
        "Clear composition, this instant only, no comic grid."
    )
    if who:
        if len(names) > 1:
            lead += (
                f" The frame MUST clearly show all of these characters: {who}. "
                "Do not drop anyone listed."
            )
        else:
            lead += f" Feature this character: {who}."
    if comment:
        lead += f" Generate the keyframe from this shot description: {comment}."
    lead += (
        " Shot notes: "
        f"shot {shot['shot_no']}; "
        f"timeline {shot['timeline'] or 'unspecified'}; "
        f"camera {shot['camera'] or 'unspecified'}; "
        f"camera move {shot['move'] or 'unspecified'}; "
        f"character action {shot['character_action'] or 'unspecified'}; "
        f"scene change {shot['scene_change'] or 'unspecified'}."
    )
    n_refs = max(1, int(character_ref_count or 1))
    prior_edit = keyframe_strategy == "edit_prior_keyframe"
    if has_character and has_scene:
        if prior_edit:
            lead += (
                " This is image-to-image EDIT of the prior keyframe of the SAME setting "
                "(first reference). Keep architecture, lighting, landmarks, faces, and costumes "
                f"{f' for {who}' if who else ''}; only change camera/pose/blocking/who is "
                "on-screen for this beat. Additional refs may include the scene-master compose "
                "still (architecture lock) and solo cast sheets (identity only). "
                "Never regenerate the set; never jump to another setting."
            )
        elif combined_cast_ref or (len(names) > 1 and n_refs == 1):
            lead += (
                " This is image-to-image. The first reference is a combined cast postcard "
                f"(all of {who or 'the cast'} on one sheet); the second is the scene. "
                "Place ALL of those people into that scene together, matching each identity "
                "and costume from the postcard, plus location, lighting, and weather."
            )
        elif n_refs > 1:
            lead += (
                f" This is image-to-image. The first {n_refs} references are CANONICAL solo "
                f"cast sheets{f' for {who}' if who else ''}. COMPOSE a new SCENE MASTER still: "
                "generate the place AND place every listed on-screen character into it. "
                "IDENTITY LOCK: same face, hair, body, and costume as each sheet — "
                "never redesign wardrobe between shots. No empty environment plate."
            )
        else:
            lead += (
                " This is image-to-image. The first reference is the canonical character sheet. "
                "Compose a SCENE MASTER still: generate the place and place that character. "
                "Keep identity and costume from the sheet."
            )
    elif has_character:
        if prior_edit:
            lead += (
                " Edit the prior same-setting keyframe (first ref). Keep the place and identity; "
                "only update this beat's action/framing."
            )
        else:
            lead += (
                " Compose SCENE MASTER from solo sheet(s): generate the setting and place "
                "the listed on-screen cast. Character look and costume must match the sheet(s)."
            )
    elif has_scene:
        lead += " Location, lighting, and weather must match the scene reference."
    if costume_lock:
        try:
            from jiuwenswarm.server.runtime.designer.experiments.clothing_lock import (
                clothing_lock_clause,
            )

            cloth = clothing_lock_clause(costume_lock, for_clip=False)
            lead += f" {cloth}" if cloth else f" Costume lock: {costume_lock}."
        except Exception:  # noqa: BLE001
            lead += f" Costume lock: {costume_lock}."
    if staging_lock:
        lead += f" {staging_lock}"
    lead += (
        " The image must be the cinematic scene itself. "
        "No subtitles, no storyboard grid, no table, no spreadsheet, no cell borders. "
        "Do not paint words like Shot, Timeline, Camera, Move, Character action, Scene change, or Comment."
    )
    lead += (
        " ANTI-CLONE: exactly one body per named character — never duplicate the same face "
        "in two places at once."
    )
    lead += (
        " CROWD LOCK: if the brief needs extras, show the SAME group layout "
        "across shots (same coats/positions). Do not empty the crowd in one "
        "shot and invent a new crowd in another. Featured cast must stay distinct from extras."
    )
    visual = _strip_markdown_tables(brief)
    if visual:
        return f"{lead}\nOverall visual style:\n{visual}"
    return lead


def fallback_character_sheet(source: str) -> str:
    return (
        "# Character\n\n"
        f"{source.strip()}\n\n"
        "- Look: follow the subject in the Brief\n"
        "- Costume / materials: match the specified style\n"
        "- Image generation is unavailable; this sheet is the intermediate artifact\n"
    )


def fallback_scene_notes(source: str) -> str:
    return (
        "# Scene\n\n"
        f"{source.strip()}\n\n"
        "- Environment only, no people\n"
        "- Image generation is unavailable; these notes are the intermediate artifact\n"
    )


def fallback_keyframe_script(source: str, shot_index: int = 1) -> str:
    shots = storyboard_shots_or_default(source)
    index = max(1, int(shot_index or 1))
    shot = shots[index - 1] if index <= len(shots) else None
    lines = [
        "# Keyframe\n",
        f"- This is the keyframe node for shot {index} only\n",
    ]
    if shot:
        lines.append(
            f"- Shot {shot['shot_no']} {shot['timeline']}: {shot['camera']} / {shot['move']}\n"
            f"- Character: {shot['character_action']}\n"
            f"- Scene: {shot['scene_change']}\n"
        )
        comment = str(shot.get("comment") or "").strip()
        if comment:
            lines.append(f"- Shot description: {comment}\n")
    lines.append("- Image generation is unavailable; these notes are the intermediate artifact\n")
    return "".join(lines)


def _publish_shot_image(
    src: Path,
    *,
    stem: str,
    ctx: NodeExecutionContext | None = None,
) -> Path:
    dest = graph_workspace_dir(ctx.graph if ctx else None) / f"{stem}{src.suffix or '.png'}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.resolve() != src.resolve():
        copy2(src, dest)
    return dest.resolve()


async def _image_or_notes(
    *,
    prompt: str,
    notes: str,
    stem: str,
    kind_if_text: str,
    reference_images: list[str] | None = None,
    size: str = "1024x1024",
    max_tries: int = 2,
    require_image: bool = True,
    ctx: NodeExecutionContext | None = None,
) -> NodeResult:
    refs = [str(p) for p in (reference_images or []) if str(p).strip()]
    # Only pass real image files — markdown/extra stubs break DashScope uploads.
    _IMG = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
    clean_refs: list[str] = []
    for raw in refs:
        path = Path(raw)
        if path.is_file() and path.suffix.lower() in _IMG:
            clean_refs.append(str(path.resolve()))
    clean_refs = clean_refs[:3]
    emit = getattr(ctx, "emit_activity", None) if ctx is not None else None
    if callable(emit):
        try:
            emit("stage", "calling image model", "image_gen")
        except TypeError:
            emit("stage", "calling image model", "image_gen")

    generated = await handler_io.generate_designer_image(
        prompt,
        size=size,
        reference_images=clean_refs or None,
        max_tries=max_tries,
    )
    err = str((generated or {}).get("error") or "")
    # DashScope often rejects ref uploads ("Cannot determine file type") — retry T2I-only.
    if (not generated or not generated.get("image_path")) and clean_refs and (
        "file type" in err.lower() or "InvalidParameter" in err or "181001" in err
    ):
        generated = await handler_io.generate_designer_image(
            prompt + " Match the described architecture and cast from text alone.",
            size=size,
            reference_images=None,
            max_tries=max(2, max_tries // 2),
        )
    if generated and generated.get("image_path"):
        path = Path(generated["image_path"])
        if ctx is not None:
            path = _publish_shot_image(path, stem=stem, ctx=ctx)
        return NodeResult(
            output_ref=file_output_ref(path, kind=NODE_TYPE_IMAGE, mime_type="image/png"),
            message="image generated",
        )
    error = str((generated or {}).get("error") or "").strip()
    if require_image:
        raise RuntimeError(
            f"image_gen required but failed for {stem}: {error or 'no image_path'}"
        )
    path = write_workspace_text(stem, notes, graph=ctx.graph if ctx else None)
    message = (
        f"image_gen failed: {error}; wrote notes"
        if error
        else "image_gen unavailable, wrote notes"
    )
    return NodeResult(
        output_ref=file_output_ref(path, kind=kind_if_text, mime_type="text/markdown"),
        message=message,
    )


def _aligned_source(ctx: NodeExecutionContext, role: str, node: DesignerGraphNode) -> str:
    return (
        collaboration_card(ctx.run_id, role)
        or role_output_text(ctx, NODE_ROLE_BRIEF)
        or graph_prompt(ctx.graph, node)
    )


def _with_card_ref(result: NodeResult, ctx: NodeExecutionContext, role: str) -> NodeResult:
    card = collaboration_card(ctx.run_id, role)
    if not card:
        return result
    path = write_workspace_text(f"designer_a2a_{ctx.run_id}_{role}", card, graph=ctx.graph)
    card_ref = file_output_ref(path, kind=NODE_TYPE_TEXT, mime_type="text/markdown")
    refs = [ref for ref in (result.output_refs or []) if ref]
    primary = result.output_ref
    if primary is not None and primary not in refs:
        refs.insert(0, primary)
    if card_ref not in refs:
        refs.append(card_ref)
    return NodeResult(
        output_ref=primary,
        output_refs=refs or [card_ref],
        message=result.message,
    )


class CharacterDesignNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        focused = str(cfg.get("prompt") or "").strip()
        source = focused or _aligned_source(ctx, NODE_ROLE_CHARACTER_DESIGN, node)
        name = str(cfg.get("character_name") or node.get("label") or "Character")
        size = _resolve_image_size(cfg, ctx.graph if isinstance(ctx.graph, dict) else None)
        combined = bool(cfg.get("combined_cast"))
        max_tries = max(2, int(cfg.get("max_image_calls") or 1))
        identity_images = [str(path) for path in identity_reference_image_paths(ctx.graph)]
        user_images = identity_images or [
            str(path) for path in user_reference_image_paths(ctx.graph)
        ]
        keep_subject = bool(identity_images) and not combined
        prompt = _character_prompt(
            f"{name}\n{source}", combined_cast=combined, keep_subject=keep_subject
        )
        if keep_subject:
            slots = "\n".join(
                f"image {i} = {Path(p).name} (this subject — preserve identity)"
                for i, p in enumerate(identity_images, start=1)
            )
            prompt = f"{prompt}\nUser reference slots:\n{slots}"
        else:
            roster = prompt_slot_roster(graph_user_references(ctx.graph))
            if roster:
                prompt = (
                    f"{prompt}\nUser reference slots "
                    f"(original files are visual authority):\n{roster}"
                )
        result = await _image_or_notes(
            prompt=prompt,
            notes=fallback_character_sheet(source),
            stem=f"designer_character_{ctx.run_id}_{ctx.node_id}",
            kind_if_text=NODE_TYPE_TEXT,
            size=size,
            max_tries=max_tries,
            reference_images=user_images or None,
            ctx=ctx,
        )
        return _with_card_ref(result, ctx, NODE_ROLE_CHARACTER_DESIGN)


class SceneNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        focused = str(cfg.get("prompt") or "").strip()
        source = focused or _aligned_source(ctx, NODE_ROLE_SCENE, node)
        size = _resolve_image_size(cfg, ctx.graph if isinstance(ctx.graph, dict) else None)
        max_tries = max(2, int(cfg.get("max_image_calls") or 1))
        strategy = str(cfg.get("scene_strategy") or "").strip()
        derive = strategy == "edit_master_view"
        refs: list[str] = []
        master_id = str(cfg.get("master_scene_node_id") or "n_scene").strip()
        if derive:
            from jiuwenswarm.server.runtime.designer.handlers.common import (
                node_ids_output_image_paths,
            )

            master_paths = node_ids_output_image_paths(ctx, [master_id])
            refs = [str(p) for p in master_paths]
            if not refs:
                derive = False
            else:
                lock = cfg.get("spatial_lock") if isinstance(cfg.get("spatial_lock"), dict) else {}
                if lock:
                    source = (
                        f"{source}\nSPATIAL LOCK: "
                        + "; ".join(f"{k}={v}" for k, v in lock.items() if str(v).strip())
                    )
        else:
            refs = [str(path) for path in user_reference_image_paths(ctx.graph)]
        scene_prompt = _scene_prompt(source, derive_from_master=derive)
        roster = prompt_slot_roster(graph_user_references(ctx.graph))
        if roster:
            scene_prompt = (
                f"{scene_prompt}\nUser reference slots (original files are visual authority):\n"
                f"{roster}"
            )
        result = await _image_or_notes(
            prompt=scene_prompt,
            notes=fallback_scene_notes(source),
            stem=f"designer_scene_{ctx.run_id}_{ctx.node_id}",
            kind_if_text=NODE_TYPE_TEXT,
            size=size,
            max_tries=max_tries,
            reference_images=refs or None,
            ctx=ctx,
        )
        return _with_card_ref(result, ctx, NODE_ROLE_SCENE)


class FrameNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        shot_index = node_shot_index(node)
        storyboard = role_output_text(ctx, NODE_ROLE_STORYBOARD)
        brief = role_output_text(ctx, NODE_ROLE_BRIEF)
        all_chars = role_output_image_paths(ctx, NODE_ROLE_CHARACTER_DESIGN)
        all_scenes = role_output_image_paths(ctx, NODE_ROLE_SCENE)
        visual = brief or graph_prompt(ctx.graph, node)
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        generate = cfg.get("generate") if isinstance(cfg.get("generate"), dict) else {}
        identity = cfg.get("identity_refs") if isinstance(cfg.get("identity_refs"), dict) else {}
        keyframe_strategy = str(
            identity.get("keyframe_strategy") or cfg.get("keyframe_strategy") or ""
        )
        meta = ctx.graph.get("metadata") if isinstance(ctx.graph.get("metadata"), dict) else {}
        skip_scene_plate = bool(meta.get("skip_scene_plate"))
        # Quality v5 compose-first: no empty Scene plates; KF generates setting+cast.
        allow_without_scene = skip_scene_plate or keyframe_strategy in {
            "compose_from_solo_refs",
            "edit_prior_keyframe",
        }
        if not all_chars or (not all_scenes and not allow_without_scene):
            missing = []
            if not all_chars:
                missing.append("Character")
            if not all_scenes and not allow_without_scene:
                missing.append("Scene")
            raise RuntimeError(
                "Keyframe generation must send "
                + " and ".join(missing)
                + " with this shot. Finish the Character and Scene nodes first."
            )
        refs_paths = collect_frame_reference_images(ctx, node)
        # Never dump every solo sheet when occupancy is empty/wrong — that paints
        # off-screen cast into the still. Prefer on_screen identity refs only.
        preferred_char_nodes = [
            str(x)
            for x in (
                (cfg.get("identity_refs") or {}).get("character_node_ids")
                if isinstance(cfg.get("identity_refs"), dict)
                else None
            )
            or (cfg.get("character_node_ids") or [])
            if str(x).strip()
        ]
        on_screen_cfg = [
            str(x)
            for x in (
                cfg.get("on_screen")
                or (identity.get("occupancy") or {}).get("must_appear")
                or cfg.get("character_ids")
                or identity.get("character_ids")
                or []
            )
            if str(x).strip()
        ]
        if refs_paths:
            refs = [str(p) for p in refs_paths]
        elif preferred_char_nodes:
            from jiuwenswarm.server.runtime.designer.handlers.common import (
                node_ids_output_image_paths,
            )

            refs = [str(p) for p in node_ids_output_image_paths(ctx, preferred_char_nodes)]
        else:
            # Fail closed: no occupancy → no cast refs (scene plate only if present).
            refs = [str(p) for p in (all_scenes[:1] if all_scenes else [])]
        shots = storyboard_shots_or_default(storyboard, visual)
        planned_action = str(cfg.get("shot_action") or generate.get("prompt") or "").strip()
        cast_names = [
            str(x).strip()
            for x in (cfg.get("cast_names") or [])
            if str(x).strip()
        ]
        costume_lock = str(identity.get("costume_lock") or cfg.get("costume_lock") or "")
        try:
            from jiuwenswarm.server.runtime.designer.experiments.shot_staging_lock import (
                ensure_cfg_staging_locks,
            )

            analysis_chars = list(
                ((ctx.graph.get("metadata") or {}).get("script_analysis") or {}).get("characters")
                or []
            )
            staging_lock = ensure_cfg_staging_locks(cfg, characters=analysis_chars)
        except Exception:  # noqa: BLE001
            staging_lock = ""
        # Detect combined cast from attached character nodes when available.
        combined_cast_ref = False
        for other in ctx.graph.get("nodes") or []:
            if not isinstance(other, dict):
                continue
            if str(other.get("id") or "") not in preferred_char_nodes:
                continue
            oc = other.get("config") if isinstance(other.get("config"), dict) else {}
            if oc.get("combined_cast"):
                combined_cast_ref = True
                if not cast_names:
                    cast_names = [
                        str(x).strip()
                        for x in (oc.get("character_names") or [])
                        if str(x).strip()
                    ] or ([str(oc.get("character_name") or "").strip()] if oc.get("character_name") else [])
        # Ref count follows on_screen / cast_names, not every attached solo predecessor.
        char_ref_count = max(
            1,
            len(cast_names)
            or len(on_screen_cfg)
            or len(preferred_char_nodes)
            or 1,
        )
        if shot_index > len(shots) and planned_action:
            shot = {
                "shot_no": str(shot_index),
                "timeline": "",
                "camera": str(cfg.get("camera") or "medium / eye-level"),
                "move": "",
                "character_action": planned_action,
                "scene_change": "",
                "comment": planned_action,
            }
        elif shot_index > len(shots):
            raise RuntimeError(
                f"Shot {shot_index} is not in the storyboard ({len(shots)} shots)."
            )
        else:
            shot = dict(shots[shot_index - 1])
        override = handler_io.node_generate_prompt(node)
        planned = str(planned_action or shot.get("character_action") or shot.get("comment") or "").strip()
        if override and not _frame_prompt_looks_contaminated(override):
            shot["comment"] = override
        elif planned and not str(shot.get("comment") or "").strip():
            shot["comment"] = planned
        elif planned and _frame_prompt_looks_contaminated(str(shot.get("comment") or "")):
            shot["comment"] = planned
        size = _resolve_image_size(cfg, ctx.graph if isinstance(ctx.graph, dict) else None)
        max_tries = max(2, int(cfg.get("max_image_calls") or 1))
        frame_prompt = _shot_frame_prompt(
            shot,
            visual,
            has_character=bool(all_chars),
            has_scene=bool(all_scenes),
            cast_names=cast_names,
            character_ref_count=char_ref_count,
            combined_cast_ref=combined_cast_ref,
            keyframe_strategy=keyframe_strategy,
            costume_lock=costume_lock,
            staging_lock=staging_lock,
        )
        from jiuwenswarm.server.runtime.designer.experiments.wan_call_locks import (
            apply_keyframe_call_locks,
        )

        frame_prompt = apply_keyframe_call_locks(
            frame_prompt,
            cfg=cfg,
            graph=ctx.graph if isinstance(ctx.graph, dict) else {},
        )
        roster = prompt_slot_roster(graph_user_references(ctx.graph))
        if roster:
            frame_prompt = (
                f"{frame_prompt}\nUser reference slots (original files are visual authority):\n"
                f"{roster}"
            )
        emit = getattr(ctx, "emit_activity", None)
        if callable(emit):
            try:
                emit("stage", "calling image model", "image_gen")
            except TypeError:
                emit("stage", "calling image model", "image_gen")
        generated = await handler_io.generate_designer_image(
            frame_prompt,
            size=size,
            reference_images=refs,
            max_tries=max_tries,
        )
        if generated and generated.get("image_path"):
            path = _publish_shot_image(
                Path(generated["image_path"]),
                stem=f"designer_frame_{ctx.run_id}_{ctx.node_id}_shot{shot_index}",
                ctx=ctx,
            )
            ref = file_output_ref(path, kind=NODE_TYPE_IMAGE, mime_type="image/png")
            return NodeResult(
                output_ref=ref,
                output_refs=[ref],
                message=f"keyframe {shot_index} generated",
            )
        last_error = str((generated or {}).get("error") or "").strip()
        raise RuntimeError(
            f"keyframe {shot_index} image_gen failed: {last_error or 'no image_path'}"
        )
