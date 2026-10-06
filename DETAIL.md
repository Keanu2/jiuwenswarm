# Designer video pipeline — DETAIL

Reproduce the AI-first **prompt → film** Play path used on branch `design`
(quality graph `designer.graph.smart_video.quality.v4`).

Short companion notes: `a.md`. This file is the full reproduction guide:
architecture, **agent spawning**, spatial lock, audio mux, scheduling, LLM roles,
media honesty, and how to run a validation pass.

---

## 1. What this pipeline does

User prompt (natural language) → Designer execution graph → one forward Play:

1. **Supervisor** (LLM) analyzes the prompt and plans node tasks / models / tools;
   may author brief / storyboard drafts once.
2. **Manager** (LLM) validates fidelity to prompt / brief / storyboard, stamps
   `spatial_lock`, prunes unused nodes, rewires coherent edges to `n_compose`,
   and enforces every leaf is an agent with tools (unless `force_handler`).
3. **Leaf agents** (openjiuwen DeepAgents via `NodeAgentHost`) and/or **handlers**
   produce brief, storyboard, solo cast sheets, **master scene + shot views**,
   keyframes, clips, speech/music, then **compose** a real `.mp4` with audible audio.
4. **Dual rater agents** score the run; feedback JSON is write-only and applied
   only on an explicit **Run again** (`use_prior_feedback`).

Heuristics run **only** when `llm_available()` is false (no Settings chat model).

---

## 2. Frontend ↔ backend

```
DesignerPage
  └─ Play → designerRunStore.advance()
  └─ designerGraphClient.startRun / bootstrap
  └─ webClient WebSocket RPC
       designer.graph.bootstrap | designer.run.start | designer.run.get
  └─ AgentServer AdapterRegistry
  └─ DesignerAdapter (gateway_adapter/designer_adapter.py)
       ├─ bootstrap → script_analysis + build_smart_video_graph
       └─ start_run → GraphExecutor.create_run / start_run
  └─ events: DESIGNER_RUN_UPDATED / NODE_UPDATED / GRAPH_UPDATED
  └─ UI bindDesignerRuntime() (+ poll designer.run.get)
```

| Layer | Path |
|-------|------|
| UI client | `jiuwenswarm/channels/web/frontend/src/features/designer/designerGraphClient.ts` |
| Run store | `…/designerRunStore.ts` |
| RPC adapter | `jiuwenswarm/server/runtime/gateway_adapter/designer_adapter.py` |
| Executor | `jiuwenswarm/server/runtime/designer/executor.py` |
| Graph build | `…/smart_graph.py` |
| Orch | `…/orchestration.py` |
| Leaf DeepAgent | `…/node_agent.py` (`NodeAgentHost`) |
| Schema / roles | `jiuwenswarm/common/schema/designer_graph.py` |

Media and run state live under `JIUWENSWARM_DATA_DIR` or `~/.jiuwenswarm`
(`agent/workspace/`, `agent/designer/graphs|runs|feedback/`).

---

## 3. Quality DAG (v4)

```
n_brief (text agent)
  → n_storyboard (table agent)
       → Manager.review_storyboard_once (fidelity / continuity / duration / crowd)
  → n_character_*  (solo identity sheets — never concatenated group sheets)
  → n_scene         (canonical master environment plate + spatial_lock; no people)
  → n_scene_*       (per-shot views: edit/ref from master — not new buildings)
  → n_frame_*       (compose solos + shot scene + master; optional edit prior KF)
  → n_clip_*        (real I2V from keyframe only; still→mp4 OFF by default)
  → n_speech / n_music (audible beds; always edged into compose when intent asks)
  → n_compose       (ffmpeg concat ALL clips + mux speech/music; non-empty .mp4)
```

Bootstrap stamp: `metadata.bootstrap = designer.graph.smart_video.quality.v4`.  
Also: `metadata.freeze_shot_topology = True` — Play does **not** rebuild
frame/clip topology via `expand_shot_nodes` (prompts may sync; nodes stay fixed).

