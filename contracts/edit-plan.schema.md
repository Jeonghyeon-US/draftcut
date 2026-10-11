# EditPlan contract

Status: normative contract. `edit-plan.schema.json` is its
machine-readable form; this Markdown wins on a conflict.

## Core semantics

The JSON files in `contracts/examples/` are contract fixtures, not a real edit.
Their IDs, dates, tool versions and approval/compatibility states are illustrative
declarations, not measured evidence or rights clearance. The caption font entry
names a macOS system font by family name and file hash as an example of a
`licensed_system_font` asset; no font, media or raster asset is supplied here.
Passing the contract checker does not establish that these examples will render.

An EditPlan is the authoritative, pre-render decision record. It maps ordered
source segments to independently timed timeline clips using the integer
rational time form from the Index contract. It must carry the paired index ID,
not a filesystem path. The contract checker compares exact `Fraction` values only.

Every canonical tick value is non-negative (`minimum: 0`): canonical time denotes
a position either within source material or on the output timeline, and both axes
begin at 0. This lower-bound rule does not assert an upper bound (`< D`); that is a
separate proposition requiring the relevant duration (the output timeline is `[0, D)`,
as `contracts/frame-grid-semantics.spec.md` specifies).

**"Ordered source segments" is ordered *per source*, not globally**.
Timeline clips are sorted by timeline start; the source-range starts
they reference must be non-decreasing **within each `source_id`**, and **no ordering
relation exists between different sources**. A multi-source plan may place source
B's `t = 5` after source A's `t = 100` without declaring anything.
`act_reorder_segments` covers reordering *within* a source: it changes the order
of *selected timeline segments* and is checked against locked-order constraints.

The executor accepts that action only when its required `ordered_segment_ids` exactly
matches the `timeline_clips` order sorted by `timeline_range.start`; the declaration
therefore records and verifies intent without changing rendering.
**The rule is named `timeline-global-source-order`; "global" means
"across the whole timeline of one source", never "across sources".**

`preserve`, `lock_order`, and unresolved `review(blocking)` constraints beat
all actions. Only a human release with an evidence ID, rationale, and timestamp
can remove a constraint; automatic release and automatic retry are forbidden.
`ready_for_render` and `rendered` plans must have no unresolved blocking review.
Define preservation and order constraints before planning. Review edit meaning
and sensitive-information exposure during quality control (QC) before and after
rendering.

Assets are records, not free-form paths. A renderable reference requires ID,
SHA-256, rights state, usage scope, source class (`project_provided` or
`external_candidate`), and `approval_status: approved`. An external candidate
is therefore not silently interchangeable with a project-provided asset.

A raster asset record may additionally declare `pixel_width` and `pixel_height`
(integers ≥ 1). These dimensions let the coverage checker determine overlay
geometry without opening asset files. If either dimension is absent, the checker
requires the PNG file as `<asset_id>.png` through `--assets-dir`; it does not guess
dimensions. Declared values must match the referenced file.

Tracks use the closed kinds `video`, `dialogue`, `music`, `sfx`, `graphics`,
and `captions`; visual tracks have explicit z-order and audio tracks have
mix-order. Every clip binds a track ID, source range, timeline range, and
rounding rule. The schema records source and timeline ranges separately; the
executor does not support J-cuts or L-cuts. Caption specifications bind a caption
track and timeline range, and can optionally point at an approved raster asset.
The renderer uses PNG overlays from `executor/caption_png.py` rather than libass.

Every action declares its canonical `v1_executable` status and verification
class. The registry currently holds **31 `yes(effect-verified)` and
0 `yes(container-only)`** entries. These statuses record historical development
measurements using **synthetic fixtures**, not real-content quality or performance;
the source media and measurement records are not included in this repository.
Registry status alone does not establish executor support: J-cuts and L-cuts have
`yes(effect-verified)` entries but no render path in this executor. `blocked`
and `no(out-of-scope)` actions are rejected before rendering.

`pre_render_qc` and `post_render_qc` use severity plus a human-escalation state;
no result may request automatic retry. Reproducibility fields reserve ffmpeg
version, exact filter arguments, two-pass loudnorm measurements, render log ID,
and output hash. Static crop and manual keyframes are allowed; automatic subject
tracking is represented as `failure` plus review, never inferred as a crop.

