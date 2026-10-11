#!/usr/bin/env python3
"""Classify detected sensitive-object motion as maskable or cut-only.

This tool reads the pure-data JSON emitted by ``locate_sensitive.py``. It
does not decode video and it does not invoke Vision. Consecutive observations
of the same matched string are paired one-to-one by nearest box centre, never
by their position in a frame's ``hits`` array.

The thresholds are deliberately CLI parameters, not universal constants.
The default hold is 60 source ticks. Convert that value using your source
timebase and adjust it for the sampling interval. Drift and margin values
also require review for your footage.

    python3 skills/mask-plan/motion_triage.py \
      --input scratch/run/sensitive.json \
      --output scratch/run/motion-triage.json \
      --max-drift 45 --margin 8 --boundary-hold 60
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


FORMAT = "draftcut.mask-plan.motion-triage.v1"
DEFAULT_MAX_DRIFT = 45.0
DEFAULT_MARGIN = 8
DEFAULT_BOUNDARY_HOLD = 60


@dataclass(frozen=True)
class Observation:
    """One sensitive match at one source PTS."""

    pts: int
    frame_index: int
    identity: str
    box: tuple[int, int, int, int]

    @property
    def centre(self) -> tuple[float, float]:
        x0, x1, y0, y1 = self.box
        return ((x0 + x1) / 2, (y0 + y1) / 2)


@dataclass
class Track:
    """A nearest-centre sequence of observations for one matched string."""

    track_id: str
    identity: str
    observations: list[Observation] = field(default_factory=list)

    @property
    def last(self) -> Observation:
        return self.observations[-1]


def non_negative_float(value: str) -> float:
    """Argparse validator for a finite non-negative pixel value."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite non-negative number") from exc
    if parsed < 0 or not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be a finite non-negative number")
    return parsed


def non_negative_int(value: str) -> int:
    """Argparse validator for a finite non-negative integer tick count."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc
    if parsed < 0 or str(parsed) != value:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def parse_frames(payload: Any) -> list[tuple[int, list[tuple[str, tuple[int, int, int, int]]]]]:
    """Validate and sort locate_sensitive-style observations by integer PTS."""
    if not isinstance(payload, list):
        raise ValueError("input must be a JSON array of sampled frames")
    frames = []
    for frame_number, frame in enumerate(payload):
        if not isinstance(frame, dict) or type(frame.get("pts")) is not int:
            raise ValueError(f"frame {frame_number} has no integer pts")
        hits = frame.get("hits")
        if not isinstance(hits, list):
            raise ValueError(f"frame at pts {frame['pts']} has no hits array")
        parsed_hits = []
        for hit_number, hit in enumerate(hits):
            if not isinstance(hit, dict):
                raise ValueError(f"hit {hit_number} at pts {frame['pts']} is not an object")
            identity = hit.get("match", hit.get("text"))
            if not isinstance(identity, str) or not identity:
                raise ValueError(f"hit {hit_number} at pts {frame['pts']} has no match or text")
            box = hit.get("box")
            if (not isinstance(box, list) or len(box) != 4 or
                    any(type(value) is not int for value in box)):
                raise ValueError(f"hit {hit_number} at pts {frame['pts']} has no integer [x0,x1,y0,y1] box")
            x0, x1, y0, y1 = box
            if x0 >= x1 or y0 >= y1:
                raise ValueError(f"hit {hit_number} at pts {frame['pts']} has an empty or inverted box")
            parsed_hits.append((identity, (x0, x1, y0, y1)))
        frames.append((frame["pts"], parsed_hits))
    frames.sort(key=lambda item: item[0])
    if any(left[0] == right[0] for left, right in zip(frames, frames[1:])):
        raise ValueError("input has duplicate source PTS samples")
    return frames


def centre_distance(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> float:
    """Euclidean distance between two source-coordinate box centres."""
    left_x = (left[0] + left[1]) / 2
    left_y = (left[2] + left[3]) / 2
    right_x = (right[0] + right[1]) / 2
    right_y = (right[2] + right[3]) / 2
    return math.hypot(right_x - left_x, right_y - left_y)


def padded_union(left: tuple[int, int, int, int], right: tuple[int, int, int, int], margin: int) -> list[int]:
    """Source-coordinate union of two boxes, expanded without unknown-frame clamping."""
    return [min(left[0], right[0]) - margin, max(left[1], right[1]) + margin,
            min(left[2], right[2]) - margin, max(left[3], right[3]) + margin]


def padded_box(box: tuple[int, int, int, int], margin: int) -> list[int]:
    """One source-coordinate box expanded without unknown-frame clamping."""
    return [box[0] - margin, box[1] + margin, box[2] - margin, box[3] + margin]


def stable_hit_key(hit: tuple[str, tuple[int, int, int, int]]) -> tuple[Any, ...]:
    """A source-array-order-independent ordering for newly created tracks."""
    identity, box = hit
    return (identity, (box[0] + box[1]) / 2, (box[2] + box[3]) / 2, box)


def build_tracks(frames: list[tuple[int, list[tuple[str, tuple[int, int, int, int]]]]],
                 boundary_hold: int) -> list[Track]:
    """Pair consecutive observations by one-to-one nearest centre distance."""
    tracks: list[Track] = []
    next_track = 1

    for frame_index, (pts, raw_hits) in enumerate(frames):
        hits = sorted(raw_hits, key=stable_hit_key)
        candidates: list[tuple[float, int, int]] = []
        for hit_index, (identity, box) in enumerate(hits):
            for track_index, track in enumerate(tracks):
                previous = track.last
                if previous.identity != identity:
                    continue
                direct_neighbour = previous.frame_index == frame_index - 1
                bridged_gap = (previous.frame_index < frame_index - 1 and
                               pts - previous.pts <= boundary_hold * 2)
                if direct_neighbour or bridged_gap:
                    candidates.append((centre_distance(previous.box, box), track_index, hit_index))

        used_tracks: set[int] = set()
        used_hits: set[int] = set()
        for _, track_index, hit_index in sorted(candidates):
            if track_index in used_tracks or hit_index in used_hits:
                continue
            identity, box = hits[hit_index]
            tracks[track_index].observations.append(Observation(pts, frame_index, identity, box))
            used_tracks.add(track_index)
            used_hits.add(hit_index)

        for hit_index, (identity, box) in enumerate(hits):
            if hit_index in used_hits:
                continue
            tracks.append(Track(f"track-{next_track:03d}", identity,
                                [Observation(pts, frame_index, identity, box)]))
            next_track += 1
    return tracks


def merge_intervals(intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge time-adjacent pieces of a track that cover the identical rectangle.

    ⛔ Rectangles are never unioned across a merge. Doing so turns a target that
    drifts slowly across the frame into one rectangle spanning its whole journey,
    which hides content the delivery is supposed to show. Two pieces collapse
    only when they already cover the same area.
    """
    merged: list[dict[str, Any]] = []
    for interval in intervals:
        if (merged and merged[-1]["kind"] == interval["kind"] and
                merged[-1]["track_id"] == interval["track_id"] and
                merged[-1]["end_pts"] == interval["start_pts"] and
                merged[-1]["rect"] == interval["rect"]):
            prior = merged[-1]
            prior["end_pts"] = interval["end_pts"]
            prior["displacement_px"] = max(prior["displacement_px"], interval["displacement_px"])
            prior["reason"] = "merged adjacent " + prior["kind"] + " coverage"
        else:
            merged.append(interval)
    return merged


