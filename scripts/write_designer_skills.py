# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One-shot writer for designer_catalog_skills_reports_trajectory/skills markdown packs."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "designer_catalog_skills_reports_trajectory" / "skills"

SCENARIOS = {
    "video": """---
name: designer-scenario-video
description: Guide video graph composition and shot pipeline (first-frame I2V, optional speech/music/silence).
---

# Designer Video Scenario Skill

## Goal
Compose a short cinematic pipeline: Brief → Character → Scene → Storyboard → Keyframes → Clips (I2V/R2V) → Compose, with optional speech/music.

## Graph creation rules
1. Always keep a Brief agent first.
2. Character and Scene sheets before Storyboard when subjects/places matter.
3. Storyboard must emit timed shots with camera, action, and keyframe prompts.
4. Each shot needs a Keyframe (first frame) then a Clip that **consumes that first frame** for image-to-video when the model supports it.
5. Compose/final stitches clips; honor audio policy from the brief.

## Audio policy
- If user says **no sound / silent / mute / 无声**: set `audio_intent.policy=silent`; do not add speech or music nodes; tell clip/compose agents to avoid implied dialogue.
- If user asks for **speech / voiceover / narration / 配音**: add Speech/TTS agent after storyboard; feed script into mix.
- If user asks for **music / BGM / 配乐**: add Music/bed agent; duck under speech if both exist.
- Default for unspecified video: optional soft bed, no forced dialogue.

## Model capabilities to exploit
- Image: t2i and i2i/editing (character consistency).
- Video: reference-to-video and **first-frame / image-to-video**.
- Audio/speech: TTS when speech is requested.

## Quality bar
Cinematic lighting, consistent identity, readable action, configured resolution clips, coherent continuity across shots.
""",
    "image": """---
name: designer-scenario-image
description: Still-image / poster / illustration pipeline.
---

# Image Scenario Skill
Brief → concept → generate/edit → refine → final still.
Prefer 1K square unless user requests another aspect. Use subject guides for humans/products/vehicles.
""",
    "music": """---
name: designer-scenario-music
description: Music / BGM generation pipeline.
---

# Music Scenario Skill
Brief → motif → arrangement → mix → final audio.
Respect duration, mood, and silence gaps if marked.
""",
    "speech": """---
name: designer-scenario-speech
description: Speech / podcast / voiceover pipeline.
---

# Speech Scenario Skill
Script → voice select → TTS → denoise → bed → mix → chapters/final.
If user says no music bed, keep speech only.
""",
    "3d": """---
name: designer-scenario-3d
description: Mesh / texture / turntable style pipeline.
---

# 3D Scenario Skill
Brief → blocking mesh → materials → lighting → preview → export.
Preserve real-world scale and subject aspect conventions.
""",
    "multimodal": """---
name: designer-scenario-multimodal
description: Cross-modal package spanning image/video/audio/3d as needed.
---

# Multimodal Scenario Skill
Detect modalities from prompt; fan out to modality agents; sync via supervisor; assemble final package.
""",
}

ORCH = {
    "supervisor": """---
name: designer-supervisor
description: Plans node directives, model choice, audio policy, and graph redesign on rerun.
---

# Supervisor Skill

You coordinate Designer agents.

## Planning
- Read scenario skill + prior feedback/trajectory.
- For each node set optimize_for, preferred_model, and a concrete task.
- Enforce audio policy (silent / speech / music).
- Prefer first-frame I2V for clips when keyframes exist.

## Rerun / redesign
- When prior ratings are low, redesign weak nodes or reorder edges.
- Pass manager recommendations into node directives.

## Rating
Score each agent 0–10 with actionable suggestions.
""",
    "manager": """---
name: designer-manager
description: Reviews run quality and produces improvement plan for next run.
---

# Manager Skill

After a run:
1. Inspect trajectory timings and tool usage.
2. Rate artifacts 0–10.
3. Write `improvement_plan` and per-node recommendations for the next Run.
4. If speech was requested but missing, force speech nodes next run; if silent was requested but audio leaked, flag compose.
""",
}

