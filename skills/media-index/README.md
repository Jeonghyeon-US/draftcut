# media-index

These tools build an Index (a list of observations) from source media. The
walkthrough below combines OCR, scene detection and screen activity for one
screen recording; separate tools add freeze observations or transcripts.
Source times use integer presentation timestamp (PTS) ticks from the stream;
they are never derived from a frame number or decimal `pts_time`.

This walkthrough requires macOS, PyObjC and saved OCR results. There is no
OCR-free `build_index.py` path. Without OCR, supply an Index in
`contracts/index.schema.md` format or use `skills/pair-align/` for an existing
edit.

OCR saves sampled frames as `sample-%04d-pts-%d.png` for downstream tools.
Use a fresh frame directory for each source and run to avoid stale samples.

Run OCR only from a normal macOS shell, not a sandboxed or restricted
environment. A sandbox can return Vision `ok=False`, `err=None`, and no
observations. The script treats that as failure rather than reporting success
with an empty observation JSON.

```bash
python3 skills/media-index/ocr_vision.py \
  --input /path/to/your-clip.mov \
  --output scratch/run/ocr.observations.json \
  --frames-dir scratch/run/ocr-frames \
  --sample-interval 1/1

python3 skills/media-index/scene_detect.py \
  --input /path/to/your-clip.mov \
  --output scratch/run/scene.boundaries.json \
  --threshold 0.01

python3 skills/media-index/activity_mad.py \
  --input /path/to/your-clip.mov \
  --output scratch/run/activity.json

python3 skills/media-index/build_index.py \
  --input /path/to/your-clip.mov \
  --ocr-json scratch/run/ocr.observations.json \
  --scene-json scratch/run/scene.boundaries.json \
  --activity-json scratch/run/activity.json \
  --source-id example-clip \
  --index-id example-clip-index \
  --output scratch/run/example-clip.index.json
```

`build_index.py` does not import Vision or call `ocr_vision.py`. It reads the
saved OCR, scene and activity intermediate files, computes the source SHA-256
itself, invokes `ffprobe` for `time_base` and `duration_ts`, and rejects any
source hash, timebase, or PTS mismatch before emitting an Index. It records the selected
observation backends, uses `transcript: []`, and creates a signal evidence
record for every emitted scene-boundary marker.

The scene threshold is a command-line parameter with a default of `0.01`. It
is retained in the intermediate JSON and evidence rationale; choose it for the
footage rather than treating it as a universal constant. `build_index.py` has a
`--probe-json` option for constrained synthetic test inputs. Omit it in routine
use and let the tool use ffprobe.

For cut drafting, the Index needs a scene boundary, an OCR character-count
change meeting the chosen `--text-delta`, and a `screen_activity` marker whose
range ends after the first scene boundary's start. Running the observation
commands does not guarantee that the footage contains those events; see
`skills/cut-decision/README.md` for the refusal conditions.

### Frame extraction failures

`extract_frames` stages and validates all requested frames before publishing
new `sample-*.png` files. A decoding failure publishes no new samples from that
run, but existing samples are not cleared. Reusing a directory can therefore
let downstream tools read stale frames, even after a successful run. Promotion
uses one `os.replace` per frame, so a failure during promotion can leave a
partial new set.

## `record_transcript.py` — add a supplied transcript

`build_index.py` observes the video and emits `transcript: []`; it does not
transcribe speech. `record_transcript.py` adds a human-supplied transcript to a
finished Index and leaves every other key untouched. Adding a transcript does
not require rerunning the observation tools. It uses the existing Index format
and validation rules in `contracts/index.schema.md`.

```bash
python3 skills/media-index/record_transcript.py \
  --index path/to/your.index.json \
  --transcript path/to/decisions.json \
  --out-index path/to/out.index.json \
  --report path/to/transcript-report.json
```

`--transcript` takes a JSON array of human decision records, not an Index or
plain text. Each record requires the twelve keys shown below; `words` is
optional. `decided_by`, `timestamp` and `rationale` are recorded in the report,
not as transcript keys in the Index.

```json
[
  {
    "utterance_id": "example-utterance",
    "source_id": "example-clip",
    "range": {
      "start": {"value": 0, "timebase": {"num": 1, "den": 1000}},
      "end": {"value": 1000, "timebase": {"num": 1, "den": 1000}}
    },
    "text": "Example",
    "provenance_status": "provided",
    "utterance_confidence": 0.8,
    "word_confidence": 0.8,
    "speaker_confidence": 0.8,
    "word_timestamps_available": true,
    "decided_by": "editor",
    "timestamp": "2026-01-01T00:00:00Z",
    "rationale": "Supplied transcript reviewed against the source",
    "words": [
      {
        "text": "Example",
        "start": {"value": 0, "timebase": {"num": 1, "den": 1000}},
        "end": {"value": 1000, "timebase": {"num": 1, "den": 1000}},
        "confidence": 0.8
      }
    ]
  }
]
```

Replace IDs, text, timing, audit metadata and confidence values with actual
human declarations; the example confidences are not defaults. Use a range
inside the named source's duration. Omit `words` and set
`word_timestamps_available` to `false` when word timestamps are unavailable.

The tool records human-supplied transcripts. It does not transcribe, judge
accuracy or readability, invent confidence values, or emit a
`transcript_segment` marker. It rejects `generated` and `unconfirmed` provenance
(origin or review status). `words` is optional but must agree with
`word_timestamps_available`.

For machine transcription, `transcribe_speech.py` provides an `mlx-whisper`
backend with word timestamps and writes the transcript and backend selections
into an Index. Install the optional `mlx-whisper` backend on a supported Apple
Silicon Mac before running it; run with `--help` for inputs and outputs, and
review the transcript for accuracy.
The default transcription language is Korean (`--language ko`); pass
`--language en` for English or another language code appropriate to the source.

`record_transcript.py` checks that `word_timestamps_available` agrees with the
presence of `words`. The schema and structural checker
(`validate_plan.py`) check each field separately; they do not check
that the flag agrees with the presence of `words`. A schema-valid Index can claim word timestamps it does not
carry.

A recorded transcript does not establish caption accuracy or readability; a
person must review both. `skills/policy-workflow/draft_captions_from_transcript.py`
reads the Index's transcript to draft the input for `record_captions.py`; it
requires `provided` or `human_reviewed` provenance and valid clip placement. See
`skills/policy-workflow/README.md` for the workflow.
