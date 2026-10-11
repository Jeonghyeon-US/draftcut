# executor — render a plan as a video

The executor applies an EditPlan to produce a video file. The planning tools write
plans; the executor renders them.

## Usage

```bash
python3 executor/assemble.py --plan plan.json --index index.json \
        --source-map source-map.json --out output.mp4 --work work/
```

| Argument | Purpose |
|---|---|
| `--plan` | EditPlan JSON; its format and validation rules are defined in `contracts/` |
| `--out` | Output video |
| `--index` · `--source-map` · `--asset-map` | Footage observations and footage/asset path mappings |
| `--work` | Intermediate directory where evidence is stored |
| `--validate-only` | Check the plan without rendering |
| `--estimate-only` | Estimate work from structural counts without rendering |

All five flags, `--plan --index --source-map --out --work`, are required to render.
Both `--validate-only` and `--estimate-only` can run with `--plan` alone.

Place `--work` and `--out` on the same filesystem: the executor promotes the
completed file with `os.replace`. Create the output parent directory before
rendering. To deliver to another volume, render with both paths on one volume,
then copy the completed output.

For overlays, captions or audio assets, also pass `--asset-map` with an
`asset_id`-to-file-path JSON object, for example:

```json
{"mask-1":"/absolute/path/mask.png","font-1":"/absolute/path/font.ttf"}
```

Keys must match the referenced `assets[].asset_id`, including fonts referenced by
`caption_styles[].font_asset_id`. The executor checks the actual files against
the plan's declared SHA-256 values. The map does not replace the plan's asset
records or rights review.

Run `validate_plan.py` first, then `--validate-only` before rendering to reject
malformed plans. Use `--estimate-only` to estimate the work beforehand.

**A `validated` result does not establish that the plan will render.** The check is
plan-internal and does not read asset files or hashes, or execute ffmpeg. Read the
`not_checked` field in its output.

## Requirements

ffmpeg / ffprobe, numpy and Pillow; see `requirements.txt`.

## Verification scope

The executor accepts 25 action names in `actions[]`. It rejects
`act_export_media`, `act_change_canvas`, `act_extract_capture_evidence_frames`
and `act_remux_media` there. Output encoding is part of the render workflow;
canvas dimensions come from `output_policy`. Frame extraction uses the separate
observation tool `skills/media-index/ocr_vision.py`. No standalone remux workflow
is exposed here. J-cuts and L-cuts have no executor render path.

`contracts/action-executability.json` contains 50 entries, 31 labelled
`yes(effect-verified)`. These are registry declarations from development tests,
including J-cuts and L-cuts; the original media and measurement logs are not
included, so these labels are not a reproducible render benchmark. Check both
the registry and the executor's accepted action names before rendering.

Run `validate_plan.py` and then the executor's `--validate-only` check before
rendering. These check plan structure; they do not establish render quality,
performance or byte-for-byte reproducibility for your footage.

Read the repository README's "What it cannot do and its limits" section for the
supported scope and limitations.

## Files

| File | Purpose |
|---|---|
| `assemble.py` | Entry point: plan to video |
| `policy.py` | Output policy (resolution, frame rate, timebase) |
| `normalize.py` · `concat_check.py` | Piece normalization and concatenation compatibility |
| `caption_png.py` | Caption PNG rendering |
| `probe.py` | ffprobe wrapper |