def intervals_for_track(track: Track, max_drift: float, margin: int, boundary_hold: int,
                        source_end_pts: int | None = None,
                        ambiguous: frozenset[tuple[str, int]] = frozenset()) -> list[dict[str, Any]]:
    """Produce half-open mask/cut intervals for a tracked sensitive object.

    The boundary hold is clamped to the source: a negative PTS is not a time in
    any timebase, and a hold past ``--source-end-pts`` would name a frame the
    source does not have. Both would be handed straight to a plan.

    ⛔ A mask rectangle asserts only what the two sampled boxes bound. It is
    sound for motion that stays inside that bound between the samples; it cannot
    see an excursion, a rotation or a growth that happens between them. That is
    a property of sampling, not of this code, and the answer to it is sampling
    density -- never a wider rectangle chosen by feel.
    """
    observations = track.observations
    first = observations[0]
    pieces: list[dict[str, Any]] = [{
        "kind": "mask", "start_pts": max(0, first.pts - boundary_hold), "end_pts": first.pts,
        "rect": padded_box(first.box, margin), "displacement_px": 0.0,
        "reason": "boundary hold before first observation", "track_id": track.track_id,
    }]
    for left, right in zip(observations, observations[1:]):
        displacement = round(centre_distance(left.box, right.box), 6)
        bridged = right.frame_index > left.frame_index + 1
        if (track.identity, left.frame_index) in ambiguous:
            pieces.append({
                "kind": "cut", "start_pts": left.pts, "end_pts": right.pts,
                "rect": None, "displacement_px": displacement,
                "reason": "two or more equal strings were on screen and their count changed; "
                          "nearest-centre pairing cannot be trusted across this interval",
                "track_id": track.track_id,
            })
            continue
        if displacement <= max_drift:
            reason = "short detection gap; paired-box union" if bridged else "paired-box drift within max-drift"
            pieces.append({
                "kind": "mask", "start_pts": left.pts, "end_pts": right.pts,
                "rect": padded_union(left.box, right.box, margin),
                "displacement_px": displacement, "reason": reason, "track_id": track.track_id,
            })
        else:
            pieces.append({
                "kind": "cut", "start_pts": left.pts, "end_pts": right.pts,
                "rect": None, "displacement_px": displacement,
                "reason": "paired-box drift exceeds max-drift; path between samples is unknown",
                "track_id": track.track_id,
            })
    last = observations[-1]
    tail_end = last.pts + boundary_hold
    if source_end_pts is not None:
        tail_end = min(tail_end, source_end_pts)
    pieces.append({
        "kind": "mask", "start_pts": last.pts, "end_pts": tail_end,
        "rect": padded_box(last.box, margin), "displacement_px": 0.0,
        "reason": "boundary hold after last observation", "track_id": track.track_id,
    })
    return merge_intervals(pieces)