### Spatial consistency

- One **master plate** (`n_scene`) locks architecture (pulpit side, aisle, windows, light).
- Shot views (`n_scene_*`) use `scene_strategy=edit_master_view` with the master as ref.
- Keyframes/clips carry `spatial_lock` + crowd lock (same congregation layout across shots).
- Manager `_spatial_geography_lock_patch` + `_manager_prune_and_cohere` stamp/rewire.

### Identity / anti-clone

- Solo cast sheets only; multi-person keyframes compose distinct sheets.
- I2V (`collect_clip_reference_images`) prefers **keyframe as first frame only** —
  do not also pass solo sheets that already appear in the keyframe (avoids dual pastors).
- Prompts include ANTI-CLONE / CROWD LOCK clauses.

### Audio

- Intent `speech_and_music` (or narrative dialogue cues) ensures `n_speech` + `n_music`.
- `ComposeNodeHandler` calls `mix_compose_soundtrack`: mix upstream speech/music when
  present, else audible cinematic bed (loudnorm). Silent films are not acceptable.
- `expand_shot_nodes` (if ever used) restores audio → compose edges.

Manager `_manager_prune_and_cohere` + LLM `prune_ids`: drop unused/orphan nodes,
**protect** clips / frames / speech / music / compose, rewire master→shot→frame,
keep every remaining node useful for `n_compose`, faithful to prompt / brief / storyboard.

---

## 4. Agent spawning (critical)

### 4.1 Who spawns what

| Role | How it is created | When |
|------|-------------------|------|
| **SupervisorAgent** | In-process class; LLM via `model_tools.call_model_tool` | Start of Play (`plan`) and end (`SupervisorReviewer.finalize`) |
| **ManagerAgent** | Same | Capabilities → `validate_plan` → brief/storyboard review → dual raters |
| **Leaf DeepAgent** | `NodeAgentHost` → openjiuwen `create_deep_agent` with Designer tools | Each ready node with `config.delegate=agent` |
| **Handler** | Role handler class (`ClipNodeHandler`, …) | `delegate=handler` or agent failure / `force_handler` |
| **Rater A / B** | Manager `dual_rate_final` → two `call_model_tool` calls | After film completes |

Supervisor / Manager are **not** graph nodes. Leaves are.

### 4.2 Leaf agent tools

DeepAgents get flat `**kwargs` LocalFunctions, including:

- `designer_graph_get` / `designer_graph_patch`
- `designer_node_run` / `designer_node_complete`
- Role tools: `call_image_model`, `call_video_model`, `call_speech_model`,
  `call_music_model`, `call_model`, `read_upstream`, `write_artifact`, …

`apply_runtime_delegate(graph)` sets `delegate=agent` when `llm_available()`,
except nodes with `force_handler=True` (speech/music beds, compose ffmpeg;
eval scripts may also force scene handlers for reliable plates).

### 4.3 Spawn trigger (ready-queue)

A leaf is **spawned** only when:

1. All data-edge predecessors are terminal (`completed` / failed handled), and
2. Concurrency slot is free (see Scheduling), and
3. Node is not already in-flight.

Spawning = `asyncio.create_task(_run_guarded(node_id))` which calls
`NodeAgentHost.execute` or the role handler.

Agents author creative specs (markdown / prompts). Handlers own raster / I2V /
ffmpeg backends when the required **media family** is missing. Primary
`output_ref` for scene/character/frame must be a real image — upstream PNGs
attached as `extra_uris` while primary is `.md` do **not** count.

---

## 5. Scheduling

Implemented in `GraphExecutor._execute_wave_run`:

