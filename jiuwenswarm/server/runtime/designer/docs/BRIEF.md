# Brief node (`n_brief`)

**Handler:** `handlers/text_nodes.py` `BriefNodeHandler`  
**Type:** text / markdown  
**Author:** the director (`Director.author_creative_brief`), then this node writes the file.

## Required input

The brief is the root. It has no upstream node.

| Source | Data |
|--------|------|
| User prompt | The film request typed in the designer |
| Graph metadata | Audio intent, style, optimize mode |
| Director | `metadata.approved_brief` when the LLM already authored one |

## What the node does

1. If `metadata.approved_brief` is set, the handler stamps production specs onto that text and does not call the model again. An empty chat model still fails closed when that text is missing.
2. Otherwise it asks the leaf text model for a creative brief: logline, cast, scenes, tone, and locks.
3. `_stamp_specs_on_text` appends production specs (character consistency, scene consistency, language, aspect).
4. The markdown is written to the workspace as `designer_brief_<run>_<node>`.

## Output to the next node

| Output | Next node |
|--------|-----------|
| Markdown brief file (`output_ref`, kind text) | `n_storyboard` via edge `e_brief_storyboard` |
| Specs copied onto graph metadata | Character, scene, and shot configs |

Characters and scenes may list the brief as an ordering input. Shots do not read the brief file. They read the storyboard row and the locks stamped from it.
