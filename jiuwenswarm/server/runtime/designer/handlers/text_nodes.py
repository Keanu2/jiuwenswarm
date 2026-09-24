# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Text intermediate handlers: brief, storyboard (includes camera script)."""

from __future__ import annotations

import re
from typing import Any, TypedDict

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_ROLE_CLIP,
    NODE_ROLE_FRAME,
    NODE_ROLE_SCENE,
    NODE_TYPE_TABLE,
    NODE_TYPE_TEXT,
    DesignerGraphNode,
    node_config,
    node_pipeline,
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
- Cast (solo identity locks — face, hair, body, FULL costume for EACH character; never concatenate)
- Setting / scene geography, lighting, landmarks, opening blocking (who sits/stands where)
- Language / speech lock (film language; exact lines if the user gave them)
- Consistency gates: character, scene, motion/continuity, camera views covering every beat
- Shot-view coverage list (distinct cameras/angles needed)
- Duration target and per-shot timing budget
- Audio policy (speech vs music)
- Production lock bible (style, axis, occupancy, wardrobe) — copy locks, do not drop them
- What to avoid
Preserve every named character and beat from the user prompt in FULL DETAIL. Output Markdown only.

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
- Character action: FULL DETAIL for THIS shot only — who is on screen, where they sit/stand,
  what they do, wardrobe hold. Match cast identity locks. Consecutive windows concatenate;
  do not restage the whole user prompt from a new camera, and do not strip the row to a
  one-liner that drops blocking/speech.
- Continuity: explicit forbids from prior shots (do not undo a completed beat unless this
  row or the user prompt asks to repeat it; posture/facing/location locks)
- Comment is the keyframe/composed-scene prompt: subjects, composition, light, action instant,
  environment — ready for image gen (composed scene with all opening-cast characters in place)
- Language: keep speech_line exact; empty = silent
- Enhance sparse prompts: crowd, atmosphere, lighting, wardrobe detail — without inventing new lead characters
- Do not invent a new world that contradicts the brief

Do not output storyboard drawings. Do not explain.

