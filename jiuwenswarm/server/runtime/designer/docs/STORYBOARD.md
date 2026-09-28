# Storyboard node (`n_storyboard`)

**Handler:** `handlers/text_nodes.py` `StoryboardNodeHandler`  
**Type:** table / markdown  
**Author:** the director (`Director.author_storyboard`), then this node writes the table.

## Required input

| Source | Data |
|--------|------|
| `n_brief` | Creative brief and production specs (hard edge) |
| Director plan | `planned_shots`: index, timeline, camera, action, on-screen cast, speech, `start_state`, `end_state` |
| Graph metadata | Shot budget, scene names, character consistency locks |

## What the node does

1. If `metadata.approved_storyboard` is set, the handler writes that text and does not call the model again. An empty chat model still fails closed when that text is missing.
2. Otherwise the leaf text model drafts the table from the brief.
3. Each row is one shot window: timeline, camera, action, consistency note, and the prompt seed for that shot.
4. The table is written to the workspace.

The storyboard is the consistency authority. Later shots must follow this row's start, action, and end, not an earlier video prompt.

## Output to the next nodes

| Output | Next nodes |
|--------|------------|
| Storyboard markdown | Every `n_character_*`, `n_scene_*`, and `n_clip_*` that lists `n_storyboard` as an input |
| Per-shot fields copied onto shot configs | Shot handler (`action`, `timeline`, speech, on-screen ids, scene id) |

Same-setting shots do not wait on each other. They wait on this table plus the character and scene images they need.
