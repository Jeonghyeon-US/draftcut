#!/usr/bin/env python3
"""Detect ffmpeg freezedetect static runs and preserve their integer PTS ticks."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any


FORMAT = "draftcut.media-index.freeze-runs.v1"


def run_json(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def video_timebase(input_path: Path) -> dict[str, int]:
    payload = run_json([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=time_base", "-of", "json", str(input_path),
    ])
    streams = payload.get("streams", [])
    if len(streams) != 1 or not isinstance(streams[0].get("time_base"), str):
        raise ValueError("ffprobe did not return one video stream time_base")
    numerator, denominator = streams[0]["time_base"].split("/", 1)
    timebase = {"num": int(numerator), "den": int(denominator)}
    if timebase["num"] <= 0 or timebase["den"] <= 0:
        raise ValueError("video time_base must be positive")
    return timebase


def frame_pts(input_path: Path) -> list[int]:
    """Read integer PTS values only; pts_time is never requested or parsed."""
    payload = run_json([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "frame=pts", "-of", "json", str(input_path),
    ])
    result: list[int] = []
    for frame in payload.get("frames", []):
        pts = frame.get("pts")
        if isinstance(pts, str) and pts.lstrip("-").isdigit():
            result.append(int(pts))
        elif type(pts) is int:
            result.append(pts)
        else:
            raise ValueError("ffprobe returned a video frame without an integer pts")
    if not result:
        raise ValueError("ffprobe returned no video-frame PTS values")
    if result != sorted(result):
        raise ValueError("video-frame PTS values are not monotonic")
    return result


def parse_noise(value: str) -> str:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("noise must be a finite non-negative number") from exc
    if parsed < 0 or parsed == float("inf") or parsed != parsed:
        raise argparse.ArgumentTypeError("noise must be a finite non-negative number")
    return value


def parse_duration(value: str) -> str:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("duration must be a finite positive number") from exc
    if parsed <= 0 or parsed == float("inf") or parsed != parsed:
        raise argparse.ArgumentTypeError("duration must be a finite positive number")
    return value


def ffmpeg_filter_path(path: Path) -> str:
    # ffmpeg filter arguments use colon as an option separator.
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def parse_metadata(path: Path) -> list[dict[str, str]]:
    """Collect freezedetect key/value pairs together with the carrying frame PTS."""
    events: list[dict[str, str]] = []
    current_pts: int | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        pts_match = re.search(r"(?:^|\s)pts:\s*(-?\d+)(?:\s|$)", line)
        if pts_match:
            current_pts = int(pts_match.group(1))
            continue
        kv_match = re.fullmatch(r"lavfi\.freezedetect\.(freeze_start|freeze_duration|freeze_end)=(.+)", line.strip())
        if kv_match:
            if current_pts is None:
                raise ValueError("ffmpeg freeze metadata reported a value before any frame PTS")
            events.append({"key": kv_match.group(1), "value": kv_match.group(2), "carrier_pts": current_pts})
    return events


def resolve_seconds_to_pts(reported: str, pts_list: list[int], timebase: Fraction) -> int:
    """Map a reported freezedetect second value onto an actual frame PTS.

    ``freezedetect`` prints seconds, so the value is re-anchored to a real frame
    rather than trusted as a time of its own.  A value that lands more than one
    frame interval away from every frame is an error, not a rounding artefact.
    """
    try:
        seconds = Fraction(reported)
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"freezedetect reported a non-numeric time {reported!r}") from exc
    target = seconds / timebase
    best = min(pts_list, key=lambda pts: abs(Fraction(pts) - target))
    gaps = [b - a for a, b in zip(pts_list, pts_list[1:])]
    tolerance = max(gaps) if gaps else 1
    if abs(Fraction(best) - target) > tolerance:
        raise ValueError(
            f"freezedetect time {reported!r} has no frame within {tolerance} ticks "
            f"(nearest frame pts {best})"
        )
    return best


def build_runs(
    events: list[dict[str, str]], pts_list: list[int], timebase: Fraction, duration_ts: int
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    open_run: dict[str, Any] | None = None
    for event in events:
        key = event["key"]
        if key == "freeze_start":
            if open_run is not None:
                raise ValueError("ffmpeg reported a freeze_start while a freeze run was still open")
            open_run = {
                "start_pts": resolve_seconds_to_pts(event["value"], pts_list, timebase),
                "reported": {"freeze_start": event["value"]},
                "carrier_pts": {"freeze_start": event["carrier_pts"]},
            }
        elif open_run is None:
            raise ValueError(f"ffmpeg reported {key} with no open freeze run")
        elif key == "freeze_duration":
            open_run["reported"]["freeze_duration"] = event["value"]
            open_run["carrier_pts"]["freeze_duration"] = event["carrier_pts"]
        else:
            open_run["reported"]["freeze_end"] = event["value"]
            open_run["carrier_pts"]["freeze_end"] = event["carrier_pts"]
            open_run["end_pts"] = resolve_seconds_to_pts(event["value"], pts_list, timebase)
            runs.append(open_run)
            open_run = None
    if open_run is not None:
        # A freeze that is still open at end of stream has no freeze_end line.
        open_run["end_pts"] = duration_ts
        open_run["reported"]["freeze_end"] = None
        open_run["open_at_end_of_stream"] = True
        runs.append(open_run)
    for run in runs:
        if run["end_pts"] <= run["start_pts"]:
            raise ValueError(f"freeze run is not forward in time: {run}")
    for earlier, later in zip(runs, runs[1:]):
        if later["start_pts"] < earlier["end_pts"]:
            raise ValueError("freeze runs overlap")
    return runs


def stream_duration_ts(input_path: Path) -> int:
    payload = run_json([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=duration_ts", "-of", "json", str(input_path),
    ])
    streams = payload.get("streams", [])
    if len(streams) != 1:
        raise ValueError("ffprobe did not return one video stream")
    duration_ts = streams[0].get("duration_ts")
    if isinstance(duration_ts, str) and duration_ts.isdigit():
        return int(duration_ts)
    if type(duration_ts) is int:
        return duration_ts
    raise ValueError("ffprobe did not return an integer duration_ts")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="read-only video source")
    parser.add_argument("--output", required=True, type=Path, help="freeze-run JSON")
    parser.add_argument(
        "--noise", type=parse_noise, default="0.003",
        help="ffmpeg freezedetect noise tolerance, retained verbatim in output (default: 0.003)",
    )
    parser.add_argument(
        "--duration", type=parse_duration, default="3",
        help="ffmpeg freezedetect minimum freeze duration in seconds (default: 3)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input.is_file():
        raise FileNotFoundError(args.input)
    if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
        raise RuntimeError("ffprobe and ffmpeg are required")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    timebase = video_timebase(args.input)
    with tempfile.NamedTemporaryFile(prefix="freeze-metadata-", suffix=".txt", dir=args.output.parent, delete=False) as handle:
        metadata_path = Path(handle.name)
    try:
        filter_graph = (
            f"freezedetect=n={args.noise}:d={args.duration},"
            f"metadata=print:file='{ffmpeg_filter_path(metadata_path)}'"
        )
        subprocess.run([
            "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(args.input),
            "-an", "-vf", filter_graph, "-f", "null", "-",
        ], check=True)
        events = parse_metadata(metadata_path)
    finally:
        metadata_path.unlink(missing_ok=True)

    runs = build_runs(
        events, frame_pts(args.input), Fraction(timebase["num"], timebase["den"]),
        stream_duration_ts(args.input),
    )
    write_json(args.output, {
        "format": FORMAT,
        "source_sha256": sha256_file(args.input),
        "timebase": timebase,
        "noise": args.noise,
        "duration": args.duration,
        "runs": runs,
    })
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"freeze_detect.py: {exc}", file=sys.stderr)
        raise SystemExit(1)
