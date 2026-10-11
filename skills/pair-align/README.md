# pair-align — recover a cut list from a finished edit

These tools align a source and a finished edit by audio to recover a cut list.
They require an existing edit and do not edit video.

## What it does

| What | Detail |
|---|---|
| Alignment | Matches source to edit automatically by audio fingerprint |
| Output | Trusted intervals, a mapping rate, and a residual error, written to `alignment.json` |
| Reruns | The alignment algorithm is deterministic for fixed input audio, parameters and software versions |

## Two tools

### 1. `align_audio.py` — align

```bash
python3 skills/pair-align/align_audio.py \
  --source /path/to/original.mov \
  --edited /path/to/edited.mp4 \
  --out alignment.json
```
Run with `--help` for the full argument list.

### 2. `plan_from_alignment.py` — turn the alignment into a plan

```bash
python3 skills/pair-align/plan_from_alignment.py \
  --alignment alignment.json \
  --source /path/to/original.mov \
  --out-dir scratch/alignment-plan
```
The converter deterministically writes the alignment result in the EditPlan
format, following its validation rules in `contracts/`. It does not decide
which intervals to keep.

Run these commands from the repository root. The converter writes `edit-plan.json`,
`index.json` and `source-map.json` into `--out-dir`. Render that bundle with
`executor/assemble.py` (see `executor/README.md`).

## Limits

- `plan_from_alignment.py` resolves format definitions relative to the
  repository root (`parents[2]`). Copying this skill on its own breaks it;
  distribute the repository as a whole.
- Review `summary.mapped_ratio` in `alignment.json`: it is the summed edited
  duration of segments meeting the summary confidence threshold, divided by the
  edited video's duration. If it is below `0.95`, do not build a plan. This is a
  human review guideline; the converter does not check the overall mapping rate.
  It selects
  `trusted: true` segments, or `confidence >= 0.6` when `trusted` is absent or null.