## Resolved backend and cost approval

`backend_selections` is the shared backend provenance field used by Index and
Skill output as well as EditPlan. Each record has `purpose` (`stt`,
`caption_renderer`, `ocr`, `shot_scene_boundary`, or `screen_activity`), `backend_id`,
`backend_version`, and a resolved
`cost_tier` of `무료-로컬` (free, local execution), `쿼터` (quota-based usage),
or `유료` (paid usage). These are literal JSON values; use these strings exactly
as written. `혼합` (mixed usage) is only a registry planning classification, not a
valid runtime selection: before execution it must resolve to one of these three
values. This avoids separate STT and subtitle-renderer provenance designs.

`cost_summary` records the plan's non-negative estimated total, currency, and
basis. Each prospective charge is a `cost_calls` record with a single call ID,
the selected backend, its resolved cost tier, an estimated cost, and either a
linked review ID or `null`. A paid (`유료`) call must link to an existing
`review(blocking)` record. The review's optional `cost_approval` is a
per-call approval record and always includes the same call ID, estimated cost,
approver, and timestamp. At render stages that review must already be approved
or released under the existing review gate; no second blocking mechanism exists.
Every backend selection resolved as paid (`유료`) must have at least one
`cost_calls` record with the same `backend_id`; otherwise the proposed paid
execution has no call-level approval path.

## Execution parameters

Execution parameters specify the values needed to apply an action. Keep clip
placement in `timeline_clips`, operation values in `actions[].params`, delivery
settings in top-level `output_policy`, and reusable caption styles in
`caption_styles`. Keep `ordered_segment_ids` at the action's top level rather
than duplicating it under `params`.

### `actions[].params` — per-action execution values

An action's permitted `params` shape and its required status depend on the action
name and plan stage, as defined in `edit-plan.schema.json`. Unknown parameter
keys are rejected.

**Where the required boundary falls.** `output_policy`, `caption_styles[]`, and
`concat_compatibility` are present on every plan at every stage, and every field
within them is required — a plan carries its delivery policy and its compatibility
verdict from the moment it exists, because the verdict's `unverified` value is
what a not-yet-checked plan is supposed to say. `params` is required even in
`draft` for `act_freeze_frame`, `act_zoom_keyframes`, `act_blur_region`,
`act_overlay_asset`, and every parameterized audio action: `act_crossfade_audio`,
`act_gain`, `act_noise_reduce`, `act_fade_audio`, `act_normalize_loudness`,
`act_add_music`, `act_add_sfx`, `act_add_ambience`, `act_mix_audio`, and
`act_duck_audio`. For `act_crop_reframe`, `act_scale_position`,
`act_speed_change`, `act_add_caption_segment`, `act_style_caption_segment`,
`act_assemble_segment`, `act_repeat_segment`, `act_ripple_delete`, and
`act_insert_segment`, the schema permits omission in `draft` but requires `params`
at `ready_for_render` and `rendered`. Executor validation may require complete
parameters before accepting a draft. Actions outside the schema's parameterized
set must not carry `params`.

The table below describes a subset of supported action parameter shapes.
Additional shapes for repeat and audio actions are defined by these `$defs` keys
in `edit-plan.schema.json`: `repeat_segment_params`, `crossfade_audio_params`,
`gain_params`, `noise_reduce_params`, `fade_audio_params`,
`normalize_loudness_params`, `add_music_params`, `add_sfx_params`,
`add_ambience_params`, `mix_audio_params`, and `duck_audio_params`.

