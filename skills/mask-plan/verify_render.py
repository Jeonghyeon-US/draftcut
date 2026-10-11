#!/usr/bin/env python3
"""Check spatial uniformity in sampled overlay rectangles in a rendered video.

The tool samples each planned overlay at the centres of its first, middle,
and last active rendered frames and decodes the rectangle as grayscale pixels.
It does not prove that a mask was applied or that all sensitive text is hidden.
A missing mask over an already-uniform source area can pass this check.
Review sensitive text separately; the CLI help and report describe the limits."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

from coverage_gate import Plan


# Same resolution rule as executor/probe.py: explicit override, then PATH,
# then the Homebrew location.
FFMPEG = Path(os.environ.get("DRAFTCUT_FFMPEG")
              or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg")
FFPROBE = Path(os.environ.get("DRAFTCUT_FFPROBE")
               or shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe")
LIMITATION = (
    "this pixel check does not replace the string check: a missing mask over an already-uniform "
    "source area can pass this pixel check; run the full forbidden-string OCR "
    "check once immediately before delivery."
)


class RenderCheckError(RuntimeError):
    """An input or decoder failure that must never become a passing result."""


def timebase_for_masks(plan_path: Path, masks: list[dict[str, Any]]) -> dict[str, Fraction]:
    """Read only each overlay's canonical timebase; Plan remains geometry authority."""
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    actions = {
        action.get("action_id"): action
        for action in payload.get("actions", [])
        if action.get("action") == "act_overlay_asset"
    }
    bases: dict[str, Fraction] = {}
    for mask in masks:
        action = actions.get(mask["id"])
        if action is None:
            raise RenderCheckError(f"mask {mask['id']!r} is absent from plan actions")
        try:
            window = action["params"]["timeline_range"]
            start, end = window["start"], window["end"]
            start_tick, end_tick = start["value"], end["value"]
            start_base, end_base = start["timebase"], end["timebase"]
            numerator, denominator = start_base["num"], start_base["den"]
        except (KeyError, TypeError) as exc:
            raise RenderCheckError(
                f"mask {mask['id']!r} has no complete canonical timeline_range") from exc
        if (type(start_tick) is not int or type(end_tick) is not int or
                type(numerator) is not int or type(denominator) is not int or
                numerator <= 0 or denominator <= 0 or start_base != end_base):
            raise RenderCheckError(
                f"mask {mask['id']!r} needs matching positive integer timebases")
        if start_tick != mask["t0"] or end_tick != mask["t1"] or end_tick <= start_tick:
            raise RenderCheckError(f"mask {mask['id']!r} has an invalid active tick range")
        bases[mask["id"]] = Fraction(numerator, denominator)
    return bases