| Knob | Value / behavior |
|------|------------------|
| Model | Continuous **ready-queue** (not barrier waves) |
| Wait | `asyncio.wait(..., return_when=FIRST_COMPLETED)` |
| Leaf concurrency | `_MAX_CONCURRENT_NODE_AGENTS = 3` (semaphore) |
| Image API concurrency | Global semaphore **2** in `handlers/common.generate_designer_image` |
| Image retries | Backoff on DashScope `RateQuota` / 429; T2I fallback if ref upload fails |
| Loops | **Forbidden** — one forward pass; ratings do not re-enter the same run |
| Topology | `freeze_shot_topology` — no mid-run `expand_shot_nodes` rebuild |
| Mid-pass gates | After storyboard: manager review once; after all keyframes: supervisor may adjust clip briefs once |

Independent branches (e.g. several solo cast sheets) run together once
`n_storyboard` is done. Compose waits on all clips (+ audio).

---

## 6. LLM orchestration (detail)

### Prerequisites

1. Load `~/.jiuwenswarm/config/.env` (`load_dotenv_runtime`) before any
   `llm_available()` / `call_model_tool`.
2. Settings must expose a chat model + API key → `llm_available() == True`.
3. Never treat `[local-tool-fallback]` / schema-echo as `source=llm`.

### Play-time order

```
1. Stamp metadata.agent_runtime (ai | heuristic)
2. Optional one-time LLM rebuild if pending_llm_analysis / non-llm bootstrap
3. Manager.decide_capabilities
4. Supervisor.plan(use_llm=…)  → node_directives, spatial_lock, brief_notes, tools
5. Manager.validate_plan(use_llm=…) → identity/continuity/spatial, prune, enforce agents
6. Ready-queue leaf execution (agent spawn / handler)
7. Manager.review_storyboard_once (after n_storyboard completes) + prune/cohere
8. Supervisor.adjust_clips_after_keyframes (once, when frames done & clips pending)
9. SupervisorReviewer.finalize + Manager.review + Manager.dual_rate_final
10. write_run_feedback → runs/*.report + designer/feedback/*.json (apply_on=run_again_only)
```

### Script analysis

`script_analysis.analyze_creative_brief(prompt, use_llm=True)` → cast, scenes,
shots, audio intent. Bootstrap may use a short timeout; Play can refine once.

### Media honesty

| Rule | Location |
|------|----------|
| `.md` / text never counts as image or video | `node_agent._ref_media_family` |
| Primary image `output_ref` required for scene/char/frame | `node_agent._result_satisfies_required_media` |
| Missing required family → handler materialize or **fail** | `NodeAgentHost` |
| still→mp4 only if `allow_still_clip_fallback` (default **false**) | `handlers/clip.py` |
| All shot clips must reach compose | `handlers/compose.collect_clip_video_paths` |
| Compose muxes speech/music (or audible bed) | `handlers/compose.mix_compose_soundtrack` |
| Image handlers must not “succeed” with notes-only | `handlers/image_nodes.py` (`require_image`) |

---

## 7. Key source files

```
jiuwenswarm/server/runtime/designer/
  smart_graph.py          # build_smart_video_graph, prune, spatial master plate
  script_analysis.py      # LLM / heuristic creative brief
  orchestration.py        # SupervisorAgent, ManagerAgent, dual raters, prune/cohere
  executor.py             # ready-queue, freeze_shot_topology, Play loop
  node_agent.py           # NodeAgentHost / DeepAgent / media honesty
  model_tools.py          # call_model_tool, llm_available
  capabilities.py         # vision/video rating modality
  continuity.py           # continuity prompt clauses
  feedback.py / trajectory.py / paths.py / skills_loader.py
  handlers/
    image_nodes.py  clip.py  compose.py  audio_nodes.py  text_nodes.py  common.py

jiuwenswarm/server/runtime/gateway_adapter/designer_adapter.py
jiuwenswarm/common/schema/designer_graph.py
scripts/run_church_play_pipeline.py   # headless reproduction of Play
```

---

## 8. Reproduce locally

### Config

1. Conda/venv with project deps (do **not** commit `.venv` / `Lib/` / `pyvenv.cfg`).
2. `~/.jiuwenswarm/config/.env` with chat + image + video keys as required by Settings.
3. `ffmpeg` on PATH (or `imageio-ffmpeg`) for compose / audio beds.

