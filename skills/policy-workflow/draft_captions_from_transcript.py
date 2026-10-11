#!/usr/bin/env python3
"""Draft caption decisions from an Index transcript, for record_captions.py.

This closes the last two rows of the tier-2 map: "auto transcription as
caption text" and "make a transcript and add captions". opened the
transcript port; nothing read it. This is the first reader.

It writes the `--captions` file that `record_captions.py` already takes, so it
adds no contract field, no contract rules and no new CLI convention.

Label captions use timing chosen by a human; this tool does not derive that
timing from `screen_activity` markers. It drafts spoken captions from transcript
utterance ranges instead.

A spoken caption is a different shape. Its time is not chosen, it is
when the sentence was said, and the transcript already carries it. There is no
rule to invent, only an axis change:

    timeline = utterance_time + (clip.timeline_range.start - clip.source_range.start)

`source_segments[]` carries `source_id`, so each utterance is matched to its
original. Drafting requires the whole utterance to fit exactly one clip with
unchanged duration and no `act_speed_change`. Cut-away utterances are skipped;
ones that straddle cuts, match multiple clips, or have changed timing are refused.
The source-to-timeline offset is used only for eligible clips.

WHAT THIS DOES NOT DO -- everything that has no verifiable reference:
  * It does not split an utterance. Nothing here can judge when a spoken line
    is too long to read.
  * It does not nudge the timing. No lead-in or hold-out constant is invented
    (forbids absorbing error into a constant).
  * It does not choose which utterances deserve captions. It emits them all
    and the human deletes.
  * It writes no "captions follow their subject" default or warning for labels;
    their placement is a human decision.
  * It invents no `evidence_ids`. With `--evidence-map` a human supplies them;
    without it they stay empty and `record_captions.py` refuses downstream, so
    the human must fill them in. Same shape as `record_overlays.py --asset-map`.
  * It invents no rationale, decider or timestamp. Those are declared once in
    `--defaults`.

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

# Declared once per project by the human, not per caption. Closed on purpose.
DEFAULTS_REQUIRED = {
    "caption_id_prefix", "language", "speaker", "style_ref", "safe_area",
    "review_status", "track_id", "overlay_x", "overlay_y",
    "decided_by", "timestamp", "rationale",
}
# -- an unreviewed transcript does not seed captions.
HUMAN_PROVENANCE = {"provided", "human_reviewed"}


class DraftError(RuntimeError):
    pass


def require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DraftError(f"{label}: non-empty string required")
    return value


def require_int(value: Any, label: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DraftError(f"{label}: integer required (got {value!r})")
    if minimum is not None and value < minimum:
        raise DraftError(f"{label}: must be >= {minimum} (got {value})")
    return value


def time_fraction(value: Any, label: str) -> Fraction:
    if not isinstance(value, dict) or set(value) != {"value", "timebase"}:
        raise DraftError(f"{label}: exactly {{value, timebase}} required")
    ticks = require_int(value.get("value"), f"{label}.value", minimum=0)
    timebase = value.get("timebase")
    if not isinstance(timebase, dict) or set(timebase) != {"num", "den"}:
        raise DraftError(f"{label}.timebase: exactly {{num, den}} required")
    num = require_int(timebase.get("num"), f"{label}.timebase.num", minimum=1)
    den = require_int(timebase.get("den"), f"{label}.timebase.den", minimum=1)
    return Fraction(ticks) * Fraction(num, den)


def range_fractions(value: Any, label: str) -> tuple[Fraction, Fraction]:
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise DraftError(f"{label}: exactly {{start, end}} required")
    start = time_fraction(value.get("start"), f"{label}.start")
    end = time_fraction(value.get("end"), f"{label}.end")
    if end <= start:
        raise DraftError(f"{label}: end must be after start ({end} <= {start})")
    return start, end


def as_time(seconds: Fraction, timebase: dict[str, int], label: str) -> dict[str, Any]:
    """Express an exact time on a timebase, refusing anything that would round.

    The canonical time contract is an integer tick count (`canonical-time-integer`),
    so a caption boundary that does not land on a tick is not representable. It
    is refused rather than rounded: rounding here would be the tool moving the
    time, which is exactly what forbids.
    """
    ticks = seconds / (Fraction(timebase["num"], timebase["den"]))
    if ticks.denominator != 1:
        raise DraftError(f"{label}: {seconds} is not an integer number of "
                         f"{timebase['num']}/{timebase['den']} ticks ({ticks}); this tool does not "
                         f"round a caption boundary (D-95)")
    return {"value": int(ticks), "timebase": dict(timebase)}


def load_defaults(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DraftError("defaults: JSON object required")
    unknown = set(raw) - DEFAULTS_REQUIRED
    if unknown:
        raise DraftError(f"defaults: unknown keys {sorted(unknown)}")
    missing = DEFAULTS_REQUIRED - set(raw)
    if missing:
        raise DraftError(f"defaults: missing keys {sorted(missing)}; this tool declares nothing "
                         f"on the human's behalf (D-98 §3)")
    for key in ("caption_id_prefix", "language", "speaker", "style_ref", "safe_area",
                "review_status", "track_id", "decided_by", "timestamp", "rationale"):
        require_text(raw[key], f"defaults.{key}")
    require_int(raw["overlay_x"], "defaults.overlay_x", minimum=0)
    require_int(raw["overlay_y"], "defaults.overlay_y", minimum=0)
    return dict(raw)


def plan_placement(plan: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str]]:
    """One row per timeline clip: its segment, source, source span and offset.

    Also returns the segments whose action list changes the time axis, which
    makes the offset wrong; those are refused rather than approximated.
    """
    segments = {s.get("segment_id"): s for s in plan.get("source_segments", [])
                if isinstance(s, dict)}
    speed_segments = {a.get("source_segment_id") for a in plan.get("actions", [])
                      if isinstance(a, dict) and a.get("action") == "act_speed_change"}
    rows: list[dict[str, Any]] = []
    for clip in plan.get("timeline_clips", []):
        if not isinstance(clip, dict):
            continue
        segment_id = clip.get("source_segment_id")
        segment = segments.get(segment_id)
        if segment is None:
            raise DraftError(f"clip {clip.get('clip_id')!r}: source_segment_id {segment_id!r} is "
                             f"not in plan.source_segments")
        source_span = range_fractions(clip.get("source_range"),
                                      f"clip {clip.get('clip_id')}.source_range")
        timeline_span = range_fractions(clip.get("timeline_range"),
                                        f"clip {clip.get('clip_id')}.timeline_range")
        rows.append({
            "clip_id": clip.get("clip_id"),
            "segment_id": segment_id,
            "source_id": segment.get("source_id"),
            "source_span": source_span,
            "offset": timeline_span[0] - source_span[0],
            "timebase": clip["timeline_range"]["start"]["timebase"],
            "span_preserved": (source_span[1] - source_span[0]) == (timeline_span[1] - timeline_span[0]),
        })
    if not rows:
        raise DraftError("plan: at least one timeline clip is required")
    return rows, speed_segments


def draft(index: dict[str, Any], plan: dict[str, Any], defaults: dict[str, Any],
          evidence_map: dict[str, Any] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    transcript = index.get("transcript")
    if not isinstance(transcript, list) or not transcript:
        raise DraftError("index.transcript is empty; run record_transcript.py first (D-97)")

    rows, speed_segments = plan_placement(plan)
    tracks = {t.get("track_id"): t for t in plan.get("tracks", []) if isinstance(t, dict)}
    styles = {s.get("style_id") for s in plan.get("caption_styles", []) if isinstance(s, dict)}
    existing = {c.get("caption_id") for c in plan.get("captions", []) if isinstance(c, dict)}

    # Pull record_captions.py's own refusals forward so a draft that cannot be
    # recorded fails here, once, instead of once per utterance downstream.
    track_id = defaults["track_id"]
    track = tracks.get(track_id)
    if track is None:
        raise DraftError(f"defaults.track_id {track_id!r} is not a track in the plan")
    if track.get("kind") != "captions":
        raise DraftError(f"defaults.track_id {track_id!r} is a {track.get('kind')!r} track, "
                         f"not 'captions'")
    if defaults["style_ref"] not in styles:
        raise DraftError(f"defaults.style_ref {defaults['style_ref']!r} is not a caption style "
                         f"in the plan")

    drafted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()

    for position, utterance in enumerate(transcript):
        if not isinstance(utterance, dict):
            raise DraftError(f"index.transcript[{position}]: object required")
        utterance_id = require_text(utterance.get("utterance_id"),
                                    f"index.transcript[{position}].utterance_id")
        label = f"utterance {utterance_id}"
        provenance = utterance.get("provenance_status")
        if provenance not in HUMAN_PROVENANCE:
            raise DraftError(f"{label}: provenance_status {provenance!r} does not seed captions; "
                             f"one of {sorted(HUMAN_PROVENANCE)} required (D-98 §4)")
        text = require_text(utterance.get("text"), f"{label}.text")
        source_id = require_text(utterance.get("source_id"), f"{label}.source_id")
        start, end = range_fractions(utterance.get("range"), f"{label}.range")

        # Exactly one clip must contain the whole utterance.
        containing = [row for row in rows
                      if row["source_id"] == source_id
                      and row["source_span"][0] <= start and end <= row["source_span"][1]]
        if len(containing) > 1:
            raise DraftError(f"{label}: contained by {len(containing)} clips "
                             f"({[r['clip_id'] for r in containing]}); the caption/clip binding "
                             f"requires exactly one (D-90)")
        if not containing:
            overlapping = [row["clip_id"] for row in rows
                           if row["source_id"] == source_id
                           and start < row["source_span"][1] and row["source_span"][0] < end]
            if overlapping:
                raise DraftError(f"{label}: straddles the cut at clips {overlapping}; a human must "
                                 f"split the utterance -- this tool does not (D-98 §3)")
            # Cut away entirely. Speech that is not in the film gets no caption.
            skipped.append({"utterance_id": utterance_id, "reason": "cut away from the timeline"})
            continue

        row = containing[0]
        if row["segment_id"] in speed_segments:
            raise DraftError(f"{label}: clip {row['clip_id']!r} carries act_speed_change, so the "
                             f"axis change is not a single offset; this tool does not "
                             f"approximate it")
        if not row["span_preserved"]:
            raise DraftError(f"{label}: clip {row['clip_id']!r} has a source span differing from "
                             f"its timeline span; the axis change is not a single offset")

        caption_id = defaults["caption_id_prefix"] + utterance_id
        if caption_id in seen:
            raise DraftError(f"{label}: caption_id {caption_id!r} would repeat; utterance IDs "
                             f"must be unique")
        if caption_id in existing:
            raise DraftError(f"{label}: caption_id {caption_id!r} is already in the plan")
        seen.add(caption_id)

        timebase = row["timebase"]
        evidence_ids = []
        if evidence_map is not None:
            supplied = evidence_map.get(row["segment_id"])
            if supplied is None:
                raise DraftError(f"{label}: evidence map has no entry for segment "
                                 f"{row['segment_id']!r}")
            if (not isinstance(supplied, list) or not supplied
                    or not all(isinstance(e, str) and e for e in supplied)):
                raise DraftError(f"evidence map[{row['segment_id']!r}]: non-empty array of "
                                 f"non-empty strings required")
            evidence_ids = list(supplied)

        drafted.append({
            "caption_id": caption_id,
            "text": text,
            "language": defaults["language"],
            "speaker": defaults["speaker"],
            "style_ref": defaults["style_ref"],
            "safe_area": defaults["safe_area"],
            "review_status": defaults["review_status"],
            "track_id": track_id,
            "source_segment_id": row["segment_id"],
            "timeline_range": {
                "start": as_time(start + row["offset"], timebase, f"{label}.timeline_range.start"),
                "end": as_time(end + row["offset"], timebase, f"{label}.timeline_range.end"),
            },
            "overlay_x": defaults["overlay_x"],
            "overlay_y": defaults["overlay_y"],
            "evidence_ids": evidence_ids,
            "decided_by": defaults["decided_by"],
            "timestamp": defaults["timestamp"],
            "rationale": defaults["rationale"],
        })

    if not drafted:
        raise DraftError("every utterance was cut away from the timeline; there is nothing to draft")

    report = {
        "schema_version": "draftcut-caption-draft-report/v1",
        "plan_id": plan.get("plan_id"),
        "index_id": index.get("index_id"),
        "mechanism": ("Maps each eligible transcript range to the timeline using its containing clip's source-to-timeline offset."),
        "from_transcript": ["text", "timeline_range", "source_segment_id", "caption_id"],
        "from_defaults": sorted(DEFAULTS_REQUIRED - {"caption_id_prefix"}),
        "not_done": ("Does not split utterances, adjust timing, or choose which eligible utterances deserve captions. Review the draft and remove unwanted captions."),
        "d95_scope": ("Label caption timing is not derived from screen_activity markers; spoken caption timing comes from the transcript."),
        "evidence_supplied": evidence_map is not None,
        "downstream_gate": ("without --evidence-map every evidence_ids is empty and "
                            "record_captions.py refuses the draft, which is deliberate: this tool "
                            "does not invent provenance"
                            if evidence_map is None else
                            "evidence_ids supplied per segment by the human"),
        "verification_limit": ("Drafting does not establish caption accuracy or readability; review the result."),
        "utterances_total": len(transcript),
        "captions_drafted": len(drafted),
        "skipped": skipped,
    }
    return drafted, report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", required=True, type=Path,
                        help="Index JSON containing transcripts with provided or human_reviewed provenance.")
    parser.add_argument("--plan", required=True, type=Path, help="merged EditPlan JSON")
    parser.add_argument("--defaults", required=True, type=Path,
                        help="project-level declarations, one JSON object with keys: "
                             + ", ".join(sorted(DEFAULTS_REQUIRED)))
    parser.add_argument("--evidence-map", type=Path,
                        help="segment_id -> evidence_ids; without it evidence stays empty and "
                             "record_captions.py refuses the draft, on purpose")
    parser.add_argument("--out-captions", required=True, type=Path,
                        help="written in record_captions.py --captions format")
    parser.add_argument("--report", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        index = json.loads(args.index.read_text(encoding="utf-8"))
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        defaults = load_defaults(json.loads(args.defaults.read_text(encoding="utf-8")))
        evidence_map = None
        if args.evidence_map is not None:
            evidence_map = json.loads(args.evidence_map.read_text(encoding="utf-8"))
            if not isinstance(evidence_map, dict):
                raise DraftError("evidence map: JSON object required")
        drafted, report = draft(index, plan, defaults, evidence_map)
    except (DraftError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        print(f"CAPTION DRAFT ERROR: {exc}", file=sys.stderr)
        return 2
    args.out_captions.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.out_captions.write_text(json.dumps(drafted, ensure_ascii=False, indent=1) + "\n",
                                 encoding="utf-8")
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"status": "drafted", "captions": str(args.out_captions),
                      "report": str(args.report),
                      "utterances_total": report["utterances_total"],
                      "captions_drafted": report["captions_drafted"],
                      "skipped": len(report["skipped"]),
                      "evidence_supplied": report["evidence_supplied"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
