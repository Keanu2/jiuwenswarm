# Reference-Led Test Catalog

**Suite file:** `jiuwenswarm/tests/unit_tests/test_reference_led_scenarios.py`  
**Upload / Enter helpers:** `jiuwenswarm/tests/unit_tests/designer/test_user_references.py`  
**Branch:** `0.2.8.beta1-referenceModeFix`  
**Last full run (this change set):** **20059 passed** (`20 × 1000` parametric + dedicated Fix tests + user_references).

---

## 1. How the 20 000 matrix is built

`_CASES_PER_SCENARIO = 1000` for each of `_SCENARIO_KINDS`:

| # | Kind | Pipeline slice |
|---|---|---|
| 1 | `product` | Solo product still, verbatim |
| 2 | `motion` | Solo motion still, verbatim/condition I2V |
| 3 | `scene` | Solo scene still, verbatim |
| 4 | `character` | Solo character still + uncovered set plate |
| 5 | `text` | No refs — classic quality.v5 |
| 6 | `character_family` | Verbatim lead + 2 companions + uncovered plates |
| 7 | `character_condition_family` | Condition lead sheet + 2 companions + plates |
| 8 | `product_cast` | Product + 3 companions + store plate |
| 9 | `scene_cast` | Scene verbatim + 3 companions + uncovered plate |
| 10 | `motion_cast` | Motion + 2 companions; no plates |
| 11 | `id_reconcile` | `xiaoyue` → `char_1`; parents companions |
| 12 | `five_stills` | 5 character uploads; 1 companion; Wan keeps uploads |
| 13 | `stale_solo_flags` | Leftover solo/suppress JSON ignored; family still generated |
| 14 | `mixed_roles` | Character + scene + product mix; uncovered cast/set |
| 15 | `two_character_stills` | Two covered leads; one companion |
| 16 | `all_cast_covered` | Three stills cover all three cast; 0 companions |
| 17 | `scene_condition_extra` | Condition scene + extra uncovered setting |
| 18 | `product_and_scene` | Product + locked scene + 3 companions + extra plate |
| 19 | `style_and_character` | Style still does not cover a person; companions still mint |
| 20 | `motion_stale_flags` | Motion + leftover suppress JSON; companions still mint |

### Dimensions averaged inside each kind (index `0..999`)

| Dimension | Formula | Distinct values |
|---|---|---|
| Shot count | `2 + (index % 3)` | 2, 3, 4 |
| Per-shot duration | `2 + ((index + shot) % 5)` | 2–6 s |
| Look | `look-{index % 17}` | 17 |
| Medium | `medium-{index % 13}` | 13 |
| Lighting | `light-{index % 11}` | 11 |
| Crowd | `crowd-{index % 7}` | 7 |
| Binding (motion / motion_cast) | even→verbatim, odd→condition | 2 |

Shared assertions (`_assert_shared_contract`): clip count, action uniqueness, timeline continuity, previous_end_state chaining, style_lock look/medium, lighting/crowd in prompts, film_duration_sec sum.

---

## 2. Parametric tests — what each kind asserts

### `test_product_scenario` × 1000
- `n_ref_01` immutable handler  
- No identity sheet / medium_change / scene plates  
- Clips R2V, contract `product`, plan[0] = product path, prompt contains `Image 1`

### `test_motion_scenario` × 1000
- I2V clips; first-frame language in prompt  
- Verbatim → `reference_first_frame` = upload path; no restyle  
- Condition → `n_restyle_01` medium_change; first_frame_node set  

### `test_scene_scenario` × 1000
- No sheets/plates  
- R2V scene contract; last plan entry = scene upload path; “last reference” cue  

### `test_character_scenario` × 1000
- Verbatim solo: card role `character_design`, no sheets, no invented plates  
- Plan[0] = `n_ref_01` upload path  

### `test_text_film_scenario` × 1000
- Not reference_led  
- Has character_design + scene nodes  
- `video_generation_overrides` force_reference_mode with fallback images  

### `test_character_family_scenario` × 1000
- Verbatim lead card kept  
- Exactly **2** `companion_cast` sheets  
- Uncovered analysis rooms plated (`n_scene_*`)  
- Plan: upload first, then two `n_character_*`  

### `test_character_condition_family_scenario` × 1000
- No verbatim card role on upload  
- **3** identity sheets (1 condition self + 2 companions)  
- Uncovered rooms plated  
- Plan leads with `n_character_*` empty path  

### `test_product_cast_scenario` × 1000
- **3** companion sheets (product covers no cast ids)  
- **1** companion store plate  
- Plan still leads with product  

### `test_scene_cast_scenario` × 1000
- **3** companion character sheets  
- Locked `set_1` stays on `n_ref_01`; uncovered `set_2` → one plate  
- Plan includes scene upload node  

### `test_motion_cast_scenario` × 1000
- I2V preserved  
- **2** companions; **no** scene plates (motion policy)

### `test_id_reconcile_scenario` × 1000
- Slot `xiaoyue` persists as `char_1`; companions are parents only  

### `test_five_stills_scenario` × 1000
- 5 verbatim character uploads; one uncovered companion + plate cards  
- Wan plan keeps all five uploads (`len(plan) ≤ 5`)  

### `test_stale_solo_flags_scenario` × 1000
- Intent still has `solo_subject` / `suppress_companions` / `keyframe_complete`  
- Topology ignores them: 2 companions + plates  

### `test_mixed_roles_scenario` × 1000
- Character + scene + product stills  
- 2 uncovered people companions; uncovered `set_2` plate; product first on plan  

