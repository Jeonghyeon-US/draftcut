#!/usr/bin/env python3
"""Pre-render mask coverage gate.

Answers, from the plan alone, the one question a rendered mask pass is usually
asked after the fact:

    for every detected sensitive box, at the instant it is on screen,
    do the active overlay rectangles cover it?

It renders nothing and decodes nothing, so it can run before each render
instead of re-reading a rendered delivery with OCR.

Limits: it assumes one source, one tick unit for detections and plan ranges, a
1:1 source-to-timeline time mapping (no repeated source ranges, no freeze
frames) and no geometric transform other than crop offsets. Only speed changes
are rejected; the other assumptions are not checked.

The detections must carry the box from the first matching pattern to the end of
the recognized line, including text after the match; see `locate_sensitive.py`.
Review the patterns and boxes to match the intended coverage.

Mask rectangles come from the plan: `act_overlay_asset` gives the origin, and
the referenced `assets[]` record gives `pixel_width`/`pixel_height`. Asset
dimensions are optional in the schema but required for this check. If the
plan omits them, pass `--assets-dir` to read dimensions from the PNG files.

    python3 skills/mask-plan/coverage_gate.py \
      --plan path/to/your.plan.json \
      --detections scratch/run/sensitive.json \
      --assets-dir path/to/assets
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path


def png_size(path: Path) -> tuple[int, int]:
    """Width and height from a PNG IHDR, without a decoder dependency."""
    header = open(path, "rb").read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"not a PNG: {path}")
    return struct.unpack(">II", header[16:24])


class Plan:
    def __init__(self, path: Path, assets_dir: Path | None):
        plan = json.loads(path.read_text())
        self.path = path
        self.fallbacks: list[str] = []

        if any(a["action"] == "act_speed_change" for a in plan["actions"]):
            raise SystemExit(
                "COVERAGE GATE: refused -- the plan has act_speed_change, so source time "
                "does not map linearly onto the timeline.  Extend the mapping before "
                "trusting this gate on such a plan.")

        # One crop may be declared per source segment; a detection is offset by the
        # crop of the segment it survives in, not by a single global crop.
        self.crop_of_segment: dict[str, tuple[int, int]] = {}
        for action in plan["actions"]:
            if action["action"] == "act_crop_reframe":
                params = action["params"]
                self.crop_of_segment[action["source_segment_id"]] = (params["x"], params["y"])

        self.clips = [(c["source_range"]["start"]["value"],
                       c["source_range"]["end"]["value"],
                       c["timeline_range"]["start"]["value"],
                       c["source_segment_id"]) for c in plan["timeline_clips"]]

        sizes: dict[str, tuple[int, int]] = {}
        for asset in plan.get("assets", []):
            if "pixel_width" in asset and "pixel_height" in asset:
                sizes[asset["asset_id"]] = (asset["pixel_width"], asset["pixel_height"])

        self.masks = []
        for action in plan["actions"]:
            if action["action"] != "act_overlay_asset":
                continue
            asset_id = action["asset_id"]
            if asset_id not in sizes:
                if assets_dir is None:
                    raise SystemExit(
                        f"COVERAGE GATE: refused -- asset '{asset_id}' declares no "
                        f"pixel_width/pixel_height and no --assets-dir was given.  "
                        f"The covered rectangle is unknown; the gate does not guess.")
                candidate = assets_dir / f"{asset_id}.png"
                if not candidate.exists():
                    raise SystemExit(
                        f"COVERAGE GATE: refused -- asset '{asset_id}' declares no "
                        f"dimensions and {candidate} does not exist.")
                sizes[asset_id] = png_size(candidate)
                if asset_id not in self.fallbacks:
                    self.fallbacks.append(asset_id)
            width, height = sizes[asset_id]
            params = action["params"]
            self.masks.append({
                "id": action["action_id"],
                "t0": params["timeline_range"]["start"]["value"],
                "t1": params["timeline_range"]["end"]["value"],
                "x0": params["overlay_x"], "y0": params["overlay_y"],
                "x1": params["overlay_x"] + width, "y1": params["overlay_y"] + height,
            })

    def locate(self, pts: int):
        """(timeline pts, crop offset) for a source instant, or None if it is cut away."""
        for start, end, timeline_start, segment_id in self.clips:
            if start <= pts < end:
                return timeline_start + (pts - start), self.crop_of_segment.get(segment_id, (0, 0))
        return None


def uncovered_fraction(box, rects, step: int = 2) -> float:
    x0, x1, y0, y1 = box
    total = miss = 0
    y = y0
    while y < y1:
        x = x0
        while x < x1:
            total += 1
            if not any(r["x0"] <= x < r["x1"] and r["y0"] <= y < r["y1"] for r in rects):
                miss += 1
            x += step
        y += step
    return (miss / total) if total else 0.0


def check(plan: Plan, detections_path: Path, step: int, limit: int) -> int:
    detections = json.loads(detections_path.read_text())
    checked = cut_away = 0
    failures = []
    for frame in detections:
        for hit in frame["hits"]:
            placed = plan.locate(frame["pts"])
            if placed is None:
                cut_away += 1 # the instant is not in the delivery at all
                continue
            timeline_pts, (crop_x, crop_y) = placed
            checked += 1
            bx0, bx1, by0, by1 = hit["box"]
            box = (bx0 - crop_x, bx1 - crop_x, by0 - crop_y, by1 - crop_y)
            active = [m for m in plan.masks if m["t0"] <= timeline_pts < m["t1"]]
            fraction = uncovered_fraction(box, active, step)
            if fraction > 0.0:
                failures.append((frame["pts"], timeline_pts,
                                 hit.get("match", hit.get("text", "")), box, fraction,
                                 [m["id"] for m in active]))

    print(f"[{detections_path.name}] boxes checked {checked} · cut away {cut_away} "
          f"→ uncovered {len(failures)}")
    for pts, timeline_pts, text, box, fraction, active in failures[:limit]:
        print(f"   src {pts:>9} → tl {timeline_pts:>9}  {text[:28]!r}  box={box}  "
              f"uncovered {fraction*100:.0f}%  active={active}")
    if limit > 0 and len(failures) > limit:
        print(f"   … {len(failures) - limit} more")
    return len(failures)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--detections", required=True, type=Path, action="append",
                        help="repeatable; output of locate_sensitive.py")
    parser.add_argument("--assets-dir", type=Path, default=None,
                        help="Read mask PNG dimensions when the plan omits asset dimensions.")
    parser.add_argument("--grid-step", type=int, default=2,
                        help="sampling step in pixels when testing a box (default 2)")
    parser.add_argument("--max-report", type=int, default=12)
    args = parser.parse_args()

    plan = Plan(args.plan, args.assets_dir)
    if plan.fallbacks:
        print(f"note: dimensions read from PNG files for {', '.join(plan.fallbacks)} "
              f"-- the plan does not declare them")
    total = sum(check(plan, path, args.grid_step, args.max_report) for path in args.detections)
    print(f"COVERAGE GATE: {'PASS' if total == 0 else f'{total} uncovered'}")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
