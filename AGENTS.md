# Draftcut — instructions for AI coding agents

English · [한국어](AGENTS.ko.md)

This file guides AI coding agents. `CLAUDE.md` also points here. When a user says
"install this repository and edit my video," follow the steps below.

## 0. What this repository does and does not do

| Step | What | Where |
|---|---|---|
| 1. Observe | Extract scene boundaries, on-screen text, speech and motion into an Index (a list of observations) | `skills/media-index/` |
| 2. Plan | Write an EditPlan using the format and validation rules in `contracts/` | `contracts/` |
| 3. Execute | Turn an EditPlan into a video file with ffmpeg | `executor/assemble.py` |

You decide what to cut. The repository provides observations, a plan format and
an executor. `skills/cut-decision/decide_cuts.py` uses a narrow rule for one silent
screen recording and does not fit general footage. For general footage, read the
Index and write the EditPlan yourself.

The output is a video file. There is no export to Premiere, Final Cut Pro or
Resolve. If the user wants to continue editing in a nonlinear editor (NLE), tell
them that this is currently unsupported.

## 1. Check the environment and ask before installing

```bash
python3 check_env.py --json
```

If `ready` is `false`, `install_commands` contains suggested installation commands.
The checker reports required module and executable presence, but does not verify
package versions, encoder or filter support, or whether your plan will render.
Explain what you would install and why, and get the user's approval before
running any installation command.

| Required | Without it |
|---|---|
| Python 3.10+ · numpy · Pillow | Python 3.10+ is required to run the scripts. numpy supports signal/audio analysis and Pillow supports caption images. Install both for the complete workflow; environment checks and some JSON-only validators/recorders use only the standard library |
| `ffmpeg` · `ffprobe` | No rendering. `DRAFTCUT_FFMPEG` / `DRAFTCUT_FFPROBE` are read by the environment checker, executor and `skills/mask-plan/verify_render.py`; observation and alignment tools need `ffmpeg` / `ffprobe` on PATH |

| Optional | Without it |
|---|---|
| pyobjc (macOS) | OCR and the Index-building walkthrough below are unavailable; the executor can use an existing valid Index and EditPlan |
| mlx-whisper (Apple Silicon) | Only speech transcription is unavailable; you can supply a transcript instead |

## 2. The workflow

### 1. Observe the footage and build an Index

This workflow requires macOS and PyObjC for OCR. `build_index.py` requires
`--ocr-json`; no OCR-free path is provided by this builder. On other platforms,
install ffmpeg/ffprobe through your platform's package manager and supply an
existing, valid Index and EditPlan to the executor.

```bash
python3 skills/media-index/scene_detect.py --input IN.mp4 --output scene.json
python3 skills/media-index/ocr_vision.py   --input IN.mp4 --output ocr.json --frames-dir frames/
python3 skills/media-index/activity_mad.py --input IN.mp4 --output activity.json
python3 skills/media-index/build_index.py  --input IN.mp4 --ocr-json ocr.json \
        --scene-json scene.json --activity-json activity.json \
        --source-id a --index-id idx1 --output index.json
```

Every tool supports `--help`. `skills/media-index/` also contains tools for
screen activity, freeze detection and speech transcription. The `decide_cuts.py`
draft rule requires scene boundaries, an OCR character-count change, and a
`screen_activity` marker whose range extends after the inferred action start.
The activity step and `--activity-json` above add observed activity markers; they
do not guarantee the required observations exist. If they are absent, write the
plan from the Index manually.

### 2. Write the EditPlan

- Format: `contracts/edit-plan.schema.json` (machine-readable) and `contracts/edit-plan.schema.md` (explanation).
- Example: `contracts/examples/edit-plan.valid.json`. Its approval states and
  timing calculations use illustrative values. They are not verified values to
  copy into a real plan.

Avoid these four mistakes:

1. Record time with integer ticks in `value` and a `timebase`. Seconds equal
   `value × timebase.num / timebase.den`, a rational value (a ratio of integers):
   `{"value": 153600, "timebase": {"num": 1, "den": 15360}}`. Never use decimal
   seconds; rounding can shift frames.
