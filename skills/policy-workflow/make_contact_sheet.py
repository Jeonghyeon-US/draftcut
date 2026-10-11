#!/usr/bin/env python3
"""Create a timestamped contact sheet for reviewing proposed speed changes.

Provenance: this reproduces how a human editor actually chose speed segments —
by opening a timestamped tile of the clip and reading it. It is a port of this
project's own earlier manual workbench script, not a new method.

What is precedent and what is ours -- keep the distinction:
  * PRECEDENT: whole-source span, `fps=1/interval,scale=320:180,format=gray`,
    a near-5x7 bitmap font burned at 2x into each cell, PPM out, ffmpeg encodes.
    The precedent chose interval/columns BY HAND per clip (3/5, 3/6, 3/6, 5/9,
    5/7, 4/7, 5/6) -- there is no formula to reproduce, so both are parameters.
  * OURS: the optional `--plan` overlay, which tints the cells that fall inside a
    proposed `act_speed_change` range so the human sees the recommendation on the
    sheet. added this; it has no precedent.

Standard library only, plus ffmpeg for decoding and for encoding the final image
(the available ffmpeg has no drawtext/subtitles filter, which is why the label is
drawn here rather than by a filter -- the precedent hit the same wall).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

CELL_W, CELL_H = 320, 180
CELL_BYTES = CELL_W * CELL_H
PAD, MARGIN, LABEL_H = 2, 2, 18
LABEL_W = 126
SPEED_BORDER = (40, 130, 255) # border on cells inside a proposed speed range
SPEED_BORDER_PX = 5 # a border, not a wash -- the human has to READ these cells

# 5x7 bitmap digits, drawn at 2x. Carried over from this project's own earlier
# contact-sheet script; not taken from a third-party font or library.
FONT = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "11100"),
    ":": ("00000", "00100", "00100", "00000", "00100", "00100", "00000"),
}


class SheetError(RuntimeError):
    pass


def rational(value: Any, label: str) -> Fraction:
    if not isinstance(value, dict) or not isinstance(value.get("timebase"), dict):
        raise SheetError(f"{label}: rational object required")
    tb = value["timebase"]
    for key, holder in (("value", value), ("num", tb), ("den", tb)):
        if not isinstance(holder.get(key), int) or isinstance(holder.get(key), bool):
            raise SheetError(f"{label}: {key} must be an integer")
    if tb["den"] < 1:
        raise SheetError(f"{label}: den must be >= 1")
    return Fraction(value["value"] * tb["num"], tb["den"])


def speed_ranges(plan: dict[str, Any], source_id: str) -> list[tuple[float, float, str]]:
    segments = {s.get("segment_id"): s for s in plan.get("source_segments", [])}
    out = []
    for action in plan.get("actions", []):
        if not isinstance(action, dict) or action.get("action") != "act_speed_change":
            continue
        segment = segments.get(action.get("source_segment_id"))
        if segment is None or segment.get("source_id") != source_id:
            continue
        params = action.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("range"), dict):
            raise SheetError(f"{action.get('action_id')}: params.range required")
        start = rational(params["range"].get("start"), "range.start")
        end = rational(params["range"].get("end"), "range.end")
        out.append((float(start), float(end), str(action.get("action_id"))))
    return out


def sample_gray(media: Path, interval: float) -> bytes:
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(media),
         "-vf", f"fps=1/{interval},scale={CELL_W}:{CELL_H},format=gray",
         "-f", "rawvideo", "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise SheetError(f"ffmpeg sampling failed: {proc.stderr.decode(errors='replace').strip()}")
    raw = proc.stdout
    if not raw or len(raw) % CELL_BYTES:
        raise SheetError(f"expected whole {CELL_W}x{CELL_H} gray frames, got {len(raw)} bytes")
    return raw


def build(raw: bytes, interval: float, cols: int,
          ranges: list[tuple[float, float, str]]) -> tuple[bytes, int, int, int]:
    count = len(raw) // CELL_BYTES
    rows = (count + cols - 1) // cols
    out_w = MARGIN * 2 + cols * CELL_W + (cols - 1) * PAD
    out_h = MARGIN * 2 + rows * CELL_H + (rows - 1) * PAD
    canvas = bytearray(out_w * out_h * 3)

    def put(x: int, y: int, rgb: tuple[int, int, int]) -> None:
        if 0 <= x < out_w and 0 <= y < out_h:
            i = (y * out_w + x) * 3
            canvas[i:i + 3] = bytes(rgb)

    marked = 0
    for index in range(count):
        row, col = divmod(index, cols)
        x0 = MARGIN + col * (CELL_W + PAD)
        y0 = MARGIN + row * (CELL_H + PAD)
        at = index * interval
        inside = any(start <= at < end for start, end, _ in ranges)
        marked += 1 if inside else 0
        cell = raw[index * CELL_BYTES:(index + 1) * CELL_BYTES]
        for y in range(CELL_H):
            base = y * CELL_W
            for x in range(CELL_W):
                v = cell[base + x]
                put(x0 + x, y0 + y, (v, v, v))
        if inside:
            # A border, deliberately NOT a wash: these are the very cells the human
            # has to read in order to accept or reject the recommendation.
            for d in range(SPEED_BORDER_PX):
                for x in range(CELL_W):
                    put(x0 + x, y0 + d, SPEED_BORDER)
                    put(x0 + x, y0 + CELL_H - 1 - d, SPEED_BORDER)
                for y in range(CELL_H):
                    put(x0 + d, y0 + y, SPEED_BORDER)
                    put(x0 + CELL_W - 1 - d, y0 + y, SPEED_BORDER)
        total = int(round(at))
        text = f"{total // 3600:02d}:{(total // 60) % 60:02d}:{total % 60:02d}"
        for y in range(LABEL_H):
            for x in range(LABEL_W):
                put(x0 + x, y0 + y, (0, 0, 0))
        for ci, ch in enumerate(text):
            for ry, bits in enumerate(FONT[ch]):
                for rx, bit in enumerate(bits):
                    if bit == "1":
                        for dy in range(2):
                            for dx in range(2):
                                put(x0 + 4 + ci * 12 + rx * 2 + dx, y0 + 2 + ry * 2 + dy,
                                    (255, 255, 255))
    return bytes(canvas), out_w, out_h, marked


def encode(ppm: bytes, out: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="draftcut-sheet-") as tmp:
        src = Path(tmp) / "sheet.ppm"
        src.write_bytes(ppm)
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(src), "-frames:v", "1", "-q:v", "2", str(out)],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise SheetError(f"ffmpeg encode failed: {proc.stderr.decode(errors='replace').strip()}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--media", required=True, type=Path, help="source video (read-only)")
    parser.add_argument("--out", required=True, type=Path, help="contact sheet image (.jpg/.png)")
    parser.add_argument("--interval", required=True, type=float,
                        help="Seconds between sampled frames; choose a value suitable for the footage.")
    parser.add_argument("--cols", required=True, type=int,
                        help="Number of thumbnails per row.")
    parser.add_argument("--plan", type=Path,
                        help="Optional EditPlan JSON; highlight thumbnails within proposed speed-change ranges.")
    parser.add_argument("--source-id", help="which source in --plan this media is "
                                            "(required with --plan)")
    parser.add_argument("--index", type=Path, help="optional JSON index of the sheet")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.interval <= 0:
        print("CONTACT SHEET ERROR: --interval must be positive", file=sys.stderr)
        return 2
    if args.cols < 1:
        print("CONTACT SHEET ERROR: --cols must be >= 1", file=sys.stderr)
        return 2
    if bool(args.plan) != bool(args.source_id):
        print("CONTACT SHEET ERROR: --plan and --source-id go together", file=sys.stderr)
        return 2
    try:
        ranges: list[tuple[float, float, str]] = []
        if args.plan:
            plan = json.loads(args.plan.read_text(encoding="utf-8"))
            ranges = speed_ranges(plan, args.source_id)
        raw = sample_gray(args.media, args.interval)
        ppm_body, width, height, marked = build(raw, args.interval, args.cols, ranges)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        encode(f"P6\n{width} {height}\n255\n".encode() + ppm_body, args.out)
    except (SheetError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        print(f"CONTACT SHEET ERROR: {exc}", file=sys.stderr)
        return 2
    cells = len(raw) // CELL_BYTES
    summary = {
        "status": "written",
        "sheet": str(args.out),
        "cells": cells,
        "grid": {"cols": args.cols, "rows": (cells + args.cols - 1) // args.cols},
        "interval_sec": args.interval,
        "span_sec": cells * args.interval,
        "speed_ranges": len(ranges),
        "cells_marked_inside_speed_ranges": marked,
    }
    if args.index:
        args.index.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(summary)
        payload["schema_version"] = "draftcut-contact-sheet-index/v1"
        payload["cell_timestamps_sec"] = [round(i * args.interval, 3) for i in range(cells)]
        payload["proposed_speed_ranges"] = [
            {"action_id": aid, "start_sec": s, "end_sec": e} for s, e, aid in ranges
        ]
        args.index.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                              encoding="utf-8")
        summary["index"] = str(args.index)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
