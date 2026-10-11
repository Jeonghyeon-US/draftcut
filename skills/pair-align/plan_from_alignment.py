#!/usr/bin/env python3
"""Turn trusted audio-alignment intervals into an executable EditPlan bundle.

This converter deliberately does not decide *which* intervals to keep. It only
translates the trusted intervals already present in an alignment document into
the closed EditPlan/Index contracts used by ``executor/assemble.py``.

One thing it does decide, because the contracts leave no choice: when the edit
places a later source moment earlier on the timeline, the plan must *declare*
that reordering. The declaration is read off the clip order rather than
supplied, so it can only ever describe what will render.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
CONCAT_SPEC = ROOT / "contracts" / "concat-copy-compatibility.spec.md"
REQUIRED_OUTPUT_FRAME_RATE = {"num": 30000, "den": 1001}
SOURCE_ID = "alignment-source"
INDEX_ID = "alignment-source-index"
PLAN_ID = "alignment-edit-plan"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def decimal_fraction(value: Any, label: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{label} must be a finite decimal number")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a finite decimal number") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be a finite decimal number")
    return Fraction(parsed)


def rational_mapping(value: Any, label: str) -> Fraction | None:
    """Read only the contract's structured rational form; never split text rates."""
    if not isinstance(value, Mapping):
        return None
    if set(value) != {"num", "den"}:
        raise ValueError(f"{label} must contain exactly num and den")
    num, den = value["num"], value["den"]
    if isinstance(num, bool) or not isinstance(num, int) or isinstance(den, bool) or not isinstance(den, int):
        raise ValueError(f"{label}.num and {label}.den must be integers")
    if num <= 0 or den <= 0:
        raise ValueError(f"{label}.num and {label}.den must be positive")
    return Fraction(num, den)


def frame_rate_from_alignment(alignment: Mapping[str, Any]) -> tuple[Fraction, str]:
    """Prefer an alignment-provided rational rate, without parsing ffprobe strings.

    The artifact retained ffprobe's raw string fields instead of the
    structured rate stated in this task. Its requested output policy fixes the
    same 30000/1001 rate, so that fixed contract value is the narrow fallback.
    A differently-rated input must carry a ``{num, den}`` mapping.
    """
    source = alignment.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("alignment.source must be an object")
    ffprobe = source.get("ffprobe")
    candidates: list[tuple[str, Any]] = [
        ("source.frame_rate", source.get("frame_rate")),
        ("source.video_frame_rate", source.get("video_frame_rate")),
    ]
    if isinstance(ffprobe, Mapping):
        candidates.extend((
            ("source.ffprobe.frame_rate", ffprobe.get("frame_rate")),
            ("source.ffprobe.video_frame_rate", ffprobe.get("video_frame_rate")),
            ("source.ffprobe.timebase", ffprobe.get("timebase")),
        ))
        streams = ffprobe.get("streams")
        if isinstance(streams, list):
            for index, stream in enumerate(streams):
                if not isinstance(stream, Mapping) or stream.get("codec_type") != "video":
                    continue
                for field in ("frame_rate", "r_frame_rate", "avg_frame_rate"):
                    candidates.append((f"source.ffprobe.streams[{index}].{field}", stream.get(field)))
    for label, candidate in candidates:
        parsed = rational_mapping(candidate, label)
        if parsed is not None:
            return parsed, label
    return Fraction(REQUIRED_OUTPUT_FRAME_RATE["num"], REQUIRED_OUTPUT_FRAME_RATE["den"]), "required_output_policy_default"


def canonical_time(seconds: Fraction) -> dict[str, Any]:
    """Represent an exact non-negative duration without a float conversion.

    Snapped video boundaries naturally reduce to 1/30000 ticks. Source
    duration is not necessarily a video-frame boundary (is not), so
    this helper preserves its exact decimal rational with its own denominator.
    """
    if seconds < 0:
        raise ValueError("canonical time must be non-negative")
    return {"value": seconds.numerator, "timebase": {"num": 1, "den": seconds.denominator}}


def canonical_range(start: Fraction, end: Fraction) -> dict[str, Any]:
    if start < 0 or end <= start:
        raise ValueError("snapped interval must be non-empty and non-negative")
    return {"start": canonical_time(start), "end": canonical_time(end)}


def snap_frame_toward_zero(seconds: Fraction, frame_rate: Fraction) -> tuple[int, str]:
    coordinate = seconds * frame_rate
    # targets the first frame boundary at or before a non-negative input
    # time. Applying that same toward-zero rule to both ends makes every
    # source duration an integer number of frames for.
    frame = coordinate.numerator // coordinate.denominator
    snapped = Fraction(frame, 1) / frame_rate
    direction = "exact" if snapped == seconds else ("up" if snapped > seconds else "down")
    return frame, direction


