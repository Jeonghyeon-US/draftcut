#!/usr/bin/env python3
"""Draft an EditPlan for one silent screen recording using an existing Observation Index.

The tool does not run OCR or scene detection. It infers the end of activity
from the last screen-activity marker after the inferred action start."""
from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any


NEAR_SECONDS = Fraction(3, 2)
TAIL_SECONDS = Fraction(3, 1)
MIN_SPEED_SECONDS = Fraction(4, 1)
MAX_SPEED_FACTOR = 10


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: JSON root must be an object")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def as_time(value: Any, label: str) -> Fraction:
    if not isinstance(value, dict) or set(value) != {"value", "timebase"}:
        raise ValueError(f"{label}: canonical time must contain value and timebase")
    tick, base = value.get("value"), value.get("timebase")
    if type(tick) is not int or not isinstance(base, dict) or set(base) != {"num", "den"}:
        raise ValueError(f"{label}: invalid canonical time")
    numerator, denominator = base.get("num"), base.get("den")
    if type(numerator) is not int or type(denominator) is not int or numerator <= 0 or denominator <= 0:
        raise ValueError(f"{label}: timebase must be positive integer rational")
    return Fraction(tick * numerator, denominator)


def canonical_time(seconds: Fraction) -> dict[str, Any]:
    """Return an exact integer tick representation; never round a decision."""
    if seconds < 0:
        raise ValueError("cannot emit a negative canonical time")
    return {
        "value": seconds.numerator,
        "timebase": {"num": 1, "den": seconds.denominator},
    }


def time_range(start: Fraction, end: Fraction) -> dict[str, Any]:
    if start >= end:
        raise ValueError("cannot emit an empty half-open range")
    return {"start": canonical_time(start), "end": canonical_time(end)}


def source_and_duration(index: dict[str, Any]) -> tuple[str, Fraction]:
    sources = index.get("sources")
    if not isinstance(sources, list) or len(sources) != 1:
        raise ValueError("L3 v1 requires exactly one source")
    source = sources[0]
    if not isinstance(source, dict) or not isinstance(source.get("source_id"), str) or not source["source_id"]:
        raise ValueError("source must have a non-empty source_id")
    duration = as_time(source.get("duration"), "source.duration")
    if duration <= 0:
        raise ValueError("source duration must be positive")
    return source["source_id"], duration


def scene_events(index: dict[str, Any], source_id: str) -> tuple[list[Fraction], str]:
    events: set[Fraction] = set()
    first_start: Fraction | None = None
    evidence_id: str | None = None
    markers = index.get("markers")
    if not isinstance(markers, list):
        raise ValueError("index markers must be an array")
    for position, marker in enumerate(markers):
        if not isinstance(marker, dict) or marker.get("marker_type") != "shot_scene_boundary":
            continue
        if marker.get("source_id") != source_id:
            continue
        current = marker.get("range")
        if not isinstance(current, dict):
            raise ValueError(f"scene marker {position}: range is required")
        start = as_time(current.get("start"), f"scene marker {position}.range.start")
        end = as_time(current.get("end"), f"scene marker {position}.range.end")
        if start >= end:
            raise ValueError(f"scene marker {position}: range must be non-empty")
        events.update((start, end))
        if first_start is None or start < first_start:
            marker_evidence = marker.get("evidence_ids")
            if not isinstance(marker_evidence, list) or not marker_evidence or not isinstance(marker_evidence[0], str):
                raise ValueError(f"scene marker {position}: evidence_ids is required")
            first_start, evidence_id = start, marker_evidence[0]
    if first_start is None or evidence_id is None:
        raise ValueError("L3 v1 requires at least one shot_scene_boundary marker")
    return sorted(events), evidence_id


def text_events(index: dict[str, Any], source_id: str, delta: int) -> list[Fraction]:
    samples: dict[Fraction, list[str]] = {}
    observations = index.get("onscreen_text")
    if not isinstance(observations, list):
        raise ValueError("index onscreen_text must be an array")
    for position, observation in enumerate(observations):
        if not isinstance(observation, dict) or observation.get("source_id") != source_id:
            continue
        text = observation.get("text")
        if not isinstance(text, str):
            raise ValueError(f"onscreen_text {position}: text must be a string")
        at = as_time(observation.get("time"), f"onscreen_text {position}.time")
        samples.setdefault(at, []).append(text)
    rows = [(at, len("\n".join(sorted(texts)))) for at, texts in sorted(samples.items())]
    return [at for offset, (at, amount) in enumerate(rows[1:], start=1)
            if abs(amount - rows[offset - 1][1]) >= delta]