### `test_two_character_stills_scenario` × 1000
- Two verbatim character cards; only `char_3` companion  

### `test_all_cast_covered_scenario` × 1000
- Three stills cover the whole family → **0** companions; rooms still plated  

### `test_scene_condition_extra_scenario` × 1000
- Condition scene + extra setting → generated plate(s)  

### `test_product_and_scene_scenario` × 1000
- Product + locked scene; 3 companions; plate for `set_2`  

### `test_style_and_character_scenario` × 1000
- Style still does not consume a character id; 2 companions + plates  

### `test_motion_stale_flags_scenario` × 1000
- Motion + leftover suppress JSON → 2 companions; I2V; no plates  

---

## 3. Dedicated (non-parametric) Fix / intent tests

| Test | What it verifies |
|---|---|
| `test_reference_led_source_has_no_scene_specific_rules` | No hardcoded scene slogans in `reference_led.py` |
| `test_text_call_shape_is_unchanged_without_a_mode` | Text path call shape without reference mode |
| `test_chat_action_edit_*` / `test_chat_shot_removal_*` | Chat edits keep reference-led handoff / rechain |
| `test_chat_cannot_remove_a_still_a_reference_led_clip_uses` | Cannot delete identity sheet a clip depends on (condition path) |
| `test_director_does_not_rebuild_a_reference_led_graph` | Director ack source `reference_led` |
| `test_character_verbatim_fills_card_and_skips_sheet` | Verbatim solo: card filled, no sheet/plate |
| `test_character_omitted_binding_defaults_to_verbatim` | Empty bindings → verbatim default |
| `test_character_condition_still_builds_identity_sheet` | Condition → sheet; no auto plate |
| `test_scene_condition_without_lock_may_invent_plate` | Scene condition invents plate |
| `test_scene_set_lock_locks_set_and_skips_plate` | set_lock skips invented plate for that set |
| `test_style_authority_without_vision_locks_to_reference` | `match_reference_still` medium |
| `test_style_authority_with_vision_inherits_named_medium` | Vision medium wins |
| `test_user_medium_wins_when_no_style_authority` | Text style kept |
| `test_product_verbatim_keeps_plan_path_and_card` | Product card + path |
| `test_motion_verbatim_i2v_without_restyle` | I2V first frame = file |
| `test_motion_condition_builds_restyle_node` | Restyle node |
| `test_enriched_classify_fields_persist_into_slots` | set_lock / style_authority / style_read stamp |
| `test_verbatim_character_multi_cast_builds_companion_sheets` | Xiaoyue-family style companions |
| `test_verbatim_character_solo_cast_skips_companion_sheets` | Solo → no companions |
| `test_condition_character_sheets_self_plus_companions` | Condition self + companions |
| `test_scene_verbatim_with_cast_builds_companion_sheets` | Scene lock + cast companions |
| `test_product_with_cast_builds_companion_sheets` | Product + cast |
| `test_motion_multi_cast_mints_companions_ignoring_stale_flags` | Motion companions even with leftover suppress JSON |
| `test_multi_still_covers_each_id_only_uncovered_get_companions` | Multi-still coverage |
| `test_stale_solo_subject_does_not_wipe_uncovered_companions` | T1: 4-cast + solo_subject still → 3 companions |
| `test_xiaoyue_alias_covers_char_1_and_parents_are_companions` | T3: id reconcile |
| `test_character_still_plates_uncovered_analysis_scenes` | T6: character job plates storyboard rooms |
| `test_scene_still_plates_only_uncovered_setting` | T7: plate uncovered set only |
| `test_plan_cap_keeps_uploads_over_generated_companions` | T8: `_cap_plan` prefers uploads |

---

## 4. Upload / Enter tests (`test_user_references.py`) — selected

| Test | What it verifies |
|---|---|
| `test_normalize_copies_path_and_assigns_ordered_slots` | Path copy + roster labels |
| `test_normalize_decodes_base64_and_rejects_over_limit` | Base64 decode + max 5 images |
| `test_materialize_for_analysis_gives_path_to_base64_only_upload` | **Enter fix:** preview path empty; analysis materialize has file |
| `test_rebase_creative_intent_paths_onto_project_refs` | Temp → project path rebase |
| `test_attach_rebases_existing_n_ref_path` | Existing `n_ref_*` path refresh |
| `test_bootstrap_graph_registers_user_references` | Classic bootstrap still attaches refs (no false fail-closed) |
| `test_classify_reference_images_*` | Product object + enriched intent fields |
| `test_user_reference_node_is_immutable_passthrough` | Handler returns upload bytes |
| `test_character_card_edits_the_reference_instead_of_redesigning` | Edit path vs redesign |
| `test_analyze_creative_brief_sends_reference_images` | Vision paths forwarded |

---

## 5. How to re-run

```bash
cd jiuwenswarm
.venv/Scripts/python.exe -m pytest \
  tests/unit_tests/test_reference_led_scenarios.py \
  tests/unit_tests/designer/test_user_references.py \
  -q --tb=line --no-cov
```

Expect ≈ **20 000** parametric scenario tests + dedicated Fix tests + user_references tests (**20059** in the last combined run).

---

## 6. Coverage notes / gaps

| Covered well | Residual |
|---|---|
| Solo vs family companions across character/product/scene/motion | Live LLM classify quality not unit-tested |
| Verbatim vs condition | Wan pixel drift not asserted |
| Suppress companions on motion_cast | Director redesign race only lightly covered |
| Base64 materialize + path rebase | Plan cap=5 truncation under huge casts not fuzzed |
| Style_lock medium on companion sheets | End-to-end browser Enter not in this suite |
