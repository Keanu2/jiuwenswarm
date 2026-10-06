# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Text intermediate handlers: brief, storyboard (includes camera script)."""

from __future__ import annotations

import re
from typing import TypedDict

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_ROLE_SCENE,
    NODE_TYPE_TABLE,
    NODE_TYPE_TEXT,
    DesignerGraphNode,
    node_config,
)
from jiuwenswarm.server.runtime.designer.handlers.common import (
    file_output_ref,
    graph_prompt,
    role_output_image_path,
    role_output_text,
    write_workspace_text,
)
from jiuwenswarm.server.runtime.designer.a2a_collab import (
    collaboration_card,
    review_storyboard_with_peers,
)
from jiuwenswarm.server.runtime.designer.subagent import complete_designer_node_text
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext, NodeResult

_BRIEF_INSTRUCTION = """Turn the request below into an executable short-film Brief.
Write English Markdown with these sections:
- User prompt (verbatim intent)
- Logline
- Cast (solo identity locks — face, hair, body, costume for EACH character; never concatenate)
- Setting / scene geography and lighting
- Consistency gates: character, scene, motion/continuity, camera views covering every beat
- Shot-view coverage list (distinct cameras/angles needed)
- Duration target and per-shot timing budget
- Audio policy (speech vs music)
- What to avoid
Preserve every named character and beat from the user prompt. Output Markdown only.

Request:
"""

# Keep Continuity as a first-class column so time-coherent forbids survive parsing.
_STORYBOARD_COLUMNS = "Shot | Timeline | Camera | Move | Character action | Continuity | Comment"

_STORYBOARD_INSTRUCTION = """Write a time-coherent storyboard from the Brief. This is a camera script table, not a drawing.
Use English Markdown. Include this heading and one table:

## Storyboard

Use a Markdown table whose columns MUST be:
Shot | Timeline | Camera | Move | Character action | Continuity | Comment

Rules:
- Cover every major beat from the user prompt (typically 3-5 shots; duration ~12-24s total unless brief says shorter)
- Timeline as start-end seconds, e.g. 0.0-4.0s — durations must sum coherently
- Camera is shot size + angle, e.g. wide/establishing, medium/eye-level, close-up/eye-level, medium/slow pan
- Move is push/pull/pan/dolly/static and speed
- Character action: who is on screen and what they do THIS shot only (match cast identity locks)
- Continuity: explicit forbids from prior shots (e.g. after a man stands and leaves, later shots MUST NOT reseat him; posture/facing/location locks)
- Comment is the keyframe prompt: subject(s), composition, light, action instant, environment — ready for image gen
- Enhance sparse prompts: crowd, atmosphere, lighting, wardrobe detail — without inventing new lead characters
- Do not invent a new world that contradicts the brief

Do not output storyboard drawings. Do not explain.

Brief:
"""

_MAX_STORYBOARD_SHOTS = 6
_TABLE_SEP_CELL = re.compile(r"^:?-{3,}:?$")


class StoryboardShot(TypedDict):
    shot_no: str
    timeline: str
    camera: str
    move: str
    character_action: str
    scene_change: str
    comment: str


_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "shot_no": ("Shot", "镜号"),
    "timeline": ("Timeline", "时间轴"),
    "camera": ("Camera", "镜头视角", "景别"),
    "move": ("Move", "运镜"),
    "character_action": ("Character action", "Character", "人物变化"),
    # Continuity is preferred; Scene change kept as alias for older tables.
    "scene_change": (
        "Continuity",
        "Scene change",
        "Scene",
        "场景变化",
        "连续性",
    ),
    "comment": ("Comment", "Notes", "注释", "备注", "画面描述", "提示词"),
}
_POSITIONAL_FIELDS = (
    "shot_no",
    "timeline",
    "camera",
    "move",
    "character_action",
    "scene_change",  # Continuity column lands here positionally
    "comment",
)


def _split_markdown_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _empty_shot() -> StoryboardShot:
    return {
        "shot_no": "",
        "timeline": "",
        "camera": "",
        "move": "",
        "character_action": "",
        "scene_change": "",
        "comment": "",
    }


def _header_field_map(cells: list[str]) -> dict[int, str] | None:
    mapping: dict[int, str] = {}
    for index, cell in enumerate(cells):
        name = cell.strip()
        if not name:
            continue
        for field, aliases in _FIELD_ALIASES.items():
            if any(alias == name or alias in name for alias in aliases):
                mapping[index] = field
                break
    if "shot_no" in mapping.values() or "timeline" in mapping.values():
        return mapping
    return None


