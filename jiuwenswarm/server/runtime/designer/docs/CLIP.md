# Shot node (`n_clip_*`)

**Handler:** `handlers/clip.py` `ClipNodeHandler`  
**Type:** video  
**One node per storyboard row.** It renders that time window as a reference-to-video shot.

## Required input

| Source | Data |
|--------|------|
| `n_storyboard` | This row: timeline, camera, action, speech, start state, end state |
| On-screen `n_character_*` | Solo images for people in this shot only |
| Matching `n_scene_*` | Empty scene image for this `setting_id` |
| Director gate | `review_leaf_media_prompt`, then `director_approve_video_prompt` |
| Optional edges | Text or video the user connected on the canvas |

The shot does not take the full user story, the brief file, or off-screen characters. It does not wait for other shots in the same scene.

## What the node does

1. Loads the storyboard row for this shot index.
2. Collects reference images: on-screen character solos and the scene still. Predecessor outputs follow edges. A user replacement is kept; an old generated file is not put back.
3. `build_clip_prompt` leads with the storyboard action, then character consistency, scene consistency, and speech locks.
4. Wan prompt cleaning and call locks trim the prompt to the video limit and lock resolution (480p path) and duration.
5. Submits one video job and waits for the file.
6. May stamp a last-frame handoff for a later shot when the graph asks for it. Same-setting shots still run concurrently.

## Output to the next node

| Output | Next node |
|--------|-----------|
| Shot video file (mp4) | `n_compose`, in storyboard order |
| Duration from the storyboard timeline | Compose, which rejects empty or zero-length files |
| Prompt artifact | Run feedback, not the next shot's video prompt |

Compose does not start a final film until every shot file is usable.
