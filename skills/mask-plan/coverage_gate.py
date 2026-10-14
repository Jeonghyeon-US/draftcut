#!/usr/bin/env python3
"""Pre-render mask coverage gate.

Answers, from the plan alone, the one question a rendered mask pass is usually
asked after the fact:

    for every detected sensitive box, at the instant it is on screen,
    do the active overlay rectangles cover it?

It renders nothing and decodes nothing, so it can run before each render
instead of re-reading a rendered delivery with OCR.

Limits: the loader refuses speed changes, freeze frames, overlapping source
ranges, duplicate asset/segment/clip IDs, multiple or unresolved sources, missing or
mixed plan timebases, and actions outside ALLOWED_ACTIONS (including scale,
padding and zoom). Crops must reference declared segments and have params; only crops on placed
segments have their geometry read. They must be unique per segment and declare
dimensions equal to the output canvas. Geometry values have integer type and
asset dimensions are positive. Declared overlay
axes must be final_timeline and coordinate origins must be top_left. Equivalent
rational timebases are accepted. Detection timebases are compared when supplied;
the current detector supplies none, so their unit remains a caller-verified assumption.
Without a crop, the plan has no source dimensions to compare with the output
canvas; unchanged source geometry remains an assumption. This is not a full
contract validator or a check of rendered frame timing or missed detections.
Clip and overlay endpoints and detection pts must be integer ticks. Clip source
and timeline lengths must be positive and equal. Detection boxes must contain
four integer coordinates with positive width and height. The grid step must be
a positive integer; mask boundaries are sampled to catch every uncovered pixel
in a detected box. Malformed plans and detections are refused, including JSON
parsing and missing-file errors.

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
from fractions import Fraction
from pathlib import Path


# Clip placement is authoritative; these declarations add no separate mapping.
# Audio effects and captions do not move source-video pixels or source time.
# Blur changes pixels without moving them and is not counted as mask coverage.
ALLOWED_ACTIONS = frozenset({
    "act_mark_segment", "act_flag_for_review", "act_flag_issue",
    "act_preserve_segment", "act_lock_order", "act_select_source_segment",
    "act_assemble_segment", "act_reorder_segments", "act_trim_segment",
    "act_ripple_delete", "act_insert_segment",
    "act_crop_reframe", "act_overlay_asset", "act_blur_region",
    "act_add_caption_segment", "act_style_caption_segment",
    "act_crossfade_audio", "act_gain", "act_normalize_loudness",
    "act_noise_reduce", "act_fade_audio", "act_duck_audio", "act_mix_audio",
    "act_add_music", "act_add_sfx", "act_add_ambience",
})


def timebase_of(time: dict, owner: str) -> Fraction:
    """Require the declared tick unit, comparing exact reduced fractions."""
    base = time.get("timebase")
    if (not isinstance(base, dict)
            or any(type(base.get(key)) is not int or base[key] <= 0
                   for key in ("num", "den"))):
        raise SystemExit(
            f"COVERAGE GATE: refused -- {owner} has no valid timebase, so the "
            "tick unit cannot be established. Declare positive integer num/den.")
    return Fraction(base["num"], base["den"])


def require_integer(value, owner: str) -> None:
    if type(value) is not int:
        raise SystemExit(
            f"COVERAGE GATE: refused -- {owner} must be an integer tick; "
            "only integer types are accepted (no strings, floats or bools).")


def require_geometry_integer(value, owner: str) -> None:
    if type(value) is not int:
        raise SystemExit(
            f"COVERAGE GATE: refused -- {owner} must have integer type; "
            "strings, floats and bools are not accepted for geometry.")


def require_step(step) -> None:
    if type(step) is not int or step < 1:
        raise SystemExit("COVERAGE GATE: refused -- grid-step must be a positive integer.")


def grid_step_arg(value: str) -> int:
    try:
        step = int(value)
    except ValueError:
        raise SystemExit("COVERAGE GATE: refused -- grid-step must be a positive integer.")
    require_step(step)
    return step


def png_size(path: Path) -> tuple[int, int]:
    """Width and height from a PNG IHDR, without a decoder dependency."""
    header = open(path, "rb").read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"not a PNG: {path}")
    if len(header) < 24:
        raise ValueError(f"truncated PNG header: {path}")
    return struct.unpack(">II", header[16:24])


class Plan:
    def __init__(self, path: Path, assets_dir: Path | None):
        try:
            plan = json.loads(path.read_text())
            self.path = path
            self.fallbacks: list[str] = []

            for action in plan["actions"]:
                name, action_id = action["action"], action["action_id"]
                if name == "act_speed_change":
                    raise SystemExit(
                        f"COVERAGE GATE: refused -- the plan has act_speed_change "
                        f"'{action_id}', so source time does not map linearly onto the "
                        "timeline. Extend the mapping before trusting this gate on such a plan.")
                if name == "act_freeze_frame":
                    raise SystemExit(
                        f"COVERAGE GATE: refused -- act_freeze_frame '{action_id}' holds "
                        "one source instant across output frames, so this gate cannot "
                        "check the full exposure duration.")
                if name not in ALLOWED_ACTIONS:
                    raise SystemExit(
                        f"COVERAGE GATE: refused -- action {name} '{action_id}' is outside "
                        "ALLOWED_ACTIONS, so its effect on geometry or source-to-timeline "
                        "mapping is not supported by this gate.")

            clips = plan["timeline_clips"]
            if not isinstance(clips, list):
                raise TypeError("timeline_clips must be a list")
            if "source_segments" not in plan:
                clip_ids = ", ".join(c["clip_id"] for c in clips)
                raise SystemExit(
                    f"COVERAGE GATE: refused -- plan '{plan.get('plan_id', path.name)}' "
                    f"has no source_segments for clips [{clip_ids}], so source identity "
                    "cannot be established.")
            sources = {}
            for segment in plan["source_segments"]:
                segment_id = segment["segment_id"]
                if segment_id in sources:
                    raise SystemExit(
                        f"COVERAGE GATE: refused -- duplicate segment_id '{segment_id}' "
                        "makes source identity ambiguous.")
                sources[segment_id] = segment.get("source_id")
            clip_sources = {}
            for clip in clips:
                if clip["clip_id"] in clip_sources:
                    raise SystemExit(
                        f"COVERAGE GATE: refused -- duplicate clip_id '{clip['clip_id']}' "
                        "makes clip placement ambiguous.")
                segment_id = clip["source_segment_id"]
                source_id = sources.get(segment_id)
                if not source_id:
                    raise SystemExit(
                        f"COVERAGE GATE: refused -- clip '{clip['clip_id']}' references "
                        f"unresolved source segment '{segment_id}', so source identity "
                        "cannot be established.")
                clip_sources[clip["clip_id"]] = source_id
            if len(set(clip_sources.values())) > 1:
                raise SystemExit(
                    f"COVERAGE GATE: refused -- clips reference multiple sources "
                    f"{clip_sources}, so a detection's source instant is ambiguous.")

            self.timebase: Fraction | None = None
            self.timebase_owner: str | None = None
            for clip in clips:
                for axis in ("source_range", "timeline_range"):
                    for endpoint in ("start", "end"):
                        require_integer(clip[axis][endpoint].get("value"),
                                        f"clip '{clip['clip_id']}' {axis}.{endpoint} value")
                        self._accept_timebase(clip[axis][endpoint],
                                              f"clip '{clip['clip_id']}' {axis}.{endpoint}")
                lengths = {axis: clip[axis]["end"]["value"] - clip[axis]["start"]["value"]
                           for axis in ("source_range", "timeline_range")}
                for axis, length in lengths.items():
                    if length <= 0:
                        raise SystemExit(
                            f"COVERAGE GATE: refused -- clip '{clip['clip_id']}' {axis} "
                            "length must be positive for a unique forward placement.")
                if lengths["source_range"] != lengths["timeline_range"]:
                    raise SystemExit(
                        f"COVERAGE GATE: refused -- clip '{clip['clip_id']}' length mismatch: "
                        f"source_range {lengths['source_range']} vs timeline_range "
                        f"{lengths['timeline_range']}; this gate requires 1:1 elapsed ticks.")
            for action in plan["actions"]:
                if action["action"] == "act_overlay_asset":
                    params = action["params"]
                    if "enable_axis" in params and params["enable_axis"] != "final_timeline":
                        raise SystemExit(
                            f"COVERAGE GATE: refused -- action '{action['action_id']}' "
                            "enable_axis must be final_timeline for mask activation.")
                    self._accept_origin(action)
                    for endpoint in ("start", "end"):
                        require_integer(params["timeline_range"][endpoint].get("value"),
                                        f"action '{action['action_id']}' timeline_range.{endpoint} value")
                        self._accept_timebase(action["params"]["timeline_range"][endpoint],
                                              f"action '{action['action_id']}' timeline_range.{endpoint}")

            # Positive source lengths make adjacent comparisons after start sorting sufficient.
            ordered = sorted(clips, key=lambda c: c["source_range"]["start"]["value"])
            for left, right in zip(ordered, ordered[1:]):
                if (max(left["source_range"]["start"]["value"], right["source_range"]["start"]["value"])
                        < min(left["source_range"]["end"]["value"], right["source_range"]["end"]["value"])):
                    raise SystemExit(
                        f"COVERAGE GATE: refused -- clips '{left['clip_id']}' and "
                        f"'{right['clip_id']}' overlap source '{clip_sources[left['clip_id']]}', "
                        "so one source instant has multiple timeline placements.")

            # One crop may be declared per source segment; a detection is offset by the
            # crop of the segment it survives in, not by a single global crop.
            self.crop_of_segment: dict[str, tuple[int, int]] = {}
            placed_segments = {clip["source_segment_id"] for clip in clips}
            for action in plan["actions"]:
                if action["action"] == "act_crop_reframe":
                    if "source_segment_id" not in action:
                        raise SystemExit(
                            f"COVERAGE GATE: refused -- act_crop_reframe '{action['action_id']}' "
                            "has no source_segment_id.")
                    segment_id = action["source_segment_id"]
                    if segment_id not in sources:
                        raise SystemExit(
                            f"COVERAGE GATE: refused -- act_crop_reframe '{action['action_id']}' "
                            f"references undeclared source segment '{segment_id}'.")
                    if "params" not in action:
                        raise SystemExit(
                            f"COVERAGE GATE: refused -- act_crop_reframe '{action['action_id']}' "
                            "has no params.")
                    if segment_id not in placed_segments:
                        continue
                    if segment_id in self.crop_of_segment:
                        raise SystemExit(
                            f"COVERAGE GATE: refused -- duplicate crop for segment '{segment_id}' "
                            f"at action '{action['action_id']}'; two or more crops are refused "
                            "even when their values are identical.")
                    self._accept_origin(action)
                    params = action["params"]
                    output = plan.get("output_policy", {})
                    if "width" not in output or "height" not in output:
                        raise SystemExit(
                            f"COVERAGE GATE: refused -- act_crop_reframe '{action['action_id']}' "
                            "has no declared output_policy width/height, so geometry after "
                            "the crop cannot be established.")
                    for key in ("x", "y", "width", "height"):
                        require_geometry_integer(params[key],
                                                 f"act_crop_reframe '{action['action_id']}' {key}")
                    for key in ("width", "height"):
                        require_geometry_integer(output[key], f"output_policy {key}")
                    if (params["width"], params["height"]) != (output["width"], output["height"]):
                        raise SystemExit(
                            f"COVERAGE GATE: refused -- act_crop_reframe '{action['action_id']}' "
                            f"size {params['width']}x{params['height']} differs from output "
                            f"{output['width']}x{output['height']}, so crop offsets alone "
                            "cannot map the detected box.")
                    self.crop_of_segment[segment_id] = (params["x"], params["y"])

            self.clips = [(c["source_range"]["start"]["value"],
                           c["source_range"]["end"]["value"],
                           c["timeline_range"]["start"]["value"],
                           c["source_segment_id"]) for c in plan["timeline_clips"]]

            sizes: dict[str, tuple[int, int]] = {}
            asset_ids = set()
            for asset in plan.get("assets", []):
                asset_id = asset["asset_id"]
                if asset_id in asset_ids:
                    raise SystemExit(
                        f"COVERAGE GATE: refused -- duplicate asset_id '{asset_id}' "
                        "makes mask dimensions ambiguous.")
                asset_ids.add(asset_id)
                for key in ("pixel_width", "pixel_height"):
                    if key in asset:
                        self._accept_asset_dimension(asset[key], asset_id, key)
                if "pixel_width" in asset and "pixel_height" in asset:
                    sizes[asset_id] = (asset["pixel_width"], asset["pixel_height"])

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
                self._accept_asset_dimension(width, asset_id, "pixel_width")
                self._accept_asset_dimension(height, asset_id, "pixel_height")
                params = action["params"]
                for key in ("overlay_x", "overlay_y"):
                    require_geometry_integer(params[key], f"action '{action['action_id']}' {key}")
                self.masks.append({
                    "id": action["action_id"],
                    "t0": params["timeline_range"]["start"]["value"],
                    "t1": params["timeline_range"]["end"]["value"],
                    "x0": params["overlay_x"], "y0": params["overlay_y"],
                    "x1": params["overlay_x"] + width, "y1": params["overlay_y"] + height,
                })
        except (KeyError, TypeError, ValueError, AttributeError, IndexError, OSError) as exc:
            raise SystemExit(
                f"COVERAGE GATE: refused -- malformed plan: "
                f"{type(exc).__name__}: {exc}") from exc

    def _accept_asset_dimension(self, value, asset_id: str, key: str) -> None:
        require_geometry_integer(value, f"asset '{asset_id}' {key}")
        if value <= 0:
            raise SystemExit(
                f"COVERAGE GATE: refused -- asset '{asset_id}' {key} must be positive.")

    def _accept_origin(self, action: dict) -> None:
        params = action["params"]
        if "coordinate_origin" in params and params["coordinate_origin"] != "top_left":
            raise SystemExit(
                f"COVERAGE GATE: refused -- action '{action['action_id']}' "
                "coordinate_origin must be top_left for pixel offsets.")

    def _accept_timebase(self, time: dict, owner: str) -> None:
        base = timebase_of(time, owner)
        if self.timebase is None:
            self.timebase, self.timebase_owner = base, owner
        elif base != self.timebase:
            raise SystemExit(
                f"COVERAGE GATE: refused -- {owner} timebase {base} differs from "
                f"{self.timebase_owner} timebase {self.timebase}, so raw ticks cannot "
                "be compared by this gate.")

    def locate(self, pts: int):
        """(timeline pts, crop offset) for a source instant, or None if it is cut away."""
        for start, end, timeline_start, segment_id in self.clips:
            if start <= pts < end:
                return timeline_start + (pts - start), self.crop_of_segment.get(segment_id, (0, 0))
        return None


def uncovered_fraction(box, rects, step: int = 2) -> float:
    require_step(step)
    x0, x1, y0, y1 = box

    def samples(start, end):
        values = set(range(start, end, step))
        if end > start:
            values.add(end - 1)
        return values

    xs, ys = samples(x0, x1), samples(y0, y1)

    def count_misses(xs, ys):
        return sum(not any(r["x0"] <= x < r["x1"] and r["y0"] <= y < r["y1"]
                           for r in rects) for y in ys for x in xs)

    total = len(xs) * len(ys)
    miss = count_misses(xs, ys)
    # Keep the grid-based fraction stable; extra boundary samples detect gaps
    # that the grid misses, rather than changing its existing denominator.
    overlapping = [r for r in rects if r["x0"] < x1 and x0 < r["x1"]
                   and r["y0"] < y1 and y0 < r["y1"]]
    for rect in overlapping:
        xs.update(rect[key] for key in ("x0", "x1") if x0 <= rect[key] < x1)
        ys.update(rect[key] for key in ("y0", "y1") if y0 <= rect[key] < y1)
    xs, ys = sorted(xs), sorted(ys)
    # Rectangle boundaries partition the box into cells of constant coverage.
    # Each cell's first integer coordinate is sampled on both axes, so an
    # uncovered pixel cannot be missed at any positive integer step.
    if miss:
        return miss / total
    boundary_miss = count_misses(xs, ys)
    return boundary_miss / (len(xs) * len(ys)) if total else 0.0


def check(plan: Plan, detections_path: Path, step: int, limit: int) -> int:
    try:
        require_step(step)
        detections = json.loads(detections_path.read_text())
        if not isinstance(detections, list):
            raise TypeError("the detections document must be a list of frames")
        checked = cut_away = 0
        failures = []
        for frame in detections:
            require_integer(frame.get("pts"), f"detection '{detections_path.name}' pts")
            if "timebase" in frame:
                plan._accept_timebase(frame, f"detection '{detections_path.name}' pts {frame['pts']}")
            if not isinstance(frame["hits"], list):
                raise TypeError("frame hits must be a list")
            for hit in frame["hits"]:
                detected_box = hit["box"]
                if (not isinstance(detected_box, (list, tuple)) or len(detected_box) != 4
                        or any(type(value) is not int for value in detected_box)
                        or detected_box[0] >= detected_box[1]
                        or detected_box[2] >= detected_box[3]):
                    raise SystemExit(
                        "COVERAGE GATE: refused -- detection box must contain four integer "
                        "coordinates with x0 < x1 and y0 < y1.")
                placed = plan.locate(frame["pts"])
                if placed is None:
                    cut_away += 1 # the instant is not in the delivery at all
                    continue
                timeline_pts, (crop_x, crop_y) = placed
                checked += 1
                bx0, bx1, by0, by1 = detected_box
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
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, OSError) as exc:
        raise SystemExit(
            f"COVERAGE GATE: refused -- malformed detections: "
            f"{type(exc).__name__}: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--detections", required=True, type=Path, action="append",
                        help="repeatable; output of locate_sensitive.py")
    parser.add_argument("--assets-dir", type=Path, default=None,
                        help="Read mask PNG dimensions when the plan omits asset dimensions.")
    parser.add_argument("--grid-step", type=grid_step_arg, default=2,
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