def result_complete_event(index: dict[str, Any], source_id: str, action_start: Fraction) -> tuple[Fraction, list[str]]:
    candidates: list[tuple[Fraction, list[str]]] = []
    for position, marker in enumerate(index.get("markers", [])):
        if not isinstance(marker, dict) or marker.get("marker_type") != "screen_activity" or marker.get("source_id") != source_id:
            continue
        current = marker.get("range")
        if not isinstance(current, dict):
            raise ValueError(f"screen_activity marker {position}: range is required")
        start = as_time(current.get("start"), f"screen_activity marker {position}.range.start")
        end = as_time(current.get("end"), f"screen_activity marker {position}.range.end")
        if start >= end:
            raise ValueError(f"screen_activity marker {position}: range must be non-empty")
        evidence_ids = marker.get("evidence_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids or not all(isinstance(item, str) and item for item in evidence_ids):
            raise ValueError(f"screen_activity marker {position}: evidence_ids is required")
        if end > action_start:
            candidates.append((end, evidence_ids))
    if not candidates:
        raise ValueError("L3 requires at least one screen_activity marker after action_start")
    return max(candidates, key=lambda candidate: candidate[0])


def speed_factor(duration: Fraction) -> int:
    """The v1 declared heuristic: integer output length nearest the 4--6 s band.

    A duration inside the band has equal zero distance. The smaller factor is
    selected deterministically; otherwise the midpoint (5 s), then the smaller
    factor, resolves the mathematical tie without adding another input signal.
    """
    def key(factor: int) -> tuple[Fraction, Fraction, int]:
        output = duration / factor
        distance = Fraction(0) if Fraction(4) <= output <= Fraction(6) else min(abs(output - 4), abs(output - 6))
        return distance, abs(output - 5), factor
    return min(range(1, MAX_SPEED_FACTOR + 1), key=key)


def floor_to_grid(value: Fraction, frame_rate: Fraction) -> Fraction:
    """Snap a non-negative time down to an exact output-frame boundary."""
    if value < 0 or frame_rate <= 0:
        raise ValueError("grid snapping requires non-negative time and positive frame rate")
    coordinate = value * frame_rate
    return Fraction(coordinate.numerator // coordinate.denominator, 1) / frame_rate


def ceil_to_grid(value: Fraction, frame_rate: Fraction) -> Fraction:
    """Snap a non-negative time up to an exact output-frame boundary."""
    if value < 0 or frame_rate <= 0:
        raise ValueError("grid snapping requires non-negative time and positive frame rate")
    coordinate = value * frame_rate
    return Fraction(-(-coordinate.numerator // coordinate.denominator), 1) / frame_rate


def snap_trim_range(trim_start: Fraction, trim_end: Fraction, duration: Fraction, frame_rate: Fraction) -> tuple[Fraction, Fraction]:
    """Expand the derived trim range to frame boundaries without exceeding source."""
    start = floor_to_grid(trim_start, frame_rate)
    end = min(ceil_to_grid(trim_end, frame_rate), floor_to_grid(duration, frame_rate))
    if start >= end:
        raise ValueError("frame-snapped trim range is empty")
    return start, end


def snap_speed_range(start: Fraction, end: Fraction, factor: int, frame_rate: Fraction) -> tuple[Fraction, Fraction] | None:
    """Inset a speed range and make its source-frame count divisible by factor."""
    snapped_start = ceil_to_grid(start, frame_rate)
    snapped_end = floor_to_grid(end, frame_rate)
    if snapped_start >= snapped_end:
        return None
    frames = (snapped_end - snapped_start) * frame_rate
    if frames.denominator != 1:
        raise ValueError("grid-snapped speed range must contain an integer frame count")
    compatible_frames = frames.numerator - (frames.numerator % factor)
    snapped_end = snapped_start + Fraction(compatible_frames, 1) / frame_rate
    if snapped_end - snapped_start < MIN_SPEED_SECONDS:
        return None
    return snapped_start, snapped_end


def speed_ranges(trim_start: Fraction, trim_end: Fraction, scenes: list[Fraction], texts: list[Fraction]) -> list[tuple[Fraction, Fraction, int]]:
    """Return only event-to-event static intervals that cannot cross text events."""
    events = sorted({event for event in scenes + texts if trim_start <= event <= trim_end})
    text_set = set(texts)
    ranges: list[tuple[Fraction, Fraction, int]] = []
    for left, right in zip(events, events[1:]):
        start, end = left + NEAR_SECONDS, right - NEAR_SECONDS
        if end - start < MIN_SPEED_SECONDS:
            continue
        # Half-open ranges include their start and exclude their end. The event
        # construction already separates adjacent incidents; retain the explicit
        # check as the policy's enforceable safety condition.
        if any(start <= event < end for event in text_set):
            continue
        factor = speed_factor(end - start)
        # A factor of one is an identity operation, not an act_speed_change.
        # Keeping it out of the plan also keeps the action list semantically
        # honest when a short static interval is already 4--6 seconds long.
        if factor > 1:
            ranges.append((start, end, factor))
    return ranges


def evidence_for_range(index: dict[str, Any], source_id: str, segment_range: tuple[Fraction, Fraction]) -> list[str]:
    """Every recorded evidence item overlapping a split segment, in index order.

    the contract checker requires an action's evidence to overlap its own segment
    (``), so a split plan cannot cite one global provenance
    list. Returning every overlapping item keeps the citation complete: the piece that
    contains ``result_complete`` cites the ``screen_activity`` evidence trim_end is
    derived from, which a single representative would have dropped because scene
    evidence is recorded first.
    """
    start, end = segment_range
    found: list[str] = []
    for evidence in index.get("evidence", []):
        if not isinstance(evidence, dict) or evidence.get("source_id") != source_id:
            continue
        evidence_id = evidence.get("evidence_id")
        current = evidence.get("range")
        if not isinstance(evidence_id, str) or not evidence_id or not isinstance(current, dict):
            continue
        evidence_start = as_time(current.get("start"), f"evidence {evidence_id}.range.start")
        evidence_end = as_time(current.get("end"), f"evidence {evidence_id}.range.end")
        if evidence_start < end and start < evidence_end:
            found.append(evidence_id)
    if not found:
        raise ValueError(f"no recorded evidence overlaps split segment {start}-{end}")
    return list(dict.fromkeys(found))


def ffmpeg_version() -> str:
    completed = subprocess.run(["ffmpeg", "-version"], check=True, capture_output=True, text=True)
    first_line = completed.stdout.splitlines()[0] if completed.stdout else ""
    match = re.search(r"ffmpeg version\s+(\S+)", first_line)
    if not match:
        raise RuntimeError("could not measure ffmpeg version")
    return match.group(1)


def build_plan(index: dict[str, Any], text_delta: int) -> dict[str, Any]:
    source_id, duration = source_and_duration(index)
    scenes, evidence_id = scene_events(index, source_id)
    texts = text_events(index, source_id, text_delta)
    if not texts:
        raise ValueError("L3 v1 requires at least one onscreen-text event")

    output_policy = {
        "width": 1920, "height": 1080, "pixel_format": "yuv420p",
        "sar": {"num": 1, "den": 1}, "frame_rate": {"num": 30, "den": 1},
        "frame_rate_mode": "cfr", "encoder_timebase": {"num": 1, "den": 15360},
        "color_primaries": "bt709", "color_transfer": "bt709", "color_matrix": "bt709",
        "color_range": "tv", "audio_policy": "none", "time_axis": "final_timeline",
    }
    frame_rate = Fraction(output_policy["frame_rate"]["num"], output_policy["frame_rate"]["den"])
    action_start = min(marker_start(index, source_id))
    result_complete, _activity_evidence_ids = result_complete_event(index, source_id, action_start)
    derived_trim_start = max(Fraction(0), action_start - NEAR_SECONDS)
    derived_trim_end = min(duration, result_complete + TAIL_SECONDS)
    if derived_trim_start >= derived_trim_end:
        raise ValueError("derived trim range is empty")
    derived_speed_ranges = speed_ranges(derived_trim_start, derived_trim_end, scenes, texts)
    trim_start, trim_end = snap_trim_range(derived_trim_start, derived_trim_end, duration, frame_rate)
    snapped_speed_ranges: list[tuple[Fraction, Fraction, int]] = []
    for start, end, factor in derived_speed_ranges:
        snapped = snap_speed_range(start, end, factor, frame_rate)
        if snapped is not None:
            snapped_start, snapped_end = snapped
            if trim_start <= snapped_start < snapped_end <= trim_end:
                snapped_speed_ranges.append((snapped_start, snapped_end, factor))
            else:
                raise ValueError("inset speed range must remain inside outer-snapped trim range")

    pieces: list[tuple[Fraction, Fraction, int | None]] = []
    cursor = trim_start
    for start, end, factor in snapped_speed_ranges:
        if start < cursor:
            raise ValueError("speed ranges must be non-overlapping and source-ordered")
        if cursor < start:
            pieces.append((cursor, start, None))
        pieces.append((start, end, factor))
        cursor = end
    if cursor < trim_end:
        pieces.append((cursor, trim_end, None))
    if not pieces:
        raise ValueError("split trim range produced no non-empty pieces")

    source_segments: list[dict[str, Any]] = []
    timeline_clips: list[dict[str, Any]] = []
    speed_actions: list[dict[str, Any]] = []
    timeline_cursor = Fraction(0)
    speed_number = 1
    for piece_number, (start, end, factor) in enumerate(pieces, start=1):
        segment_id = f"seg-{piece_number:02d}"
        clip_id = f"clip-{piece_number:02d}"
        piece_range = time_range(start, end)
        source_segments.append({"segment_id": segment_id, "source_id": source_id, "source_range": piece_range})
        output_duration = end - start if factor is None else (end - start) / factor
        timeline_range = time_range(timeline_cursor, timeline_cursor + output_duration)
        timeline_clips.append({
            "clip_id": clip_id,
            "source_segment_id": segment_id,
            "track_id": "v1",
            "rounding": "toward_zero",
            "source_range": piece_range,
            "timeline_range": timeline_range,
        })
        if factor is not None:
            speed_actions.append({
                "action_id": f"a-speed-{speed_number:02d}",
                "action": "act_speed_change",
                "v1_executable": "yes(effect-verified)",
                "verification_kind": "effect_measurement",
                "source_segment_id": segment_id,
                "evidence_ids": evidence_for_range(index, source_id, (start, end)),
                "params": {
                    "range": piece_range,
                    "factor": {"num": factor, "den": 1},
                    "audio_handling": "drop",
                },
            })
            speed_number += 1
        timeline_cursor += output_duration

    plan_id = f"{source_id}-plan"
    #: one act_trim_segment per segment. The contract checker requires an action's evidence to
    # overlap its own segment, so a single trim action on the
    # first piece could only cite evidence from the head of the source and would drop
    # the screen_activity evidence that trim_end is derived from. Declaring the
    # trim per piece keeps every piece's provenance attached to the piece it justifies.
    actions: list[dict[str, Any]] = [{
        "action_id": f"a-trim-{number:02d}",
        "action": "act_trim_segment",
        "v1_executable": "yes(effect-verified)",
        "verification_kind": "effect_measurement",
        "source_segment_id": segment["segment_id"],
        "evidence_ids": evidence_for_range(index, source_id, (piece[0], piece[1])),
    } for number, (segment, piece) in enumerate(zip(source_segments, pieces), start=1)]
    actions.extend(speed_actions)

    return {
        "schema_version": "1.0",
        "plan_id": plan_id,
        "index_id": index.get("index_id"),
        "stage": "draft",
        "source_segments": source_segments,
        "timeline_clips": timeline_clips,
        "actions": actions,
        "constraints": {"preserve": [], "lock_orders": [], "releases": []},
        "assets": [],
        "reviews": [],
        "tracks": [{"track_id": "v1", "kind": "video", "z_order": 0}],
        "captions": [],
        "output_policy": output_policy,
        "caption_styles": [],
        "concat_compatibility": {
            "verdict": "unverified", "spec_id": "draftcut-concat-copy-compatibility",
            "spec_version": "1", "evidence_ids": [evidence_id], "review_id": None,
        },
        "backend_selections": copy.deepcopy(index.get("backend_selections", [])),
        "cost_summary": {"estimated_total": {"amount": 0, "currency": "USD", "basis": "local-only: ffmpeg and macOS Vision"}},
        "cost_calls": [],
        "reproducibility": {
            "ffmpeg_version": ffmpeg_version(),
            "filter_arguments": [
                "trim", "setpts",
                f"l3_text_delta={text_delta}",
                "l3_speed_factor=integer-nearest-4-to-6-second-output;cap=10",
            ],
            "loudnorm_two_pass": {"status": "not_applicable"},
            "render_log_id": plan_id,
            "output_sha256": None,
        },
        "qc": {
            "pre_render": {"severity": "info", "state": "pending", "auto_retry": False},
            "post_render": {"severity": "info", "state": "pending", "auto_retry": False},
        },
    }


def marker_start(index: dict[str, Any], source_id: str) -> list[Fraction]:
    starts: list[Fraction] = []
    for position, marker in enumerate(index.get("markers", [])):
        if isinstance(marker, dict) and marker.get("marker_type") == "shot_scene_boundary" and marker.get("source_id") == source_id:
            current = marker.get("range")
            if isinstance(current, dict):
                starts.append(as_time(current.get("start"), f"scene marker {position}.range.start"))
    if not starts:
        raise ValueError("L3 v1 requires at least one shot_scene_boundary marker")
    return starts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path, help="Existing Observation Index JSON.")
    parser.add_argument("--output", required=True, type=Path, help="EditPlan JSON to write")
    parser.add_argument("--text-delta", default=30, type=int, help="minimum per-sample text-count change (default: 30)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.text_delta <= 0:
        raise ValueError("--text-delta must be a positive integer")
    write_json(args.output, build_plan(read_json(args.index), args.text_delta))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"decide_cuts.py: {exc}", file=sys.stderr)
        raise SystemExit(1)