| Action | `params` |
|---|---|
| `act_crop_reframe` | `x`, `y`, `width`, `height` (integer pixels), `coordinate_origin` |
| `act_scale_position` | `width`, `height` (integer pixels), `background` (an `rgba`), `fit` |
| `act_speed_change` | `range` (a source-axis range), `factor` (`rational`, > 0), `audio_handling` |
| `act_freeze_frame` | `source_time`, `timeline_duration` (canonical time); the executor derives the sole speed factor from the declared output frame grid |
| `act_zoom_keyframes` | `interpolation: linear`, and at least two `keyframes[]` of timeline `time`, positive rational `zoom`, integer output-canvas `center_x`/`center_y` |
| `act_blur_region` | `filter` (`gaussian` or `box`), positive rational `strength`, and even output-canvas `x`, `y`, `width`, `height`; no time field (the whole piece) |
| `act_add_caption_segment` | `caption_id`, `overlay_x`, `overlay_y` (integer pixels), `enable_axis` |
| `act_style_caption_segment` | declaration: exactly `caption_id`, `style_id`; each names one caption and one style, `style_id` equals that caption's `style_ref`, and the caption has one `act_add_caption_segment` action |
| `act_assemble_segment` | `join_mode` |
| `act_ripple_delete` | declaration: exactly `scope: all_tracks`; `source_segment_id` names the left timeline piece, whose immediate right neighbour has the same `source_id`, a positive source-axis gap, and no timeline gap |
| `act_insert_segment` | declaration: exactly `mode: ripple`; `source_segment_id` names the inserted timeline piece, whose immediate neighbours share a `source_id`, touch on the source axis, and have no timeline gaps around the inserted piece |
| `act_overlay_asset` | `overlay_x`, `overlay_y` (integer pixels), `enable_axis`, `timeline_range` |

**`act_speed_change.factor` is a *speed* multiplier, not a duration multiplier**.
`factor = 2` means **twice as fast**: the clip's output duration is
`source_duration / factor` and the executor emits `setpts=PTS*den/num`. The direction
is expressed as division: a 105-second source range at `factor = 2` occupies
52.5 seconds on the timeline.

**The direction is easy to get backwards, and nothing cross-checks it for you:
the contract checker does not verify timeline duration against `factor`.**
The fixture `contracts/examples/edit-plan.valid.json` still shows a `factor` of 2
on a 105-second source range with a 105-second timeline range — arithmetic that is
wrong under **either** reading. The checker accepts it because it never performs this
multiplication. **Do not copy that example's numbers**; follow the rule stated above.

**`act_overlay_asset` carries its own `timeline_range`** because, unlike a
caption, an overlaid asset has no `captions[]` record to read a range from. The
asset itself is named by the action's top-level `asset_id` and must resolve to
one approved `assets[]` record. It supports an
opaque mask over an on-screen identifier, which no other action in this parameter
set can express: `act_crop_reframe` removes only edges,
and a caption cannot cover a region without writing text into it.

**`ordered_segment_ids` stays where it already is — a top-level property of the
action — and does not appear under `params`.** One meaning gets one canonical
home; allowing both positions would create duplicate fields for the same meaning.

Two shared value shapes are used throughout this layer. **`rational`** is
`{"num": integer, "den": integer ≥ 1}`, never a decimal. **`rgba`** is
`{"r","g","b","a"}`, each an integer 0–255. Pixel positions and sizes are
integers. `act_crop_reframe` coordinates and dimensions are measured in source
pixels with a top-left origin; cropping occurs before scaling and padding.
`act_scale_position` width and height must match the output canvas. Caption and
asset overlay positions, blur rectangles, and zoom centres are measured in the
`output_policy` canvas, also with a top-left origin.

**Closed sets.** The following are closed value sets in the same sense as
`marker_type` and `tracks[].kind`: extending one requires a contract revision,
which is the point — it forces the widening to be a decision rather than a
side effect.

| Field | Values | Why only this |
|---|---|---|
| `coordinate_origin` | `top_left` | one origin convention, stated rather than assumed |
| `fit` | `contain` | the crop→scale→pad chain is the only opened geometry path |
| `audio_handling` | `drop` or `resample` | `drop` makes a silent speed-changed piece; `resample` keeps source sound at the speed factor when audio is present |
| `enable_axis` | `final_timeline` | see below |
| `join_mode` | `concat_copy` | `concat` + `-c copy` is the only opened join |
| `alignment` | `center` | `executor/caption_png.py` centres each line; anything else would be a value the renderer cannot honour |

**Do not read a closed set as a placeholder to be filled in later.** Each set
records the behaviours that the executor can currently honour. Declaring a value
the renderer cannot produce creates a criterion the execution path cannot satisfy.

