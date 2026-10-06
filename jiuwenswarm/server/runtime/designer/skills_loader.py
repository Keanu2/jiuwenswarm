# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Load Designer scenario / agent / subject skills from designer_catalog_and_skills."""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from jiuwenswarm.server.runtime.designer.paths import skills_dir

logger = logging.getLogger(__name__)

_ROLE_ALIASES: dict[str, str] = {
    "brief": "brief",
    "character": "character",
    "character_design": "character",
    "scene": "scene",
    "storyboard": "storyboard",
    "frame": "frame",
    "keyframe": "frame",
    "clip": "clip",
    "compose": "compose",
    "final": "compose",
    "audio": "audio_bed",
    "music": "music",
    "speech": "speech_tts",
    "tts": "speech_tts",
    "mesh": "mesh",
    "3d": "mesh",
}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("skill read failed %s: %s", path, exc)
        return ""


@lru_cache(maxsize=1)
def _skill_index() -> dict[str, Path]:
    root = skills_dir()
    index: dict[str, Path] = {}
    if not root.is_dir():
        return index
    for path in root.rglob("*.md"):
        key = path.stem.lower()
        index[key] = path
        # also index relative posix without suffix
        rel = path.relative_to(root).with_suffix("").as_posix().lower()
        index[rel] = path
    return index


def load_skill(*keys: str) -> str:
    """Return first matching skill markdown body (without YAML frontmatter if present)."""
    index = _skill_index()
    for key in keys:
        norm = str(key or "").strip().lower().replace("\\", "/")
        if not norm:
            continue
        path = index.get(norm) or index.get(norm.split("/")[-1])
        if path is None:
            continue
        text = _read_text(path)
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                text = parts[2].lstrip("\n")
        return text.strip()
    return ""


def load_scenario_skill(scenario: str) -> str:
    return load_skill(f"scenarios/{scenario}", scenario)


def load_orchestration_skill(role: str) -> str:
    return load_skill(f"orchestration/{role}", role)


def load_agent_skill(role_or_node: str) -> str:
    raw = str(role_or_node or "").strip().lower()
    # n_character / n_clip_1 → character / clip
    if raw.startswith("n_"):
        raw = raw[2:]
    raw = re.sub(r"_\d+$", "", raw)
    alias = _ROLE_ALIASES.get(raw, raw)
    return load_skill(f"agents/{alias}", alias)


def load_subject_skill(subject: str) -> str:
    return load_skill(f"subjects/{subject}", subject)


def detect_subjects(prompt: str) -> list[str]:
    text = (prompt or "").lower()
    found: list[str] = []
    checks = [
        ("human", ("person", "people", "human", "man", "woman", "courier", "actor", "character", "人", "角色", "快递员")),
        ("vehicle", ("car", "truck", "bike", "motorcycle", "vehicle", "车", "汽车")),
        ("product", ("product", "bottle", "phone", "shoe", "packaging", "产品", "包装")),
        ("animal", ("dog", "cat", "bird", "animal", "宠物", "动物")),
        ("architecture", ("building", "interior", "room", "street", "alley", "建筑", "巷", "室内")),
        ("nature", ("forest", "ocean", "mountain", "sky", "nature", "自然", "风景")),
    ]
    for name, kws in checks:
        if any(kw in text for kw in kws):
            found.append(name)
    return found or ["human"]


def detect_audio_intent(prompt: str) -> dict[str, Any]:
    """Infer speech / music / silence policy from the user prompt."""
    text = (prompt or "").lower()
    silent_markers = (
        "no sound",
        "no audio",
        "silent",
        "mute",
        "without sound",
        "without audio",
        "无声",
        "不要声音",
        "不要配音",
        "静音",
        "无配音",
    )
    speech_markers = (
        "speech",
        "voiceover",
        "voice-over",
        "narration",
        "dialogue",
        "tts",
        "spoken",
        "say ",
        "says ",
        "配音",
        "旁白",
        "对白",
        "台词",
        "语音",
    )
    music_markers = (
        "music",
        "bgm",
        "soundtrack",
        "score",
        "song",
        "配乐",
        "音乐",
        "b gm",
    )
    if any(m in text for m in silent_markers):
        return {
            "policy": "silent",
            "include_speech": False,
            "include_music": False,
            "notes": "User requested silence / no audio.",
        }
    include_speech = any(m in text for m in speech_markers)
    include_music = any(m in text for m in music_markers)
    if include_speech and include_music:
        policy = "speech_and_music"
    elif include_speech:
        policy = "speech"
    elif include_music:
        policy = "music"
    else:
        # Video default: optional soft bed unless silent
        policy = "optional_music"
        include_music = True
    return {
        "policy": policy,
        "include_speech": include_speech,
        "include_music": include_music,
        "notes": f"Detected audio policy={policy}",
    }


def skill_bundle_for_graph(
    *,
    scenario: str,
    prompt: str,
    node_roles: list[str] | None = None,
) -> dict[str, Any]:
    subjects = detect_subjects(prompt)
    audio = detect_audio_intent(prompt)
    agent_skills = {
        role: load_agent_skill(role) for role in (node_roles or [])
        if load_agent_skill(role)
    }
    return {
        "scenario": scenario,
        "scenario_skill": load_scenario_skill(scenario),
        "supervisor_skill": load_orchestration_skill("supervisor"),
        "manager_skill": load_orchestration_skill("manager"),
        "agent_skills": agent_skills,
        "subjects": subjects,
        "subject_skills": {s: load_subject_skill(s) for s in subjects},
        "audio": audio,
    }


def attach_skills_metadata(graph: dict[str, Any], prompt: str | None = None) -> dict[str, Any]:
    """Attach task-specific agent skills to nodes; overall skills only on orchestration meta."""
    meta = dict(graph.get("metadata") or {})
    scenario = str(meta.get("scenario") or "video")
    text = prompt or str(graph.get("description") or "")
    roles: list[str] = []
    for node in graph.get("nodes") or []:
        cfg = dict(node.get("config") or {})
        role = str(cfg.get("role") or cfg.get("agent_role") or node.get("id") or "")
        roles.append(role)
        # Leaf agents: only their own agent skill (short). No scenario / supervisor dump.
        skill_text = load_agent_skill(role) or load_agent_skill(str(node.get("id") or ""))
        if skill_text:
            cfg["skill_id"] = _ROLE_ALIASES.get(role, role)
            cfg["skill_excerpt"] = skill_text[:1200]
        else:
            cfg.pop("skill_excerpt", None)
        # Do not attach subject encyclopedia to every node here.
        cfg.pop("scenario_skill_excerpt", None)
        node["config"] = cfg
    bundle = skill_bundle_for_graph(scenario=scenario, prompt=text, node_roles=roles)
    # Overall skills live only on graph metadata for supervisor / manager.
    meta["skills_package"] = "designer_catalog_skills_reports_trajectory"
    meta["scenario_skill_excerpt"] = (bundle.get("scenario_skill") or "")[:4000]
    meta["supervisor_skill_excerpt"] = (bundle.get("supervisor_skill") or "")[:4000]
    meta["manager_skill_excerpt"] = (bundle.get("manager_skill") or "")[:4000]
    meta["subject_keys"] = bundle.get("subjects") or []
    meta["audio_intent"] = bundle.get("audio") or {}
    meta["skill_guided"] = True
    meta["skill_policy"] = (
        "Leaf nodes: agents/<role>.md only. "
        "Supervisor/manager: orchestration + scenario skills in metadata."
    )
    graph["metadata"] = meta
    return graph
