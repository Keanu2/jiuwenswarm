# Scene node (`n_scene_*`)

**Handler:** `handlers/image_nodes.py` `SceneNodeHandler`  
**Type:** image  
**One node per setting.** The image is empty scene specs: the room or location, with no people baked in.

## Required input

| Source | Data |
|--------|------|
| `n_storyboard` | Setting name, architecture, light, and time of day |
| `n_brief` | Optional ordering edge |
| Node config | `setting_id`, scene specs, style lock, spatial lock |
| Earlier scene image | Only when this node is a derived view (`scene_strategy=edit_master_view`) |
| Director gate | Approved still prompt before the image call |

People are not an input. They enter later, from character solos, at shot time.

## What the node does

1. Builds a scene prompt from the storyboard setting text and scene specs.
2. If this is a derived view, it edits the master scene image instead of inventing a new place.
3. `ensure_still_tool_prompt` keeps the prompt as an empty environment.
4. Calls image generation at the locked size.
5. Writes the PNG, or fallback scene notes if generation fails.

## Output to the next node

| Output | Next node |
|--------|-----------|
| Empty scene PNG | Every `n_clip_*` with the same `setting_id` |
| Scene specs (architecture, light, props) | Those shots' reference list and prompt locks |

Shots do not start until this image is on disk, unless the graph explicitly skips scene specs.
