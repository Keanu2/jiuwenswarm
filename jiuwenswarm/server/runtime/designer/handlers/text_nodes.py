# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Text intermediate handlers: brief, storyboard (includes camera script)."""

from __future__ import annotations

import logging
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
from jiuwenswarm.server.runtime.designer.handlers import common as handler_io
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

logger = logging.getLogger(__name__)

_DEFAULT_DURATION_SECONDS = 5
_MIN_DURATION_SECONDS = 2
_MAX_DURATION_SECONDS = 30
_DURATION_SECONDS = re.compile(
    r"(?i)(?:duration\s*[：:]\s*)?(\d{1,3}(?:\.\d+)?)\s*-?\s*"
    r"(?:seconds?|secs?|秒|(?<![a-z])s(?![a-z]))"
)

_BRIEF_INSTRUCTION = """Turn the request below into an executable short-film Brief.
Write English Markdown with: one-line logline, visual style, main character/subject, setting, and duration in seconds.
Read the duration from the user request (for example 10-second, 10 seconds, 10秒). If the request does not say how long, use 5 seconds.
Only list constraints that are in the request. Do not add extra bans or a different setting.
Output Markdown only, no explanation.

Request:
"""

_STORYBOARD_COLUMNS = "Shot | Timeline | Camera | Move | Character action | Scene change | Comment"

_STORYBOARD_REQUIREMENTS = f"""Storyboard requirements:
- Write a camera-script table from the Brief below. This is not a drawing
- English Markdown only, with heading ## Storyboard and exactly one table
- Columns MUST be: {_STORYBOARD_COLUMNS}
- Read the film duration from the Brief Duration field or the original user request. Do not assume 5 seconds unless neither mentions duration
- Cover that full duration with 2-6 shots and no timeline gaps
- Timeline as start-end seconds, e.g. 0.0-2.0s
- Camera is shot size + angle, e.g. close-up / over-shoulder, medium / eye-level
- Move is push/pull/pan/dolly/static and speed
- Character action must match the Brief: same subject, look, costume; only write motion, facing, and enter/exit for this shot
- Scene change must match the Brief: same place, weather, lighting. Never invent a different world
- Comment is the keyframe prompt for this shot: subject, composition, light, action instant, environment from the Brief. Write English that can go straight to image generation. Do not only repeat other columns
- Every row must be about the Brief's subject and location. Do not use unrelated stock locations
- Do not invent a new character or a new world
- Table cells must be a single line: no newlines, no Markdown headings inside a cell
- Do not output storyboard drawings. Do not explain
"""


def build_storyboard_llm_prompt(
    brief: str, alignment: str = "", user_request: str = ""
) -> str:
    """Chat-API prompt: storyboard rules plus the full Brief (and optional sheets)."""
    parts = [
        _STORYBOARD_REQUIREMENTS.strip(),
        "",
        "## Brief",
        (brief or "").strip() or "(missing Brief — use only the user request if it appears above)",
    ]
    request = (user_request or "").strip()
    if request and request not in (brief or ""):
        parts.extend(["", "## User request", request])
    extra = (alignment or "").strip()
    if extra:
        parts.extend(["", "## Character and scene alignment", extra])
    parts.extend(
        [
            "",
            "Write the storyboard table now from the Brief. Follow the storyboard requirements.",
        ]
    )
    return "\n".join(parts)