### UI path

1. Start AgentServer / web channel as usual for this repo.
2. Designer → bootstrap video prompt → **Play**.
3. Inspect `~/.jiuwenswarm/agent/workspace/` for `designer_compose_*_bgm.mp4`.

### Headless smoke / church prompt

```bash
# from repo root, with env activated (example: conda env `new`)
conda run -n new --no-capture-output python scripts/run_church_play_pipeline.py --timeout-sec 2700
```

Writes under `pipeline_test_out/` locally (gitignored): report JSON + compose mp4 + clips.

Example prompt used in validation:

> I want the video of a man standing in a church explaining something from the Bible
> and the crowd listened while another man gets up and leaves the church while the
> man at the pulpit is still speaking, this man is sitting in the front, and then
> the camera pans to a woman whose tears begin to flow as she nods gently agreeing
> with what the man is saying and a child next to her listening profusely

Expect: `run_status=completed`, `n_scene` + `n_scene_*` as images, all `n_clip_*` mp4s,
`n_speech` + `n_music`, compose `_bgm.mp4` with an **Audio** stream.

### Import-level check (no media cost)

```bash
python -c "from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph, prune_non_contributing_nodes; from jiuwenswarm.server.runtime.designer.orchestration import SupervisorAgent, ManagerAgent, _manager_prune_and_cohere; from jiuwenswarm.server.runtime.designer.handlers.compose import mix_compose_soundtrack; print('ok')"
```

Topology smoke (master + edit views + audio):

```bash
python -c "from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph; a={'characters':[{'id':'char_1','name':'Preacher','description':'p'},{'id':'char_2','name':'Leaver','description':'l'}],'scenes':[{'id':'scene_1','name':'Church','description':'nave'}],'shots':[{'shot_index':1,'action':'preach','camera':'wide','character_ids':['char_1']},{'shot_index':2,'action':'leaves while preacher still speaking','camera':'medium','character_ids':['char_1','char_2']}],'audio':{'policy':'speech_and_music','include_speech':True,'include_music':True}}; g=build_smart_video_graph(project_id='t',prompt='church',analysis=a,ai_mode=True); ids=[n['id'] for n in g['nodes']]; assert 'n_scene' in ids and 'n_music' in ids and 'n_speech' in ids; assert g['metadata']['bootstrap'].endswith('v4'); print('topology_ok', ids)"
```

---

## 9. Git / tag

- Branch: `design`
- Docs: `DETAIL.md`, `a.md`
- Annotated tag for the agent-spawning ready-queue quality path:
  **`agents-spawning`** (move forward when cutting a new quality milestone)

Do not commit: videos, `pipeline_test_out/`, `results_eval/`, trajectory dumps,
`.venv` / `Lib/` / `pyvenv.cfg`, competition/eval scratch scripts unless intended,
or accidental Windows venv binaries under `scripts/`.

---

## 10. Troubleshooting

| Symptom | Check |
|---------|--------|
| Heuristic-only run | `llm_available()`, dotenv, Settings chat model |
| `.md` marked as video/image | `_ref_media_family` / primary `output_ref`; force handler |
| Pastor cloned (walk + pulpit) | I2V refs must be keyframe-only; shot cast must list **two** distinct men |
| Crowd differs every shot | `spatial_lock.crowd_rule`; keyframe CROWD LOCK prompts |
| Silent film | compose must mux `n_speech`/`n_music` or audible bed; check Audio stream |
| Missing clip in film | `collect_clip_video_paths` must include every `n_clip_*` |
| still freezes as clips | `allow_still_clip_fallback` must be false |
| Orphan speech / cast | prune + edges into `n_compose`; protect audio from `prune_ids` |
| DashScope 429 / file type | image semaphore + retries; T2I fallback without bad refs |
| No compose mp4 | all clips real video; compose handler error in logs |
| Mid-run shot graph rewrite | `freeze_shot_topology` should be true on quality.v4 |
