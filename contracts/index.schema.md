# Observation Index contract

Status: normative contract. `index.schema.json` is the machine-readable
form; if they diverge, this document wins and the JSON is a defect.

## Canonical rules

All canonical time is an integer tick value: `{ "value": integer,
"timebase": {"num": positive integer, "den": positive integer} }`.
Its seconds value is `value * num / den`; decimal seconds are display-only and
are never compared by a compiler or the contract checker. A range is half-open `[start, end)`.
Source and timeline ranges are separate fields. A mapping rounds an exact
rational result **toward zero** to the destination tick; the unrounded rational
and rounding rule are retained. This prevents floating-point threshold drift.

## Converting external decimal times

External material can state time in decimal seconds: edit plans may use
seconds as JSON numbers, and speech-to-text (STT) backends may return word times
the same way.
Converting such a value to canonical tick form follows three rules.

1. **Read the decimal as an exact rational, not as a float.** The literal `1.1` is
  the rational 11/10. Binary floating-point conversion can shift a mathematically
  exact tick boundary.
2. **Round toward zero at the last step only**, per the canonical rule above, and
  retain the unrounded rational together with the original literal. The literal
  is retained because it, not our tick, is what the external source asserted.
3. **For variable-frame-rate (VFR) input, use recorded frame PTS
  (presentation timestamps), not frame index ÷ nominal frame rate.** A nominal
  rate does not establish the presentation time of each source frame.

External clip times and STT word timestamps use the same conversion rule.

`marker_type` is a closed set: `transcript_segment`, `shot_scene_boundary`,
`silence_interval`, `loudness_peak`, `onscreen_text`,
`sensitive_information_candidate`, `behavior_reaction_candidate`, and
`screen_activity`. New marker kinds require a contract revision.

### Screen activity observation

`screen_activity` is an observation (a pixel-activity interval), not
`result_complete`; downstream editing logic derives `result_complete` and it is
not written here.
Its range is `[previous sample, last active sample)`.

Evidence is immutable by ID. Frame evidence records a source time and SHA-256;
signal evidence records tool and version; human evidence records reviewer and
rationale. Transcript provenance is one of `provided`, `generated`,
`human_reviewed`, `unconfirmed`, with utterance, word, and speaker confidence.
When using a speech-to-text (STT) backend, record its resolved backend
selection. Local transcription is implemented in
`skills/media-index/transcribe_speech.py`.

## STT provenance, word times, and chunk seams

Every Index records its resolved `backend_selections`. A selection has one
`purpose` (`stt`, `caption_renderer`, `ocr`, `shot_scene_boundary`, or `screen_activity`),
`backend_id`, `backend_version`, and resolved `cost_tier`. The only runtime cost
tiers are `무료-로컬` (free, local execution), `쿼터` (quota-based usage), and
`유료` (paid usage). These are literal JSON values; use these strings exactly as
written. `혼합` (mixed usage) is a registry planning classification, not a valid
runtime selection. This one record family is
deliberately shared with EditPlan and Skill output; these purposes do not get
separate provenance fields.

Each transcript utterance explicitly states `word_timestamps_available`.
`words` is optional so a backend that cannot emit word timing remains valid.
When present, each word is a closed `{text, start, end, confidence}` record;
`start` and `end` use the same integer-tick rational time form above, never
decimal seconds.

When a transcription is chunked, `transcription_chunks` records each chunk's
immutable ID, original source range, actual measured offset, and non-empty
reassembly evidence IDs. No chunk-number × fixed-duration approximation is a
contract value. The field is optional because a supplied transcript or a small
input need not have been chunked. For every chunk, `actual_offset` must equal
that chunk's `original_range.start` exactly after conversion to the common
rational timebase; an approximation derived from chunk number or nominal
duration is invalid. No two chunks' half-open `original_range` values may
overlap. A non-overlapping gap is surfaced by the record but is not itself a
Contract-checker rejection condition.

## On-screen text observations

`onscreen_text` is an optional array of closed `{source_id, time, text,
confidence}` records. `time` is one canonical source time, not a range: an OCR
observation is one sampled frame, and adding an end tick would invent an
unobserved duration. Its `text` is non-empty and its `confidence` is within the
unit interval.

The field is optional because an empty array means the source was observed and
no text was found, while absence means no observation was made; those meanings
must remain distinct. These observations do not add a bounding box: although
an engine may provide one, v1 editing decisions do not use it. They also do not
belong in `transcript`, whose required `speaker_confidence` would force an OCR
observation to invent a speaker value.

## Capture provenance and validation

A source may carry optional `capture` provenance. Its closed `target` records the
identified `app`, `window_identifier`, and `permission_signal`; `operation_path` is either
`human_direct` or `steering`, with neither treated as a default. `validation`
records the `verdict`, non-negative `off_target_frame_count`, and one or
more extracted-frame `evidence_ids`. `refilm_reason` is available for a
refilm finding.

The contract checker rejects a `pass` verdict with any off-target frame:
recording a pass when another app or window appears even once is forbidden. The contract checker rejects a
`refilm` verdict without a non-empty reason, so the required re-shoot condition
remains auditable. Finally, every validation evidence ID must resolve to
top-level `frame` evidence: agent reports, accessibility trees, and human or
signal evidence cannot replace the required actual extracted frames.

Rationale: provenance, confidence, and evidence IDs make observations auditable,
while closed marker types keep Index builders interoperable.
