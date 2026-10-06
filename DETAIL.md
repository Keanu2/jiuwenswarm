# Designer video pipeline — DETAIL

Reproduce the AI-first **prompt → film** Play path used on branch `design`
(quality graph `designer.graph.smart_video.quality.v3`).

Short companion notes: `a.md`. This file is the full reproduction guide:
architecture, **agent spawning**, scheduling, LLM roles, media honesty, and
how to run a validation pass.

---

## 1. What this pipeline does

User prompt (natural language) → Designer execution graph → one forward Play:

1. **Supervisor** (LLM) analyzes the prompt and plans node tasks / models / tools.
2. **Manager** (LLM) validates fidelity, identity, continuity; prunes orphans;
   enforces every leaf is an agent with tools (unless `force_handler`).
3. **Leaf agents** (openjiuwen DeepAgents via `NodeAgentHost`) and/or **handlers**
   produce brief, storyboard, solo cast sheets, per-shot scenes, keyframes, clips,
   speech/music, then **compose** a real `.mp4`.
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

## 3. Quality DAG (v3)

```
n_brief (text agent)
  → n_storyboard (table agent)
       → Manager.review_storyboard_once (fidelity / continuity / duration)
  → n_character_*  (solo identity sheets — never concatenated group sheets)
  → n_scene_*      (one environment view per shot; no people)
  → n_frame_*      (compose solos + that shot’s scene; optional edit prior KF)
  → n_clip_*       (real I2V; still→mp4 OFF by default)
  → n_speech / n_music (optional; always edge into compose when present)
  → n_compose      (ffmpeg concat + BGM mux; must emit non-empty .mp4)
```

Bootstrap stamp: `metadata.bootstrap = designer.graph.smart_video.quality.v3`.

Manager calls `prune_non_contributing_nodes()` so every kept node can reach
`n_compose`. Speech without an edge to compose is pruned or must be wired.

---

## 4. Agent spawning (critical)

### 4.1 Who spawns what

| Role | How it is created | When |
|------|-------------------|------|
| **SupervisorAgent** | In-process class; LLM via `model_tools.call_model_tool` | Start of Play (`plan`) and end (`SupervisorReviewer.finalize`) |
| **ManagerAgent** | Same | Capabilities → `validate_plan` → storyboard review → dual raters |
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
except nodes with `force_handler=True` (typical for speech/music beds and for
strict media materialization in eval scripts).

### 4.3 Spawn trigger (ready-queue)

A leaf is **spawned** only when:

1. All data-edge predecessors are terminal (`completed` / failed handled), and
2. Concurrency slot is free (see Scheduling), and
3. Node is not already in-flight.

Spawning = `asyncio.create_task(_run_guarded(node_id))` which calls
`NodeAgentHost.execute` or the role handler.

Agents author creative specs (markdown / prompts). Handlers own raster / I2V /
ffmpeg backends when the required **media family** is missing.

---

## 5. Scheduling

Implemented in `GraphExecutor._execute_wave_run`:

| Knob | Value / behavior |
|------|------------------|
| Model | Continuous **ready-queue** (not barrier waves) |
| Wait | `asyncio.wait(..., return_when=FIRST_COMPLETED)` |
| Leaf concurrency | `_MAX_CONCURRENT_NODE_AGENTS = 3` (semaphore) |
| Image API concurrency | Global semaphore **2** in `handlers/common.generate_designer_image` |
| Image retries | Backoff on DashScope `RateQuota` / 429 (up to ~4–6 tries) |
| Loops | **Forbidden** — one forward pass; ratings do not re-enter the same run |
| Mid-pass gates | After storyboard: manager review once; after all keyframes: supervisor may adjust clip briefs once |

Independent branches (e.g. several solo cast sheets) run together once
`n_storyboard` is done. Compose waits on all clips (+ audio if present).

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
4. Supervisor.plan(use_llm=…)  → node_directives, brief_notes, preferred_model, tools
5. Manager.validate_plan(use_llm=…) → identity/continuity patches, prune, enforce agents
6. Ready-queue leaf execution (agent spawn / handler)
7. Manager.review_storyboard_once (after n_storyboard completes)
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
| Missing required family → handler materialize or **fail** | `NodeAgentHost` |
| still→mp4 only if `allow_still_clip_fallback` (default **false**) | `handlers/clip.py` |
| Compose must produce non-empty `.mp4` | `handlers/compose.py` |
| Image handlers must not “succeed” with notes-only | `handlers/image_nodes.py` (`require_image`) |

---

## 7. Key source files

```
jiuwenswarm/server/runtime/designer/
  smart_graph.py          # build_smart_video_graph, prune_non_contributing_nodes
  script_analysis.py      # LLM / heuristic creative brief
  orchestration.py        # SupervisorAgent, ManagerAgent, dual raters
  executor.py             # ready-queue, agent spawn, Play loop
  node_agent.py           # NodeAgentHost / DeepAgent
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
3. `ffmpeg` on PATH for compose.

### UI path

1. Start AgentServer / web channel as usual for this repo.
2. Designer → bootstrap video prompt → **Play**.
3. Inspect `~/.jiuwenswarm/agent/workspace/` for `designer_compose_*_bgm.mp4`.

### Headless smoke / church prompt

```bash
# from repo root, with env activated and PYTHONPATH/package install as you normally use
python scripts/run_church_play_pipeline.py --timeout-sec 2700
```

Writes under `pipeline_test_out/` locally (gitignored): report JSON + compose mp4.

Example prompt used in validation:

> I want the video of a man standing in a church explaining something from the Bible
> and the crowd listened while another man gets up and leaves the church while the
> man at the pulpit is still speaking, this man is sitting in the front, and then
> the camera pans to a woman whose tears begin to flow as she nods gently agreeing
> with what the man is saying and a child next to her listening profusely

### Import-level check (no media cost)

```bash
python -c "from jiuwenswarm.server.runtime.designer.smart_graph import build_smart_video_graph, prune_non_contributing_nodes; from jiuwenswarm.server.runtime.designer.orchestration import SupervisorAgent, ManagerAgent; from jiuwenswarm.server.runtime.designer.executor import GraphExecutor; print('ok')"
```

---

## 9. Git / tag

- Branch: `design`
- Doc: this file (`DETAIL.md`)
- Annotated tag for the agent-spawning ready-queue quality path:
  **`agents-spawning`**

Do not commit: videos, `pipeline_test_out/`, `results_eval/`, trajectory dumps,
`.venv` / `Lib/` / `pyvenv.cfg`, or accidental Windows venv binaries under `scripts/`.

---

## 10. Troubleshooting

| Symptom | Check |
|---------|--------|
| Heuristic-only run | `llm_available()`, dotenv, Settings chat model |
| `.md` marked as video | `_ref_media_family`; force handler materialize |
| still freezes as clips | `allow_still_clip_fallback` must be false |
| Orphan speech / cast | prune + edges into `n_compose` |
| DashScope 429 | image semaphore + retries; lower leaf concurrency |
| No compose mp4 | all clips real video; compose handler error in logs |
