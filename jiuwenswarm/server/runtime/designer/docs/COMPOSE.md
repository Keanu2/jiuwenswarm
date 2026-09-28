# Compose node (`n_compose`)

**Handler:** `handlers/compose.py` `ComposeNodeHandler`  
**Type:** video  
**One node for the finished film.**

## Required input

| Source | Data |
|--------|------|
| Every `n_clip_*` | A completed, non-empty video file |
| Run state | Each shot status is completed. Failed or missing shots block compose |
| Optional audio nodes | Speech or music stems when those backends exist |

The handler polls until every expected shot is usable. It refuses a film made of zero-length stubs.

## What the node does

1. Lists shot nodes in storyboard order.
2. Waits until each shot status is completed and `collect_clip_video_paths` returns a real file per shot.
3. Rejects paths that fail a duration check (`_clip_video_usable`).
4. Concatenates shot 1, then shot 2, and so on. If stream copy fails, it falls back to a filter concat.
5. Muxes audio stems when they were generated. Otherwise the shot files already carry their own sound.
6. Writes the final mp4 into the workspace.

## Output

| Output | Consumer |
|--------|----------|
| Final composed video | The designer canvas asset and the project workspace |
| Node completion message | The director's end-of-run rating (`finalize`, `review`, `assign_dual_raters`) |

Nothing in the graph is downstream of compose. The director writes the run report after this file exists. That report does not change the video unless the user runs again.
