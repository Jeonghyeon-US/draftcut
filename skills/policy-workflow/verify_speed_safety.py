#!/usr/bin/env python3
"""Check sampled pixel changes in accelerated video ranges.

The report's `policy_text` describes sampling the beginning, middle and end of
speed segments, checking for UI changes, and recommending shorter segments when
changes are found. The policy is this project's own working rule.

This tool does the *verification and the report*. It deliberately does NOT shrink
the segment: the policy says to shrink but never says by how much, and nothing in
the repo grounds a shrink amount. Inventing one would be the same shape of
mistake as promoting a plan on unchecked evidence.

Frames are sampled as 320x180 gray images. Standard library only.

⛔ `--threshold` is a provisional CLI parameter, not a contract constant. The
threshold must be calibrated on the material being checked;
three sampled frames and their mean pixel differences do not prove that no UI
change occurred elsewhere in the segment.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

WIDTH, HEIGHT = 320, 180
FRAME_BYTES = WIDTH * HEIGHT
EDGE_INSET_SEC = Fraction(1, 20) # 0.05s, so the sampled frame is inside the range


class SafetyError(RuntimeError):
    """The inputs could not be verified at all (as opposed to a policy violation)."""


def rational(value: Any, label: str) -> Fraction:
    if not isinstance(value, dict):
        raise SafetyError(f"{label}: rational object required")
    tb = value.get("timebase")
    if not isinstance(tb, dict):
        raise SafetyError(f"{label}: timebase required")
    for key, holder in (("value", value), ("num", tb), ("den", tb)):
        if not isinstance(holder.get(key), int) or isinstance(holder.get(key), bool):
            raise SafetyError(f"{label}: {key} must be an integer")
    if tb["den"] < 1:
        raise SafetyError(f"{label}: den must be >= 1")
    return Fraction(value["value"] * tb["num"], tb["den"])


def grab(media: Path, at: Fraction, work: Path, tag: str) -> bytes:
    out = work / f"{tag}.gray"
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{float(at):.6f}", "-i", str(media),
         "-frames:v", "1", "-vf", f"scale={WIDTH}:{HEIGHT},format=gray",
         "-f", "rawvideo", "-y", str(out)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise SafetyError(f"{tag}: ffmpeg failed at {float(at):.3f}s: {proc.stderr.strip()}")
    raw = out.read_bytes()
    if len(raw) != FRAME_BYTES:
        raise SafetyError(f"{tag}: expected {FRAME_BYTES} gray bytes, got {len(raw)}")
    return raw


def mad(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / FRAME_BYTES


def verify(plan: dict[str, Any], source_map: dict[str, str], work: Path,
           threshold: float) -> dict[str, Any]:
    segments = {s.get("segment_id"): s for s in plan.get("source_segments", [])}
    actions = [a for a in plan.get("actions", [])
               if isinstance(a, dict) and a.get("action") == "act_speed_change"]
    findings: list[dict[str, Any]] = []
    for action in actions:
        aid = action.get("action_id")
        segment = segments.get(action.get("source_segment_id"))
        if segment is None:
            raise SafetyError(f"{aid}: source_segment_id does not resolve")
        source_id = segment.get("source_id")
        media = source_map.get(source_id)
        if not media:
            raise SafetyError(f"{aid}: source-map has no path for {source_id!r}")
        params = action.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("range"), dict):
            raise SafetyError(f"{aid}: params.range required")
        start = rational(params["range"].get("start"), f"{aid}.range.start")
        end = rational(params["range"].get("end"), f"{aid}.range.end")
        if end <= start:
            raise SafetyError(f"{aid}: range must be non-empty")
        middle = (start + end) / 2
        first = grab(Path(media), start + EDGE_INSET_SEC, work, f"{aid}-start")
        mid = grab(Path(media), middle, work, f"{aid}-middle")
        last = grab(Path(media), end - EDGE_INSET_SEC, work, f"{aid}-end")
        pairs = {
            "start_middle": mad(first, mid),
            "middle_end": mad(mid, last),
            "start_end": mad(first, last),
        }
        worst = max(pairs.values())
        findings.append({
            "action_id": aid,
            "source_id": source_id,
            "range_sec": {"start": float(start), "end": float(end)},
            "duration_sec": float(end - start),
            "mad": pairs,
            "worst_mad": worst,
            "verdict": "new_ui_appeared" if worst > threshold else "static",
        })
    violations = [f for f in findings if f["verdict"] == "new_ui_appeared"]
    return {
        "schema_version": "draftcut-speed-safety-report/v1",
        "policy": "speed_ramp_policy.verification_required",
        "policy_text": ("For every speed segment, sample the first, middle and last frame and confirm "
                        "no new UI element appeared. If one did, shorten the segment and report it."),
        "frame_sampling": {"width": WIDTH, "height": HEIGHT, "pixel_format": "gray",
                           "edge_inset_sec": float(EDGE_INSET_SEC)},
        "threshold": threshold,
        "threshold_note": ("Maximum mean absolute grayscale difference (MAD) between sampled frames. Calibrate this threshold for your footage; a low value does not establish that no UI change occurred."),
        "plan_id": plan.get("plan_id"),
        "segments_checked": len(findings),
        "violations": len(violations),
        "prescription": ("shrink the listed segments and report -- this tool reports only, "
                         "because the policy does not say by how much to shrink"),
        "findings": findings,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", required=True, type=Path, help="EditPlan JSON")
    parser.add_argument("--source-map", required=True, type=Path,
                        help='{"<source_id>": "<absolute media path>"} JSON')
    parser.add_argument("--report", required=True, type=Path, help="safety report JSON")
    parser.add_argument("--work", required=True, type=Path, help="scratch directory for frames")
    parser.add_argument("--threshold", type=float, default=1.0,
                        help="Maximum mean absolute grayscale difference (MAD) between sampled frames "
                             "(default: 1.0). Calibrate for your footage; a low value does not "
                             "establish that no UI change occurred.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.threshold <= 0:
        print("SPEED SAFETY ERROR: --threshold must be positive", file=sys.stderr)
        return 2
    args.work.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        source_map = json.loads(args.source_map.read_text(encoding="utf-8"))
        report = verify(plan, source_map, args.work, args.threshold)
    except (SafetyError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        print(f"SPEED SAFETY ERROR: {exc}", file=sys.stderr)
        return 2
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                           encoding="utf-8")
    summary = {"status": "violations_reported" if report["violations"] else "static",
               "segments_checked": report["segments_checked"],
               "violations": report["violations"], "report": str(args.report)}
    print(json.dumps(summary, ensure_ascii=False))
    if report["violations"]:
        for finding in report["findings"]:
            if finding["verdict"] == "new_ui_appeared":
                print(f"SPEED SAFETY: {finding['action_id']} "
                      f"({finding['source_id']} {finding['range_sec']['start']:.3f}-"
                      f"{finding['range_sec']['end']:.3f}) worst MAD "
                      f"{finding['worst_mad']:.3f} > {report['threshold']} "
                      f"-- shrink this segment and report", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
