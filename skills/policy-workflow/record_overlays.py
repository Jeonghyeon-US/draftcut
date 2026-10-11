#!/usr/bin/env python3
"""Record a human's still-overlay decisions into a merged EditPlan.

The sibling of `record_captions.py`, and a RECORDER for the same reason: the
contract requires `asset_id` AND `params` on every `act_overlay_asset`
(`contracts/edit-plan.schema.json` action conditional 6 -- the only one of the
eight that makes either required), so L3 has no way to emit an empty overlay
slot for a human to fill, and it emits `assets: []` anyway
(`skills/cut-decision/decide_cuts.py:333-379`). The human decides what to
cover and where; this writes it down.

⛔ DO NOT PORT THE CAPTION BINDING RULE HERE. measured why:

  * L2 never reads `act_overlay_asset.source_segment_id`. Only crop
    (`executor/assemble.py:320-327`), speed (`:361-364`) and clips (`:838-937`)
    read it; the overlay path (`:615-642`) reads `asset_id` and `params` only.
  * One segment can carry many overlays. N:1 is normal -- a mask follows a
    moving object.

The reason is structural: a caption's time lives in a separate `captions[]`
record, so the action had to point back at it and had to be 1:1. An
overlay carries `timeline_range` inside its own `params`, so asset, position
and time are all stated by one action. There is nothing to point at, and so no
binding defect to close. This tool adds no contract rules and opens no contract slot.

WHAT IT DOES THAT NOTHING ELSE DOES: writes overlays into a plan at all. Of the
five files that touch `act_overlay_asset`, three only read it
(`skills/mask-plan/coverage_gate.py`, `skills/mask-plan/verify_render.py`,
`executor/assemble.py`) and the only writer was a per-material, one-off plan
builder on the masking path.

It is DELIBERATELY PURPOSE-NEUTRAL. The same action carries redaction masks
(the canonical plan's 76), CTA cards and cursor emphasis; nothing in the
contract, the contract checker or L2 branches on which (the caption equivalent of this is ).

WHAT IT DOES NOT DO:
  * It does not decide what to cover, where, or for how long.
  * It does not invent an approval. Rights, usage scope, source class and the
    approval itself are declared by the human, per asset, in the decision file;
    only the SHA-256 and the PNG's pixel dimensions are measured.
  * It does not check that a mask actually covers what it should -- that is
    `skills/mask-plan/coverage_gate.py`, which runs after this.
  * It does not resize or generate the overlay image.

⭐ It does measure `pixel_width`/`pixel_height` from the PNG, which closes at the
source the gap `coverage_gate.py` documents: "plans written before 
have no dimensions".

Standard library only.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import struct
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

DECISION_REQUIRED = {
    "asset_id", "overlay_x", "overlay_y", "timeline_range", "source_segment_id",
    "evidence_ids", "decided_by", "timestamp", "rationale",
}
# Present only when the asset is not already registered in the plan.
ASSET_DECLARATION = {"asset_path", "rights_status", "usage_scope", "source_class", "approval"}
DECISION_OPTIONAL = {"action_id"} | ASSET_DECLARATION
SOURCE_CLASSES = {"project_provided", "external_candidate"}
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class OverlayError(RuntimeError):
    pass


def require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OverlayError(f"{label}: non-empty string required")
    return value


def require_int(value: Any, label: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OverlayError(f"{label}: integer required (got {value!r})")
    if minimum is not None and value < minimum:
        raise OverlayError(f"{label}: must be >= {minimum} (got {value})")
    return value


def time_fraction(value: Any, label: str) -> Fraction:
    if not isinstance(value, dict) or set(value) != {"value", "timebase"}:
        raise OverlayError(f"{label}: exactly {{value, timebase}} required")
    ticks = require_int(value.get("value"), f"{label}.value", minimum=0)
    timebase = value.get("timebase")
    if not isinstance(timebase, dict) or set(timebase) != {"num", "den"}:
        raise OverlayError(f"{label}.timebase: exactly {{num, den}} required")
    num = require_int(timebase.get("num"), f"{label}.timebase.num", minimum=1)
    den = require_int(timebase.get("den"), f"{label}.timebase.den", minimum=1)
    return Fraction(ticks) * Fraction(num, den)


def range_fractions(value: Any, label: str) -> tuple[Fraction, Fraction]:
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise OverlayError(f"{label}: exactly {{start, end}} required")
    start = time_fraction(value.get("start"), f"{label}.start")
    end = time_fraction(value.get("end"), f"{label}.end")
    # executor/assemble.py:637-640 -- L2 rejects an empty or negative range.
    if end <= start:
        raise OverlayError(f"{label}: end must be after start ({end} <= {start})")
    return start, end


def overlaps(a: tuple[Fraction, Fraction], b: tuple[Fraction, Fraction]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def index_evidence_ranges(index: dict[str, Any]) -> dict[str, tuple[Fraction, Fraction]]:
    out: dict[str, tuple[Fraction, Fraction]] = {}
    for item in index.get("evidence", []):
        if not isinstance(item, dict) or not isinstance(item.get("evidence_id"), str):
            continue
        try:
            out[item["evidence_id"]] = range_fractions(item.get("range"), "evidence range")
        except OverlayError:
            continue
    return out


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def png_size(path: Path) -> tuple[int, int]:
    """Width and height from a PNG IHDR, without a decoder dependency.

    Same read as `skills/mask-plan/coverage_gate.py:38`; measuring it here means
    the plan carries the dimensions that gate would otherwise have to guess.
    """
    header = path.open("rb").read(24)
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise OverlayError(f"{path}: not a PNG (the overlay asset must be a PNG)")
    width, height = struct.unpack(">II", header[16:24])
    if width < 1 or height < 1:
        raise OverlayError(f"{path}: PNG reports a zero dimension")
    return width, height


def register_asset(decision: dict[str, Any], label: str) -> dict[str, Any]:
    """Build one assets[] record from a measured file plus the human's declaration."""
    missing = ASSET_DECLARATION - set(decision)
    if missing:
        raise OverlayError(f"{label}: asset {decision['asset_id']!r} is not in the plan, so it must be "
                           f"declared here; missing {sorted(missing)}. This tool measures the hash and "
                           f"the pixel size, but it does not invent rights, scope or approval")
    path = Path(require_text(decision["asset_path"], f"{label}.asset_path"))
    if not path.is_file():
        raise OverlayError(f"{label}.asset_path {str(path)!r} is not a file")
    source_class = decision["source_class"]
    if source_class not in SOURCE_CLASSES:
        raise OverlayError(f"{label}.source_class must be one of {sorted(SOURCE_CLASSES)}")
    # The schema pins approval_status to the constant "approved", so the record
    # can carry nothing else; the human has to say so in as many words.
    if decision["approval"] != "approved":
        raise OverlayError(f"{label}.approval must be the literal \"approved\"; an asset in a plan is "
                           f"approved by the contract's own constant, so it may not be recorded "
                           f"without the human saying it")
    width, height = png_size(path)
    return {
        "asset_id": decision["asset_id"],
        "sha256": sha256_of(path),
        "rights_status": require_text(decision["rights_status"], f"{label}.rights_status"),
        "usage_scope": require_text(decision["usage_scope"], f"{label}.usage_scope"),
        "source_class": source_class,
        "approval_status": "approved",
        "pixel_width": width,
        "pixel_height": height,
    }


