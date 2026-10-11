#!/usr/bin/env python3
"""Sample video frames and write macOS Vision OCR observations with raw PTS ticks.

Run this program from a macOS shell with Vision access.  It deliberately exits
non-zero when Vision reports ``ok=False``: an empty OCR result is not evidence
that OCR was successfully attempted in a sandboxed process.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any


FORMAT = "draftcut.media-index.ocr-observations.v1"

# Input seeking lands on the keyframe at or before the target, so a small margin
# keeps the first requested frame of every chunk inside the decoded range.
SEEK_MARGIN = Fraction(2, 1)


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


def parse_positive_fraction(value: str) -> Fraction:
    try:
        parsed = Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError("sample interval must be a positive rational, e.g. 1/1") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("sample interval must be positive")
    return parsed


def choose_sample_pts(all_pts: list[int], timebase: dict[str, int], interval: Fraction) -> list[int]:
    """Choose real frame PTS values at approximately the requested spacing."""
    tick_interval = interval / Fraction(timebase["num"], timebase["den"])
    selected = [all_pts[0]]
    next_target = Fraction(all_pts[0]) + tick_interval
    for pts in all_pts[1:]:
        if Fraction(pts) >= next_target:
            selected.append(pts)
            next_target = Fraction(pts) + tick_interval
    return selected


def ffmpeg_filter_path(path: Path) -> str:
    # ffmpeg filter arguments use colon as an option separator.
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def emitted_pts(showinfo_log: str) -> list[int]:
    """Read back the integer PTS ffmpeg actually emitted, in output order."""
    values: list[int] = []
    for line in showinfo_log.splitlines():
        if "Parsed_showinfo" not in line:
            continue
        match = re.search(r"(?:^|\s)pts:\s*(-?\d+)(?:\s|$)", line)
        if match:
            values.append(int(match.group(1)))
    return values


def extract_frames(input_path: Path, pts_list: list[int], frames_dir: Path, timebase: Fraction) -> list[Path]:
    """Extract every selected frame in one decode pass, keyed by input-timebase PTS.

    One ``select=eq(pts,N)`` invocation per frame re-decodes the source from the
    start each time, so the cost grows with the square of the clip length.  A
    single pass is used instead, and the PTS ffmpeg reports for the frames it
    emitted is compared against the requested list rather than assumed.
    """
    frames_dir.mkdir(parents=True, exist_ok=True)
    produced: list[int] = []
    outputs = [frames_dir / f"sample-{ordinal:04d}-pts-{pts}.png" for ordinal, pts in enumerate(pts_list)]
    chunk_size = 60
    # A failed run must not leave a name that locate_sensitive.py will consume.
    # TemporaryDirectory also removes staging on both ffmpeg and validation failure.
    #
    # ⛔ `dir=frames_dir` is a correctness condition, not tidiness: os.replace is
    # atomic only within one filesystem (rename(2) returns EXDEV across them).  Stage
    # somewhere else -- /tmp, say -- and a --frames-dir on another volume makes the
    # promotion below raise instead of promoting.  The gate asserts this.
    #
    # ⚠ The promotion is one os.replace per frame, not one for the set.  A fault
    # part-way through still leaves some names promoted; what this closes is the long
    # window (a whole chunk of ffmpeg decoding), not the rename loop itself.
    with tempfile.TemporaryDirectory(prefix=".extract-frames-", dir=frames_dir) as raw_staging:
        staging = Path(raw_staging)
        for offset in range(0, len(pts_list), chunk_size):
            chunk = pts_list[offset:offset + chunk_size]
            # Input seeking keeps the whole run near a single decode of the source;
            # -copyts keeps the original PTS so eq(pts,N) still names real frames.
            seek = max(Fraction(0), Fraction(chunk[0]) * timebase - SEEK_MARGIN)
            expression = "+".join(f"eq(pts\\,{pts})" for pts in chunk)
            completed = subprocess.run([
                "ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-y", "-copyts",
                "-ss", f"{float(seek):.6f}",
                "-i", str(input_path),
                "-vf", f"select='{expression}',showinfo",
                "-vsync", "0", "-frames:v", str(len(chunk)), str(staging / f"chunk-{offset:04d}-%05d.png"),
            ], check=True, capture_output=True, text=True)
            chunk_produced = emitted_pts(completed.stderr)
            if chunk_produced != chunk:
                raise RuntimeError(
                    f"ffmpeg emitted {len(chunk_produced)} frames whose PTS do not match the {len(chunk)} requested"
                )
            produced.extend(chunk_produced)
            for ordinal, pts in enumerate(chunk):
                extracted = staging / f"chunk-{offset:04d}-{ordinal + 1:05d}.png"
                if not extracted.is_file() or extracted.stat().st_size == 0:
                    raise RuntimeError(f"ffmpeg did not extract frame at pts={pts}")
        if produced != pts_list:
            raise RuntimeError("the extracted frame PTS do not match the requested list")
        for ordinal, final in enumerate(outputs):
            offset = ordinal - ordinal % chunk_size
            extracted = staging / f"chunk-{offset:04d}-{ordinal - offset + 1:05d}.png"
            os.replace(extracted, final)
    return outputs


def recognize_png(png: Path) -> list[dict[str, Any]]:
    """Use the required Vision request shape; caller handles a false success flag."""
    from Foundation import NSURL  # Imported here so module import works without PyObjC.
    from Vision import VNImageRequestHandler, VNRecognizeTextRequest

    req = VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLanguages_(["ko-KR", "en-US"])
    h = VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(png)), None)
    ok, err = h.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(f"Vision request failed (ok=False, err={err!r})")
    return [
        {"text": str(candidate.string()), "confidence": float(candidate.confidence())}
        for observation in (req.results() or [])
        for candidate in observation.topCandidates_(1)
    ]


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="read-only video source")
    parser.add_argument("--output", required=True, type=Path, help="OCR observation JSON")
    parser.add_argument("--frames-dir", required=True, type=Path, help="directory for extracted sample PNGs")
    parser.add_argument(
        "--sample-interval", type=parse_positive_fraction, default=Fraction(1, 1),
        help="sampling interval in seconds as an exact rational (default: 1/1)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input.is_file():
        raise FileNotFoundError(args.input)
    if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
        raise RuntimeError("ffprobe and ffmpeg are required")

    timebase = video_timebase(args.input)
    selected_pts = choose_sample_pts(frame_pts(args.input), timebase, args.sample_interval)
    pngs = extract_frames(args.input, selected_pts, args.frames_dir,
                          Fraction(timebase["num"], timebase["den"]))
    frames: list[dict[str, Any]] = []
    for pts, png in zip(selected_pts, pngs):
        # Do not filter confidence or text.  Vision's observed values are retained verbatim.
        frames.append({"pts": pts, "observations": recognize_png(png)})

    write_json(args.output, {
        "format": FORMAT,
        "source_sha256": sha256_file(args.input),
        "timebase": timebase,
        "sampling": {"interval": {"num": args.sample_interval.numerator, "den": args.sample_interval.denominator}},
        "frames": frames,
    })
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"ocr_vision.py: {exc}", file=sys.stderr)
        raise SystemExit(1)
