# Designer pipeline docs

Continuity mode: **`scene_card_plus_clip_shots`** (no per-shot keyframes).  
Empty scene plates + on-screen character solos + R2V clips.  
Defaults: **IMAGE_GEN** = MiniMax `image-01`; **VIDEO_GEN** = `MiniMax-H3-Max`.
See [model setup](./MODELS.md) for credentials and existing-instance updates.

**Continuity authority:** storyboard per-shot `start_state` → action/camera/speech → `end_state`.  
No prior-clip Wan prose; same-setting clips run **concurrently** once storyboard + needed solos + scene are ready.

| Doc | Covers |
|-----|--------|
| [MODELS.md](./MODELS.md) | Default models, credentials, migration and verification |
| [BRIEF.md](./BRIEF.md) | `n_brief` — creative brief + Production Lock Bible |
| [STORYBOARD.md](./STORYBOARD.md) | `n_storyboard` — timed windows + start/end state |
| [CHARACTER.md](./CHARACTER.md) | `n_character_*` — solo identity sheets |
| [SCENE.md](./SCENE.md) | `n_scene_*` — empty environment plates |
| [CLIP.md](./CLIP.md) | `n_clip_*` — R2V shots (on-screen solos + scene) |
| [ORCHESTRATORS.md](./ORCHESTRATORS.md) | Supervisor + Manager (not leaf nodes) |

Parent map: [`../README.md`](../README.md) · Repo narrative: [`../../../../../DETAIL.md`](../../../../../DETAIL.md)

## Graph topology (quality.v5)

```
n_brief
   └── n_storyboard
          ├── n_character_*  (also edged from n_brief)
          ├── n_scene_*      (also edged from n_brief)
          └── n_clip_*       (storyboard + ON-SCREEN solos + scene only)
                 └── n_compose
```

Same-setting clips do **not** depend on each other. Overseeing agents run around these nodes (plan / capability / leaf prompt gate / ratings), not as canvas nodes.
