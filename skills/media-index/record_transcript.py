#!/usr/bin/env python3
"""Record a human's transcript into an existing Index.

This is a human-input port: it takes prose a human wrote, as a JSON file.
`build_index.py` emits `"transcript": []` because that tool measures a video; it
has no way to know what was said.

It is a RECORDER, not a generator. Machine transcription is a separate tool
(`transcribe_speech.py`), which declares its backend. That is why this tool
refuses `generated`: a machine transcript must arrive through a tool that
declares its backend. What this may and may not do:

  * -- the contract can be filled by hand. `words` is OPTIONAL: with
    `word_timestamps_available: false` an utterance costs 14 primitive values.
    Word-level alignment costs 8 more per word (~1,284 values per minute), which
    is not hand-input work; that arrives with an STT backend.
  * -- this does NOT live in `build_index.py`. The 14 canonical
    Indexes already exist without a transcript, and
    an argument there would mean re-running ffprobe, OCR, scene and activity
    just to add words. This one takes the finished Index and lays the
    transcript on top. No contract changes and no new contract rules.
  * -- it refuses `generated` and `unconfirmed`. This is the port for
    what a HUMAN supplies; a machine transcript must arrive with its
    `backend_selections` entry, which is the STT tool's job, not this one's.
    A local engine was found after writing, which strengthens the
    refusal rather than weakening it.
  * -- it does not invent the confidences. Writing `1.0` on the human's
    behalf is the same shape of invention as inventing an asset approval
The human declares them.
  * -- it emits no `transcript_segment` marker. Markers require
    `evidence_ids`, and the `kind:"human"` evidence path is still open debt.
    The contract asks no evidence of an utterance.

WHY IT REFUSES MORE THAN THE CONTRACT CHECKER DOES: that checker checks each transcript field against the
schema but never cross-checks `word_timestamps_available` against `words`
A plan can therefore claim word timestamps it does not
carry, or carry words while denying them -- a silent inconsistency, the same
shape as the off-frame overlay origin caught. This tool rejects it
at recording time. It also refuses an empty `text`, which the schema permits
(`transcript.text` has no `minLength`, unlike `caption.text`).

WHAT THIS TOOL DOES NOT DO:
  * It does not transcribe. It does not translate. It does not punctuate.
  * It does not judge accuracy or readability. Those stay with the human, so
    "a transcript exists" is not "the captions are verified".
  * It does not reject overlapping utterances. The contract demands
    non-overlap only of `transcription_chunks`, and speakers really do overlap.
    It counts them in the report instead.
  * It does not write the human's rationale/decider/timestamp into the Index.
    The contract's transcript record is `additionalProperties: false` with no
    slot for it, so that provenance goes to the report. Same as.

Standard library only.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

# The human's decision file. Closed on purpose: an unknown key is a typo or a
# field this tool would silently drop, and either way the human should hear it.
DECISION_REQUIRED = {
    "utterance_id", "source_id", "range", "text", "provenance_status",
    "utterance_confidence", "word_confidence", "speaker_confidence",
    "word_timestamps_available", "decided_by", "timestamp", "rationale",
}
DECISION_OPTIONAL = {"words"}
# The contract's transcript record, in contract order (index.schema.json:28).
UTTERANCE_FIELDS = ("utterance_id", "source_id", "range", "text", "provenance_status",
                    "utterance_confidence", "word_confidence", "speaker_confidence",
                    "word_timestamps_available")
CONFIDENCE_FIELDS = ("utterance_confidence", "word_confidence", "speaker_confidence")
# -- this is the human port. A machine transcript needs a backend
# declaration, which this tool does not write.
HUMAN_PROVENANCE = {"provided", "human_reviewed"}
MACHINE_PROVENANCE = {"generated", "unconfirmed"}
WORD_FIELDS = ("text", "start", "end", "confidence")


class TranscriptError(RuntimeError):
    pass


def require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TranscriptError(f"{label}: non-empty string required")
    return value


def require_int(value: Any, label: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TranscriptError(f"{label}: integer required (got {value!r})")
    if minimum is not None and value < minimum:
        raise TranscriptError(f"{label}: must be >= {minimum} (got {value})")
    return value


def require_confidence(value: Any, label: str) -> float:
    # -- the tool never supplies a default here. An absent confidence
    # is a missing human declaration, not a 1.0.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TranscriptError(f"{label}: number required (got {value!r}); "
                              f"this tool does not invent confidences (D-97)")
    if not 0 <= value <= 1:
        raise TranscriptError(f"{label}: must be within 0..1 (got {value})")
    return float(value)


def require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise TranscriptError(f"{label}: boolean required (got {value!r})")
    return value


def time_fraction(value: Any, label: str) -> Fraction:
    if not isinstance(value, dict):
        raise TranscriptError(f"{label}: object with value/timebase required")
    if set(value) != {"value", "timebase"}:
        raise TranscriptError(f"{label}: exactly {{value, timebase}} required (got {sorted(value)})")
    ticks = require_int(value.get("value"), f"{label}.value", minimum=0)
    timebase = value.get("timebase")
    if not isinstance(timebase, dict) or set(timebase) != {"num", "den"}:
        raise TranscriptError(f"{label}.timebase: exactly {{num, den}} required")
    num = require_int(timebase.get("num"), f"{label}.timebase.num", minimum=1)
    den = require_int(timebase.get("den"), f"{label}.timebase.den", minimum=1)
    return Fraction(ticks) * Fraction(num, den)


def range_fractions(value: Any, label: str) -> tuple[Fraction, Fraction]:
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise TranscriptError(f"{label}: exactly {{start, end}} required")
    start = time_fraction(value.get("start"), f"{label}.start")
    end = time_fraction(value.get("end"), f"{label}.end")
    # The contract checker's half-open-range rule rejects start >= end.
    if end <= start:
        raise TranscriptError(f"{label}: end must be after start ({end} <= {start})")
    return start, end


def overlaps(a: tuple[Fraction, Fraction], b: tuple[Fraction, Fraction]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def source_durations(index: dict[str, Any]) -> dict[str, Fraction]:
    out: dict[str, Fraction] = {}
    for source in index.get("sources", []):
        if not isinstance(source, dict) or not isinstance(source.get("source_id"), str):
            continue
        out[source["source_id"]] = time_fraction(source.get("duration"),
                                                 f"index source {source['source_id']}.duration")
    if not out:
        raise TranscriptError("index: at least one source with a duration is required")
    return out


def check_words(words: Any, utterance_range: tuple[Fraction, Fraction], label: str) -> int:
    """Verify the optional word array against the utterance that carries it.

    the contract checker never cross-checks these, so a plan can pass the schema while
    claiming words outside the utterance it belongs to.
    """
    if not isinstance(words, list) or not words:
        raise TranscriptError(f"{label}: non-empty array required when present "
                              f"(the contract sets minItems 1); omit the key instead")
    previous_end: Fraction | None = None
    for position, word in enumerate(words):
        where = f"{label}[{position}]"
        if not isinstance(word, dict):
            raise TranscriptError(f"{where}: object required")
        if set(word) != set(WORD_FIELDS):
            raise TranscriptError(f"{where}: exactly {sorted(WORD_FIELDS)} required "
                                  f"(got {sorted(word)})")
        require_text(word.get("text"), f"{where}.text")
        start, end = range_fractions({"start": word.get("start"), "end": word.get("end")}, where)
        require_confidence(word.get("confidence"), f"{where}.confidence")
        if start < utterance_range[0] or end > utterance_range[1]:
            raise TranscriptError(f"{where}: [{start}, {end}) falls outside its utterance "
                                  f"[{utterance_range[0]}, {utterance_range[1]})")
        if previous_end is not None and start < previous_end:
            raise TranscriptError(f"{where}: starts at {start}, before the previous word ended "
                                  f"at {previous_end}; words must be in ascending order")
        previous_end = end
    return len(words)


def record(index: dict[str, Any], decisions: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    out = copy.deepcopy(index)
    transcript = out.setdefault("transcript", [])
    if not isinstance(transcript, list):
        raise TranscriptError("index: transcript must be an array")
    durations = source_durations(out)

    existing_ids = {u.get("utterance_id") for u in transcript if isinstance(u, dict)}
    recorded_ranges: list[tuple[str, tuple[Fraction, Fraction]]] = [
        (str(u.get("utterance_id")), range_fractions(u.get("range"),
                                                     f"existing utterance {u.get('utterance_id')}.range"))
        for u in transcript if isinstance(u, dict)
    ]
    applied: list[dict[str, Any]] = []
    overlapping: list[list[str]] = []

    for position, decision in enumerate(decisions):
        label = f"transcript[{position}]"
        if not isinstance(decision, dict):
            raise TranscriptError(f"{label}: object required")
        unknown = set(decision) - DECISION_REQUIRED - DECISION_OPTIONAL
        if unknown:
            raise TranscriptError(f"{label}: unknown keys {sorted(unknown)}")
        missing = DECISION_REQUIRED - set(decision)
        if missing:
            raise TranscriptError(f"{label}: missing keys {sorted(missing)}")

        utterance_id = require_text(decision.get("utterance_id"), f"{label}.utterance_id")
        label = f"utterance {utterance_id}"
        if utterance_id in existing_ids:
            raise TranscriptError(f"{label}: already present in the Index; "
                                  f"the contract checker's index-source-id requires unique utterance IDs")
        existing_ids.add(utterance_id)

        source_id = require_text(decision.get("source_id"), f"{label}.source_id")
        if source_id not in durations:
            raise TranscriptError(f"{label}: source {source_id!r} is not in the Index sources "
                                  f"({sorted(durations)})")
        # The contract checker requires ranges to stay inside the source.
        utterance_range = range_fractions(decision.get("range"), f"{label}.range")
        if utterance_range[1] > durations[source_id]:
            raise TranscriptError(f"{label}: range ends at {utterance_range[1]}, past the "
                                  f"{durations[source_id]}s duration of source {source_id!r}")

        # The schema allows an empty transcript text (no minLength, unlike
        # caption.text). An utterance with nothing said is not a recording.
        text = require_text(decision.get("text"), f"{label}.text")

        provenance = decision.get("provenance_status")
        if provenance in MACHINE_PROVENANCE:
            raise TranscriptError(f"{label}: provenance_status {provenance!r} is not this tool's "
                                  f"business (D-97) -- it records what a human supplies. A machine "
                                  f"transcript must arrive with its backend_selections entry")
        if provenance not in HUMAN_PROVENANCE:
            raise TranscriptError(f"{label}.provenance_status: one of {sorted(HUMAN_PROVENANCE)} "
                                  f"required (got {provenance!r})")

        confidences = {field: require_confidence(decision.get(field), f"{label}.{field}")
                       for field in CONFIDENCE_FIELDS}
        available = require_bool(decision.get("word_timestamps_available"),
                                 f"{label}.word_timestamps_available")

        # -- the gap the contract checker leaves open. Either both or neither.
        word_count = 0
        if "words" in decision:
            if not available:
                raise TranscriptError(f"{label}: carries words but declares "
                                      f"word_timestamps_available false; the contract checker does not cross-check "
                                      f"these two, so this tool does")
            word_count = check_words(decision["words"], utterance_range, f"{label}.words")
        elif available:
            raise TranscriptError(f"{label}: declares word_timestamps_available true but carries "
                                  f"no words; the contract checker does not cross-check these two, so this tool does")

        decided_by = require_text(decision.get("decided_by"), f"{label}.decided_by")
        timestamp = require_text(decision.get("timestamp"), f"{label}.timestamp")
        rationale = require_text(decision.get("rationale"), f"{label}.rationale")

        # Overlap is legal -- speakers overlap -- but the human should see it.
        collided = [other_id for other_id, other in recorded_ranges
                    if overlaps(utterance_range, other)]
        if collided:
            overlapping.append([utterance_id, *collided])
        recorded_ranges.append((utterance_id, utterance_range))

        utterance = {
            "utterance_id": utterance_id,
            "source_id": source_id,
            "range": copy.deepcopy(decision["range"]),
            "text": text,
            "provenance_status": provenance,
            "utterance_confidence": decision["utterance_confidence"],
            "word_confidence": decision["word_confidence"],
            "speaker_confidence": decision["speaker_confidence"],
            "word_timestamps_available": available,
        }
        if word_count:
            utterance["words"] = copy.deepcopy(decision["words"])
        transcript.append(utterance)
        applied.append({
            "utterance_id": utterance_id, "source_id": source_id,
            "provenance_status": provenance, "word_count": word_count,
            "confidences": confidences,
            "decided_by": decided_by, "timestamp": timestamp, "rationale": rationale,
        })

    report = {
        "schema_version": "draftcut-transcript-decisions-report/v1",
        "index_id": out.get("index_id"),
        "mechanism": ("Records human-supplied transcript text, source ranges, provenance, and confidence values in the Index."),
        "not_done": ("this tool does not transcribe (it never invokes a speech-to-text backend), does not "
                     "judge accuracy or readability, emits no transcript_segment marker (markers "
                     "require evidence_ids and the kind:\"human\" evidence path is open debt), and "
                     "refuses generated/unconfirmed provenance -- a machine transcript must arrive "
                     "with its backend_selections entry"),
        "human_provenance_location": ("Reviewer metadata is stored in this report because the Index transcript schema has no fields for it."),
        "gate_pulled_forward": ("Checks that word_timestamps_available agrees with words and that transcript text is non-empty."),
        "verification_limit": ("Review transcript accuracy and caption readability separately."),
        "utterances_recorded": len(applied),
        "utterances_total": len(transcript),
        "words_recorded": sum(row["word_count"] for row in applied),
        "overlapping_utterances": overlapping,
        "applied": applied,
    }
    return out, report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", required=True, type=Path,
                        help="existing Index JSON from build_index.py")
    parser.add_argument("--transcript", required=True, type=Path,
                        help="JSON array of utterances, one object per utterance with keys: "
                             + ", ".join(sorted(DECISION_REQUIRED))
                             + " (optional: " + ", ".join(sorted(DECISION_OPTIONAL)) + ")")
    parser.add_argument("--out-index", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        index = json.loads(args.index.read_text(encoding="utf-8"))
        decisions = json.loads(args.transcript.read_text(encoding="utf-8"))
        if not isinstance(decisions, list) or not decisions:
            raise TranscriptError("transcript: non-empty JSON array required")
        out, report = record(index, decisions)
    except (TranscriptError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        print(f"TRANSCRIPT RECORD ERROR: {exc}", file=sys.stderr)
        return 2
    args.out_index.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.out_index.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"status": "recorded", "index": str(args.out_index),
                      "report": str(args.report),
                      "utterances_recorded": report["utterances_recorded"],
                      "utterances_total": report["utterances_total"],
                      "words_recorded": report["words_recorded"],
                      "overlapping_utterances": len(report["overlapping_utterances"])},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
