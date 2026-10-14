# mask-plan

Three tools help prepare and check a masking plan: `locate_sensitive.py`,
`motion_triage.py` and `coverage_gate.py`. After rendering, use
`verify_render.py` for a sampled pixel check, within the limits below.

The coverage gate checks plan geometry without rendering. Start by extracting
source frames with OCR on macOS with PyObjC and Vision access. Use a fresh frame
directory for every source and run, and use that same directory for detection.

```bash
# 1. Extract source frames into a fresh directory
python3 skills/media-index/ocr_vision.py \
  --input /path/to/your-clip.mov \
  --output scratch/run/ocr.observations.json \
  --frames-dir scratch/run/frames --sample-interval 1/1

# 2. Find the string and the area to cover
python3 skills/mask-plan/locate_sensitive.py \
  --frames-dir scratch/run/frames --width 1920 --height 1080 \
  --pattern 'PRIVATE_[A-Z0-9]+' \
  --output scratch/run/sensitive.json

# 3. Assess motion before writing the plan
python3 skills/mask-plan/motion_triage.py \
  --input scratch/run/sensitive.json \
  --output scratch/run/motion-triage.json \
  --max-drift 45 --margin 8 --boundary-hold 60
```

Next, have a person write `scratch/run/plan.json` and prepare its mask assets,
using the motion report and the guidance under **Write the masking plan** below.
Only then check the plan's coverage:

```bash
# 4. Check whether the plan covers the detected boxes while they are visible
python3 skills/mask-plan/coverage_gate.py \
  --plan scratch/run/plan.json \
  --detections scratch/run/sensitive.json
```

After coverage review, render the plan with `executor/assemble.py` (see
`executor/README.md`), then perform the pixel and rendered-frame OCR checks
below.

Use the actual frame dimensions and sensitive-string pattern for your material;
the command above uses generic example values.

## Check pixels and strings before delivery

Run the pixel check on every render or repair cycle that uses a supported plan.
`verify_render.py` shares the coverage gate's Plan loader and all its plan
refusals listed below. For deliveries outside that supported scope, use
rendered-frame OCR and human inspection; this command cannot complete the
pixel check.

It reuses the mask rectangles and active timeline ranges from the plan, then decodes only each
rectangle at its first, middle, and last active **rendered frames** as grayscale
raw bytes. It reads the rendered video's `r_frame_rate`, `start_time`, and
`duration` with `ffprobe`, selects frame indices whose **centres** are inside
the half-open active window, and retains the first and last such frames. Thus
the sample grid is the rendered frame grid, not the plan's tick grid.
It measures spatial standard deviation rather than requiring a mask color:
the EditPlan format records rectangle geometry, not its color. Its format and
validation rules are defined in `contracts/edit-plan.schema.md`.

```bash
python3 skills/mask-plan/verify_render.py \
  --rendered scratch/run/delivery.mp4 \
  --plan scratch/run/plan.json \
  --samples-per-mask 3 --max-stddev 2.0 \
  --output scratch/run/render-check.json
```

This quickly checks whether the renderer made each planned rectangle uniform.
It will not catch a missing mask where the original pixels were already uniform. **This pixel check does not replace the string check.** Before
delivery, extract frames from the rendered video with `skills/media-index/ocr_vision.py`
and run `locate_sensitive.py` on those frames with the strings to be covered.
Review the detections and unsampled intervals; no complete full-delivery
forbidden-string checker is included. The pixel check does not approve delivery.

If no rendered-frame centre falls inside an active mask window, the pixel check
records that mask as `"skipped": "no rendered frame inside the active window"`.
This means no rendered-frame centre fell inside the declared window, so the
checker took no pixel sample. It does not prove that the overlay appeared on no
delivered frame: the renderer evaluates overlays at frame PTS and snaps the
start down to the containing frame. Inspect skipped windows manually before
delivery. Each is counted and printed as `skipped=N`, not silently omitted or
marked as a suspect sample.

