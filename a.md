# Designer pipeline (`a.md`)

AI-first, **forward-only** quality path: prompt → film (real images/video/audio).
**No per-shot keyframes** — clips are the shots.

Full reproduction: **`DETAIL.md`**. Module map:
`jiuwenswarm/server/runtime/designer/README.md`.
Per-node pipelines: `jiuwenswarm/server/runtime/designer/docs/INDEX.md`.

## Frontend ↔ backend

| Step | Where |
|------|--------|
| Bootstrap / Play | Web UI `designerGraphClient.bootstrap` / `startRun` → RPC |
| Gateway | `designer_adapter.py` builds/saves graph, starts `GraphExecutor` |
| Graph build | `smart_graph.build_smart_video_graph` after `script_analysis.analyze_creative_brief` |
| Play loop | `executor._execute_wave_run`: Supervisor → Manager → ready-queue leaves → dual raters |
| Live updates | WebSocket run events; media under `~/.jiuwenswarm/agent/workspace/` |

Heuristics run **only** when `llm_available()` is false.

## Quality DAG (v5 — no keyframes)

```
Brief (Supervisor + Manager gate)
  → Storyboard (Supervisor + Manager gate; start_state → beat → end_state)
  → Solo character sheets (identity / wardrobe)
  → Scene specs per setting_id (environment only)
  → Clips as shots (R2V: on-screen solos + scene specs;
       story-form prompt from this row’s start/end;
       same-setting clips concurrent when deps ready)
  → Optional speech / music
  → Compose (hard-wait usable clip/audio mp4 → ffmpeg concat)
```

Bootstrap stamp: `designer.graph.smart_video.quality.v5` with
`scene_continuity_mode=scene_card_plus_clip_shots`.
There are **no** `n_frame_*` nodes. There are **no** clip→clip edges.

## Orchestration (one pass)

1. Supervisor analysis + plan (storyboard owns continuity)
2. Manager validates / prunes / stamps locks
3. Ready-queue leaves (max concurrency **3**); clips fire when *their* deps are ready
4. Manager rewrites clip prompts to story form (omit exited cast)
5. Compose only when all predecessor media exists on disk
6. Dual raters (write-only feedback)

## Agents

| Agent | Does |
|-------|------|
| Supervisor | Brief, storyboard (+ start/end), shot count, plan |
| Manager | Approve, prune, leaf prompt rewrite, ratings |
| Leaf DeepAgent | Image / video / ffmpeg tools per node |
| Handler | Deterministic materialize when delegated |

## Continuity rules (general)

- Continuity authority = storyboard `start_state` / `end_state` + occupancy (not prior Wan prose)
- Same `setting_id` → same architecture / plate; never cross rooms in one clip
- Pose/placement from **this** storyboard row — no sit/stand hardcodes on the video call
- Exited cast omitted from later same-setting prompts until returned on_screen
- Video API: positive story form only (no forbid / examples / lock banners)
- Clip deps = storyboard + **on-screen** solos + scene only
