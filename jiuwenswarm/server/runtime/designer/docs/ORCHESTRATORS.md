# Overseeing agents (Supervisor & Manager)

These are **not** canvas nodes. They orchestrate the graph around leaf nodes (brief, storyboard, characters, scenes, clips, compose).

**File:** `orchestration.py`  
**Scheduler:** `executor.py` (`GraphExecutor`)  
**Leaf runtime:** `node_agent.py` (`NodeAgentHost`)

---

## 1. Who does what

| Agent | Role | Canvas node? |
|-------|------|----------------|
| **SupervisorAgent** | Plan / cast / storyboard (incl. start/end) / shot budget / tasks | No |
| **ManagerAgent** | Capabilities, plan validation, **leaf media prompt gate**, speech uniqueness, ratings | No |
| **GraphExecutor** | Ready queue, hard deps, concurrency ≤3 | No (runtime) |
| **NodeAgentHost** | Per-leaf DeepAgent when `delegate=agent` | Hosts leaf tools |

---

## 2. Call chain on Play

```
UI Play
  → designer_adapter.start_run
  → GraphExecutor._execute_wave_run
       → ManagerAgent.decide_capabilities
       → SupervisorAgent.plan (if needed)
       → ManagerAgent.validate_plan / prune / identity patches
       → continuous ready waves:
            for each ready leaf (clips concurrent when deps ready):
              ManagerAgent.review_leaf_media_prompt
              NodeAgentHost.execute  OR  handler.execute
            storyboard sync / compose gate
       → Manager dual_rate_final
```

---

## 3. SupervisorAgent

### Responsibilities
- Author brief + storyboard with per-shot `start_state` / `end_state`, occupancy, speech  
- Shot budget (`director_contract`)  
- Assign `supervisor_task` on leaves  
- Design / refresh execution graph  

### Does not
- Call Wan/Qwen for every leaf  
- Appear as a canvas node  

---

## 4. ManagerAgent

### Responsibilities
1. **`decide_capabilities`** — backends → modality plan  
2. **`validate_plan`** — cast, spatial, axis/aspect locks, prune dead paths  
3. **`review_leaf_media_prompt`** before each media leaf:
   - Clips: storyboard continuity contract, speech uniqueness vs prior **storyboard** rows, `supervisor_approve_video_prompt`, prose coverage  
   - Stills: style / costume / spatial as needed  
   - Reject prompts that restate forbidden speech or film prior row’s action  
4. **Ratings** after compose  

### Clip gate contract
- Story-form only on the video API  
- Authority = this row’s start → beat → end (not prior Wan dump)  
- Config remains full lock source of truth  

---

## 5. GraphExecutor

| Concern | Behavior |
|---------|----------|
| Ready queue | Continuous; concurrency cap **3** |
| Clip deps | Storyboard + **on-screen** solos + scene (hard) |
| Same-setting clips | **Concurrent** — no prior-clip soft wait |
| Compose | Hard wait for real on-disk mp4s |
| Storyboard sync | After storyboard completes, refresh Shot fields |

---

## 6. Relation to leaves

```
                    Supervisor (plan / storyboard start–end)
                           │
                           ▼
              Manager (validate + leaf prompt gate)
                           │
         ┌─────────────────┼─────────────────┐
         ▼                 ▼                 ▼
      Brief/SB         Char/Scene          Clips (parallel)
   (text handlers)   (Qwen images)    (VIDEO_GEN R2V)
         ▲                 ▲                 ▲
         └──────── Manager.review_leaf ──────┘
                           │
                     Executor waves
                           │
                      Compose / rate
```

---

## 7. Key files

| File | Overseer piece |
|------|----------------|
| `orchestration.py` | SupervisorAgent, ManagerAgent |
| `executor.py` | Waves, readiness |
| `smart_graph.py` | DAG (no clip→clip edges) |
| `storyboard_shot_state.py` | start/end normalize + stamp |
| `clip_continuity_contract.py` | Storyboard already_done / speech uniqueness / reject redo |
| `video_prompt_practice.py` | Story-form rewrite |
| `leaf_agent_continuity.py` | Domain-agnostic leaf instructions |

---

## 8. Design rules (current)

- Scene specs required; no `n_frame_*`  
- Continuity = storyboard start/end + occupancy  
- Images: Qwen; video: Wan/Seedance/MiniMax per `VIDEO_GEN_*`  
- 480p clips; no compose until real media  
- Domain-agnostic gates — no scene-specific hardcodes  

---

## 9. Related leaf docs

[BRIEF](./BRIEF.md) · [STORYBOARD](./STORYBOARD.md) · [CHARACTER](./CHARACTER.md) · [SCENE](./SCENE.md) · [CLIP](./CLIP.md) · [INDEX](./INDEX.md)