_MAX_STORYBOARD_SHOTS = 6
_TABLE_SEP_CELL = re.compile(r"^:?-{3,}:?$")
_MARKDOWN_FENCE = re.compile(r"```(?:markdown|md)?\s*\n([\s\S]*?)```", re.IGNORECASE)


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
    "scene_change": ("Scene change", "Scene", "场景变化"),
    "comment": ("Comment", "Notes", "注释", "备注", "画面描述", "提示词"),
}
_POSITIONAL_FIELDS = (
    "shot_no",
    "timeline",
    "camera",
    "move",
    "character_action",
    "scene_change",
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


def unwrap_storyboard_markdown(text: str) -> str:
    """Use fenced markdown if the model wrapped the table in a code block."""
    raw = (text or "").strip()
    if not raw:
        return ""
    match = _MARKDOWN_FENCE.search(raw)
    if match:
        inner = match.group(1).strip()
        if "|" in inner:
            return inner
    return raw


def parse_storyboard_shots(text: str) -> list[StoryboardShot]:
    """Read shot rows from the storyboard markdown table."""
    shots: list[StoryboardShot] = []
    header_seen = False
    field_map: dict[int, str] | None = None
    for line in unwrap_storyboard_markdown(text).splitlines():
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


def _markdown_table_cell(text: str, limit: int = 180) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def brief_duration_seconds(
    source: str, default: int = _DEFAULT_DURATION_SECONDS
) -> int:
    """Seconds from a Brief Duration field, else the first duration in the request."""
    text = source or ""
    duration_line = re.search(
        r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?duration(?:\*\*)?\s*[：:].+$",
        text,
    )
    blob = duration_line.group(0) if duration_line else text
    match = _DURATION_SECONDS.search(blob)
    if match is None and duration_line is not None:
        match = _DURATION_SECONDS.search(text)
    if match is None:
        return default
    try:
        value = float(match.group(1))
    except (TypeError, ValueError):
        return default
    seconds = int(round(value))
    return max(_MIN_DURATION_SECONDS, min(_MAX_DURATION_SECONDS, seconds))


def _format_timeline_seconds(value: float) -> str:
    return f"{value:.1f}"


def brief_logline(source: str) -> str:
    """Pull the usable one-line subject from a Brief markdown or raw prompt."""
    lines = [line.strip() for line in (source or "").splitlines()]
    for line in lines:
        stripped = line.lstrip("-* ").strip()
        if stripped.lower().startswith("logline:"):
            value = stripped.split(":", 1)[-1].strip()
            if value:
                return value
    body = [
        line.lstrip("-* ").strip()
        for line in lines
        if line and not line.startswith("#") and not line.lower().startswith("duration:")
        and not line.lower().startswith("resolution:")
        and not line.lower().startswith("visual:")
    ]
    return " ".join(body).strip() or " ".join(lines).strip() or "short film"


_GENERATE_PREFIX = re.compile(
    r"(?is)^(?:please\s+)?(?:generate|make|create|write|写|生成)\s+"
    r"(?:a\s+|an\s+|一[个条段]?)?(?:\d+p\s+)?(?:video|clip|film|short\s*film|短视频|影片|视频)"
    r"(?:[^,，;；]{0,80})?[,，;；]\s*"
)
_CAM_SPEC = re.compile(r"(?i)\bat least two cams?,?\s*")
_STORY_SPLIT = re.compile(r"(?i)\s+(?:and|then)\s+|，然后|然后|，并")


def brief_story_focus(source: str) -> str:
    """Drop duration/resolution boilerplate so shots follow the Brief's actual action."""
    logline = brief_logline(source)
    focused = _GENERATE_PREFIX.sub("", logline, count=1)
    focused = _CAM_SPEC.sub("", focused)
    focused = " ".join(focused.split()).strip(" ,，")
    return focused or logline


def _story_beats(focus: str) -> tuple[str, str]:
    match = _STORY_SPLIT.search(focus)
    if match and match.start() >= 8:
        left = focus[: match.start()].strip(" ,，")
        right = focus[match.end() :].strip(" ,，")
        if len(left) >= 8 and len(right) >= 8:
            return left, right
    return focus, focus


def fallback_brief(prompt: str) -> str:
    logline = _markdown_table_cell(brief_logline(prompt) or prompt, limit=400)
    duration = brief_duration_seconds(prompt)
    return (
        "# Brief\n\n"
        f"- Logline: {logline}\n"
        f"- Duration: {duration} seconds\n"
        "- Resolution: 480P (dev)\n"
        "- Visual: follow the user description, avoid unrelated elements\n"
    )


_MARKDOWN_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")


def _drop_markdown_tables(text: str) -> str:
    lines = [
        line
        for line in (text or "").splitlines()
        if not _MARKDOWN_TABLE_LINE.match(line) and line.strip().count("|") < 2
    ]
    return "\n".join(lines).strip()


def fallback_storyboard(prompt: str) -> str:
    """Keep fallback shots on the Brief subject; never a stock demo world."""
    focus = brief_story_focus(prompt) or (prompt or "").strip() or "the briefed action"
    beat1, beat2 = _story_beats(focus)
    duration = float(brief_duration_seconds(prompt))
    split = round(duration * 0.4, 1)
    split = min(max(1.0, split), max(1.0, duration - 1.0))
    start = _format_timeline_seconds(0.0)
    mid = _format_timeline_seconds(split)
    end = _format_timeline_seconds(duration)
    shot1 = (
        f"| 1 | {start}-{mid}s | wide / eye-level | slow pan to the main action | "
        f"{_markdown_table_cell(beat1)} | "
        f"{_markdown_table_cell('location from the Brief: ' + beat1)} | "
        f"{_markdown_table_cell('Wide establishing shot: ' + beat1)} |"
    )
    shot2 = (
        f"| 2 | {mid}-{end}s | medium close-up / eye-level | follow then hold | "
        f"{_markdown_table_cell(beat2)} | "
        f"{_markdown_table_cell('same location as shot 1; continue: ' + beat2)} | "
        f"{_markdown_table_cell('Closer coverage: ' + beat2)} |"
    )
    return (
        "# Storyboard\n\n"
        "## Storyboard\n\n"
        f"| {_STORYBOARD_COLUMNS} |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        f"{shot1}\n"
        f"{shot2}\n"
    )


class BriefNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        source = graph_prompt(ctx.graph, node)
        try:
            text = await complete_designer_node_text(
                _BRIEF_INSTRUCTION + source,
                delegate=str(node_config(node).get("delegate") or ""),
            )
        except Exception:
            logger.exception("Designer brief LLM failed; using fallback from the request")
            text = ""
        if not text:
            logger.warning("Designer brief empty; using fallback from the request")
            text = fallback_brief(source)
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
        character_notes = _drop_markdown_tables(character_notes)
    if scene_notes:
        scene_notes = _drop_markdown_tables(scene_notes)
    if character_notes:
        parts.append("Character sheet / notes (character action must match):\n" + character_notes)
    elif role_output_image_path(ctx, NODE_ROLE_CHARACTER_DESIGN) is not None:
        parts.append("A character sheet exists. Character action must match that look, costume, and materials. Do not invent a new character.")
    if scene_notes:
        parts.append("Scene sheet / notes (scene change must match):\n" + scene_notes)
    elif role_output_image_path(ctx, NODE_ROLE_SCENE) is not None:
        parts.append("A scene sheet exists. Scene change must match that space, weather, and lighting. Do not change location.")
    return "\n\n".join(parts)


async def _complete_storyboard_table(prompt: str) -> str:
    """Call the default chat model. Retry once if the reply has no shot table."""
    text = ""
    for attempt in (1, 2):
        try:
            text = unwrap_storyboard_markdown(
                await handler_io.complete_designer_text(prompt, max_tokens=2400)
            )
        except Exception:
            logger.exception(
                "Designer storyboard chat API failed attempt=%s", attempt
            )
            text = ""
        if parse_storyboard_shots(text):
            return text
        logger.warning(
            "Designer storyboard chat API missing a shot table; attempt=%s", attempt
        )
    return text


class StoryboardNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        source = role_output_text(ctx, NODE_ROLE_BRIEF) or graph_prompt(ctx.graph, node)
        alignment = _storyboard_alignment_context(ctx)
        prompt = build_storyboard_llm_prompt(
            source, alignment, user_request=graph_prompt(ctx.graph, node)
        )
        logger.info(
            "Designer storyboard calling chat API brief_chars=%s prompt_chars=%s",
            len(source or ""),
            len(prompt),
        )
        text = await _complete_storyboard_table(prompt)
        if parse_storyboard_shots(text):
            logger.info("Designer storyboard using LLM table from the Brief")
        else:
            logger.warning(
                "Designer storyboard LLM missing a shot table; writing Brief-derived shots"
            )
            text = fallback_storyboard(source)
        try:
            reviewed = await review_storyboard_with_peers(
                text, run_id=ctx.run_id, brief=source
            )
        except Exception:
            reviewed = ""
        if parse_storyboard_shots(unwrap_storyboard_markdown(reviewed)):
            text = unwrap_storyboard_markdown(reviewed)
        path = write_workspace_text(f"designer_storyboard_{ctx.run_id}_{ctx.node_id}", text)
        return NodeResult(
            output_ref=file_output_ref(path, kind=NODE_TYPE_TABLE, mime_type="text/markdown"),
            message="storyboard table written",
        )
