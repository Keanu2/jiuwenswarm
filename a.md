# Designer pipeline (`a.md`)

AI-first, **forward-only** quality path for prompt → film (real images/video/audio).

## Frontend ↔ backend

| Step | Where |
|------|--------|
| Bootstrap / Play | Web UI `designerGraphClient.bootstrap` / `startRun` → RPC `designer.graph.bootstrap` / `designer.run.start` |
| Gateway | `designer_adapter.py` (AgentServer) builds/saves graph, starts `DesignerExecutor` |
| Graph build | `smart_graph.build_smart_video_graph` after `script_analysis.analyze_creative_brief` |
| Play loop | `executor._execute_wave_run`: Supervisor → Manager → ready-queue leaves → dual raters |
| Live updates | Run state published to UI via gateway/WebSocket; media under `~/.jiuwenswarm/agent/workspace/` |

Heuristics run **only** when `llm_available()` is false (no Settings chat model).

## Quality DAG (v3)

```
Brief (agent)
  → Storyboard (agent) → Manager storyboard review (once)
  → Solo character sheets (one each, never concatenated)
     + Per-shot scene views (environment only)
  → Keyframes (compose solos + that shot’s scene; optional edit prior KF)
  → Clips (real I2V; still→mp4 disabled by default)
     + Speech / Music (handlers until backends; always edge into compose)
  → Film / Compose (ffmpeg; must emit real non-empty .mp4)
```

Manager **prunes** any node that cannot reach `n_compose`. Compose / clips refuse markdown stubs as video.

## Orchestration (one pass, no loops)

1. Supervisor LLM analysis + plan (brief notes, per-node tasks/tools/models)
2. Manager validates fidelity, identity/continuity, enforces agents+tools, prunes orphans
3. Ready-queue leaves (max concurrency **3**); after storyboard completes → manager continuity/enhance once
4. After keyframes → supervisor may adjust clip briefs once
5. End: supervisor finalize + manager review + **two rater agents**; aggregate JSON under `designer/feedback/` and `runs/*.report` — applied **only** on Run again (`use_prior_feedback`)

Image API calls are further capped (semaphore **2**) with RateQuota backoff. See **`DETAIL.md`** for full reproduction.

## Media honesty

- `.md` / text never counts as image or video (`node_agent._ref_media_family`)
- Clip still→mp4 only if `allow_still_clip_fallback` (default **false**)
- Compose raises if output is missing / not `.mp4` / tiny file

## Where outputs live

Root: `JIUWENSWARM_DATA_DIR` or `~/.jiuwenswarm`.

| Path | Contents |
|------|----------|
| `agent/workspace/` | Generated media (clips, compose mp4) |
| `agent/designer/graphs\|runs\|feedback/` | Graphs, run state, rater JSON |
| `designer_catalog_skills_reports_trajectory/` | Trajectory / eval reports |

## If something looks heuristic-only

1. Confirm dotenv + Settings chat model (`llm_available()`).
2. New bootstrap / Play so analysis is `script_analysis_mode=llm`.
3. Check `metadata.ai_agent_pipeline`, `agent_runtime`, `bootstrap` (`…quality.v3`).