def _shot_from_cells(
    cells: list[str],
    field_map: dict[int, str] | None,
    fallback_no: int,
) -> StoryboardShot | None:
    shot = _empty_shot()
    if field_map:
        for index, field in field_map.items():
            if index < len(cells):
                shot[field] = cells[index]
    else:
        for index, field in enumerate(_POSITIONAL_FIELDS):
            if index < len(cells):
                shot[field] = cells[index]
    if not shot["shot_no"]:
        shot["shot_no"] = str(fallback_no)
    if not re.match(r"^\d+", shot["shot_no"]) and len(cells) < 4:
        return None
    return shot


def parse_storyboard_shots(text: str) -> list[StoryboardShot]:
    """Read shot rows from the storyboard markdown table."""
    shots: list[StoryboardShot] = []
    header_seen = False
    field_map: dict[int, str] | None = None
    for line in (text or "").splitlines():
        if "|" not in line:
            continue
        cells = _split_markdown_row(line)
        if not cells or not any(cells):
            continue
        if all(_TABLE_SEP_CELL.match(cell) for cell in cells if cell):
            continue
        joined = "".join(cells)
        header_hit = any(
            marker.casefold() in joined.casefold()
            for marker in ("Shot", "Timeline", "镜号", "时间轴")
        )
        if not header_seen and header_hit:
            header_seen = True
            field_map = _header_field_map(cells)
            continue
        if not header_seen:
            continue
        shot = _shot_from_cells(cells, field_map, len(shots) + 1)
        if shot is None:
            continue
        shots.append(shot)
        if len(shots) >= _MAX_STORYBOARD_SHOTS:
            break
    return shots


def storyboard_shots_or_default(text: str, prompt: str = "") -> list[StoryboardShot]:
    shots = parse_storyboard_shots(text)
    if shots:
        return shots
    return parse_storyboard_shots(fallback_storyboard(prompt))


def shot_generate_prompt(shot: StoryboardShot) -> str:
    """Turn one storyboard row into the keyframe/clip generate prompt."""
    comment = str(shot.get("comment") or "").strip()
    if comment:
        return comment
    parts: list[str] = []
    timeline = str(shot.get("timeline") or "").strip()
    if timeline:
        parts.append(f"Timeline {timeline}")
    for label, key in (
        ("Camera", "camera"),
        ("Camera move", "move"),
        ("Character action", "character_action"),
        ("Scene change", "scene_change"),
    ):
        value = str(shot.get(key) or "").strip()
        if value:
            parts.append(f"{label} {value}")
    return "; ".join(parts)


def fallback_brief(prompt: str) -> str:
    return (
        "# Brief\n\n"
        f"**User prompt (verbatim intent):** {prompt}\n\n"
        f"**Logline:** {prompt[:280]}\n\n"
        "- Cast: lock face/hair/body/costume per named character (solo sheets)\n"
        "- Setting: follow the user description; keep architecture/lighting consistent\n"
        "- Continuity: time-coherent actions (no reseating someone who already left)\n"
        "- Duration: ~12-20 seconds unless prompt says otherwise\n"
        "- Visual: cinematic, coherent lighting, no subtitles/watermarks\n"
    )


def fallback_storyboard(prompt: str) -> str:
    return (
        "# Storyboard\n\n"
        "## Storyboard\n\n"
        f"| {_STORYBOARD_COLUMNS} |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        f"| 1 | 0.0-4.0s | wide / establishing | slow push | establish subjects from prompt | hold geography | {prompt[:120]} |\n"
        "| 2 | 4.0-8.0s | medium / eye-level | hold | main action continues | no reset of prior poses | medium eye-level follow-through |\n"
        "| 3 | 8.0-12.0s | close-up / eye-level | slow pan | reaction beat | prior exits stay gone | emotional close-up reaction |\n"
    )


class BriefNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        cfg = node_config(node)
        prewritten = str(cfg.get("prewritten") or "").strip()
        if prewritten or cfg.get("skip_llm"):
            text = prewritten or fallback_brief(graph_prompt(ctx.graph, node))
            path = write_workspace_text(f"designer_brief_{ctx.run_id}_{ctx.node_id}", text)
            return NodeResult(
                output_ref=file_output_ref(path, kind=NODE_TYPE_TEXT, mime_type="text/markdown"),
                message="brief written (supervisor prewrite)",
            )
        source = graph_prompt(ctx.graph, node)
        skill = str(cfg.get("skill_excerpt") or "")
        audio = (ctx.graph.get("metadata") or {}).get("audio_intent") or {}
        instruction = _BRIEF_INSTRUCTION
        if skill:
            instruction = skill[:2500] + "\n\n" + instruction
        if audio:
            instruction += f"\nAudio policy: {audio}\n"
        try:
            text = await complete_designer_node_text(
                instruction + source,
                delegate=str(cfg.get("delegate") or ""),
            )
        except Exception:
            text = ""
        if not text:
            text = (
                str(cfg.get("draft_prewritten") or "").strip()
                or fallback_brief(source)
            )
        path = write_workspace_text(f"designer_brief_{ctx.run_id}_{ctx.node_id}", text)
        return NodeResult(
            output_ref=file_output_ref(path, kind=NODE_TYPE_TEXT, mime_type="text/markdown"),
            message="brief written",
        )


