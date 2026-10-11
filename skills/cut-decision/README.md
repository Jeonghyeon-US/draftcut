# cut-decision

This tool drafts an EditPlan from an existing Index for one silent screen
recording with a single source. It does not invoke OCR or scene detection.
All planned times use exact integer rational ticks. It emits no captions and
records the chosen OCR text-change threshold in `reproducibility`.

The observation workflow requires macOS, PyObjC and saved OCR results. Follow
`skills/media-index/README.md` to create OCR and scene observations, then include
screen activity when building the Index:

```bash
python3 skills/media-index/activity_mad.py \
  --input /path/to/your-clip.mov --output scratch/run/activity.json
python3 skills/media-index/build_index.py \
  --input /path/to/your-clip.mov \
  --ocr-json scratch/run/ocr.observations.json \
  --scene-json scratch/run/scene.boundaries.json \
  --activity-json scratch/run/activity.json \
  --source-id example-clip --index-id example-clip-index \
  --output scratch/run/example-clip.index.json
```

There is no OCR-free `build_index.py` path. Without OCR, supply an Index in
`contracts/index.schema.md` format or use `skills/pair-align/` for an existing
edit; the observations required by this cut drafter still need to be present.

```bash
python3 skills/cut-decision/decide_cuts.py \
  --index scratch/run/example-clip.index.json \
  --output scratch/run/example-clip.plan.json \
  --text-delta 30
```

`--text-delta` defaults to `30`; choose it for each run rather than treating it
as a constant for all footage. The tool requires exactly one source, a
`shot_scene_boundary` marker, an OCR character-count change meeting
`--text-delta`, and a `screen_activity` marker whose range ends after
`action_start` (the first scene boundary's start). Producing activity JSON alone
does not guarantee such a marker. If the required observations are absent, the
tool rejects the Index; write the plan manually instead.
