# Scene node pipeline (`n_scene_*`)

**Builder:** `smart_graph.build_smart_video_graph`  
**Handler / tool:** scene image path via `call_image_model` / `generate_designer_image`  
**Type:** `image` — **empty environment plate** (no people)

---

## 1. Purpose

One plate per `setting_id`: empty room / location for R2V.  
People come only from character solos at clip time — never baked into the scene PNG.

---

## 2. Graph inputs / edges

| Item | Value |
|------|--------|
| Typical inputs | `n_storyboard` (+ often `n_brief`) |
| Downstream | All clips with matching `setting_id` |
| Label | Setting name / id |

Hard for clips: compose/clip wait until real scene PNG on disk.

---

## 3. Config at build

| Field | Meaning |
|-------|---------|
| `role` | `scene` |
| `setting_id` / description | Geography |
| `style_lock`, spatial locks | Continuity of place |
| `tools` | `call_image_model`, `read_upstream` |
| Image backend | **IMAGE_GEN** (Qwen) |

---

## 4. Runtime sequence

```
Storyboard ready
        ↓
Manager.review_leaf_media_prompt (still)
        ↓
generate empty plate (no cast)
        ↓
PNG → last R2V reference on matching clips
```

---

## 5. Output

| Output | Form |
|--------|------|
| `output_ref` | Empty scene PNG |
| UI Assets | Image tile |

---

## 6. Key functions

| # | Function | File |
|---|----------|------|
| 1 | Scene nodes + setting map | `smart_graph.py` |
| 2 | Empty-plate prompt practice | image prompt helpers |
| 3 | Clip refs: solos then scene last | `handlers/clip.py` |

---

## 7. Design notes

- Never peopled keyframes (`n_frame_*` removed).  
- Same-setting continuity is storyboard start/end + this plate — never cross rooms in one clip.
