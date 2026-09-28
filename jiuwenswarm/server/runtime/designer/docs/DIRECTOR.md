# Director

**Class:** `Director` in `orchestration.py`  
**Not a canvas node.** One agent oversees the film from the user prompt through the composed video.

The director authors the plan and also gates it. It is not spawned as a second agent.

## What it receives

| When | Input |
|------|--------|
| Bootstrap | User prompt, optional reference images, optimize mode (`cost` or `quality`) |
| Play | The execution graph, configured chat/vision/video models, leaf node state |
| Each media leaf | That node's prompt plus upstream storyboard, character, and scene outputs |
| After compose | Per-node messages, artifact summaries, and optional vision notes |

## What it does, in order

1. **Capabilities.** `decide_capabilities` reads which chat, vision, and video models are configured and stamps a modality plan on the graph.
2. **Brief.** `author_creative_brief` writes the creative brief and production specs (cast, costumes, style, language, occupancy).
3. **Storyboard.** `author_storyboard` writes timed shots: who is on screen, the action, camera, speech, `start_state`, and `end_state`.
4. **Graph.** `design_execution_graph` builds or refreshes nodes and edges: brief → storyboard → characters and scenes → shots → compose.
5. **Validate.** `validate_plan` checks cast, scene consistency, aspect and style locks, prunes nodes that cannot reach compose, and onboards user-added canvas nodes.
6. **Leaf gate.** `review_leaf_media_prompt` runs before each character, scene, and shot call. It keeps wardrobe, scene specs, speech uniqueness, and the current storyboard row.
7. **Ratings.** After compose, `finalize`, `review`, and `assign_dual_raters` score the run. Ratings are stored and apply only on Run again.

If no chat model is configured, the same methods use deterministic heuristics (`plan_fast`, `validate_plan` without an LLM).

## What it outputs to the next stage

| Output | Consumer |
|--------|----------|
| Brief markdown and production specs | Storyboard node |
| Storyboard rows | Character, scene, and shot nodes |
| Node configs (`director_task`, locks, prompts) | Leaf agents and handlers |
| Approved media prompt | The image or video call for that node |
| Feedback bundle under `runs/` | The next Play, only if the user runs again |

The director does not call the image or video model for every leaf. Leaf nodes do that after the gate.
