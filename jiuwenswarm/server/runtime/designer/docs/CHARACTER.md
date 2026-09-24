# Character node pipeline (`n_character_*`)

**Builder:** `smart_graph.build_smart_video_graph`  
**Handler / tool:** `handlers/character.py`, `node_agent.call_image_model` → `generate_designer_image`  
**Type:** `image` — **solo identity sheet** (one person, plain backdrop)

---

## 1. Purpose

One node per cast member: a **clean solo reference** for R2V.  
Not a peopled scene; not a keyframe.  
Clips only depend on characters that are **on_screen** for that shot.

---

## 2. Graph inputs / edges

| Item | Value |
|------|--------|
| Typical inputs | `n_storyboard` (+ often `n_brief` for order) |
| Downstream | Only clips where this id is on_screen |
| Label | Character name / short id |

Offscreen / exited characters are **not** clip inputs until they return on_screen.

---

## 3. Config at build

| Field | Meaning |
|-------|---------|
| `role` | `character` |
| `character_id` / name / description | Identity |
| `costume_lock`, `style_lock` | Appearance locks |
| `tools` | `call_image_model`, `read_upstream` |
| `delegate` | `agent` \| `handler` |
| Image backend | **IMAGE_GEN** (Qwen) |

---

## 4. Runtime sequence

```
Storyboard (+ brief) ready
        ↓
Manager.review_leaf_media_prompt (still)
        ↓
call_image_model → generate_designer_image
  · one person, plain backdrop
  · costume + style locks enforced
        ↓
PNG output_ref → R2V character refs for on-screen clips
```

---

## 5. Output

| Output | Form |
|--------|------|
| `output_ref` | Solo PNG |
| UI Assets | Image tile |

---

## 6. Key functions

| # | Function | File |
|---|----------|------|
| 1 | Character nodes in graph | `smart_graph.py` |
| 2 | `generate_designer_image` | image gen path / character handler |
| 3 | Cast dedupe / occupancy | `orchestration.py`, smart_graph |
| 4 | Style lock enforcement | Manager + prompt practice |

---

## 7. Design notes

- Cast dedupe / occupancy prevent spawn-from-nowhere and disappearances.  
- Crowd / group sheets are not used as peopled keyframes — empty scene + solos only.
