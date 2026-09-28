# Unused designer pieces removed

This file tracks deletions on this branch: graph fields nothing read, the unused `NodeAgent` class, and docs that no longer described anything. Removing them does not change fail-closed chat-model behavior.

`director_composed_on_bootstrap` stays. `executor.py` reads it and skips a second Enter redesign when the director already composed the graph. `designer_adapter.py` and `orchestration.py` set it.

The scan covered the designer runtime, the designer gateway adapter, designer tests, and the frontend designer feature. Keys that are written into a prompt, a tool result, or a script report were left in place, because those objects are consumed as a whole even when no later line calls `.get()` on that one key.

## Flags removed earlier in this cleanup

| Field | Files | What it was |
|---|---|---|
| `director_owns_graph` | `smart_graph.py`, `orchestration.py`, `executor.py` | Always `True`. A rename of `supervisor_owns_graph`, which `design` deleted. `executor.py` copied it onto a rebuilt graph and never branched on it. |
| `allow_still_clip_fallback` | `smart_graph.py` | Always `False` on the shot config and on graph metadata. `design` removed the `clip.py` reader that turned a failed video call into a still mp4. |
| `ai_agent_pipeline` | `smart_graph.py` | Always `True` in `build_smart_video_graph`. This used to switch agent mode versus the heuristic path. `design` always stamps creative nodes as agents and stopped writing the flag. |
| `all_nodes_agents` | `smart_graph.py` | Always `True` next to `ai_agent_pipeline`. `design` deleted it. Nothing read it. |
| `lean_pipeline` | `orchestration.py` | Always `False` after a director rebuild. The executor used to treat it like `freeze_shot_topology`. That read is gone. |
| `graph_designed_by_director` | `orchestration.py` | `False` when a redesign would shrink the cast, `True` after a rebuild. A rename of `graph_designed_by_supervisor`. Write only. |
| `pending_director_graph` | `orchestration.py` | Set `False` on that same cast-shrink return. A rename of `pending_supervisor_graph`. Write only. |
| `pending_llm_analysis` | `orchestration.py` | Set `False` on that same cast-shrink return. `design` removed the readers that skipped redesign while analysis was still pending. |
| `skip_scene_specs` | `orchestration.py` | Forced `False` on metadata, shot config, and `identity_refs`. Readers that skipped scene cards are gone. Scene specs stay the only path. |

`executor.py` also had an early read of `director_composed_on_bootstrap` that was overwritten before the Enter-redesign decision. That dead read was removed. The later read in the same function is the one that still decides whether to redesign.

## Class removed

`class NodeAgent` in `orchestration.py` was an old per-node runner. Nothing constructed it. The live runner is `NodeAgentHost` in `node_agent.py`.

Inside that class, and only there:

| Field | What it was |
|---|---|
| `artifact_summary` | Text parsed from the model JSON and returned on the unused runner's feedback and payload. |
| `model_used` | The model name returned on that same unused feedback dict. |

## Labels and summaries removed in this pass

| Field | Files | What it was |
|---|---|---|
| `director_id` | `smart_graph.py`, `composer.py`, `static_graphs.py` | The string `"director"` inside `metadata.orchestration`. A rename of `supervisor_id` on `design`. Nothing looked it up. The run calls `Director()` directly. |
| `validated_by_director` | `orchestration.py` | Always `True` on `consistency_plan`. A rename of `validated_by_manager` on `design`. Nothing looked it up. |
| `consistency_plan` | `smart_graph.py`, `orchestration.py` | A stored summary of lock policy (`character_identity`, `costume_lock`, `empty_scene_specs`, `validated_by_director`, and the rest). `orchestration.py` copied it and wrote it back. No other file read it. The real locks stay on `scene_continuity_mode`, `scene_locks`, `scene_masters`, and node config. |
| `metadata.orchestration` | `smart_graph.py`, `composer.py`, `static_graphs.py` | A stored note: `director_id`, `planner: director_llm`, a flow string, and a description of the director. Nothing read the object. |
| `agentic` | `smart_graph.py`, `composer.py`, `static_graphs.py` | Always `True` on graph metadata. Nothing branched on it. Creative nodes are already stamped `delegate: agent`. The word "agentic" in the UI progress sentence is ordinary text and was left as is. |
| `catalog_schema` | `composer.py` | Copied the catalog file's `schema_version` onto the graph. Nothing read it. The extra `load_node_catalog()` call that existed only to read that version was removed with it. `catalog_nodes_by_id()` still loads the catalog for the nodes. |

## Docs removed

| File | What it was | What replaced the link |
|---|---|---|
| `jiuwenswarm/server/runtime/designer/docs/ORCHESTRATORS.md` | A three-line stub. It named `Director` in `orchestration.py` and pointed at `DIRECTOR.md` and `PIPELINE.md`. It did not describe inputs, steps, or outputs. The overseer detail lives in those two files. | Root `README.md` and `jiuwenswarm/server/runtime/designer/README.md` now link to `DIRECTOR.md`. |

The other designer docs stay. `INDEX.md`, `PIPELINE.md`, `DIRECTOR.md`, `BRIEF.md`, `STORYBOARD.md`, `CHARACTER.md`, `SCENE.md`, `CLIP.md`, and `COMPOSE.md` each still name a handler, inputs, what the stage does, and what it passes on.