def verify_asset_file(asset: dict[str, Any], asset_map: dict[str, Any]) -> str:
    """Resolve one asset to a real file and re-measure its hash, as L2 will."""
    asset_id = asset.get("asset_id")
    path_value = asset_map.get(asset_id)
    if not isinstance(path_value, str) or not path_value:
        raise OverlayError(f"asset map has no path for overlay asset {asset_id!r}; "
                           f"the plan alone cannot prove this overlay will render")
    path = Path(path_value)
    if not path.is_file():
        raise OverlayError(f"overlay asset {asset_id!r} maps to {path_value!r}, which is not a file")
    declared = asset.get("sha256")
    if not isinstance(declared, str) or not SHA256_RE.match(declared):
        raise OverlayError(f"overlay asset {asset_id!r}.sha256 must be 64 lowercase hex characters")
    measured = sha256_of(path)
    if measured != declared:
        raise OverlayError(f"overlay asset {asset_id!r}: declared sha256 {declared} but "
                           f"{path_value} measures {measured}")
    return path_value


def record(plan: dict[str, Any], index: dict[str, Any], decisions: list[dict[str, Any]],
           asset_map: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    out = copy.deepcopy(plan)
    actions = out.setdefault("actions", [])
    assets = out.setdefault("assets", [])
    segments = {s.get("segment_id"): s for s in out.get("source_segments", []) if isinstance(s, dict)}
    evidence_ranges = index_evidence_ranges(index)
    policy = out.get("output_policy")
    if not isinstance(policy, dict):
        raise OverlayError("plan.output_policy is required to place an overlay")
    frame_w = require_int(policy.get("width"), "output_policy.width", minimum=1)
    frame_h = require_int(policy.get("height"), "output_policy.height", minimum=1)

    assets_by_id = {a.get("asset_id"): a for a in assets if isinstance(a, dict)}
    existing_action_ids = {a.get("action_id") for a in actions if isinstance(a, dict)}
    applied: list[dict[str, Any]] = []
    registered: list[str] = []
    verified: dict[str, str] = {}

    for position, decision in enumerate(decisions, start=1):
        label = f"overlays[{position}]"
        if not isinstance(decision, dict):
            raise OverlayError(f"{label}: object required")
        keys = set(decision)
        missing = DECISION_REQUIRED - keys
        if missing:
            raise OverlayError(f"{label}: missing required keys {sorted(missing)}. "
                               f"This tool does not invent placement, timing, rationale or evidence")
        unknown = keys - DECISION_REQUIRED - DECISION_OPTIONAL
        if unknown:
            raise OverlayError(f"{label}: unknown keys {sorted(unknown)}")

        asset_id = require_text(decision["asset_id"], f"{label}.asset_id")
        segment_id = require_text(decision["source_segment_id"], f"{label}.source_segment_id")
        decided_by = require_text(decision["decided_by"], f"{label}.decided_by")
        timestamp = require_text(decision["timestamp"], f"{label}.timestamp")
        rationale = require_text(decision["rationale"], f"{label}.rationale")

        if asset_id not in assets_by_id:
            asset = register_asset(decision, label)
            assets.append(asset)
            assets_by_id[asset_id] = asset
            registered.append(asset_id)
        else:
            declared_here = ASSET_DECLARATION & keys
            if declared_here:
                raise OverlayError(f"{label}: asset {asset_id!r} is already in the plan, so "
                                   f"{sorted(declared_here)} may not be redeclared here")
            asset = assets_by_id[asset_id]
        # The contract checker rejects rendering an unapproved asset.
        if asset.get("approval_status") != "approved":
            raise OverlayError(f"{label}: asset {asset_id!r} is not approved")
        if asset_map is not None and asset_id not in verified:
            verified[asset_id] = verify_asset_file(asset, asset_map)

        # source_segment_id is provenance here, not a binding: L2 does not read
        # it for overlays and one segment legitimately carries many.
        if segment_id not in segments:
            raise OverlayError(f"{label}.source_segment_id {segment_id!r} is not in "
                               f"plan.source_segments")

        start, end = range_fractions(decision["timeline_range"], f"{label}.timeline_range")
        overlay_x = require_int(decision["overlay_x"], f"{label}.overlay_x", minimum=0)
        overlay_y = require_int(decision["overlay_y"], f"{label}.overlay_y", minimum=0)
        # An origin outside the output frame renders nothing at all -- ffmpeg
        # simply clips it away. That is a silent no-op, the same shape as the
        # unrendered caption record found in the probe.
        if overlay_x >= frame_w or overlay_y >= frame_h:
            raise OverlayError(f"{label}: origin ({overlay_x}, {overlay_y}) is outside the "
                               f"{frame_w}x{frame_h} output frame, so this overlay would render "
                               f"nothing")

        evidence_ids = decision["evidence_ids"]
        if (not isinstance(evidence_ids, list) or not evidence_ids
                or not all(isinstance(e, str) and e for e in evidence_ids)):
            raise OverlayError(f"{label}.evidence_ids: non-empty array of non-empty strings required")
        unresolved = [e for e in evidence_ids if e not in evidence_ranges]
        if unresolved:
            raise OverlayError(f"{label}: evidence not in the paired Index: {unresolved}")
        # The contract checker's action-evidence rule applies here.
        segment_range = range_fractions(segments[segment_id].get("source_range"),
                                        f"segment {segment_id}.source_range")
        stray = [e for e in evidence_ids if not overlaps(segment_range, evidence_ranges[e])]
        if stray:
            raise OverlayError(f"{label}: evidence {stray} does not overlap segment "
                               f"{segment_id!r}; the contract checker's action-evidence rejects that")

        action_id = require_text(decision.get("action_id") or f"a-overlay-{position:04d}",
                                 f"{label}.action_id")
        if action_id in existing_action_ids:
            raise OverlayError(f"{label}: action_id {action_id!r} is already in the plan")
        existing_action_ids.add(action_id)

        actions.append({
            "action_id": action_id,
            "action": "act_overlay_asset",
            "v1_executable": "yes(effect-verified)",
            "verification_kind": "effect_measurement",
            "source_segment_id": segment_id,
            "asset_id": asset_id,
            "evidence_ids": list(evidence_ids),
            # executor/assemble.py:343-359 -- exactly these four keys.
            "params": {"overlay_x": overlay_x, "overlay_y": overlay_y,
                       "enable_axis": "final_timeline",
                       "timeline_range": copy.deepcopy(decision["timeline_range"])},
        })
        applied.append({
            "action_id": action_id, "asset_id": asset_id, "source_segment_id": segment_id,
            "overlay": {"x": overlay_x, "y": overlay_y},
            "pixel_size": {"width": asset.get("pixel_width"), "height": asset.get("pixel_height")},
            "evidence_ids": list(evidence_ids), "decided_by": decided_by,
            "timestamp": timestamp, "rationale": rationale,
        })

    report = {
        "schema_version": "draftcut-overlay-decisions-report/v1",
        "plan_id": out.get("plan_id"),
        "mechanism": ("Records the asset, position, and active timeline range for each overlay."),
        "not_done": ("this tool does not decide what to cover, where or for how long (D-84); does "
                     "not invent an approval; and does not check that a mask covers what it should "
                     "-- run skills/mask-plan/coverage_gate.py after this"),
        "no_binding_rule": ("overlays carry no caption-style binding: L2 does not read their "
                           "source_segment_id and one segment legitimately carries many (D-96)"),
        "human_provenance_location": "report only -- the action record has no slot for it",
        "asset_map_verified": asset_map is not None,
        "render_guarantee": ("Referenced asset files and hashes were verified; full rendering was not tested." if asset_map is not None
                             else "NOT verified: without --asset-map this plan is not shown to "
                                  "render; L2 requires an asset map at render time"),
        "assets_registered": registered,
        "assets_verified": verified,
        "overlays_recorded": len(applied),
        "applied": applied,
    }
    return out, report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", required=True, type=Path, help="merged EditPlan JSON")
    parser.add_argument("--index", required=True, type=Path, help="paired Index JSON")
    parser.add_argument("--overlays", required=True, type=Path,
                        help="JSON array of overlay decisions, one object per overlay with keys: "
                             + ", ".join(sorted(DECISION_REQUIRED))
                             + "; plus " + ", ".join(sorted(ASSET_DECLARATION))
                             + " when the asset is not already in the plan")
    parser.add_argument("--asset-map", type=Path,
                        help="asset_id -> real path map; without it the asset files and their "
                             "SHA-256 are NOT verified and the plan is not shown to render")
    parser.add_argument("--out-plan", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        index = json.loads(args.index.read_text(encoding="utf-8"))
        decisions = json.loads(args.overlays.read_text(encoding="utf-8"))
        if not isinstance(decisions, list) or not decisions:
            raise OverlayError("overlays: non-empty JSON array required")
        asset_map = None
        if args.asset_map is not None:
            asset_map = json.loads(args.asset_map.read_text(encoding="utf-8"))
            if not isinstance(asset_map, dict):
                raise OverlayError("asset map: JSON object required")
        out, report = record(plan, index, decisions, asset_map)
    except (OverlayError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        print(f"OVERLAY RECORD ERROR: {exc}", file=sys.stderr)
        return 2
    args.out_plan.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.out_plan.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"status": "recorded", "plan": str(args.out_plan), "report": str(args.report),
                      "overlays_recorded": report["overlays_recorded"],
                      "assets_registered": len(report["assets_registered"]),
                      "asset_map_verified": report["asset_map_verified"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