Run `locate_sensitive.py` from a macOS shell with Vision access. Restricted
environments may cause Vision to fail with `ok=False` and no usable error
message. The script treats this as a failure rather than an empty detection
result; do not interpret the failure as "nothing sensitive on screen".

`coverage_gate.py` checks data without decoding or rendering, so it can run in
a sandbox. It refuses unsupported source, time and geometry mappings rather than
checking only their first placement. The exact refusals and remaining assumptions
are listed below. A PASS outside those remaining assumptions does not establish
coverage.

Checking without opening asset files is possible only for overlays whose
referenced `assets[]` declare `pixel_width`/`pixel_height`. These fields are
optional in the plan format; if absent, supply `--assets-dir` so the gate can
read the PNG dimensions.
Without dimensions or a file to measure, it refuses rather than guessing.

## Write the masking plan

These tools do not turn detections into an EditPlan. Write the plan using
`contracts/edit-plan.schema.md`: choose the crop, mask geometry, active timeline
ranges and assets for the material. No automatic masking plan builder is included.

A person must decide what to cover and what to leave readable, and review
partial exposure and detection failures before publishing.

Four decisions remain for a person after motion assessment: whether a detected
string must actually be covered (and whether partial exposure is allowed); whether
a `cut` interval really comes out of the edit; which mask asset a track gets; and
which overlapping evidence IDs to record. For those IDs, `validate_plan.py`
only requires that they exist and overlap.

The tools do not record how a motion assessment interval became an overlay action.
Keep that decision trace with the plan for human review.

## Decide mask or cut before making a plan

`motion_triage.py` assesses motion between detection and planning. It pairs
identical matched strings by nearest box centre, so matching strings in a
conversation and a message input remain separate tracks even if Vision changes
their array order in the next sample. For every paired interval it reports
the centre displacement and either:

- `mask`: the union of the two source-coordinate boxes, expanded by `--margin`
- `cut`: `rect: null`; the unknown path between fast-moving samples must not be
 turned into an invented mask rectangle

It also extends the first and last observed boxes by `--boundary-hold` source
ticks. A short detection absence of at most twice that hold stays one mask
interval whose rectangle is the union on both sides. Intervals are half-open:
`[start_pts, end_pts)`.

The leading hold is always clamped at 0 so a detection near the start does not
produce a negative PTS in the plan. Pass `--source-end-pts` to clamp the trailing
hold to the source end as well.

```bash
python3 skills/mask-plan/motion_triage.py \
  --input scratch/run/sensitive.json \
  --output scratch/run/motion-triage.json \
  --max-drift 45 --margin 8 --boundary-hold 60
```

All three thresholds are command-line options to choose for the footage,
not plan-wide constants. Defaults are `45` px for centre drift, `8` px for the
edge margin and `60` source ticks for the hold; these are starting parameters, not validated
safety thresholds. Change `--boundary-hold` for the source timebase and sampling
cadence before relying on it. The output records every actual parameter value.

## Choose the detection box and mask dimensions

**Use the box from the first regex match to the end of the recognised line.**
The reported box includes any text after the match. `boundingBoxForRange:`
gives the box of the requested character range. `locate_sensitive.py` reports
one such box per line, so write the pattern to start where the part to cover
starts. Feeding a whole-line
box to the gate also tests the parts that the plan intentionally leaves visible;
partial exposure needs human review.

**Read mask geometry from the plan.** `act_overlay_asset` gives the position
through `overlay_x`/`overlay_y`; dimensions come from `pixel_width`/`pixel_height`
on `assets[]` records. Asset dimensions are optional in the schema but required
for this check. If the plan omits them, pass `--assets-dir` so the gate can read
them from PNG files. It reports that fallback and refuses to guess.

## Inputs the coverage check rejects

- `act_speed_change` or `act_freeze_frame`: a source instant does not map to one
 output instant with the same elapsed time