AGENTS = {
    "brief": """# Brief Agent Skill
Turn user intent into an executable brief: logline, visual style, subjects, setting, duration, audio policy (speech/music/silent), constraints.
Detect subjects (human/vehicle/product/...) and note aspect guidance.
""",
    "character": """# Character Agent Skill
Produce a consistent character sheet. Apply subject guides (esp. human aspect ratios, costume, materials). Prefer identity-locking references for later i2i / I2V.
""",
    "scene": """# Scene Agent Skill
Establish place, weather, lighting, props. Keep continuity with brief. Provide references usable by keyframe/clip agents.
""",
    "storyboard": """# Storyboard Agent Skill
Emit a timed camera table. Each shot needs: timeline, camera, move, action, scene change, and a keyframe prompt. Align actions to character sheet and place to scene sheet.
""",
    "frame": """# Keyframe / First-Frame Agent Skill
Generate the still that will seed I2V. Match storyboard comment + character/scene continuity. Prefer readable silhouette and strong composition for motion start.
""",
    "clip": """# Clip Agent Skill
Generate shot video. Prefer **first-frame I2V** when a keyframe exists; otherwise R2V/T2V. Keep duration short. Honor silent policy (no implied dialogue) or leave room for later speech mix.
""",
    "compose": """# Compose / Final Agent Skill
Stitch clips, normalize resolution, apply audio mix policy: silent → no tracks; speech → voiceover; music → bed; both → duck music under speech.
""",
    "audio_bed": """# Audio Bed Agent Skill
Create non-vocal bed/SFX matching mood. Keep headroom for speech. Skip entirely when policy=silent.
""",
    "speech_tts": """# Speech / TTS Agent Skill
Write or refine spoken lines, choose voice, synthesize speech. Sync timing to storyboard. Skip when policy=silent or user forbids speech.
""",
    "music": """# Music Agent Skill
Compose motif/BGM for the requested mood and duration. Respect silent policy.
""",
    "mesh": """# Mesh / 3D Agent Skill
Block real-world scale assets; apply subject aspect conventions; prepare preview-friendly outputs.
""",
}

SUBJECTS = {
    "human": """# Human Subject Guide
- Portrait/character sheets: prefer 3:4 or 2:3 vertical; full-body turnaround 9:16 or 2:3.
- Keep head~1/7–1/8 body for adult; consistent eye line; avoid warped limbs.
- Costume/materials must persist across keyframes and clips.
""",
    "vehicle": """# Vehicle Subject Guide
- Side profile ~16:9; 3/4 front hero ~3:2; keep wheel/body proportions realistic.
- Preserve brand-agnostic silhouette consistency across shots.
""",
    "product": """# Product Subject Guide
- Hero packshot 1:1 or 4:5; floating product with soft reflections.
- Label text legible; consistent SKU colors for i2i edits.
""",
    "animal": """# Animal Subject Guide
- Match species proportions; fur/feather detail; eye catchlights.
- Prefer 3:2 for action; 1:1 for portrait cuteness.
""",
    "architecture": """# Architecture / Place Guide
- Establishing shots 16:9 or 2.39:1; interiors avoid extreme vertical stretch.
- Keep vanishing points stable across storyboard continuity.
""",
    "nature": """# Nature Guide
- Landscapes 16:9; macro details 1:1; weather/light continuity across clips.
""",
}


def write_map(subdir: str, mapping: dict[str, str]) -> None:
    directory = ROOT / subdir
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in mapping.items():
        (directory / f"{name}.md").write_text(body.strip() + "\n", encoding="utf-8")


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    write_map("scenarios", SCENARIOS)
    write_map("orchestration", ORCH)
    write_map("agents", AGENTS)
    write_map("subjects", SUBJECTS)
    index = {
        "schema_version": "designer-skills-index.v1",
        "scenarios": sorted(SCENARIOS),
        "agents": sorted(AGENTS),
        "subjects": sorted(SUBJECTS),
        "orchestration": sorted(ORCH),
    }
    (ROOT / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("skills written", sum(1 for _ in ROOT.rglob("*.md")))


if __name__ == "__main__":
    main()