2. Ranges are half-open: `start` is included and `end` is excluded.
3. Every `action` needs a `source_segment_id`, including reordering actions.
4. The time ranges of evidence referenced by `evidence_ids` must overlap the
   action's source segment range.

### 3. Check before rendering, in this order

```bash
python3 validate_plan.py --plan plan.json --index index.json     # format and structural rules
python3 executor/assemble.py --plan plan.json --validate-only    # executor's own checks
python3 executor/assemble.py --plan plan.json --estimate-only    # estimated render time
```

Run both checks before estimating. `validate_plan.py` validates the JSON Schema
in `contracts/` and its structural rules, and identifies the exact field at fault:

```text
CONTRACT FAIL: edit-plan schema: /actions/2: required property 'source_segment_id' is missing
```

**A `validated` result from `--validate-only` does not establish that the plan is
correct.** It runs only the executor's plan-internal checks and does not read the
schema. A plan that violates the format or validation rules can still print
`validated` and render an incorrect result. Run `validate_plan.py` first.
`--validate-only` also does not read asset files or hashes, or execute ffmpeg;
see the `not_checked` field in its output.

### 4. Render with all five required flags

```bash
python3 executor/assemble.py --plan plan.json --index index.json \
        --source-map source-map.json --out out.mp4 --work work/
```

`source-map.json` maps each `source_id` in the plan to a real file:

```json
{"a": "/absolute/path/IN.mp4"}
```

Place `--work` and `--out` on the same filesystem, and create the output parent
directory before rendering. The executor promotes the completed file with
`os.replace`; copy the completed output afterward to deliver to another volume.
If the plan uses overlay images, caption fonts or audio assets, also pass `--asset-map asset-map.json`.
The map is a JSON object from the plan's `assets[].asset_id` values to file paths,
including fonts referenced by `caption_styles[].font_asset_id`, for example:

```json
{"mask-1": "/absolute/path/mask.png", "font-1": "/absolute/path/font.ttf"}
```

The executor checks the actual files against the declared SHA-256 values.
On success, the executor returns `{"status": "assembled", ...}`.

## 3. When it fails

| Symptom | Meaning |
|---|---|
| `--index --source-map --work required unless --estimate-only is used` | You did not pass all five flags from step 4 |
| `CONTRACT FAIL: ...` | `validate_plan.py` identifies the exact field and rule at fault |
| `{"status": "rejected", ...}` | A rejected `--validate-only` result includes `errors`; estimate/render failures report `error`. Read those messages and, when present, `assemble-evidence.json` in `--work` |
| Rendering stops partway through | Media, ffmpeg, compatibility, frame-count, audio or asset failures can stop a render. Inspect the reported stage and evidence in `--work` |

Read the specific rejection message and fix the plan rather than guessing.
Do not bypass the checks. A plan that violates the format or validation rules
may render, but its result cannot be relied on.

## 4. Explain these limits to the user

- Deciding what to cut is your judgment; the repository does not guarantee it.
- The output is a video file and cannot be exported as an NLE project.
- Color grading, lookup tables (LUTs), multicam, animated graphics, caption-text
  replacement and dubbing are unsupported.
- The executor accepts 25 action names in `actions[]`. Check both
  `contracts/action-executability.json` and the executor's supported scope. The
  registry's `yes(effect-verified)` labels record effect verification, not a
  guarantee of a plan-to-render path: J-cuts and L-cuts have that label but no
  executor render path. The development fixtures and logs are not included in
  this repository. Run both `validate_plan.py` and `executor/assemble.py --validate-only` before rendering.
- `act_export_media`, `act_change_canvas`, `act_extract_capture_evidence_frames`
  and `act_remux_media` are rejected in `actions[]`. Export is the executor's
  normal output, canvas settings belong in `output_policy`, and frame extraction
  uses separate observation tools. There is no independent remux render path.
