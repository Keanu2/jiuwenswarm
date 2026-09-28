# Designer runtime — module map

Package: `jiuwenswarm/server/runtime/designer/`

AI-first **prompt → film** path with **no per-shot keyframes**. Clips are the shots.
Scene specs + on-screen character solos + R2V (Wan / Seedance / MiniMax).
**Continuity authority:** storyboard per-shot `start_state` → beat → `end_state`.
Same-setting clips run **concurrently** once storyboard + needed solos + scene are ready.

See repo [`README.md`](../../../../../README.md) and [`DETAIL.md`](../../../../../DETAIL.md).

**Per-node / overseer pipeline docs:** [`docs/INDEX.md`](./docs/INDEX.md)
([brief](./docs/BRIEF.md) · [storyboard](./docs/STORYBOARD.md) · [character](./docs/CHARACTER.md) · [scene](./docs/SCENE.md) · [clip](./docs/CLIP.md) · [orchestrators](./docs/ORCHESTRATORS.md)).

---

## End-to-end call chain

```
UI Play
  → designer_adapter.start_run
  → GraphExecutor._execute_wave_run
       → SupervisorAgent.plan
       → ManagerAgent.validate_plan / review_leaf_media_prompt
       → NodeAgentHost (or handler) per ready node
       → handlers/{image,clip,compose,audio,text}_nodes
       → Manager dual_rate_final
```

Bootstrap (before Play):

```
designer.graph.bootstrap
  → script_analysis.analyze_creative_brief
  → smart_graph.build_smart_video_graph
  → Supervisor brief + storyboard (optional on bootstrap)
```

---

## Core files

| File | Responsibility |
|------|----------------|
| `smart_graph.py` | Build quality.v5 DAG: brief → storyboard → solos → empty scenes → clips → compose (**no clip→clip**) |
| `script_analysis.py` | LLM cast, scenes, shots, occupancy (`heuristic_analysis` is test-only) |
| `orchestration.py` | Supervisor / Manager; storyboard start/end authoring; leaf prompt rewrite; ratings |
| `executor.py` | Ready-queue scheduler (concurrency ≤3); storyboard→clip sync; compose hard-wait |
| `node_agent.py` | Leaf DeepAgent tools (`call_image_model` → `generate_designer_image`, `call_video_model`, `ffmpeg_compose`) |
| `node_labels.py` | Canvas titles (`Brief:…`, `Scene N:…`, `Scene S: Shot K:…`) |
| `model_tools.py` | Shared LLM / media tool wrappers |
| `media_model_playbook.py` | Model family rules (image vs video) |
| `audio_locks.py` | Language / speech / BGM locks |

### Handlers

| File | Nodes |
|------|-------|
| `handlers/text_nodes.py` | Brief, storyboard |
| `handlers/image_nodes.py` | Character solos, scene specs |
| `handlers/clip.py` | Clip R2V (refs = **on-screen** solos + scene; story-form prompt) |
| `handlers/compose.py` | Final film; waits for real clip/audio files |
| `handlers/audio_nodes.py` | Speech / music beds |
| `handlers/common.py` | `generate_designer_image`, path helpers |

### Pipeline (locks & continuity)

| File | Responsibility |
|------|----------------|
| `storyboard_shot_state.py` | Normalize / chain / stamp `start_state` + `end_state` |
| `clip_continuity_contract.py` | Storyboard already_done / speech uniqueness / reject redo |
| `video_prompt_practice.py` | Compose / approve **story-form** video prompts; omit exited cast |
| `image_prompt_practice.py` | Solo / empty-plate still prompt hygiene |
| `media_prompt_limits.py` | Prompt length / token caps |
| `wan_call_locks.py` | Supervisor rewrite into practice prompt for video API |
| `wan_r2v_best_practices.py` | Positive R2V formula (no examples / negatives on the call) |
| `wan_reference_binding.py` | Attach-order rules for refs |
| `wan_prompt_hygiene.py` | Scrub lock banners from narrative |
| `clip_story_state.py` | Self Wan stamp / exit helpers (prior-clip ensure is no-op when start_state present) |
| `clip_prompt_handoff.py` | Save `last_wan_prompt` on **self** only (no next-clip Wan lore) |
| `clip_shot_scope.py` | Per-window beat filling from storyboard time |
| `keyframe_policy.py` | Scene specs / setting ensembles (legacy name; no `n_frame_*`) |
| `production_bible.py` | Lock bible stamped into brief/storyboard |
| `leaf_agent_continuity.py` | Domain-agnostic leaf instructions |
| `clothing_lock.py` / `shot_staging_lock.py` / `axis_locks.py` / `cast_prop_locks.py` | Wardrobe, staging, screen axis, cast vs props |
| `director_contract.py` | Shot budget / exit carry |
| `skills/wan-reference-video/` | Optional skill pack for leaf agents |

---

## Agent responsibilities (detail)

### Supervisor

- Author brief + storyboard (with per-shot start/end states) from user prompt
- Own `target_shot_count` (prefer ≤8, hard ≤16)
- Plan leaf tasks/tools; finalize after compose

### Manager

- Gate brief/storyboard fidelity; validate start/end chain
- Prune nodes that cannot reach `n_compose`
- Before every clip tool call: `review_leaf_media_prompt` →
  continuity contract + `supervisor_approve_video_prompt` (concise story form,
  character locks enforced, exited cast omitted, no negatives/examples on the API body)
- Dual rate final film

### Leaf

- Character: solo sheet only (one person, plain backdrop)
- Scene: empty plate only
- Clip: one story-form R2V prompt + `call_video_model` (deps = storyboard + on-screen solos + scene)
- Compose: `ffmpeg_compose` only after predecessors have real media

---

## Config / models

Loaded from `~/.jiuwenswarm/config/.env` (never commit):

- Chat: DeepSeek / etc. (`API_KEY`, `API_BASE`)
- Image: Qwen / image-01 / … (`IMAGE_GEN_*`)
- Video: `VIDEO_GEN_*` (Wan, Seedance, or MiniMax)

Clips default **480p**. Image path uses `generate_designer_image` (not the harness
`call_image_model` LocalFunction path). Assets panel unions media + text tiles from
`output_ref` (brief/storyboard markdown included).
