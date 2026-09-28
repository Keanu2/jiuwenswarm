# Brief node pipeline (`n_brief`)

**Builder:** `smart_graph.build_smart_video_graph`  
**Handler:** `handlers/text_nodes.BriefNodeHandler`  
**Type:** `table` (markdown creative brief)

---

## 1. Purpose

Produce the **creative brief** and stamp the **Production Lock Bible** (cast, costumes, style, language, occupancy rules).  
**Only `n_storyboard` reads the brief as a hard graph input.**  
Characters / scenes / clips get locks via config + bible on storyboard — they do **not** edge from brief for media generation (except character/scene soft edges for ordering when present).

---

## 2. Graph position

```
n_brief
   └── n_storyboard
          ├── n_character_*
          ├── n_scene_*
          └── n_clip_*
```

| Item | Value |
|------|--------|
| Inputs | `[]` (root) |
| Downstream hard | `n_storyboard` only (`e_brief_storyboard`) |
| Soft / order | Characters & scenes may also list `n_brief` in `inputs` for early unlock |
| Clips | **Never** wire `n_brief` — use storyboard |

---

## 3. Config at build

| Field | Meaning |
|-------|---------|
| `role` | `brief` |
| `prompt` | User creative brief |
| `tools` | `call_model`, `write_artifact` |
| `delegate` | `agent` |
| `skill_id` | `brief` |

---

## 4. Runtime sequence

```
Play / graph start
        ↓
BriefNodeHandler or agent
  · write brief markdown
  · stamp Production Lock Bible (cast / costume / style / language)
        ↓
COMPLETED → unlocks storyboard (and optionally char/scene order edges)
```

---

## 5. Output

| Output | Form |
|--------|------|
| `output_ref` | Brief markdown (+ bible) |
| UI Assets | Text tile |

---

## 6. Key functions

| # | Function | File |
|---|----------|------|
| 1 | Brief node create | `smart_graph.py` |
| 2 | `BriefNodeHandler.execute` | `handlers/text_nodes.py` |
| 3 | Supervisor `author_brief` | `orchestration.py` |
| 4 | Lock bible helpers | `production_lock_bible.py` / text_nodes |

---

## 7. Design notes

- Brief is planning authority for **storyboard**, not a second beat sheet for clips.  
- Clip prompts must not dump the raw brief; Manager rewrites from storyboard row + locks.