When `act_speed_change.params.audio_handling` is `drop` and
`output_policy.audio_policy` is `present`, the speed-changed piece carries one
generated silent audio stream whose duration matches that piece's output video.
When `audio_handling` is `resample`, `output_policy.audio_policy` must be
`present`; the executor applies one `atempo=<factor>` audio filter. With
`audio_policy: none`, `resample` is rejected and `drop` continues to emit no
audio stream. Because this is a single `atempo` filter, a `resample` factor
must be within its supported `1/2`–`100/1` range.

`factor` is a rational, not a float, for the same reason canonical time is: a
decimal factor introduces binary64 rounding errors.

`act_freeze_frame` declares one source instant and its output hold duration; it
does not declare a factor. The executor accepts it only when the clip source range
is exactly one output frame and `timeline_duration * frame_rate` is integral, then
uses the deterministic factor `(1 / frame_rate) / timeline_duration`, with dropped
audio becoming generated silence when the output has audio. `loop` and `tpad` are
not part of this contract or path.

Captions may not overlap, and each caption must occupy at least one frame of the
output grid: with `f = output_policy.frame_rate`, a caption is displayable only when
`ceil(end * f) > ceil(start * f)`. The executor rejects a caption that fails this,
and it rejects it in plan validation — `--validate-only` reaches the check, so a plan
does not have to be rendered to learn that a caption is too short. Together the two
rules bound how many captions a timeline can carry: **at most one caption per output
frame**, i.e. `N <= duration * frame_rate`. That bound is a property of the contract,
not a measured limit; nothing has to be rendered to know where it lies.

`act_zoom_keyframes` uses timeline-axis times. `interpolation` is closed to
`linear`; first and last keyframes must be the clip timeline start and end, and
times must strictly increase. The executor applies the resulting `zoompan` only
after the canonical `fps=…:round=near` stage and before `setsar`; it never removes
or moves that CFR stage.

`act_blur_region` applies one fixed rectangle for the whole piece: it has no
time or tracking field. `filter: gaussian` maps to `gblur=sigma=<strength>` and
`filter: box` maps to `boxblur=luma_radius=<strength>:luma_power=1`; the box
filter's `luma_power` is fixed at `1`. All four rectangle fields are even, and
the rectangle must remain inside the `output_policy` canvas. The executor places
`split` → `crop` → blur → `overlay` before the canonical `fps`/`setsar` tail.
It rejects an `act_blur_region` and `act_zoom_keyframes` on the same source
segment because their declared coordinate spaces are not jointly specified.

**`enable_axis` is required and is not decoration.** Its single permitted
value, `final_timeline`, binds overlay times to the output timeline. The executor
composes captions and asset overlays after piece normalization and joining, so
source-axis speed changes and frame-rate conversion have already occurred.

### `output_policy` — delivery policy, outside the edit block

Delivery encoding policy is a top-level object, not an action.

`width`, `height`, `pixel_format`, `sar`, `frame_rate`, `frame_rate_mode`
(`cfr`), `encoder_timebase`, `color_primaries`, `color_transfer`, `color_matrix`,
`color_range`, `audio_policy` (`none` or `present`), and `time_axis`
(`final_timeline`).

**The colour fields and `sar` are required.** Declare sample aspect ratio and
colour metadata explicitly. The executor writes the declared values during
normalization, and the compatibility checker probes the resulting pieces against
`output_policy`; encoding alone is not evidence that the policy was applied.

**These are delivery decisions, and putting them in the contract is what
forces the executor to write them explicitly instead of letting the source leak
through.** This is prevention; `concat_compatibility` below is detection. Both are
needed, and they are not the same mechanism.

`frame_rate_mode: cfr` specifies constant frame rate. The executor resamples
video to `output_policy.frame_rate`; 30/1 is an example, not a global constant.
Compatibility checks use packet DTS (decode timestamps) for continuity. Packets
are returned in decode order, so do not expect constant or monotonic adjacent
packet PTS when B-frames are present. The current specification permits only its
declared boundary tolerances at joins.

**`encoder_timebase` is required and may not be left to the encoder default.**
The executor passes the declared value through `-enc_time_base`; the
compatibility checker compares each piece's probed timebase with that value.

