# Clip node pipeline (`n_clip_*`)

**Mode:** `metadata.scene_continuity_mode = scene_card_plus_clip_shots`  
**Strategy:** `keyframe_strategy = clip_from_scene_and_solos`  
**Builder:** `smart_graph.build_smart_video_graph`  
**Handlers / tools:** `handlers/clip.py`, `node_agent.call_video_model`

---

## 1. Purpose

Each clip is **one storyboard time window** filmed with R2V:  
**on-screen solo sheets** as character refs + **empty scene plate** as the room.  
Prompt is **story-form** from this row’s `start_state` → beat → `end_state`.  
No peopled keyframes; no prior-clip edge.

---

## 2. Graph inputs (concurrent)

Built as:

```text
clip_inputs = ["n_storyboard", *focus_char_nodes]  # on-screen solos only
            + scene_nid                            # this setting's empty plate
```

| Dependency | Kind | Why |
|------------|------|-----|
| `n_storyboard` | Hard | Beat / camera / speech / start–end authority |
| **On-screen** `n_character_*` | Hard | Only people in frame this shot |
| `n_scene_*` for `setting_id` | Hard | Empty environment plate |

### Not wired

| Not an input | Notes |
|--------------|-------|
| Prior same-setting clip | Continuity is storyboard start/end — clips run in parallel |
| `n_brief` | Clips use storyboard + cfg locks |
| Offscreen / exited solos | No image ref until returned on_screen |
| Prior clip mp4 / Wan prose | Debug/regenerate only (`last_wan_prompt` on self) |

No `continuity_clip_node_id`. Edges: `e_{src}_{n_clip_K}` for storyboard, on-screen solos, scene.

---

## 3. Config fields (source of truth)

### Topology / identity
- `role=clip`, `shot_index`, `keyframe_strategy`
- `setting_id`, `scene_node_id`, `first_of_setting`
- `character_ids`, `on_screen`, `offscreen`, `character_node_ids`, `cast_names`
- `start_state`, `end_state`, `seat_anchors`, `pose_holds`, `already_done`
- `inputs`, `tools=["call_video_model","read_upstream"]`
- `delegate` = `agent` | `handler`, `max_video_calls=1`

### Beat / locks
- `shot_action`, `timeline`, `camera`, `cast_actions`
- `occupancy`, `crowd_lock`, `costume_lock`, `style_lock`, `language_lock`
- `speech_line` / `speech_by_character`, `forbidden_speech` (from prior **storyboard** rows)
- `aspect_lock` / video size → **480p**

### Debug only (not leaf continuity lore)
- `last_wan_prompt`, `regenerate_packet`, `clip_prompt_preview`

---

## 4. Upstream media when ready

| Artifact | From | Use |
|----------|------|-----|
| On-screen solo PNGs | Character nodes | R2V character1… |
| Empty scene PNG | Scene node | Last R2V ref (room) |
| Storyboard row | Storyboard + cfg | start/action/end/speech |

---

## 5. Runtime sequence

```
deps satisfied (storyboard + on-screen solos + scene)
        ↓
Executor ready wave  (same-setting clips may run together; concurrency ≤3)
        ↓
Manager.review_leaf_media_prompt
  · merge_storyboard_continuity / enforce_speech_uniqueness
  · stamp start/end onto cfg
  · supervisor_approve_video_prompt → compose_practice_prompt
  · ensure_story_lock_coverage (prose)
        ↓
Leaf call_video_model / ClipNodeHandler
  · refs: on-screen solos → scene last
  · story-form body; reject restated forbidden speech / prior-action redo
  · VIDEO_GEN (Wan / Seedance / MiniMax), 480p
        ↓
Save mp4; last_wan_prompt on self only (no next-clip Wan stamp)
        ↓
Compose hard-waits all clip mp4s
```

### Story-form contract (`video_prompt_practice.py`)

Positive narrative only. Opening from `start_state` / `pose_holds`.  
Name only on-screen people; omit exited until returned.  
No LOCK essays / forbid lists / “do not redo” on the API body.

---

## 6. Key functions

| # | Function | File |
|---|----------|------|
| 1 | `build_smart_video_graph` (clip nodes) | `smart_graph.py` |
| 2 | `stamp_shot_states_on_clip_cfg` | `storyboard_shot_state.py` |
| 3 | `merge_storyboard_continuity` / `prompt_violates_continuity` | `clip_continuity_contract.py` |
| 4 | `ManagerAgent.review_leaf_media_prompt` | `orchestration.py` |
| 5 | `supervisor_approve_video_prompt` | `video_prompt_practice.py` |
| 6 | `collect_clip_reference_images` | `handlers/clip.py` |
| 7 | `call_video_model` / `generate_clip_video` | `node_agent.py`, `handlers/clip.py` |
| 8 | `stamp_wan_prompt_handoff` (self only) | `clip_prompt_handoff.py` |

---

## 7. Downstream

- `n_compose` — hard wait for all clip mp4s (+ audio when present)  
- UI regenerate — `last_wan_prompt` / regenerate packet on this node
