#!/usr/bin/env python3
"""Assemble one Draftcut Observation Index from OCR and scene intermediate JSON.

This program never imports or calls Vision.  Its only OCR input is the JSON
created by ``ocr_vision.py`` (or a structurally equivalent recorded result).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any


OCR_FORMAT = "draftcut.media-index.ocr-observations.v1"
SCENE_FORMAT = "draftcut.media-index.scene-boundaries.v1"
OCR_BACKEND = {
    "purpose": "ocr",
    "backend_id": "apple-vision-vnrecognizetextrequest",
    "backend_version": "revision-3/build-26A5416b",
    "cost_tier": "무료-로컬",
}
SCENE_BACKEND = {
    "purpose": "shot_scene_boundary",
    "backend_id": "ffmpeg-select-scene",
    "backend_version": "8.1.2",
    "cost_tier": "무료-로컬",
}
FREEZE_FORMAT = "draftcut.media-index.freeze-runs.v1"
FREEZE_BACKEND = {
    "purpose": "shot_scene_boundary",
    "backend_id": "ffmpeg-freezedetect",
    "backend_version": "8.1.2",
    "cost_tier": "무료-로컬",
}
ACTIVITY_FORMAT = "draftcut.media-index.activity-samples.v1"
ACTIVITY_BACKEND_ID = "ffmpeg-signalstats-mad"


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: JSON root must be an object")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_timebase(value: Any, label: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"num", "den"}:
        raise ValueError(f"{label}: timebase must have exactly num and den")
    if type(value["num"]) is not int or type(value["den"]) is not int or value["num"] <= 0 or value["den"] <= 0:
        raise ValueError(f"{label}: timebase num/den must be positive integers")
    return {"num": value["num"], "den": value["den"]}


def probe_source(input_path: Path) -> dict[str, Any]:
    completed = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=time_base,duration_ts:frame=pts",
        "-show_frames", "-of", "json", str(input_path),
    ], check=True, capture_output=True, text=True)
    payload = json.loads(completed.stdout)
    streams = payload.get("streams", [])
    if len(streams) != 1:
        raise ValueError("ffprobe did not return exactly one v:0 stream")
    stream = streams[0]
    raw_timebase = stream.get("time_base")
    if not isinstance(raw_timebase, str) or "/" not in raw_timebase:
        raise ValueError("ffprobe did not return stream time_base")
    numerator, denominator = raw_timebase.split("/", 1)
    duration_ts = stream.get("duration_ts")
    try:
        duration = int(duration_ts)
    except (TypeError, ValueError) as exc:
        raise ValueError("ffprobe did not return integer duration_ts") from exc
    if duration <= 0:
        raise ValueError("duration_ts must be positive")
    frame_pts: list[int] = []
    for frame_index, frame in enumerate(payload.get("frames", [])):
        raw_pts = frame.get("pts") if isinstance(frame, dict) else None
        if isinstance(raw_pts, str) and raw_pts.lstrip("-").isdigit():
            pts = int(raw_pts)
        elif type(raw_pts) is int:
            pts = raw_pts
        else:
            raise ValueError(f"ffprobe frame {frame_index} did not return integer pts")
        frame_pts.append(pts)
    if not frame_pts:
        raise ValueError("ffprobe did not return v:0 frame PTS values")
    return {
        "timebase": canonical_timebase({"num": int(numerator), "den": int(denominator)}, "ffprobe"),
        "duration_ts": duration,
        "frame_pts": frame_pts,
    }


def read_probe_fixture(path: Path) -> dict[str, Any]:
    """A test seam for synthetic assembly checks; normal use calls ffprobe."""
    payload = read_json(path)
    if set(payload) != {"timebase", "duration_ts", "frame_pts"}:
        raise ValueError("probe fixture must contain exactly timebase, duration_ts, and frame_pts")
    if type(payload["duration_ts"]) is not int or payload["duration_ts"] <= 0:
        raise ValueError("probe fixture duration_ts must be a positive integer")
    frame_pts = payload["frame_pts"]
    if not isinstance(frame_pts, list) or not frame_pts:
        raise ValueError("probe fixture frame_pts must be a non-empty array")
    for frame_index, pts in enumerate(frame_pts):
        if type(pts) is not int:
            raise ValueError(f"probe fixture frame_pts[{frame_index}] must be an integer")
    return {
        "timebase": canonical_timebase(payload["timebase"], "probe fixture"),
        "duration_ts": payload["duration_ts"],
        "frame_pts": frame_pts,
    }


def require_intermediate_header(payload: dict[str, Any], label: str, expected_format: str, source_sha256: str, timebase: dict[str, int]) -> None:
    if payload.get("format") != expected_format:
        raise ValueError(f"{label}: unexpected format")
    if payload.get("source_sha256") != source_sha256:
        raise ValueError(f"{label}: source_sha256 does not match input")
    if canonical_timebase(payload.get("timebase"), f"{label}.timebase") != timebase:
        raise ValueError(f"{label}: timebase does not match source")


def require_pts(value: Any, label: str, duration: int) -> int:
    if type(value) is not int:
        raise ValueError(f"{label}: pts must be an integer")
    if not 0 <= value < duration:
        raise ValueError(f"{label}: pts must satisfy 0 <= pts < duration_ts")
    return value


def time_at(pts: int, timebase: dict[str, int]) -> dict[str, Any]:
    return {"value": pts, "timebase": dict(timebase)}


def source_range(start: int, end: int, timebase: dict[str, int]) -> dict[str, Any]:
    if end <= start:
        raise ValueError("cannot emit an empty evidence/marker range")
    return {"start": time_at(start, timebase), "end": time_at(end, timebase)}


def parse_nonnegative_fraction(value: str, label: str) -> Fraction:
    try:
        parsed = Fraction(value)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"{label} must be a non-negative rational") from exc
    if parsed < 0:
        raise ValueError(f"{label} must be a non-negative rational")
    return parsed


def parse_cell_share(value: str) -> Fraction:
    parsed = parse_nonnegative_fraction(value, "activity cell share")
    if not 0 < parsed <= 1:
        raise ValueError("activity cell share must satisfy 0 < share <= 1")
    return parsed


def ffmpeg_version() -> str:
    completed = subprocess.run(["ffmpeg", "-version"], check=True, capture_output=True, text=True)
    match = re.search(r"^ffmpeg version\s+(\S+)", completed.stdout, re.M)
    if match is None:
        raise RuntimeError("ffmpeg -version did not report a version")
    return match.group(1)


def freeze_records(
    freeze: dict[str, Any], source_id: str, timebase: dict[str, int], duration: int, frame_pts: set[int]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Emit one boundary marker per freeze-run edge.

    A static run is bounded by the two moments where the picture changed, and
    those moments are shot boundaries already covered by ``shot_scene_boundary``.
    No new ``marker_type`` is needed and none is invented.
    """
    runs = freeze.get("runs")
    if not isinstance(runs, list):
        raise ValueError("freeze JSON: runs must be an array")
    noise = freeze.get("noise")
    minimum = freeze.get("duration")
    if not isinstance(noise, str) or not isinstance(minimum, str):
        raise ValueError("freeze JSON: noise and duration must be strings")
    edges: list[int] = []
    for position, run in enumerate(runs):
        if not isinstance(run, dict):
            raise ValueError(f"freeze JSON: run {position} must be an object")
        for key in ("start_pts", "end_pts"):
            raw = run.get(key)
            # A run still open at end of stream ends on duration_ts, which is the
            # stream end rather than a frame; it opens no new boundary.
            if type(raw) is int and raw == duration:
                continue
            pts = require_pts(raw, f"freeze JSON run {position} {key}", duration)
            if pts not in frame_pts:
                raise ValueError(f"freeze JSON run {position} {key}: pts is not an actual source frame PTS")
            edges.append(pts)
    edges = sorted(set(edges))
    evidence: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []
    for offset, start in enumerate(edges, start=1):
        end = edges[offset] if offset < len(edges) else duration
        if end <= start:
            continue
        evidence_id = f"ev-freeze-{offset:04d}"
        evidence_range = source_range(start, end, timebase)
        evidence.append({
            "evidence_id": evidence_id,
            "kind": "signal",
            "source_id": source_id,
            "range": evidence_range,
            "provenance": {
                "tool": FREEZE_BACKEND["backend_id"],
                "version": FREEZE_BACKEND["backend_version"],
                "rationale": f"ffmpeg freezedetect run edge; noise={noise}; duration={minimum}",
            },
        })
        markers.append({
            "marker_id": f"m-freeze-{offset:04d}",
            "marker_type": "shot_scene_boundary",
            "source_id": source_id,
            "range": evidence_range,
            "evidence_ids": [evidence_id],
        })
    return evidence, markers