For VFR input, source-time calculations use recorded frame PTS and the source
timebase. Resampling establishes the declared output grid; it does not invalidate
source-time arithmetic. Determine the source's timing from its probe data rather
than assuming that every source is VFR.

### `caption_styles[]` — reusable style definitions

`captions[].style_ref` references this array, separating reusable definitions
from their application sites. **Do
not add a second typography field to `captions[]`** — that would create two
canonical homes for one meaning.

`style_id`, `font_family`, `font_asset_id`, `font_size_px`, `line_gap_px`,
`alignment`, `fill_rgba`, `background_rgba`, `padding_px` (`x`, `y`),
`corner_radius_px`, and `max_width_ratio` (a `positive_rational`). Every field is
required on every entry; `font_size_px` is a positive integer and other pixel fields
are non-negative integers. `max_width_ratio` must satisfy 0 < ratio <= 1 because the
renderer cannot draw a text box wider than the frame.

**The font is `font_asset_id`, an `assets[]` reference — not a path.** "Assets
are records, not free-form paths" is already normative above, and a font is
exactly the kind of thing that carries rights state and needs a hash: a plan that
renders with a substituted font produces different pixels. `font_family` records
its family name; the renderer resolves the font through the verified asset
reference. Store typography as structured values so the style remains inspectable
independently of the rendered PNG. Caption text and display intervals alone do
not preserve those typography values.

`executor/caption_png.py` consumes `line_gap_px` from the contract style and
passes it to the renderer. Direct calls without contract inputs use `--line-gap`
with a default of 8 pixels; contract mode does not accept that override.

### `concat_compatibility` — a verdict, not a value list

**This block holds a verdict and its provenance. It does not hold the
compatibility conditions themselves.**

`verdict` is `compatible`, `incompatible`, or **`unverified`**; `spec_id` and
`spec_version` name the condition list the verdict was produced against;
`evidence_ids` carry the check output; `review_id` links the blocking review when
the verdict is `incompatible`.

`review_id` is a **required key with a nullable value** — the same shape
`cost_calls[].review_id` already uses, and for the same reason: the key always
being present is what makes "no review applies here" an explicit statement rather
than an omission. When `verdict` is `incompatible` it must be non-null and must
name an existing review whose `blocking` is true. When the verdict is `compatible`
or `unverified` it may be null; if it is non-null it must still name an existing
review, so a dangling reference is rejected either way.

- **`incompatible` forbids render.** It must link an existing `review(blocking)`;
 no second blocking mechanism is introduced.
- **`unverified` forbids `ready_for_render` and `rendered`.** `concat` with
 `-c copy` passes mismatched pieces with exit 0 and no warning, so "not checked"
 must never read as "passed". This is the failure mode the whole block exists for.
- **`spec_id` and `spec_version` are closed sets, not free strings**.
 The permitted spec ID is **`draftcut-concat-copy-compatibility`**, with versions
 **`1`** or **`2`** accepted by the schema. The current condition list in
 `contracts/concat-copy-compatibility.spec.md` and the executor checker use
 version **`2`**; a version-1 verdict is not a verdict against the current list.
 **This is what stops the field from being filled in as a plausible-looking
 placeholder** — `"spec_id": "concat-compat"` is now rejected by the contract checker rather than
 silently accepted, and a new version of the condition list has to be declared here
 before any plan can claim it. Widening the set is a contract revision, which is the
 point, exactly as it is for the other closed sets in this layer.
- **`spec_version` is the load-bearing field.** The condition list can
 change; without a version, an old verdict silently ages into a false assurance.
- The conditions are **not** a list of values that must be equal. Each has a
 scope of validity (a coded video sequence, MP4 sample description, or Matroska
 `TrackEntry`), and `closed GOP`, first-packet keyframe, edit list, and container
 timescale are not equality conditions at all. Writing them as
 surface equality builds a wrong check.

Rationale: ordered mapping prevents unintended sequence reversal; explicit
tracks and axes describe independent timing; captions and raster assets describe
visual additions; reproducibility records make renders auditable; executability
and QC gates control rendering; static, manual, and failure modes state the
limits of automation.