- clip or overlay endpoint `value` ticks whose type is not integer, non-positive
 clip source or timeline lengths, or unequal source/timeline lengths
- duplicate `segment_id` entries in `source_segments`, duplicate `clip_id` entries
 in `timeline_clips`, or duplicate `asset_id` entries in `assets`
- overlapping source ranges in timeline clips, including repeated placements;
 ranges that only touch at their half-open endpoints are accepted
- clips referencing multiple sources, missing `source_segments`, or a clip whose
 `source_segment_id` does not resolve to a segment with a `source_id`
- missing or mixed `timebase` values at either endpoint of clip source/timeline
 ranges or overlay timeline ranges; equivalent fractions such as 1/600 and
 2/1200 are accepted
- any action outside `coverage_gate.py`'s `ALLOWED_ACTIONS`, including unknown
 names, scale/padding, zoom and repeat declarations; the list accepts handled
 crops and overlays, clip-placement declarations, annotations, fixed blur,
 captions and audio effects that do not move source-video pixels or source time
- a crop missing `source_segment_id` or `params`, or referencing a segment not
 declared in `source_segments`, even if no timeline clip places that segment
- a crop on a segment referenced by a timeline clip whose `width`/`height` differs
 from `output_policy.width/height`, or whose output dimensions are undeclared:
 crop offsets alone cannot map it; only crops with `params` on declared, unplaced
 segments are ignored
- two or more crops on a placed segment, even when their values are identical
- geometry values read by the gate whose type is not integer: asset
 `pixel_width`/`pixel_height`, overlay `overlay_x`/`overlay_y`, placed-crop
 `x`/`y`/`width`/`height`, and `output_policy.width/height`; floats, strings and
 bools are refused, and asset dimensions must also be positive
- a declared overlay `enable_axis` other than `final_timeline`, or a declared
 `coordinate_origin` other than `top_left` on an overlay or a placed crop; absent
 fields retain the same axis and origin assumptions
- an overlay whose asset declares no dimensions and has no file to measure

All plan refusals also apply to `verify_render.py` through its shared loader.
When a detection frame supplies a `timebase`, the coverage check compares it
with the plan and refuses invalid or different units. Detection `pts` must be
of integer type, even on frames with no hits; strings, floats and bools are
refused. Each detection box must contain exactly four values of integer type
with `x0 < x1` and `y0 < y1`; empty or reversed boxes are refused. The sampling
step must be of integer type and at least 1; the default remains 2. Malformed
plans and detections, including JSON parsing failures and missing input files,
produce a refusal rather than an uncaught input exception.

## Not covered here

The current `locate_sensitive.py` output supplies no detection timebase. Its
integer `pts` must use the plan's tick unit; the gate cannot verify that unit when
it is absent. With no crop, the EditPlan has no source dimensions to compare with
the output canvas, so unchanged source geometry remains an unchecked assumption.
The loader requires positive, equal clip source/timeline lengths but is not a
full contract validator and does not verify rendered timing. Speed and zoom
actions are refused by name regardless of their speed factor or zoom value.
Clip placement is authoritative: segment `source_range` need not match the
clip range, and timeline overlaps across tracks are not rejected.

Within these remaining assumptions, the gate checks coverage of the boxes **that
were detected**. It adds intersecting mask boundaries to the sample grid, so it
misses no uncovered integer pixel in a detected box at any valid positive integer
step, including the default step of 2. This geometry check says nothing about
what detection missed or whether the masks rendered correctly. Temporal sampling
still requires judgment: inspect transitions and fast movement at a suitable
cadence before delivery.

Do not invent a mask for an unknown motion path. When `motion_triage.py` reports
`cut`, a person must decide whether to remove that interval or obtain more
observations and revise the plan.

Render timing and repair-cycle measurements from development cannot be
reproduced from this repository; the source media and measurement logs are
not included.