def read_concat_identity() -> tuple[str, str]:
    text = CONCAT_SPEC.read_text(encoding="utf-8")
    spec_id = re.search(r"^- `spec_id`: \*\*`([^`]+)`\*\*", text, re.MULTILINE)
    spec_version = re.search(r"^- `spec_version`: \*\*`([^`]+)`\*\*", text, re.MULTILINE)
    if spec_id is None or spec_version is None:
        raise ValueError(f"could not read spec_id/spec_version from {CONCAT_SPEC}")
    return spec_id.group(1), spec_version.group(1)


def trusted_segments(alignment: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = alignment.get("segments")
    if not isinstance(raw, list):
        raise ValueError("alignment.segments must be an array")
    selected: list[Mapping[str, Any]] = []
    for index, segment in enumerate(raw):
        if not isinstance(segment, Mapping):
            raise ValueError(f"alignment.segments[{index}] must be an object")
        trusted = segment.get("trusted")
        if trusted is None:
            confidence = segment.get("confidence")
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise ValueError(f"alignment.segments[{index}].confidence must be numeric when trusted is absent")
            keep = confidence >= 0.6
        elif isinstance(trusted, bool):
            keep = trusted
        else:
            raise ValueError(f"alignment.segments[{index}].trusted must be boolean")
        if keep:
            selected.append(segment)
    selected.sort(key=lambda item: decimal_fraction(item.get("edit_start_s"), "segment.edit_start_s"))
    if not selected:
        raise ValueError("alignment has no trusted segments")
    return selected


def build_bundle(alignment: Mapping[str, Any], source_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, str], dict[str, Any]]:
    source = alignment.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("alignment.source must be an object")
    duration = decimal_fraction(source.get("duration_s"), "alignment.source.duration_s")
    if duration <= 0:
        raise ValueError("alignment.source.duration_s must be positive")
    actual_sha256 = sha256_file(source_path)
    recorded_sha256 = source.get("sha256")
    if isinstance(recorded_sha256, str) and recorded_sha256 and recorded_sha256 != actual_sha256:
        raise ValueError("--source SHA-256 does not match alignment.source.sha256")
    frame_rate, frame_rate_origin = frame_rate_from_alignment(alignment)
    spec_id, spec_version = read_concat_identity()
    segments = trusted_segments(alignment)

    source_segments: list[dict[str, Any]] = []
    clips: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    trim_actions: list[dict[str, Any]] = []
    timeline_cursor = Fraction(0, 1)
    snap_report: list[dict[str, Any]] = []

    for number, segment in enumerate(segments, start=1):
        start_input = decimal_fraction(segment.get("src_start_s"), f"segment {number}.src_start_s")
        end_input = decimal_fraction(segment.get("src_end_s"), f"segment {number}.src_end_s")
        start_frame, start_direction = snap_frame_toward_zero(start_input, frame_rate)
        end_frame, end_direction = snap_frame_toward_zero(end_input, frame_rate)
        if end_frame <= start_frame:
            raise ValueError(f"segment {number} collapses after frame-grid snapping")
        start = Fraction(start_frame, 1) / frame_rate
        end = Fraction(end_frame, 1) / frame_rate
        if end > duration:
            raise ValueError(f"segment {number} ends after alignment.source.duration_s after snapping")
        source_range = canonical_range(start, end)
        timeline_end = timeline_cursor + (end - start)
        timeline_range = canonical_range(timeline_cursor, timeline_end)
        segment_id = f"seg-{number:03d}"
        evidence_id = f"ev-alignment-{number:03d}"
        rationale = (
            f"Audio-alignment interval {number}; source start snapped {start_direction} "
            f"to frame {start_frame}, source end snapped {end_direction} to frame {end_frame}; "
            f"confidence={segment.get('confidence')!r}."
        )
        evidence.append({
            "evidence_id": evidence_id,
            "kind": "signal",
            "source_id": SOURCE_ID,
            "range": source_range,
            "provenance": {"tool": "pair-align/plan_from_alignment.py", "version": "1", "rationale": rationale},
        })
        source_segments.append({"segment_id": segment_id, "source_id": SOURCE_ID, "source_range": source_range})
        clips.append({
            "clip_id": f"clip-{number:03d}", "source_segment_id": segment_id, "track_id": "v1",
            "rounding": "toward_zero", "source_range": source_range, "timeline_range": timeline_range,
        })
        trim_actions.append({
            "action_id": f"trim-{number:03d}", "action": "act_trim_segment",
            "v1_executable": "yes(effect-verified)", "verification_kind": "effect_measurement",
            "source_segment_id": segment_id, "evidence_ids": [evidence_id],
        })
        snap_report.append({
            "segment_id": segment_id, "start_frame": start_frame, "end_frame": end_frame,
            "start_direction": start_direction, "end_direction": end_direction,
        })
        timeline_cursor = timeline_end

    # An edit may place a later source moment earlier on the timeline (cold
    # open does). The contract checker permits that only when the plan declares it, and L2 accepts the
    # declaration only when it names the order that will actually render. Both are
    # satisfied by the clip order itself, so the declaration is derived, never guessed.
    ordered_segment_ids = [segment["segment_id"] for segment in source_segments]
    source_starts = [Fraction(clip["source_range"]["start"]["value"],
                              clip["source_range"]["start"]["timebase"]["den"]) for clip in clips]
    reordering_declared = source_starts != sorted(source_starts)
    reorder_actions: list[dict[str, Any]] = []
    if reordering_declared:
        reorder_actions.append({
            "action_id": "reorder-trusted-alignment", "action": "act_reorder_segments",
            "v1_executable": "yes(effect-verified)", "verification_kind": "effect_measurement",
            "source_segment_id": source_segments[0]["segment_id"],
            "ordered_segment_ids": ordered_segment_ids,
            "evidence_ids": [evidence[0]["evidence_id"]],
        })

    actions = trim_actions + reorder_actions + [{
        "action_id": "assemble-trusted-alignment", "action": "act_assemble_segment",
        "v1_executable": "yes(effect-verified)", "verification_kind": "effect_measurement",
        "source_segment_id": source_segments[0]["segment_id"],
        "ordered_segment_ids": ordered_segment_ids,
        "evidence_ids": [evidence[0]["evidence_id"]], "params": {"join_mode": "concat_copy"},
    }]
    index = {
        "schema_version": "1.0", "index_id": INDEX_ID,
        "sources": [{"source_id": SOURCE_ID, "sha256": actual_sha256, "duration": canonical_time(duration)}],
        "evidence": evidence, "markers": [], "transcript": [], "backend_selections": [],
    }
    plan = {
        "schema_version": "1.0", "plan_id": PLAN_ID, "index_id": INDEX_ID, "stage": "draft",
        "source_segments": source_segments, "timeline_clips": clips, "actions": actions,
        "constraints": {"preserve": [], "lock_orders": [], "releases": []}, "assets": [], "reviews": [],
        "tracks": [{"track_id": "v1", "kind": "video", "z_order": 0}], "captions": [],
        "output_policy": {
            "width": 1920, "height": 1080, "pixel_format": "yuv420p", "sar": {"num": 1, "den": 1},
            "frame_rate": {"num": frame_rate.numerator, "den": frame_rate.denominator}, "frame_rate_mode": "cfr",
            "encoder_timebase": {"num": 1, "den": frame_rate.numerator},
            "color_primaries": "bt709", "color_transfer": "bt709", "color_matrix": "bt709", "color_range": "tv",
            "audio_policy": "present", "time_axis": "final_timeline",
        },
        "caption_styles": [],
        "concat_compatibility": {"verdict": "unverified", "spec_id": spec_id, "spec_version": spec_version,
                                 "evidence_ids": [], "review_id": None},
        "backend_selections": [], "cost_summary": {"estimated_total": {"amount": 0, "currency": "USD", "basis": "no external backend calls"}},
        "cost_calls": [],
        "reproducibility": {"ffmpeg_version": "not-probed", "filter_arguments": ["trim", "concat"],
                              "loudnorm_two_pass": {"status": "not_applicable"}, "render_log_id": "not-rendered", "output_sha256": None},
        "qc": {"pre_render": {"severity": "info", "state": "pending", "auto_retry": False},
               "post_render": {"severity": "info", "state": "pending", "auto_retry": False}},
    }
    report = {"piece_count": len(clips), "duration_seconds": float(timeline_cursor), "frame_rate_origin": frame_rate_origin,
              "reordering_declared": reordering_declared,
              "snapping": snap_report, "concat_spec": {"spec_id": spec_id, "spec_version": spec_version}}
    return plan, index, {SOURCE_ID: str(source_path)}, report


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alignment", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    if not args.source.is_file():
        parser.error(f"--source is not a file: {args.source}")
    try:
        alignment = json.loads(args.alignment.read_text(encoding="utf-8"))
        if not isinstance(alignment, Mapping):
            raise ValueError("alignment must be a JSON object")
        plan, index, source_map, report = build_bundle(alignment, args.source)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        write_json(args.out_dir / "edit-plan.json", plan)
        write_json(args.out_dir / "index.json", index)
        write_json(args.out_dir / "source-map.json", source_map)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps({"status": "generated", **report, "out_dir": str(args.out_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