def ceil_fraction(value: Fraction) -> int:
    """Return an exact ceiling without converting a time to float."""
    return -(-value.numerator // value.denominator)


def seconds_text(value: Fraction) -> str:
    """Convert an exact time to decimal seconds only at the ffmpeg boundary."""
    return f"{float(value):.12f}"


def probe_frame_grid(rendered: Path) -> dict[str, Any]:
    """Read the rendered CFR frame grid; never invent one from the plan."""
    if not FFPROBE.is_file():
        raise RenderCheckError(f"ffprobe is unavailable at {FFPROBE}")
    args = [str(FFPROBE), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate,start_time,duration:format=start_time,duration",
            "-of", "json", str(rendered)]
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RenderCheckError(f"ffprobe failed: {detail or 'no diagnostic'}")
    try:
        payload = json.loads(result.stdout)
        stream = payload["streams"][0]
        rate = Fraction(stream["r_frame_rate"])
        start = Fraction(stream.get("start_time", payload.get("format", {}).get("start_time")))
        duration = Fraction(stream.get("duration", payload.get("format", {}).get("duration")))
    except (IndexError, KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise RenderCheckError(
            "ffprobe did not provide a usable video r_frame_rate, start_time, and duration") from exc
    if rate <= 0 or duration <= 0:
        raise RenderCheckError("ffprobe returned a non-positive video frame rate or duration")
    frame_count = duration * rate
    if frame_count.denominator != 1:
        raise RenderCheckError(
            "ffprobe duration does not describe an integral r_frame_rate frame grid; refusing to guess")
    if frame_count.numerator < 1:
        raise RenderCheckError("ffprobe returned a video with no rendered frames")
    return {"rate": rate, "start": start, "duration": duration,
            "frame_count": frame_count.numerator}


def frame_indices_for_window(start: Fraction, end: Fraction,
                             grid: dict[str, Any]) -> list[int]:
    """Return valid indices whose exact frame centres lie in ``[start, end)``."""
    rate: Fraction = grid["rate"]
    first = ceil_fraction((start - grid["start"]) * rate - Fraction(1, 2))
    after_last = ceil_fraction((end - grid["start"]) * rate - Fraction(1, 2))
    first = max(0, first)
    after_last = min(grid["frame_count"], after_last)
    return list(range(first, max(first, after_last)))


def sample_frames(indices: list[int], count: int) -> list[int]:
    """Choose active frames evenly, retaining first and last without duplicates."""
    if count < 1:
        raise RenderCheckError("--samples-per-mask must be at least 1")
    actual_count = min(count, len(indices))
    if actual_count == 0:
        return []
    if actual_count == 1:
        return [indices[0]]
    span = len(indices)
    return [indices[(index * (span - 1)) // (actual_count - 1)]
            for index in range(actual_count)]


def frame_centre(index: int, grid: dict[str, Any]) -> Fraction:
    """Return one rendered frame's centre time on the probed frame grid."""
    return grid["start"] + Fraction(2 * index + 1, 2) / grid["rate"]


def frame_decode_time(index: int, grid: dict[str, Any]) -> Fraction:
    """Return the frame PTS used to ask ffmpeg for the centre-selected frame."""
    return grid["start"] + Fraction(index, 1) / grid["rate"]


def decode_gray(rendered: Path, rect: dict[str, int], seconds: str) -> bytes:
    """Decode one cropped frame into raw grayscale bytes, without image files."""
    if not FFMPEG.is_file():
        raise RenderCheckError(f"ffmpeg is unavailable at {FFMPEG}")
    width = rect["x1"] - rect["x0"]
    height = rect["y1"] - rect["y0"]
    if width <= 0 or height <= 0:
        raise RenderCheckError(f"invalid crop rectangle {rect}")
    args = [str(FFMPEG), "-hide_banner", "-nostdin", "-loglevel", "error",
            "-ss", seconds, "-i", str(rendered),
            "-vf", f"crop={width}:{height}:{rect['x0']}:{rect['y0']}",
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    expected = width * height
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RenderCheckError(f"ffmpeg failed at {seconds}s: {detail or 'no diagnostic'}")
    if len(result.stdout) != expected:
        raise RenderCheckError(
            f"ffmpeg returned {len(result.stdout)} gray bytes at {seconds}s; expected {expected} "
            "(the requested frame centre may be outside the rendered video)")
    return result.stdout


def gray_statistics(raw: bytes) -> dict[str, float | int]:
    """Return population statistics for one decoded grayscale rectangle."""
    mean = sum(raw) / len(raw)
    variance = sum((value - mean) ** 2 for value in raw) / len(raw)
    return {"mean": mean, "stddev": variance ** 0.5, "max": max(raw)}


def check_render(rendered: Path, plan_path: Path, assets_dir: Path | None,
                 samples_per_mask: int, max_stddev: float) -> dict[str, Any]:
    """Measure every requested mask sample and retain successes and failures alike."""
    if max_stddev < 0:
        raise RenderCheckError("--max-stddev must be non-negative")
    plan = Plan(plan_path, assets_dir)
    bases = timebase_for_masks(plan_path, plan.masks)
    grid = probe_frame_grid(rendered)
    reports: list[dict[str, Any]] = []
    suspects = 0
    skipped = 0
    for mask in plan.masks:
        rect = {key: mask[key] for key in ("x0", "y0", "x1", "y1")}
        samples: list[dict[str, Any]] = []
        window_start = mask["t0"] * bases[mask["id"]]
        window_end = mask["t1"] * bases[mask["id"]]
        indices = frame_indices_for_window(window_start, window_end, grid)
        if not indices:
            reports.append({"mask_id": mask["id"], "rectangle": rect, "samples": samples,
                            "skipped": "no rendered frame inside the active window"})
            skipped += 1
            continue
        for index in sample_frames(indices, samples_per_mask):
            centre = frame_centre(index, grid)
            seconds = seconds_text(frame_decode_time(index, grid))
            record: dict[str, Any] = {"frame_index": index,
                                      "frame_center_seconds": seconds_text(centre)}
            try:
                statistics = gray_statistics(decode_gray(rendered, rect, seconds))
                passing = statistics["stddev"] <= max_stddev
                record.update(statistics, passed=passing)
                if not passing:
                    suspects += 1
            except RenderCheckError as exc:
                record.update(passed=False, error=str(exc))
                suspects += 1
            samples.append(record)
        reports.append({"mask_id": mask["id"], "rectangle": rect, "samples": samples})
    return {
        "rendered": str(rendered), "plan": str(plan_path), "samples_per_mask": samples_per_mask,
        "max_stddev": max_stddev, "masks": reports, "suspect_samples": suspects,
        "skipped_masks": skipped,
        "frame_grid": {"r_frame_rate": str(grid["rate"]), "start_time": seconds_text(grid["start"]),
                       "duration": seconds_text(grid["duration"]), "frame_count": grid["frame_count"]},
        "limitation": LIMITATION,
    }


def print_summary(report: dict[str, Any]) -> None:
    """Print a compact human-readable view of the same measurements in JSON."""
    for mask in report.get("masks", []):
        print(f"[{mask['mask_id']}] rect={mask['rectangle']}")
        if "skipped" in mask:
            print(f"  SKIPPED {mask['skipped']}")
        for sample in mask["samples"]:
            if "error" in sample:
                print(f"  frame {sample['frame_index']:>7} ({sample['frame_center_seconds']}s) "
                      f"ERROR {sample['error']}")
            else:
                state = "OK" if sample["passed"] else "SUSPECT"
                print(f"  frame {sample['frame_index']:>7} ({sample['frame_center_seconds']}s) "
                      f"mean={sample['mean']:.3f} stddev={sample['stddev']:.3f} "
                      f"max={sample['max']} {state}")
    print(f"note: {LIMITATION}")
    print(f"skipped={report.get('skipped_masks', 0)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog=LIMITATION,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rendered", required=True, type=Path, help="rendered delivery video")
    parser.add_argument("--plan", required=True, type=Path, help="EditPlan containing overlay masks")
    parser.add_argument("--assets-dir", type=Path, default=None,
                        help="PNG dimension fallback for plans without asset dimensions")
    parser.add_argument("--samples-per-mask", type=int, default=3,
                        help="maximum samples per active rendered frame range (default: 3: first, middle, last)")
    parser.add_argument("--max-stddev", type=float, default=2.0,
                        help="maximum grayscale spatial standard deviation (default: 2.0)")
    parser.add_argument("--output", type=Path, default=None, help="write per-sample JSON report here")
    args = parser.parse_args()

    try:
        report = check_render(args.rendered, args.plan, args.assets_dir,
                              args.samples_per_mask, args.max_stddev)
    except (OSError, ValueError, json.JSONDecodeError, RenderCheckError, SystemExit) as exc:
        report = {"rendered": str(args.rendered), "plan": str(args.plan), "masks": [],
                  "suspect_samples": 1, "skipped_masks": 0, "error": str(exc), "limitation": LIMITATION}
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print_summary(report)
    suspects = report["suspect_samples"]
    print("RENDER CHECK: PASS" if suspects == 0 else f"RENDER CHECK: {suspects} suspect")
    return 0 if suspects == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
