# Draftcut

English · [한국어](README.ko.md)

Draftcut is a set of video-editing tools for developers and editors working with AI coding
agents. It extracts observations from footage and uses ffmpeg to turn an EditPlan—a JSON
document describing the edits—into a video file. It is not a nonlinear editor (NLE), and
choosing what to cut is the agent's responsibility.

## Fastest way to use it

Clone this repository, then tell your AI coding agent:

> "Install this repository and edit `<my video>` for me. My editing goal is `<goal>`, with `<output requirements>`."

The agent follows [AGENTS.md](AGENTS.md), explains what it needs and asks for approval
before installing anything, then examines your footage, writes the plan and renders it.
To check what is missing yourself:

```bash
python3 check_env.py
```

## What it can do

### 1. Observe the footage

An Index is a list of observations with timecodes, used to decide the edits.

| Observation | Method |
|---|---|
| Scene changes | ffmpeg scene detection |
| Frozen or still stretches | Freeze detection |
| High or low motion stretches | Frame-to-frame difference |
| On-screen text | Apple Vision optical character recognition (OCR), macOS only |
| Spoken words | Local Whisper transcription with per-word timings |

Times use integer presentation timestamp (PTS) ticks, the units of the video's timebase,
rather than decimal seconds. This avoids accumulated rounding errors that shift frames.

### 2. Apply 25 editing action types

| Area | Actions |
|---|---|
| Cut and join | Trim, concatenate, reorder, insert, ripple delete (close the gap after deletion), repeat a segment |
| Time | Speed up or slow down, freeze frame |
| Frame | Crop or reframe, keyframed zoom, scale and position, change aspect ratio (such as 16:9 → 9:16), overlay images or logos |
| Redaction | Blur regions containing personal data, account names or sensitive on-screen strings |
| Captions | Burn in captions, style individual ranges, draft captions from a transcript |
| Audio | Loudness normalization, noise reduction, gain, fades, crossfade, music ducking under speech, mixing, music, sound effect and ambience beds |
| Output and observation | Encoded video output and canvas settings are handled by the executor; separate observation tools extract frames |

The executor accepts 25 action names in `actions[]`. The registry also lists operations
that are not accepted there: `act_export_media` is the executor's normal output,
`act_change_canvas` is configured through `output_policy`, and
`act_extract_capture_evidence_frames` is handled by separate observation tools.
There is no independent `act_remux_media` render path. All four names are rejected in
`actions[]`. Check both `contracts/action-executability.json` and the executor's
supported scope; registry labels alone do not establish render support.

### 3. Check the plan and rendered output

| Stage | Check |
|---|---|
| Before rendering | Plan format and structural rules: `validate_plan.py` |
| Before rendering | Executor's plan-internal checks: `executor/assemble.py --validate-only` |
| Before rendering | Estimated render time: `executor/assemble.py --estimate-only` |
| Before rendering | Whether planned mask rectangles cover detected boxes: `skills/mask-plan/coverage_gate.py` |
| Before rendering | Speed-change safety and whether pieces can concatenate without re-encoding |
| After rendering | Whether sampled pixels in planned mask rectangles are uniform within a threshold: `skills/mask-plan/verify_render.py` |

Run `validate_plan.py` before `--validate-only`. The latter does not read the plan schema,
asset files or hashes, or execute ffmpeg; a `validated` result alone does not establish
that the plan is correct or will render.

`coverage_gate.py` assumes one source, the same tick unit for detections and all plan
ranges, a 1:1 time mapping (no repeated source intervals or freeze frames), and no geometric
transforms except crop offsets. It does not validate these assumptions: it checks only the
first matching clip and does not account for scale, padding or zoom. It rejects speed
changes. A PASS outside these limits does not establish coverage, and the tool cannot
check missed detections.

### 4. Recover a cut list from a finished edit

The tools in `skills/pair-align/` align the source and an existing edit using audio
fingerprints, then recover what was kept and dropped. The alignment algorithm is
deterministic for fixed input audio, parameters and software versions.
`plan_from_alignment.py` converts the alignment into an EditPlan, selecting segments
with `trusted: true`, or `confidence >= 0.6` when `trusted` is absent or null.
In `alignment.json`, `summary.mapped_ratio` is the sum of edited-time durations of
segments that meet the summary confidence threshold, divided by the edited video's
duration. Do not build a plan when this ratio is below 0.95 (95%): this is a human
review guideline that the converter does not enforce. These tools read an existing
edit; they do not edit video.

## What it cannot do and its limits

| Not supported | Detail |
|---|---|
| Export to Premiere, Resolve or Final Cut Pro | No OTIO, XML or MLT export. The output is a video file, not an editing project |
| Color grading or lookup tables (LUTs) | Not implemented |
| Multicam angle selection | Not implemented |
| Animated graphics | Not implemented |
| J-cuts or L-cuts | Out of scope |
| Replacing caption text or dubbing | Not implemented |
| Deciding what to cut on its own | No general-purpose tool is included; your agent proposes the cuts |
| OCR and transcription outside their supported platforms | OCR requires Apple Vision on macOS; transcription requires Apple Silicon. Other features use the required Python packages and ffmpeg/ffprobe |

Every cut proposal is a draft that needs review. During development on two long
spoken-word videos, a first-pass proposal found only some of the material the editor
cut, and 6–17 wrongly cut spans had to be restored. These observations cannot be
reproduced from this repository: the source media and development records are not
included. Review all proposed cuts before treating the plan as a finished edit.

