---
name: designer-scenario-video
description: Guide video graph composition and shot pipeline (R2V shots, optional speech/music/silence).
---

# Designer Video Scenario Skill

## Goal
Compose a short cinematic pipeline: Brief → Character → Scene → Storyboard → Clips (R2V) → Compose, with optional speech/music.

## Graph creation rules
1. Always keep a Brief agent first.
2. Character and Scene sheets before Storyboard when subjects/places matter.
3. Storyboard must emit timed shots with camera, action, and shot prompts.
4. Each shot is an R2V shot from on-screen solos + scene specs (no per-shot keyframe required).
5. Compose/final stitches clips; honor audio policy from the brief.
6. Named director styles live in `metadata.video_style` (e.g. `final_frame_reverse` = reference still is the LAST 1s endpoint; reverse-form the action; see `skills/styles/`).

## Audio policy
- If user says **no sound / silent / mute / 无声**: set `audio_intent.policy=silent`; do not add speech or music nodes; tell clip/compose agents to avoid implied dialogue.
- If user asks for **speech / voiceover / narration / 配音**: add Speech/TTS agent after storyboard; feed script into mix.
- If user asks for **music / BGM / 配乐**: add Music/bed agent; duck under speech if both exist.
- Default for unspecified video: optional soft bed, no forced dialogue.

## Model capabilities to exploit
- Image: t2i and i2i/editing (character consistency).
- Video: reference-to-video (solos + scene specs).
- Audio/speech: TTS when speech is requested.

## Quality bar
Cinematic lighting, consistent identity, readable action, configured resolution clips, coherent continuity across shots.