def activity_records(
    activity: dict[str, Any], source_id: str, timebase: dict[str, int], duration: int, frame_pts: set[int],
    threshold: Fraction, cell_share: Fraction, threshold_text: str, cell_share_text: str, backend_version: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Emit one observed pixel-activity marker for each consecutive active run."""
    samples = activity.get("samples")
    if not isinstance(samples, list):
        raise ValueError("activity JSON: samples must be an array")
    sampling = activity.get("sampling")
    if not isinstance(sampling, dict) or set(sampling) != {"interval"}:
        raise ValueError("activity JSON: sampling must contain only interval")
    interval = canonical_timebase(sampling.get("interval"), "activity JSON sampling.interval")
    analysis = activity.get("analysis")
    if not isinstance(analysis, dict) or analysis != {
        "scale": "320x180", "pixel_format": "gray", "cells": {"cols": 16, "rows": 9},
    }:
        raise ValueError("activity JSON: analysis must be 320x180 gray with 16x9 cells")

    normalized: list[dict[str, Any]] = []
    previous_pts: int | None = None
    for position, sample in enumerate(samples):
        if not isinstance(sample, dict) or set(sample) != {"pts", "prev_pts", "whole", "cells"}:
            raise ValueError(f"activity JSON sample {position} must contain only pts, prev_pts, whole, and cells")
        pts = require_pts(sample["pts"], f"activity JSON sample {position} pts", duration)
        prev_pts = require_pts(sample["prev_pts"], f"activity JSON sample {position} prev_pts", duration)
        if pts not in frame_pts:
            raise ValueError(f"activity JSON sample {position} pts: pts is not an actual source frame PTS")
        if prev_pts not in frame_pts:
            raise ValueError(f"activity JSON sample {position} prev_pts: pts is not an actual source frame PTS")
        if prev_pts >= pts:
            raise ValueError(f"activity JSON sample {position}: prev_pts must be before pts")
        if previous_pts is not None and prev_pts != previous_pts:
            raise ValueError(f"activity JSON sample {position}: prev_pts must equal the previous sample pts")
        whole = sample["whole"]
        if type(whole) not in {int, float}:
            raise ValueError(f"activity JSON sample {position}: whole must be numeric")
        try:
            whole_value = Fraction(str(whole))
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError(f"activity JSON sample {position}: whole must be finite") from exc
        if whole_value < 0 or whole_value > 255:
            raise ValueError(f"activity JSON sample {position}: whole must be 0..255")
        cells = sample["cells"]
        if not isinstance(cells, list) or len(cells) != 144:
            raise ValueError(f"activity JSON sample {position}: cells must contain 144 values")
        if any(type(cell) is not int or not 0 <= cell <= 255 for cell in cells):
            raise ValueError(f"activity JSON sample {position}: cells must be integer 0..255 values")
        normalized.append({"pts": pts, "prev_pts": prev_pts, "cells": cells})
        previous_pts = pts

    totals = [0] * 144
    for sample in normalized:
        for cell, value in enumerate(sample["cells"]):
            totals[cell] += value
    total = sum(totals)
    selected: list[int] = []
    if total:
        running = 0
        for cell in sorted(range(144), key=lambda cell: (-totals[cell], cell)):
            selected.append(cell)
            running += totals[cell]
            if Fraction(running, total) >= cell_share:
                break
    values = [
        Fraction(sum(sample["cells"][cell] for cell in selected), len(selected)) if selected else Fraction(0)
        for sample in normalized
    ]

    evidence: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []
    run_start: int | None = None
    for position, value in enumerate(values + [Fraction(-1)]):
        active = position < len(values) and value >= threshold
        if active and run_start is None:
            run_start = position
        if not active and run_start is not None:
            run_end = position - 1
            start = normalized[run_start]["prev_pts"]
            end = normalized[run_end]["pts"]
            evidence_id = f"ev-activity-{len(evidence) + 1:04d}"
            marker_id = f"m-activity-{len(markers) + 1:04d}"
            activity_range = source_range(start, end, timebase)
            rationale = (
                "screen activity run; "
                f"threshold={threshold_text}; cell_share={cell_share_text}; cells={len(selected)}/144; "
                f"interval={interval['num']}/{interval['den']}; scale=320x180 gray"
            )
            evidence.append({
                "evidence_id": evidence_id,
                "kind": "signal",
                "source_id": source_id,
                "range": activity_range,
                "provenance": {
                    "tool": ACTIVITY_BACKEND_ID,
                    "version": backend_version,
                    "rationale": rationale,
                },
            })
            markers.append({
                "marker_id": marker_id,
                "marker_type": "screen_activity",
                "source_id": source_id,
                "range": activity_range,
                "evidence_ids": [evidence_id],
            })
            run_start = None
    return evidence, markers


def assemble(
    *, source_id: str, index_id: str, digest: str, probe: dict[str, Any], ocr: dict[str, Any], scene: dict[str, Any],
    freeze: dict[str, Any] | None = None, activity: dict[str, Any] | None = None,
    activity_threshold: Fraction = Fraction(1, 4), activity_cell_share: Fraction = Fraction(4, 5),
    activity_threshold_text: str = "0.25", activity_cell_share_text: str = "0.8",
) -> dict[str, Any]:
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("source_id must be a non-empty string")
    if not isinstance(index_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]*", index_id) is None:
        raise ValueError("index_id must match ^[a-z0-9][a-z0-9_-]*$")
    timebase = probe["timebase"]
    duration = probe["duration_ts"]
    frame_pts = set(probe["frame_pts"])
    require_intermediate_header(ocr, "ocr JSON", OCR_FORMAT, digest, timebase)
    require_intermediate_header(scene, "scene JSON", SCENE_FORMAT, digest, timebase)

    frames = ocr.get("frames")
    if not isinstance(frames, list):
        raise ValueError("ocr JSON: frames must be an array")
    if not frames:
        raise ValueError("ocr JSON: frames must not be empty")
    onscreen_text: list[dict[str, Any]] = []
    for frame_index, frame in enumerate(frames):
        if not isinstance(frame, dict) or set(frame) != {"pts", "observations"} or not isinstance(frame["observations"], list):
            raise ValueError(f"ocr JSON: frame {frame_index} must contain only pts and observations")
        pts = require_pts(frame["pts"], f"ocr JSON frame {frame_index}", duration)
        if pts not in frame_pts:
            raise ValueError(f"ocr JSON frame {frame_index}: pts is not an actual source frame PTS")
        for observation_index, observation in enumerate(frame["observations"]):
            if not isinstance(observation, dict) or set(observation) != {"text", "confidence"}:
                raise ValueError(f"ocr JSON: observation {frame_index}/{observation_index} must contain only text and confidence")
            text = observation["text"]
            confidence = observation["confidence"]
            if not isinstance(text, str) or not text:
                raise ValueError(f"ocr JSON: observation {frame_index}/{observation_index} text must be non-empty")
            if type(confidence) not in {int, float} or not 0 <= confidence <= 1:
                raise ValueError(f"ocr JSON: observation {frame_index}/{observation_index} confidence must be 0..1")
            onscreen_text.append({"source_id": source_id, "time": time_at(pts, timebase), "text": text, "confidence": confidence})

    raw_boundaries = scene.get("boundaries")
    if not isinstance(raw_boundaries, list):
        raise ValueError("scene JSON: boundaries must be an array")
    boundaries: list[dict[str, Any]] = []
    for position, boundary in enumerate(raw_boundaries):
        if not isinstance(boundary, dict) or set(boundary) != {"pts", "scene_score"}:
            raise ValueError(f"scene JSON: boundary {position} must contain only pts and scene_score")
        pts = require_pts(boundary["pts"], f"scene JSON boundary {position}", duration)
        score = boundary["scene_score"]
        if type(score) not in {int, float}:
            raise ValueError(f"scene JSON: boundary {position} scene_score must be numeric")
        boundaries.append({"pts": pts, "scene_score": score})
    boundaries.sort(key=lambda item: item["pts"])
    if len({item["pts"] for item in boundaries}) != len(boundaries):
        raise ValueError("scene JSON: boundary PTS values must be unique")

    evidence: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []
    threshold = scene.get("threshold")
    if not isinstance(threshold, str) or not threshold:
        raise ValueError("scene JSON: threshold must be a non-empty string")
    for offset, boundary in enumerate(boundaries, start=1):
        start = boundary["pts"]
        end = boundaries[offset]["pts"] if offset < len(boundaries) else duration
        evidence_id = f"ev-scene-{offset:04d}"
        marker_id = f"m-scene-{offset:04d}"
        evidence_range = source_range(start, end, timebase)
        evidence.append({
            "evidence_id": evidence_id,
            "kind": "signal",
            "source_id": source_id,
            "range": evidence_range,
            "provenance": {
                "tool": SCENE_BACKEND["backend_id"],
                "version": SCENE_BACKEND["backend_version"],
                "rationale": f"ffmpeg scene boundary; threshold={threshold}; scene_score={boundary['scene_score']}",
            },
        })
        markers.append({
            "marker_id": marker_id,
            "marker_type": "shot_scene_boundary",
            "source_id": source_id,
            "range": evidence_range,
            "evidence_ids": [evidence_id],
        })

    if freeze is not None:
        require_intermediate_header(freeze, "freeze JSON", FREEZE_FORMAT, digest, timebase)
        freeze_evidence, freeze_markers = freeze_records(freeze, source_id, timebase, duration, frame_pts)
        evidence.extend(freeze_evidence)
        markers.extend(freeze_markers)

    backends = [dict(OCR_BACKEND), dict(SCENE_BACKEND)]
    if activity is not None:
        require_intermediate_header(activity, "activity JSON", ACTIVITY_FORMAT, digest, timebase)
        version = ffmpeg_version()
        activity_evidence, activity_markers = activity_records(
            activity, source_id, timebase, duration, frame_pts,
            activity_threshold, activity_cell_share, activity_threshold_text, activity_cell_share_text, version,
        )
        evidence.extend(activity_evidence)
        markers.extend(activity_markers)
        backends.append({
            "purpose": "screen_activity",
            "backend_id": ACTIVITY_BACKEND_ID,
            "backend_version": version,
            "cost_tier": "무료-로컬",
        })

    return {
        "schema_version": "1.0",
        "index_id": index_id,
        "sources": [{"source_id": source_id, "sha256": digest, "duration": time_at(duration, timebase)}],
        "evidence": evidence,
        "markers": markers,
        "transcript": [],
        "onscreen_text": onscreen_text,
        "backend_selections": backends,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="read-only source video")
    parser.add_argument("--ocr-json", required=True, type=Path, help="output from ocr_vision.py")
    parser.add_argument("--scene-json", required=True, type=Path, help="output from scene_detect.py")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--index-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--freeze-json", type=Path, help="optional output from freeze_detect.py")
    parser.add_argument("--activity-json", type=Path, help="optional output from activity_mad.py")
    parser.add_argument("--activity-threshold", default="0.25", help="Minimum mean absolute pixel difference (MAD) for an active interval, as a non-negative rational (default: 0.25).")
    parser.add_argument("--activity-cell-share", default="0.8", help="Select cells accounting for this fraction of total observed activity; a value in (0, 1] (default: 0.8).")
    parser.add_argument("--probe-json", type=Path, help="Use saved probe data for synthetic tests instead of running ffprobe; omit this option for normal media.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input.is_file():
        raise FileNotFoundError(args.input)
    if args.probe_json is None and shutil.which("ffprobe") is None:
        raise RuntimeError("ffprobe is required unless --probe-json is supplied for a synthetic test")
    digest = sha256_file(args.input)
    probe = read_probe_fixture(args.probe_json) if args.probe_json else probe_source(args.input)
    activity_threshold = parse_nonnegative_fraction(args.activity_threshold, "activity threshold")
    activity_cell_share = parse_cell_share(args.activity_cell_share)
    index = assemble(
        source_id=args.source_id,
        index_id=args.index_id,
        digest=digest,
        probe=probe,
        ocr=read_json(args.ocr_json),
        scene=read_json(args.scene_json),
        freeze=read_json(args.freeze_json) if args.freeze_json else None,
        activity=read_json(args.activity_json) if args.activity_json else None,
        activity_threshold=activity_threshold,
        activity_cell_share=activity_cell_share,
        activity_threshold_text=args.activity_threshold,
        activity_cell_share_text=args.activity_cell_share,
    )
    write_json(args.output, index)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"build_index.py: {exc}", file=sys.stderr)
        raise SystemExit(1)