## Install

Check the environment first. This script reports missing dependencies and installs nothing.
It checks whether required modules and executables are present, but does not verify
package versions, encoder or filter support, or whether your plan will render.

```bash
python3 check_env.py          # human-readable table
python3 check_env.py --json   # machine-readable output, including install commands
```

The required dependencies are Python 3.10+, numpy, Pillow, and ffmpeg/ffprobe.

```bash
pip install -r requirements.txt     # numpy and Pillow
brew install ffmpeg                 # macOS example; ffmpeg and ffprobe are not bundled
```

OCR (PyObjC, macOS) and speech transcription (mlx-whisper, Apple Silicon) are optional
for the executor, but the Index-building workflow below requires OCR. `build_index.py`
requires `--ocr-json`; no OCR-free path is provided by this builder. On other platforms,
install ffmpeg/ffprobe through your platform's package manager and supply an existing,
valid Index and EditPlan to the executor. `DRAFTCUT_FFMPEG` and `DRAFTCUT_FFPROBE` are read by the
environment checker, executor and `skills/mask-plan/verify_render.py`. Observation and
alignment tools use `ffmpeg` and `ffprobe` on PATH; add their directory to PATH too.

### What runs, what is sent and what is downloaded

Draftcut launches local ffmpeg and ffprobe processes to inspect and process media.
The repository code contains no calls to upload video, audio or transcripts to a network
service. Optional speech transcription passes a local media file and a model path or Hugging
Face repository to mlx-whisper; that library may download the selected model weights for
local transcription. Model weights are not bundled. The installation commands above
download Python packages and ffmpeg/ffprobe separately.

## Try it by hand

For the agent workflow, follow [AGENTS.md](AGENTS.md). The commands below run from the
repository root; this Index-building workflow requires macOS and PyObjC for OCR.

```bash
# 1. Observe the footage
python3 skills/media-index/scene_detect.py --input a.mp4 --output scene.json
python3 skills/media-index/ocr_vision.py --input a.mp4 --output ocr.json --frames-dir frames/
python3 skills/media-index/activity_mad.py --input a.mp4 --output activity.json
python3 skills/media-index/build_index.py --input a.mp4 --ocr-json ocr.json \
        --scene-json scene.json --activity-json activity.json \
        --source-id a --index-id idx1 --output index.json
```

For step 2, write `plan.json` from the Index, or have your agent write it, following
`contracts/edit-plan.schema.md`. The Index format is in `contracts/index.schema.md`.
For one silent screen recording only, `decide_cuts.py` offers a narrow rule for drafting
cuts. It requires scene boundaries, an OCR character-count change, and a
`screen_activity` marker whose range extends after the inferred action start.
The activity step and `--activity-json` above add observed activity markers; they
do not guarantee the required observations exist. If they are absent, write the
plan from the Index manually. This rule does not fit general footage:

```bash
python3 skills/cut-decision/decide_cuts.py --index index.json --output plan.json
```

```bash
# 3. Check the plan, then estimate render time
python3 validate_plan.py --plan plan.json --index index.json
python3 executor/assemble.py --plan plan.json --validate-only
python3 executor/assemble.py --plan plan.json --estimate-only

# 4. Render; all five flags are required
python3 executor/assemble.py --plan plan.json --index index.json \
        --source-map source-map.json --out out.mp4 --work work/
```

Before step 4, create `source-map.json` to map each `source_id` to a real file, for example
`{"a": "/absolute/path/a.mp4"}`. The `--index`, `--source-map` and `--work` flags cannot be
omitted: without them, rendering stops with
`--index --source-map --work required unless --estimate-only is used`.
Place `--work` and `--out` on the same filesystem, and create the output parent
directory before rendering. The executor uses `os.replace` to promote the completed
file. To deliver on another volume, copy the completed output afterward.
If the plan uses overlay images, caption fonts or audio assets, also pass `--asset-map asset-map.json`.
This JSON object maps the plan's `assets[].asset_id` values to file paths, including
fonts referenced by `caption_styles[].font_asset_id`, for example
`{"mask-1": "/absolute/path/mask.png", "font-1": "/absolute/path/font.ttf"}`.
The executor checks these files against the declared SHA-256 values.
Every tool supports `--help`.

## Layout

```text
executor/         Render an EditPlan as a video; separate from the planning tools
skills/
  media-index/    Observe scenes, freezes, motion, on-screen text and speech
  cut-decision/   Draft cuts for one silent screen recording from an Index
  pair-align/     Align source and finished edit by audio; recover a cut list
  mask-plan/      Find sensitive strings, plan masks, check before and after rendering
  policy-workflow/ Draft captions, apply/check speed changes, merge plans, make contact sheets (preview images combining several video frames)
contracts/        JSON formats and validation rules for Index, EditPlan and tool outputs
```

## License

Apache License 2.0; see [LICENSE](LICENSE). [NOTICE](NOTICE) records third-party components
and ffmpeg licensing details. Draftcut runs ffmpeg as a separate process, does not link
libav libraries and includes no ffmpeg binary. Do not redistribute ffmpeg with Draftcut.
Bundling an ffmpeg binary brings that build's license obligations, including GPL obligations
for GPL builds. See NOTICE for details.
