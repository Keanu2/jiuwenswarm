# Designer pipeline (`a.md`)

AI-first, **forward-only** quality path for prompt → film (real images/video/audio).
Full reproduction: **`DETAIL.md`**.

## Frontend ↔ backend

| Step | Where |
|------|--------|
| Bootstrap / Play | Web UI `designerGraphClient.bootstrap` / `startRun` → RPC `designer.graph.bootstrap` / `designer.run.start` |
| Gateway | `designer_adapter.py` (AgentServer) builds/saves graph, starts `DesignerExecutor` |
| Graph build | `smart_graph.build_smart_video_graph` after `script_analysis.analyze_creative_brief` |
| Play loop | `executor._execute_wave_run`: Supervisor → Manager → ready-queue leaves → dual raters |
| Live updates | Run state published to UI via gateway/WebSocket; media under `~/.jiuwenswarm/agent/workspace/` |

Heuristics run **only** when `llm_available()` is false (no Settings chat model).

## Quality DAG (v4)

```
Brief (agent)
  → Storyboard (agent) → Manager storyboard review (once)
  → Solo character sheets (one each, never concatenated)
  → Scene **master plate** (canonical architecture / spatial_lock)
  → Per-shot scene views (**edit/ref from master** — not independent T2I worlds)
  → Keyframes (compose solos + shot scene + master; optional edit prior KF)
  → Clips (I2V from keyframe only — anti-clone; still→mp4 off)
     + Speech / Music (handlers; edged into compose)
  → Film / Compose (concat **all** clips + mux speech/music; real non-empty .mp4)
```

Manager **prunes** unused nodes, rewires spatial edges, protects clips/audio, and keeps
fidelity to prompt / brief / storyboard. Bootstrap: `designer.graph.smart_video.quality.v4`
with `freeze_shot_topology` (no mid-run shot expand).

## Orchestration (one pass, no loops)

1. Supervisor LLM analysis + plan (`spatial_lock`, brief notes, per-node tasks/tools)
2. Manager validates fidelity / identity / continuity / spatial; prune + cohere + agents
3. Ready-queue leaves (max concurrency **3**); after storyboard → manager review once
4. After keyframes → supervisor may adjust clip briefs once
5. End: finalize + dual raters; feedback applied **only** on Run again

Image API: semaphore **2** + RateQuota backoff; ref-upload failures fall back to T2I.

## Media honesty

- `.md` never counts as image/video; primary `output_ref` must be real media for scene/char/frame
- Clip still→mp4 only if `allow_still_clip_fallback` (default **false**)
- Compose requires every shot clip; muxes speech/music (or audible bed)

## Where outputs live

Root: `JIUWENSWARM_DATA_DIR` or `~/.jiuwenswarm`.

| Path | Contents |
|------|----------|
| `agent/workspace/` | Generated media (clips, compose mp4) |
| `agent/designer/graphs\|runs\|feedback/` | Graphs, run state, rater JSON |

## Headless check

```bash
conda run -n new --no-capture-output python scripts/run_church_play_pipeline.py --timeout-sec 2700
```

## If something looks heuristic-only

1. Confirm dotenv + Settings chat model (`llm_available()`).
2. New bootstrap / Play so analysis is `script_analysis_mode=llm`.
3. Check `metadata.ai_agent_pipeline`, `agent_runtime`, `bootstrap` (`…quality.v4`).
