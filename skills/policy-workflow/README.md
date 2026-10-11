# policy-workflow

These tools merge draft plans for individual sources into one project and
support human review of speed changes.

Run the speed-review tools separately. A person inspects the contact sheet;
the other tools record and check that person's decision. `merge_plans.py` does
not invoke them.

## Merge plans

Before running the merge, create `scratch/run/project.json`. The format uses
`schema_version: "draftcut-l4-project-definition/v1"` to identify a project
merge definition, a non-empty `project_id`, and an ordered, non-empty
`materials` array. Each material requires exactly `source_id`, `plan`, `index`
and an absolute `media_path`; source IDs must be unique. Relative plan and Index
paths are resolved from the directory containing `project.json`.

```json
{
  "schema_version": "draftcut-l4-project-definition/v1",
  "project_id": "example-project",
  "materials": [
    {
      "source_id": "example-clip",
      "plan": "example-clip.plan.json",
      "index": "example-clip.index.json",
      "media_path": "/absolute/path/your-clip.mov"
    }
  ]
}
```

Replace the paths and IDs with your prepared per-source plans, Indexes and
media. Creating the directory below does not create those inputs.

```bash
V=scratch/run
mkdir -p "$V"
python3 skills/policy-workflow/merge_plans.py \
  --project $V/project.json \
  --out-plan $V/merged.plan.json --out-index $V/merged.index.json \
  --out-source-map $V/merged.source-map.json --out-report $V/report.json
```

`material_paths()` in `skills/policy-workflow/merge_plans.py` validates this
input. `media_path` in the source map is always absolute.

The merge runs `executor/assemble.py --validate-only` on its output and records
the result under the legacy report field `l2_dryrun` (the executor's plan-only
check). It reports the result without rejecting the merge. The report includes the check's
`not_checked` list: passing does not establish that the plan can render.

```json
{"entrypoint": "executor/assemble.py --validate-only", "status": "ok",
 "errors": [], "stages_not_reached": [],
 "not_checked": ["asset map contents (keys and paths)", "asset file existence and SHA-256",
                 "caption PNG renderer", "ffmpeg execution"]}
```

The merge does not open media. It writes
`"ffmpeg_version": "unverified-at-L4-draft"` into the merged plan's
reproducibility record to indicate that the ffmpeg version has not been
verified. The check reads only the plan; media, assets and actual ffmpeg
execution still need checking at render time.

## Review speed changes in four steps

The development media and end-to-end measurement logs are not included, so
the manual checks from development cannot be reproduced from this repository.

### 1. Read the proposed speed changes

The proposal is the draft plan's `act_speed_change` actions, not a separate file.

### 2. Inspect a contact sheet

```bash
python3 skills/policy-workflow/make_contact_sheet.py \
  --media /path/to/source.mov --out $V/sheet.jpg \
  --interval 3 --cols 6 \
  --plan $V/merged.plan.json --source-id example-clip \
  --index $V/sheet.index.json
```

`--interval` and `--cols` are required; there is no formula for choosing them.
Choose them for the material and review the resulting sheet. The command above
uses example values, not validated sampling thresholds.

### 3. Record the human decisions

```bash
python3 skills/policy-workflow/apply_speed_decisions.py \
  --plan $V/merged.plan.json --index $V/merged.index.json \
  --decisions path/to/decisions.json \
  --out-plan $V/p2.plan.json --report $V/p2.report.json
# → {"status": "recorded", "decisions_applied": 2, "preserve_total": 1, "releases_total": 1}
```

The EditPlan format and its validation rules define preservation and release:
`constraints.preserve[]` makes `validate_plan.py` reject destructive
actions on that segment unless a human release exists. `constraints.releases[]` lifts the constraint with
`released_by: "human"`, a timestamp, a rationale, and an `evidence_id` that must
exist in the paired Index. The person supplies the rationale and evidence ID.

This tool does not remove speed actions. Removing one changes the clip's
timeline duration and shifts every later clip. Rebuild the draft plans and
merge them again to make that change.

### 4. Check speed-change safety

Use `$V/p2.plan.json` for subsequent checks and rendering: it contains the
recorded human decisions. First run both plan checks:

```bash
python3 validate_plan.py \
  --plan $V/p2.plan.json --index $V/merged.index.json
python3 executor/assemble.py --validate-only --plan $V/p2.plan.json
```

If a preserved segment still has a destructive action without a human release,
rebuild and merge the draft plans before continuing. The speed-safety check
samples speed ranges; it does not resolve preservation constraints.

