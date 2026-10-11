#!/usr/bin/env python3
"""Measure mean absolute pixel differences (MAD) between sampled frames, for the whole frame and a 16-by-9 grid of cells."""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import json
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

from ocr_vision import (
    SEEK_MARGIN,
    choose_sample_pts,
    emitted_pts,
    ffmpeg_filter_path,
    frame_pts,
    parse_positive_fraction,
    sha256_file,
    video_timebase,
)


FORMAT = "draftcut.media-index.activity-samples.v1"
SCALE = "320x180"
CELL_COLS = 16
CELL_ROWS = 9
CELL_COUNT = CELL_COLS * CELL_ROWS


def parse_signalstats(path: Path) -> list[tuple[int, Decimal]]:
    """Read the PTS and YAVG emitted by one signalstats branch."""
    import re

    rows: list[tuple[int, Decimal]] = []
    current_pts: int | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        pts_match = re.search(r"(?:^|\s)pts:\s*(-?\d+)(?:\s|$)", line)
        if pts_match:
            current_pts = int(pts_match.group(1))
            continue
        value_match = re.fullmatch(r"lavfi\.signalstats\.YAVG=(.+)", line.strip())
        if value_match:
            if current_pts is None:
                raise RuntimeError("ffmpeg signalstats reported YAVG before a frame PTS")
            try:
                value = Decimal(value_match.group(1))
            except InvalidOperation as exc:
                raise RuntimeError("ffmpeg signalstats reported a non-decimal YAVG") from exc
            if not value.is_finite() or value < 0 or value > 255:
                raise RuntimeError("ffmpeg signalstats reported YAVG outside 0..255")
            rows.append((current_pts, value))
            current_pts = None
    return rows


def chunks_with_overlap(selected_pts: list[int]) -> list[list[int]]:
    """Make at-most-60-frame chunks, sharing each boundary source frame."""
    chunks: list[list[int]] = []
    start = 0
    while start < len(selected_pts):
        end = min(start + 60, len(selected_pts))
        chunks.append(selected_pts[start:end])
        if end == len(selected_pts):
            break
        start = end - 1
    return chunks


def measure_chunk(
    input_path: Path, requested: list[int], timebase: Fraction, work: Path
) -> list[dict[str, Any]]:
    """Measure differences for one selected-frame chunk and prove its PTS mapping.

    ``showinfo`` is before the scale/blend branch so it reports the actual input
    frame PTS selected by ffmpeg.  The signalstats and cells branches split the
    same blended grayscale frames; their output records therefore describe the
    same consecutive selected-frame pairs.
    """
    if len(requested) < 2:
        return []
    metadata_path = work / "signalstats.txt"
    cells_path = work / "cells.raw"
    seek = max(Fraction(0), Fraction(requested[0]) * timebase - SEEK_MARGIN)
    expression = "+".join(f"eq(pts\\,{pts})" for pts in requested)
    graph = (
        f"[0:v]select='{expression}',showinfo,scale=320:180,format=gray,"
        "tblend=all_mode=difference,split[whole][cells];"
        f"[whole]signalstats,metadata=print:file='{ffmpeg_filter_path(metadata_path)}',nullsink;"
        "[cells]scale=16:9:flags=area,format=gray[out]"
    )
    completed = subprocess.run([
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-y", "-copyts",
        "-ss", f"{float(seek):.6f}", "-i", str(input_path), "-filter_complex", graph,
        "-map", "[out]", "-vsync", "0", "-an", "-f", "rawvideo", "-pix_fmt", "gray", str(cells_path),
    ], check=True, capture_output=True, text=True)
    actual_selected = emitted_pts(completed.stderr)
    if actual_selected != requested:
        raise RuntimeError(
            f"ffmpeg emitted {len(actual_selected)} frames whose PTS do not match the {len(requested)} requested"
        )
    statistics = parse_signalstats(metadata_path)
    raw = cells_path.read_bytes()
    if len(raw) % CELL_COUNT:
        raise RuntimeError("ffmpeg cell rawvideo length is not a multiple of 144 bytes")
    cells = [list(raw[offset:offset + CELL_COUNT]) for offset in range(0, len(raw), CELL_COUNT)]
    stat_pts = [pts for pts, _ in statistics]
    # Most ffmpeg builds omit the initial tblend output.  If one emits its
    # documented synthetic self-difference instead, remove it from both split
    # branches: the initial selected frame has no prev_pts and is not a sample.
    if stat_pts == requested and len(cells) == len(requested):
        statistics = statistics[1:]
        cells = cells[1:]
        stat_pts = stat_pts[1:]
    expected = requested[1:]
    if stat_pts != expected or len(cells) != len(expected):
        raise RuntimeError("ffmpeg MAD output PTS/cell count does not match consecutive selected-frame pairs")
    return [
        {"pts": pts, "prev_pts": prev_pts, "whole": float(value), "cells": frame_cells}
        for prev_pts, pts, (_, value), frame_cells in zip(requested, expected, statistics, cells)
    ]


def measure_samples(input_path: Path, selected_pts: list[int], timebase: Fraction, output_parent: Path) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="activity-mad-", dir=output_parent) as raw_work:
        work = Path(raw_work)
        for chunk_index, requested in enumerate(chunks_with_overlap(selected_pts)):
            chunk_work = work / f"chunk-{chunk_index:04d}"
            chunk_work.mkdir()
            samples.extend(measure_chunk(input_path, requested, timebase, chunk_work))
    if [sample["pts"] for sample in samples] != selected_pts[1:]:
        raise RuntimeError("activity samples do not cover exactly the selected source-frame PTS pairs")
    if [sample["prev_pts"] for sample in samples] != selected_pts[:-1]:
        raise RuntimeError("activity samples lost a previous selected source-frame PTS at a chunk boundary")
    return samples


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="read-only video source")
    parser.add_argument("--output", required=True, type=Path, help="activity-samples JSON")
    parser.add_argument(
        "--sample-interval", type=parse_positive_fraction, default=Fraction(1, 2),
        help="sampling interval in seconds as an exact rational (default: 1/2)",
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
    selected = choose_sample_pts(frame_pts(args.input), timebase, args.sample_interval)
    samples = measure_samples(args.input, selected, Fraction(timebase["num"], timebase["den"]), args.output.parent)
    write_json(args.output, {
        "format": FORMAT,
        "source_sha256": sha256_file(args.input),
        "timebase": timebase,
        "sampling": {"interval": {"num": args.sample_interval.numerator, "den": args.sample_interval.denominator}},
        "analysis": {
            "scale": SCALE,
            "pixel_format": "gray",
            "cells": {"cols": CELL_COLS, "rows": CELL_ROWS},
        },
        "samples": samples,
    })
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"activity_mad.py: {exc}", file=sys.stderr)
        raise SystemExit(1)