Brief:
"""

_MAX_STORYBOARD_SHOTS = 16
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
    """Read shot rows from storyboard markdown (pipe table OR hierarchical ### Shot)."""
    table = _parse_storyboard_table(text)
    if table:
        return table
    return _parse_storyboard_hierarchical(text)


def _parse_storyboard_table(text: str) -> list[StoryboardShot]:
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


_HIER_SHOT_RE = re.compile(
    r"(?im)^###\s*Shot\s+(\d+)\s*[—\-–:]?\s*(.*)$"
)
_HIER_FIELD_RE = re.compile(
    r"(?im)^-\s*(Timeline|Camera|Camera move|Move|Action|Character action|"
    r"Comment|Keyframe|Doing|Speech)\s*:\s*(.*)$"
)


def _parse_storyboard_hierarchical(text: str) -> list[StoryboardShot]:
    """Parse smart_graph hierarchical storyboard (### Shot N / - Action: …)."""
    shots: list[StoryboardShot] = []
    current: StoryboardShot | None = None
    for line in (text or "").splitlines():
        head = _HIER_SHOT_RE.match(line.strip())
        if head:
            if current is not None:
                shots.append(current)
                if len(shots) >= _MAX_STORYBOARD_SHOTS:
                    return shots
            idx = int(head.group(1))
            title = str(head.group(2) or "").strip()
            current = {
                "shot_no": str(idx),
                "timeline": "",
                "camera": "",
                "move": "",
                "character_action": "",
                "scene_change": "",
                "comment": title,
            }
            continue
        if current is None:
            continue
        field = _HIER_FIELD_RE.match(line.strip())
        if not field:
            continue
        key = field.group(1).strip().casefold()
        val = field.group(2).strip()
        if key == "timeline":
            current["timeline"] = val
        elif key == "camera":
            current["camera"] = val
        elif key in {"camera move", "move"}:
            current["move"] = val
        elif key in {"action", "character action", "doing"}:
            # Prefer Action over Doing if both appear; first non-empty wins unless Action.
            if key == "action" or not current.get("character_action"):
                current["character_action"] = val
            if key == "action":
                current["comment"] = val or current.get("comment") or ""
        elif key in {"comment", "keyframe"}:
            current["comment"] = val
        elif key == "speech" and not current.get("character_action"):
            current["character_action"] = val
    if current is not None and len(shots) < _MAX_STORYBOARD_SHOTS:
        shots.append(current)
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


def sync_shot_nodes_from_storyboard_markdown(
    graph: dict[str, Any],
    markdown: str,
) -> list[str]:
    """Refresh frame/clip shot_action + camera from the authored storyboard table.

    Only updates beat text — does not touch identity/occupancy wiring.
    """
    notes: list[str] = []
    shots = parse_storyboard_shots(markdown or "")
    if not shots:
        return notes
    by_index: dict[int, StoryboardShot] = {i: shot for i, shot in enumerate(shots, start=1)}
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        role = str(node_pipeline(node) or "").strip().lower()
        if role not in {NODE_ROLE_FRAME, NODE_ROLE_CLIP, "keyframe"}:
            continue
        cfg = dict(node.get("config") or {})
        idx = int(cfg.get("shot_index") or 0)
        shot = by_index.get(idx)
        if not isinstance(shot, dict):
            continue
        narrative = str(shot.get("comment") or shot.get("character_action") or "").strip()
        camera = str(shot.get("camera") or "").strip()
        timeline = str(shot.get("timeline") or "").strip()
        changed = False
        if narrative:
            cfg["shot_action"] = narrative[:500]
            changed = True
        if camera:
            cfg["camera"] = camera[:120]
            changed = True
        if timeline:
            cfg["timeline"] = timeline[:40]
            changed = True
        if narrative:
            gen = dict(cfg.get("generate") or {}) if isinstance(cfg.get("generate"), dict) else {}
            existing = str(gen.get("prompt") or "")
            lead = (
                f"Film shot {idx} only. Camera {cfg.get('camera') or 'medium / eye-level'}. "
                f"Action: {narrative[:300]}."
            )
            if "Action:" not in existing or narrative[:80] not in existing:
                if existing and any(
                    m in existing.upper()
                    for m in ("SCENE BIBLE", "STAGING LOCK", "OCCUPANCY", "COSTUME")
                ):
                    gen["prompt"] = f"{lead}\n{existing}"[:2000]
                else:
                    gen["prompt"] = lead[:1200]
                cfg["generate"] = gen
                changed = True
        if changed:
            node["config"] = cfg
            notes.append(f"{node.get('id')}: synced from storyboard shot {idx}")
    return notes


_DURATION_FIELD_RE = re.compile(
    r"(?im)^(?:[-*]\s*)?(?:\*\*)?duration(?:\*\*)?\s*:?\s*~?\s*(\d{1,2}(?:\.\d+)?)",
)
_DURATION_INLINE_RE = re.compile(
    r"(\d{1,2}(?:\.\d+)?)\s*-?\s*(?:seconds?|secs?|秒)",
    re.I,
)
_LOGLINE_RE = re.compile(
    r"(?im)^(?:[-*]\s*)?(?:\*\*)?logline(?:\*\*)?\s*:\s*(.+)$",
)


def brief_duration_seconds(text: str, default: int = 5) -> int:
    """Read an explicit duration from a brief or user request."""
    from jiuwenswarm.server.runtime.designer.experiments.clip_shot_scope import (
        requested_film_duration_sec,
    )

    asked = requested_film_duration_sec(text or "")
    if asked is not None:
        return int(asked)
    source = text or ""
    field = _DURATION_FIELD_RE.search(source)
    if field:
        try:
            sec = int(round(float(field.group(1))))
        except (TypeError, ValueError):
            sec = 0
        if 1 <= sec <= 30:
            return sec
    match = _DURATION_INLINE_RE.search(source)
    if match:
        try:
            sec = int(round(float(match.group(1))))
        except (TypeError, ValueError):
            sec = 0
        if 1 <= sec <= 30:
            return sec
    return default


def brief_logline(brief: str) -> str:
    text = brief or ""
    match = _LOGLINE_RE.search(text)
    if match:
        return match.group(1).strip().strip("*").strip()
    match = re.search(r"(?i)\*\*logline:\*\*\s*(.+)", text)
    if match:
        return match.group(1).strip()
    return ""


def brief_story_focus(prompt: str) -> str:
    text = (prompt or "").strip()
    text = re.sub(
        r"^(?:generate|create|make|please\s+(?:make|create))\s+"
        r"(?:a\s+)?(?:\d{3,4}p\s+)?(?:video|film|clip|short)?"
        r"(?:\s+in\s+\d+\s+seconds?)?"
        r"(?:\s*,\s*(?:at least\s+)?(?:two|2)\s+cams?)?"
        r"[,:]?\s*",
        "",
        text,
        flags=re.I,
    )
    return text.strip(" ,.")


def fallback_brief(prompt: str) -> str:
    duration = brief_duration_seconds(prompt)
    focus = brief_story_focus(prompt) or prompt
    return (
        "# Brief\n\n"
        f"**User prompt (verbatim intent):** {prompt}\n\n"
        f"**Logline:** {focus[:280]}\n\n"
        "- Cast: lock face/hair/body/costume per named character (solo sheets)\n"
        "- Setting: follow the user description; keep architecture/lighting consistent\n"
        "- Continuity: time-coherent actions (no reseating someone who already left)\n"
        f"- Duration: {duration} seconds\n"
        "- Visual: cinematic, coherent lighting, no subtitles/watermarks\n"
    )


def fallback_storyboard(prompt: str) -> str:
    from jiuwenswarm.server.runtime.designer.experiments.clip_shot_scope import (
        apply_shot_scope,
        needs_duration_slicing,
    )

    duration = float(brief_duration_seconds(prompt))
    focus = (brief_logline(prompt) or brief_story_focus(prompt) or prompt).strip()[:120]
    if needs_duration_slicing(prompt):
        scoped = apply_shot_scope({"shots": []}, prompt)
        rows = []
        for shot in scoped.get("shots") or []:
            idx = int(shot.get("shot_index") or len(rows) + 1)
            tl = str(shot.get("timeline") or "")
            act = str(shot.get("action") or focus)[:120].replace("|", "/")
            rows.append(
                f"| {idx} | {tl} | medium / eye-level | hold | {act} | hold geography | {act} |"
            )
        if rows:
            return (
                "# Storyboard\n\n"
                "## Storyboard\n\n"
                f"| {_STORYBOARD_COLUMNS} |\n"
                "| --- | --- | --- | --- | --- | --- | --- |\n"
                + "\n".join(rows)
                + "\n"
            )
    if duration <= 10:
        mid = min(4.0, max(2.0, round(duration * 0.4, 1)))
        rows = [
            f"| 1 | 0.0-{mid:.1f}s | wide / establishing | slow push | {focus} | hold geography | {focus} |",
            f"| 2 | {mid:.1f}-{duration:.1f}s | medium / eye-level | hold | {focus} | no reset of prior poses | {focus} |",
        ]
    else:
        t1 = round(duration / 3, 1)
        t2 = round(duration * 2 / 3, 1)
        rows = [
            f"| 1 | 0.0-{t1:.1f}s | wide / establishing | slow push | {focus} | hold geography | {focus} |",
            f"| 2 | {t1:.1f}-{t2:.1f}s | medium / eye-level | hold | {focus} | no reset of prior poses | {focus} |",
            f"| 3 | {t2:.1f}-{duration:.1f}s | close-up / eye-level | slow pan | {focus} | prior exits stay gone | {focus} |",
        ]
    return (
        "# Storyboard\n\n"
        "## Storyboard\n\n"
        f"| {_STORYBOARD_COLUMNS} |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        + "\n".join(rows)
        + "\n"
    )


def _stamp_bible_on_text(text: str, ctx: NodeExecutionContext) -> str:
    try:
        from jiuwenswarm.server.runtime.designer.experiments.production_bible import (
            append_bible_to_markdown,
            build_production_bible,
        )

        meta = ctx.graph.get("metadata") if isinstance(ctx.graph.get("metadata"), dict) else {}
        bible = str(meta.get("production_bible") or "").strip()
        if not bible:
            analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
            bible = build_production_bible(
                analysis,
                user_prompt=str(ctx.graph.get("description") or ""),
            )
        return append_bible_to_markdown(text, bible)
    except Exception:  # noqa: BLE001
        return text


class BriefNodeHandler:
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        cfg = node_config(node)
        prewritten = str(cfg.get("prewritten") or "").strip()
        if prewritten or cfg.get("skip_llm"):
            text = prewritten or fallback_brief(graph_prompt(ctx.graph, node))
            text = _stamp_bible_on_text(text, ctx)
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
        text = _stamp_bible_on_text(text, ctx)
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
            sync_shot_nodes_from_storyboard_markdown(ctx.graph, text)
            text = _stamp_bible_on_text(text, ctx)
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
                    max_tokens=16384,
                ),
                timeout=45.0,
            )
        except Exception:
            text = ""
        if not text:
            text = str(cfg.get("draft_prewritten") or "").strip() or fallback_storyboard(source)
        sync_shot_nodes_from_storyboard_markdown(ctx.graph, text)
        text = _stamp_bible_on_text(text, ctx)
        path = write_workspace_text(f"designer_storyboard_{ctx.run_id}_{ctx.node_id}", text)
        return NodeResult(
            output_ref=file_output_ref(path, kind=NODE_TYPE_TABLE, mime_type="text/markdown"),
            message="storyboard table written",
        )