```bash
python3 skills/policy-workflow/verify_speed_safety.py \
  --plan $V/p2.plan.json --source-map $V/merged.source-map.json \
  --report $V/safety.json --work $V/work
```

The tool samples near the start, middle and end of every sped-up range and
compares mean absolute grayscale differences. Its UI-change verdict is a
threshold-based proxy, not UI recognition. Review the sampled frames.
The tool does not shorten the segment; its report requests shortening without
specifying an amount.

Exit codes: `0` clean · `1` violations found · `2` the tool itself could not run
(missing media, bad threshold). On `2` no report is written. The scratch `--work`
directory is created before verification, so an empty one can survive a failure.

`--threshold` is a command-line parameter, not a constant in the plan format.
It sets the ceiling for mean absolute grayscale differences between sampled
frames. Calibrate it
for the material and inspect the frames; it is not a guarantee that no new UI
element appeared.

Its inputs are the plan containing the recorded decisions and the merged source
map. Run it separately from the merge and review its report before rendering.

## `draft_captions_from_transcript.py` — draft captions from a transcript

This tool reads the Index's transcript and writes the `--captions` input file
for `record_captions.py`, using the existing format and validation rules.
Transcript provenance must be `provided` or `human_reviewed`; a generated or
unconfirmed transcript cannot seed these drafts.

Create `scratch/run/defaults.json` with all twelve required keys:

```json
{
  "caption_id_prefix": "caption-",
  "language": "en",
  "speaker": "speaker-1",
  "style_ref": "caption-style-1",
  "safe_area": "title-safe",
  "review_status": "draft",
  "track_id": "caption-track",
  "overlay_x": 0,
  "overlay_y": 0,
  "decided_by": "editor",
  "timestamp": "2026-01-01T00:00:00Z",
  "rationale": "Replace with the editor's caption decision rationale"
}
```

Replace these values with actual project declarations. `track_id` must name a
`captions` track and `style_ref` an existing caption style in the input plan.
That style must reference an approved font asset before recording captions.
Choose placement and review metadata for the material; the example values do
not establish readability or human approval.

Create `scratch/run/evidence-map.json` mapping each source segment used by a
caption to a non-empty array of evidence IDs:

```json
{"example-segment": ["example-evidence"]}
```

Use actual segment IDs and evidence IDs from the paired plan and Index.

```bash
python3 skills/policy-workflow/draft_captions_from_transcript.py \
  --index scratch/run/index-with-transcript.json \
  --plan path/to/your.plan.json \
  --defaults scratch/run/defaults.json \
  --evidence-map scratch/run/evidence-map.json \
  --out-captions scratch/run/draft-captions.json \
  --report scratch/run/draft-report.json
```

Four of the sixteen decision keys are derived for each utterance from the
transcript, its plan placement and the caption ID prefix:
`text`, `timeline_range`, `source_segment_id`, `caption_id`. Eleven more are
declared once per project in `--defaults`; that input also requires
`caption_id_prefix`, making twelve required defaults keys. The prefix generates
caption IDs. `evidence_ids` comes from the per-segment `--evidence-map`.

A spoken caption's source timing comes from the transcript. The tool maps it
to the timeline with a single offset, requiring one containing clip and equal
source and timeline spans.

The tool rejects inputs where a single offset would be incorrect: a speed
change on the clip, spans that disagree, an utterance straddling a cut, or a
boundary that is not an integer tick. It never rounds a caption boundary,
because that would change its time.

It does not split utterances, adjust timing or choose which utterances to
caption. It emits all utterances on the timeline for a person to review and
delete as needed. Utterances cut away from the timeline are skipped, not
rejected. Without `--evidence-map`, drafts carry no evidence and
`record_captions.py` rejects them.

Review and edit the draft caption decisions, then record them into a new plan:

```bash
python3 skills/policy-workflow/record_captions.py \
  --plan path/to/your.plan.json \
  --index scratch/run/index-with-transcript.json \
  --captions scratch/run/draft-captions.json \
  --out-plan scratch/run/captioned.plan.json \
  --report scratch/run/caption-report.json \
  --asset-map path/to/your.asset-map.json
```

The optional `--asset-map` maps each font asset ID to its real file path so the
recorder can verify the file and SHA-256. If omitted, that file verification is
not performed; rendering still requires the asset map. Drafting alone does not
add captions to the EditPlan. Use `scratch/run/captioned.plan.json` for subsequent
validation and rendering.

No speech video or caption reference is included in this repository; spoken
caption quality cannot be verified here.
