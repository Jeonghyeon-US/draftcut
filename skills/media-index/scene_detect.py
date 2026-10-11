#!/usr/bin/env python3
"""Detect ffmpeg scene-change frames and preserve their integer PTS ticks."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


FORMAT = "draftcut.media-index.scene-boundaries.v1"


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
    return {"num": int(numerator), "den": int(denominator)}


def parse_threshold(value: str) -> str:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("threshold must be a finite non-negative number") from exc
    if parsed < 0 or parsed == float("inf") or parsed != parsed:
        raise argparse.ArgumentTypeError("threshold must be a finite non-negative number")
    return value


def ffmpeg_filter_path(path: Path) -> str:
    # ffmpeg filter arguments use colon as an option separator.
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def parse_metadata(path: Path) -> list[dict[str, Any]]:
    """Read only integer ``pts`` metadata; never consume a ``pts_time`` value."""
    boundaries: list[dict[str, Any]] = []
    current_pts: int | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        pts_match = re.search(r"(?:^|\s)pts:\s*(-?\d+)(?:\s|$)", line)
        if pts_match:
            current_pts = int(pts_match.group(1))
            continue
        score_match = re.fullmatch(r"lavfi\.scene_score=(.+)", line.strip())
        if score_match and current_pts is not None:
            boundaries.append({"pts": current_pts, "scene_score": float(score_match.group(1))})
            current_pts = None
    if len({entry["pts"] for entry in boundaries}) != len(boundaries):
        raise ValueError("ffmpeg scene metadata contains duplicate PTS values")
    return boundaries


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="read-only video source")
    parser.add_argument("--output", required=True, type=Path, help="scene-boundary JSON")
    parser.add_argument(
        "--threshold", type=parse_threshold, default="0.01",
        help="ffmpeg select scene threshold, retained verbatim in output (default: 0.01)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input.is_file():
        raise FileNotFoundError(args.input)
    if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
        raise RuntimeError("ffprobe and ffmpeg are required")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="scene-metadata-", suffix=".txt", dir=args.output.parent, delete=False) as handle:
        metadata_path = Path(handle.name)
    try:
        filter_graph = f"select=gt(scene\\,{args.threshold}),metadata=print:file='{ffmpeg_filter_path(metadata_path)}'"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", str(args.input),
            "-an", "-vf", filter_graph, "-f", "null", "-",
        ], check=True)
        boundaries = parse_metadata(metadata_path)
    finally:
        metadata_path.unlink(missing_ok=True)

    write_json(args.output, {
        "format": FORMAT,
        "source_sha256": sha256_file(args.input),
        "timebase": video_timebase(args.input),
        "threshold": args.threshold,
        "boundaries": boundaries,
    })
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"scene_detect.py: {exc}", file=sys.stderr)
        raise SystemExit(1)