def ambiguous_transitions(frames: list[tuple[int, list[tuple[str, tuple[int, int, int, int]]]]]
                          ) -> frozenset[tuple[str, int]]:
    """Frames after which nearest-centre pairing of one string cannot be trusted.

    When two or more equal strings are on screen and their number changes in the
    next sample, the surviving hit can be nearer to the wrong track than to its
    own -- a moving target is then absorbed by a static decoy and its journey
    silently loses its mask. The tool does not guess which one moved: it says
    the interval must be cut, exactly as it does for motion it cannot bound.
    """
    marked: set[tuple[str, int]] = set()
    counts = []
    for _, hits in frames:
        per_identity: dict[str, int] = {}
        for identity, _box in hits:
            per_identity[identity] = per_identity.get(identity, 0) + 1
        counts.append(per_identity)
    for index, current in enumerate(counts[:-1]):
        following = counts[index + 1]
        for identity, count in current.items():
            if count >= 2 and following.get(identity, 0) != count:
                marked.add((identity, index))
    return frozenset(marked)


def triage(payload: Any, max_drift: float, margin: int, boundary_hold: int,
           source_end_pts: int | None = None) -> dict[str, Any]:
    """Return a serialisable motion-triage result from locate_sensitive JSON."""
    frames = parse_frames(payload)
    tracks = build_tracks(frames, boundary_hold)
    ambiguous = ambiguous_transitions(frames)
    intervals = []
    for track in tracks:
        intervals.extend(intervals_for_track(track, max_drift, margin, boundary_hold,
                                             source_end_pts, ambiguous))
    kept = [item for item in intervals if item["end_pts"] > item["start_pts"]]
    dropped = [{"track_id": item["track_id"], "pts": item["start_pts"], "reason": item["reason"]}
               for item in intervals if item["end_pts"] <= item["start_pts"]]
    kept.sort(key=lambda item: (item["start_pts"], item["end_pts"], item["track_id"], item["kind"]))
    gaps = [right[0] - left[0] for left, right in zip(frames, frames[1:])]
    return {
        "format": FORMAT,
        "parameters": {
            "max_drift": max_drift,
            "margin": margin,
            "boundary_hold": boundary_hold,
            "source_end_pts": source_end_pts,
        },
        # The linear-motion assumption is only as strong as the sampling; the
        # consumer is told how far apart the samples actually were.
        "sampling": {"frames": len(frames), "max_sample_gap_pts": max(gaps) if gaps else 0},
        # ⛔ A zero-length interval covers nothing, so it is not emitted -- but it
        # is never dropped silently: a single observation with --boundary-hold 0
        # is a detection this tool cannot mask, and the caller must see that.
        "dropped_zero_length": dropped,
        "intervals": kept,
    }


def parse_args() -> argparse.Namespace:
    """Parse the pure-data input and material-dependent triage parameters."""
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path,
                        help="locate_sensitive.py JSON; source PTS and source-coordinate boxes")
    parser.add_argument("--output", required=True, type=Path,
                        help="motion-triage JSON")
    parser.add_argument("--max-drift", type=non_negative_float, default=DEFAULT_MAX_DRIFT,
                        help="maximum paired centre movement in source pixels for a mask (default: 45; material-dependent)")
    parser.add_argument("--margin", type=non_negative_int, default=DEFAULT_MARGIN,
                        help="source pixels added to each mask-union edge (default: 8; material-dependent)")
    parser.add_argument("--boundary-hold", type=non_negative_int, default=DEFAULT_BOUNDARY_HOLD,
                        help="source ticks to extend first/last mask; also bridges gaps up to twice this value (default: 60; timebase-dependent)")
    parser.add_argument("--source-end-pts", type=non_negative_int, default=None,
                        help="last source tick; clamps the trailing boundary hold so it cannot "
                             "name a frame the source does not have (the leading hold is always "
                             "clamped at 0)")
    return parser.parse_args()


def main() -> int:
    """Run the pure-data classifier and write its JSON result."""
    args = parse_args()
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        result = triage(payload, args.max_drift, args.margin, args.boundary_hold,
                        args.source_end_pts)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"motion triage: {exc}") from exc
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"MOTION TRIAGE: tracks={len({item['track_id'] for item in result['intervals']})} "
          f"intervals={len(result['intervals'])} "
          f"cuts={sum(item['kind'] == 'cut' for item in result['intervals'])} "
          f"max_sample_gap_pts={result['sampling']['max_sample_gap_pts']}")
    if result["dropped_zero_length"]:
        print(f"⛔ {len(result['dropped_zero_length'])} detection(s) produced zero-length mask intervals. "
              f"Review the sampling interval and --boundary-hold; sample more frequently "
              f"or adjust the hold as appropriate.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
