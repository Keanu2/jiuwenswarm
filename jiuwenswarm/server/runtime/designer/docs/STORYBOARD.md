# Storyboard node pipeline (`n_storyboard`)

**Builder:** `smart_graph.build_smart_video_graph`  
**Handler:** `handlers/text_nodes.StoryboardNodeHandler`  
**Type:** `table` (markdown storyboard)  
**Continuity module:** `experiments/storyboard_shot_state.py`

---

## 1. Purpose

Turn the approved brief (+ analysis shots) into a **time-coherent storyboard**.  
Each row is **one closed continuity window**:

`start_state` → action / camera / speech → `end_state`

Same `setting_id`: shot N `start_state` must match shot N−1 `end_state` (Manager validates; builder fills gaps).  
Clips do **not** read prior Wan text for plot — this board is the sole continuity authority.

Also appends the **Production Lock Bible** for leaf context.

---

## 2. Graph inputs / edges

| Item | Value |
|------|--------|
| Inputs | `["n_brief"]` |
| Edge | `e_brief_storyboard` |
| Downstream | → all characters, scenes, clips |
| Label | `Storyboard:…` |

---

## 3. Config at build

| Field | Meaning |
|-------|---------|
| `role` | `storyboard` |
| `prompt` | User prompt |
| `planned_shots` | Shot list from analysis / Supervisor |
| `prewritten` / `draft_prewritten` | Markdown table or hierarchical scenes |
| `skip_llm` | True when Supervisor/heuristic prewrite |
| `inputs` | `["n_brief"]` |
| `tools` | `write_artifact` (± `call_model`) |
| `delegate` | `agent` \| `handler` |
| `skill_id` | `storyboard` |

### Per-shot fields (required for continuity)

| Field | Meaning |
|-------|---------|
| `timeline`, `camera`, `action` / `cast_actions` | This window only |
| `on_screen`, `offscreen`, `exiting_character_ids` | Occupancy |
| `speech_line` / `speech_by_character`, `language_lock` | Dialogue (empty = silent) |
| `start_state` | Opening pose / seats / facing / on_screen |
| `end_state` | Closing pose / seats / exited / speech_done |
| `already_done`, `continuity_lock` | Prior finished beats in this setting |
| `setting_id` | Geography key |

`ensure_shot_start_end_states` fills missing start/end and chains same-setting opens from prior ends.

---

## 4. Upstream artifacts

| Artifact | From |
|----------|------|
| Brief markdown | `n_brief` `output_ref` |

---

## 5. Runtime sequence

```
n_brief COMPLETED
        ↓
Storyboard ready
        ↓
StoryboardNodeHandler  OR  agent
        ↓
prewritten / planned_shots / LLM storyboard
  ensure_shot_start_end_states
  sync_shot_nodes_from_storyboard_markdown → clip configs
  stamp Production Lock Bible
        ↓
COMPLETED → unlocks characters, scenes, clips (clips concurrent thereafter)
```

---

## 6. Output

| Output | Form |
|--------|------|
| `output_ref` | Markdown storyboard (+ bible) |
| Side effect | Clip configs get action/camera/speech/start_state/end_state |

---

## 7. Key functions

| # | Function | File |
|---|----------|------|
| 1 | `_write_storyboard_markdown` | `smart_graph.py` |
| 2 | `ensure_shot_start_end_states` / `validate_storyboard_state_chain` | `storyboard_shot_state.py` |
| 3 | `StoryboardNodeHandler.execute` | `handlers/text_nodes.py` |
| 4 | `sync_shot_nodes_from_storyboard_markdown` | text_nodes / executor |
| 5 | Supervisor `author_storyboard` | `orchestration.py` |

---

## 8. Design notes

- Soft prefer ≤8 shots, hard max 16.  
- Explicit N-shot / N分镜 is a ceiling.  
- Domain-agnostic — no scene-specific hardcodes.  
- Assets UI shows storyboard as a **text** tile (not an empty image card).
