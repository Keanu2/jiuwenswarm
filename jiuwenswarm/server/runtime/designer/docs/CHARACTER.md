# Character node (`n_character_*`)

**Handler:** `handlers/image_nodes.py` `CharacterDesignNodeHandler`  
**Type:** image  
**One node per cast member.** The image is a solo identity sheet: one person, plain backdrop, locked costume.

## Required input

| Source | Data |
|--------|------|
| `n_storyboard` | Who this character is and how they should look |
| `n_brief` | Optional ordering edge and production specs |
| Node config | `character_name`, costume lock, style lock, prompt |
| User uploads | Reference images, when the user attached them |
| Director gate | `review_leaf_media_prompt` approves the still prompt before the image call |

The node does not need other characters' images. It does not need the scene image.

## What the node does

1. Builds a character prompt from the name, storyboard text, and locks.
2. `ensure_still_tool_prompt` keeps the prompt in the image-model form (solo sheet, no scene action).
3. Calls image generation (`VISUAL_GEN`) with the locked size. User reference files are passed through when present.
4. If generation fails, it writes fallback character notes instead of an empty output.
5. Stores the image path on the node output and a character card ref for later shots.

## Output to the next node

| Output | Next node |
|--------|-----------|
| Solo PNG (or notes if the image call failed) | Only shots whose storyboard row lists this character **on screen** |
| Identity lock (face, hair, body, costume) | Those shots' reference-image list |

A character who is off screen for a shot is not an input to that shot.