def _storyboard_alignment_context(ctx: NodeExecutionContext) -> str:
    parts: list[str] = []
    character_notes = (
        collaboration_card(ctx.run_id, NODE_ROLE_CHARACTER_DESIGN)
        or role_output_text(ctx, NODE_ROLE_CHARACTER_DESIGN)
    )
    scene_notes = (
        collaboration_card(ctx.run_id, NODE_ROLE_SCENE)
        or role_output_text(ctx, NODE_ROLE_SCENE)
    )
    if character_notes:
        parts.append("Character sheet / notes (character action must match):\n" + character_notes)
    elif role_output_image_path(ctx, NODE_ROLE_CHARACTER_DESIGN) is not None:
        parts.append("A character sheet exists. Character action must match that look, costume, and materials. Do not invent a new character.")
    if scene_notes:
        parts.append("Scene sheet / notes (scene change must match):\n" + scene_notes)
    elif role_output_image_path(ctx, NODE_ROLE_SCENE) is not None:
        parts.append("A scene sheet exists. Scene change must match that space, weather, and lighting. Do not change location.")
    return "\n\n".join(parts)


class StoryboardNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        import asyncio

        cfg = node_config(node)
        prewritten = str(cfg.get("prewritten") or "").strip()
        planned = cfg.get("planned_shots")
        # Supervisor-authored storyboard: never block on LLM / image understanding.
        if prewritten or cfg.get("skip_llm"):
            text = prewritten
            if not text and isinstance(planned, list) and planned:
                rows = [
                    f"| {_STORYBOARD_COLUMNS} |",
                    "| --- | --- | --- | --- | --- | --- | --- |",
                ]
                for i, shot in enumerate(planned[:_MAX_STORYBOARD_SHOTS], start=1):
                    if not isinstance(shot, dict):
                        continue
                    lock = shot.get("continuity_lock") if isinstance(shot.get("continuity_lock"), dict) else {}
                    cont = "; ".join(f"{k}={v}" for k, v in list(lock.items())[:3]) or "hold continuity"
                    rows.append(
                        "| {shot} | {tl} | {cam} | static | {action} | {cont} | {kf} |".format(
                            shot=i,
                            tl=str(shot.get("timeline") or f"{(i-1)*4:.1f}-{i*4:.1f}s"),
                            cam=str(shot.get("camera") or "medium / eye-level"),
                            action=str(shot.get("action") or shot.get("title") or "")[:120],
                            cont=cont[:120],
                            kf=str(shot.get("keyframe_prompt") or shot.get("action") or "")[:160],
                        )
                    )
                text = "## Storyboard\n\n" + "\n".join(rows) + "\n"
            if not text:
                text = fallback_storyboard(
                    role_output_text(ctx, NODE_ROLE_BRIEF) or graph_prompt(ctx.graph, node)
                )
            path = write_workspace_text(f"designer_storyboard_{ctx.run_id}_{ctx.node_id}", text)
            return NodeResult(
                output_ref=file_output_ref(path, kind=NODE_TYPE_TABLE, mime_type="text/markdown"),
                message="storyboard written (supervisor prewrite)",
            )

        source = role_output_text(ctx, NODE_ROLE_BRIEF) or graph_prompt(ctx.graph, node)
        alignment = _storyboard_alignment_context(ctx)
        planned_block = ""
        if isinstance(planned, list) and planned:
            import json as _json

            planned_block = (
                "\n\nPlanned shots from supervisor casting (honor these beats; expand camera detail):\n"
                + _json.dumps(planned, ensure_ascii=False, indent=2)
                + "\n"
            )
        prompt = _STORYBOARD_INSTRUCTION + source + planned_block
        if alignment:
            prompt = f"{prompt}\n\n{alignment}\n"
        text = ""
        try:
            text = await asyncio.wait_for(
                complete_designer_node_text(
                    prompt,
                    delegate=str(cfg.get("delegate") or ""),
                    max_tokens=1600,
                ),
                timeout=45.0,
            )
        except Exception:
            text = ""
        if not text:
            text = str(cfg.get("draft_prewritten") or "").strip() or fallback_storyboard(source)
        path = write_workspace_text(f"designer_storyboard_{ctx.run_id}_{ctx.node_id}", text)
        return NodeResult(
            output_ref=file_output_ref(path, kind=NODE_TYPE_TABLE, mime_type="text/markdown"),
            message="storyboard table written",
        )
