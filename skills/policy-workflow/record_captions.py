#!/usr/bin/env python3
"""Record a human's caption decisions into a merged EditPlan.

This is the fourth piece of the caption slot, and it is a RECORDER, not a
generator. It records the following declarations and checks:

  * -- captions are input and placement, not generation. The contract
    rejects an empty caption slot (`captions[].text` is required with
    `minLength: 1`), so L3 has nothing to recommend and keeps emitting
    `captions: []`. There is no `action_id` anchor to hang a human decision on,
    the way `act_speed_change` anchors the speed slot.
  * -- this runs AFTER the merge. `merge_plans.py` deliberately rejects a
    non-empty `captions` on its inputs, so captions arrive here, downstream.
  * + the contract -- the caption/clip binding is closed by BACK-REFERENCE, inventing no
    field: caption_id -> act_add_caption_segment.params.caption_id ->
    action.source_segment_id -> timeline_clips[].source_segment_id. Two weak
    points must both be bitten: that back-reference must be unique, and one
    segment must resolve to exactly one clip unless its source-range reuse is
    explicitly declared non-contiguous.
  * -- `evidence_ids` stays provenance. It is NOT the slot for "which
    event this caption explains": evidence IDs are not globally unique,
    and signal/human evidence can describe a range rather than a single instant.
  * -- the contract does not carry a caption KIND (label vs narration).
    Nothing in the contract checker, L2 or L4 branches on it, so it lives in the tool input and in
    which skill produced the text.
  * -- the tool DOES NOT COMPUTE THE TIME. The human gives the caption's
    timeline range; it is not inferred from a screen-activity marker.

So the human gives the wording, the target segment, and the timing. This tool
writes them down, and refuses anything it cannot verify.

WHY IT REFUSES SO MUCH: passing the schema is not the same as rendering
`--validate-only` looks at none of the asset map, the real font
file, its SHA-256, the PNG renderer or ffmpeg. L2 demands more than the schema
does, and this tool brings those rejections forward to recording time:

  * `params` is exactly {caption_id, overlay_x, overlay_y, enable_axis}
    (`executor/assemble.py:328-342`) -- four keys, no more, no less.
  * exactly one add action per caption_id, and one captions[] record per action
    (`executor/assemble.py:445,462,470`).
  * `text` and `style_ref` non-empty, the style resolving to exactly one
    caption style and its font to exactly one asset
    (`executor/assemble.py:480-499`).
  * `timeline_range` non-negative and non-empty (`executor/assemble.py:500-503`).
  * `evidence_ids` existing in the paired Index and overlapping the action's
    source segment -- what the contract checker's `action-evidence` rule
    requires.

With `--asset-map` it also does what nothing else in the plan layer does: it
resolves the font asset to a real file on disk and re-measures its SHA-256. The
font path lives OUTSIDE the plan (in the asset map), which is why a
plan alone can never prove it will render. Without the flag the report says so
in plain words rather than implying a render guarantee.

WHAT THIS TOOL DOES NOT DO:
  * It does not choose the time.
  * It does not choose the target segment. A caption can span cuts; time overlap
    alone does not decide the intended source segment.
  * It does not invent text, style, rationale or evidence.
  * It does not emit `act_style_caption_segment`; `captions[].style_ref` binds
    the declared style, and L2 reads that.
  * It does not write the human's rationale/timestamp into the plan. The
    caption record is `additionalProperties: false` with no slot for it, so that
    provenance goes to the report instead. (no contract change) applies.

Standard library only.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

# The human's decision file. Closed on purpose: an unknown key is a typo or a
# field this tool would silently drop, and either way the human should hear it.
DECISION_REQUIRED = {
    "caption_id", "text", "language", "speaker", "style_ref", "safe_area",
    "review_status", "track_id", "source_segment_id", "timeline_range",
    "overlay_x", "overlay_y", "evidence_ids", "decided_by", "timestamp", "rationale",
}
DECISION_OPTIONAL = {"action_id"}
# The contract's caption record, in contract order.
CAPTION_FIELDS = ("caption_id", "text", "language", "speaker", "style_ref",
                  "safe_area", "review_status", "track_id", "timeline_range")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class CaptionError(RuntimeError):
    pass


def require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CaptionError(f"{label}: non-empty string required")
    return value


def require_int(value: Any, label: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CaptionError(f"{label}: integer required (got {value!r})")
    if minimum is not None and value < minimum:
        raise CaptionError(f"{label}: must be >= {minimum} (got {value})")
    return value


def time_fraction(value: Any, label: str) -> Fraction:
    if not isinstance(value, dict):
        raise CaptionError(f"{label}: object with value/timebase required")
    if set(value) != {"value", "timebase"}:
        raise CaptionError(f"{label}: exactly {{value, timebase}} required (got {sorted(value)})")
    ticks = require_int(value.get("value"), f"{label}.value", minimum=0)
    timebase = value.get("timebase")
    if not isinstance(timebase, dict) or set(timebase) != {"num", "den"}:
        raise CaptionError(f"{label}.timebase: exactly {{num, den}} required")
    num = require_int(timebase.get("num"), f"{label}.timebase.num", minimum=1)
    den = require_int(timebase.get("den"), f"{label}.timebase.den", minimum=1)
    return Fraction(ticks) * Fraction(num, den)


def range_fractions(value: Any, label: str) -> tuple[Fraction, Fraction]:
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise CaptionError(f"{label}: exactly {{start, end}} required")
    start = time_fraction(value.get("start"), f"{label}.start")
    end = time_fraction(value.get("end"), f"{label}.end")
    # executor/assemble.py:500-503 -- L2 rejects an empty or negative range.
    if end <= start:
        raise CaptionError(f"{label}: end must be after start ({end} <= {start})")
    return start, end


def overlaps(a: tuple[Fraction, Fraction], b: tuple[Fraction, Fraction]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def index_evidence_ranges(index: dict[str, Any]) -> dict[str, tuple[Fraction, Fraction]]:
    out: dict[str, tuple[Fraction, Fraction]] = {}
    for item in index.get("evidence", []):
        if not isinstance(item, dict) or not isinstance(item.get("evidence_id"), str):
            continue
        try:
            out[item["evidence_id"]] = range_fractions(item.get("range"),
                                                       f"index evidence {item['evidence_id']}.range")
        except CaptionError:
            # A malformed Index is the contract checker's job to reject, not this tool's; skipping
            # here means the evidence simply does not resolve, which we do report.
            continue
    return out


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_font_file(asset: dict[str, Any], asset_map: dict[str, Any]) -> str:
    """Resolve one font asset to a real file and re-measure its hash.

    The font path is not in the plan: it is in the asset
    map, which has no schema of its own. L2 re-measures the SHA-256 at render
    time (`executor/assemble.py:519-558`); doing it here means a recorded plan
    that will not render fails now instead of at ffmpeg.
    """
    asset_id = asset.get("asset_id")
    path_value = asset_map.get(asset_id)
    if not isinstance(path_value, str) or not path_value:
        raise CaptionError(f"asset map has no path for font asset {asset_id!r}; "
                           f"the plan alone cannot prove this caption will render")
    path = Path(path_value)
    if not path.is_file():
        raise CaptionError(f"font asset {asset_id!r} maps to {path_value!r}, which is not a file")
    declared = asset.get("sha256")
    if not isinstance(declared, str) or not SHA256_RE.match(declared):
        raise CaptionError(f"font asset {asset_id!r}.sha256 must be 64 lowercase hex characters")
    measured = sha256_of(path)
    if measured != declared:
        raise CaptionError(f"font asset {asset_id!r}: declared sha256 {declared} but "
                           f"{path_value} measures {measured}")
    return path_value


def record(plan: dict[str, Any], index: dict[str, Any], decisions: list[dict[str, Any]],
           asset_map: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    out = copy.deepcopy(plan)
    captions = out.setdefault("captions", [])
    actions = out.setdefault("actions", [])
    styles = out.get("caption_styles", [])
    assets = out.get("assets", [])
    tracks = out.get("tracks", [])
    segments = {s.get("segment_id"): s for s in out.get("source_segments", [])
                if isinstance(s, dict)}
    clips = [c for c in out.get("timeline_clips", []) if isinstance(c, dict)]
    evidence_ranges = index_evidence_ranges(index)

    if not isinstance(captions, list) or not isinstance(actions, list):
        raise CaptionError("plan.captions and plan.actions must be arrays")

    existing_caption_ids = {c.get("caption_id") for c in captions if isinstance(c, dict)}
    existing_action_ids = {a.get("action_id") for a in actions if isinstance(a, dict)}
    # executor/assemble.py:445 -- two add actions on one caption_id is a rejection.
    bound_caption_ids = {a.get("params", {}).get("caption_id")
                         for a in actions if isinstance(a, dict)
                         and a.get("action") == "act_add_caption_segment"
                         and isinstance(a.get("params"), dict)}

    applied: list[dict[str, Any]] = []
    fonts_verified: dict[str, str] = {}
    seen_ids: set[str] = set()

    for position, decision in enumerate(decisions, start=1):
        label = f"captions[{position}]"
        if not isinstance(decision, dict):
            raise CaptionError(f"{label}: object required")
        keys = set(decision)
        missing = DECISION_REQUIRED - keys
        if missing:
            raise CaptionError(f"{label}: missing required keys {sorted(missing)}. "
                               f"This tool does not invent text, timing, rationale or evidence")
        unknown = keys - DECISION_REQUIRED - DECISION_OPTIONAL
        if unknown:
            raise CaptionError(f"{label}: unknown keys {sorted(unknown)}")

        caption_id = require_text(decision["caption_id"], f"{label}.caption_id")
        if caption_id in seen_ids:
            raise CaptionError(f"{label}: caption_id {caption_id!r} appears twice in the decisions")
        if caption_id in existing_caption_ids or caption_id in bound_caption_ids:
            raise CaptionError(f"{label}: caption_id {caption_id!r} is already in the plan")
        seen_ids.add(caption_id)

        text = require_text(decision["text"], f"{label}.text")
        language = require_text(decision["language"], f"{label}.language")
        speaker = require_text(decision["speaker"], f"{label}.speaker")
        style_ref = require_text(decision["style_ref"], f"{label}.style_ref")
        safe_area = require_text(decision["safe_area"], f"{label}.safe_area")
        review_status = require_text(decision["review_status"], f"{label}.review_status")
        track_id = require_text(decision["track_id"], f"{label}.track_id")
        segment_id = require_text(decision["source_segment_id"], f"{label}.source_segment_id")
        # Human provenance. It has no home in the contract, so it is
        # required here and written to the report, never invented.
        decided_by = require_text(decision["decided_by"], f"{label}.decided_by")
        timestamp = require_text(decision["timestamp"], f"{label}.timestamp")
        rationale = require_text(decision["rationale"], f"{label}.rationale")

        matching_tracks = [t for t in tracks if isinstance(t, dict) and t.get("track_id") == track_id]
        if len(matching_tracks) != 1:
            raise CaptionError(f"{label}.track_id {track_id!r} must resolve to exactly one track")
        if matching_tracks[0].get("kind") != "captions":
            raise CaptionError(f"{label}.track_id {track_id!r} is a "
                               f"{matching_tracks[0].get('kind')!r} track, not 'captions'")

        # executor/assemble.py:490-499 -- style and font must each resolve to exactly one.
        matching_styles = [s for s in styles if isinstance(s, dict) and s.get("style_id") == style_ref]
        if len(matching_styles) != 1:
            raise CaptionError(f"{label}.style_ref {style_ref!r} must resolve to exactly one "
                               f"caption style")
        font_asset_id = matching_styles[0].get("font_asset_id")
        matching_assets = [a for a in assets if isinstance(a, dict) and a.get("asset_id") == font_asset_id]
        if not isinstance(font_asset_id, str) or len(matching_assets) != 1:
            raise CaptionError(f"{label}: caption style {style_ref!r}.font_asset_id must resolve "
                               f"to exactly one asset")
        font_asset = matching_assets[0]
        if font_asset.get("approval_status") != "approved":
            raise CaptionError(f"{label}: font asset {font_asset_id!r} is not approved; "
                               f"the PNG renderer is fail-closed on approval")
        if asset_map is not None and font_asset_id not in fonts_verified:
            fonts_verified[font_asset_id] = verify_font_file(font_asset, asset_map)

        # back-reference, both weak points. the contract is the narrow
        # exception: a declared non-contiguous source-range reuse deliberately
        # names more than one clip without making the target ambiguous.
        if segment_id not in segments:
            raise CaptionError(f"{label}.source_segment_id {segment_id!r} is not in "
                               f"plan.source_segments")
        segment_clips = [c for c in clips if c.get("source_segment_id") == segment_id]
        segment = segments[segment_id]
        try:
            segment_range = range_fractions(segment.get("source_range"), f"segment {segment_id}.source_range")
        except CaptionError:
            segment_range = None
        declared_noncontiguous_repeat = any(
            isinstance(action, dict)
            and action.get("action") == "act_repeat_segment"
            and isinstance(action.get("params"), dict)
            and action["params"] == {"contiguous": False}
            and isinstance(segments.get(action.get("source_segment_id")), dict)
            and segments[action["source_segment_id"]].get("source_id") == segment.get("source_id")
            and segment_range is not None
            # The contract checks positive source-range overlap, not identity:
            # a repeated range can overlap a longer segment without matching it.
            and (lambda declared: min(declared[1], segment_range[1]) - max(declared[0], segment_range[0]) > 0)(
                range_fractions(segments[action["source_segment_id"]].get("source_range"), "repeat source_range"))
            for action in actions
        )
        if len(segment_clips) != 1 and not declared_noncontiguous_repeat:
            raise CaptionError(f"{label}: segment {segment_id!r} resolves to "
                               f"{len(segment_clips)} timeline clips; the caption/clip binding "
                               f"requires exactly one (D-90)")

        start, end = range_fractions(decision["timeline_range"], f"{label}.timeline_range")
        overlay_x = require_int(decision["overlay_x"], f"{label}.overlay_x", minimum=0)
        overlay_y = require_int(decision["overlay_y"], f"{label}.overlay_y", minimum=0)

        evidence_ids = decision["evidence_ids"]
        if (not isinstance(evidence_ids, list) or not evidence_ids
                or not all(isinstance(e, str) and e for e in evidence_ids)):
            raise CaptionError(f"{label}.evidence_ids: non-empty array of non-empty strings required")
        unresolved = [e for e in evidence_ids if e not in evidence_ranges]
        if unresolved:
            raise CaptionError(f"{label}: evidence not in the paired Index: {unresolved}")
        # The contract checker's action-evidence rule applies here.
        segment_range = range_fractions(segments[segment_id].get("source_range"),
                                        f"segment {segment_id}.source_range")
        stray = [e for e in evidence_ids if not overlaps(segment_range, evidence_ranges[e])]
        if stray:
            raise CaptionError(f"{label}: evidence {stray} does not overlap segment "
                               f"{segment_id!r}; the contract checker's action-evidence rejects that")

        action_id = decision.get("action_id") or f"a-caption-{caption_id}"
        action_id = require_text(action_id, f"{label}.action_id")
        if action_id in existing_action_ids:
            raise CaptionError(f"{label}: action_id {action_id!r} is already in the plan")
        existing_action_ids.add(action_id)

        captions.append({
            "caption_id": caption_id, "text": text, "language": language, "speaker": speaker,
            "style_ref": style_ref, "safe_area": safe_area, "review_status": review_status,
            "track_id": track_id, "timeline_range": copy.deepcopy(decision["timeline_range"]),
        })
        actions.append({
            "action_id": action_id,
            "action": "act_add_caption_segment",
            "v1_executable": "yes(effect-verified)",
            "verification_kind": "effect_measurement",
            "source_segment_id": segment_id,
            "evidence_ids": list(evidence_ids),
            # executor/assemble.py:328-342 -- exactly these four keys.
            "params": {"caption_id": caption_id, "overlay_x": overlay_x,
                       "overlay_y": overlay_y, "enable_axis": "final_timeline"},
        })
        existing_caption_ids.add(caption_id)
        bound_caption_ids.add(caption_id)
        applied.append({
            "caption_id": caption_id, "action_id": action_id, "source_segment_id": segment_id,
            "clip_id": segment_clips[0].get("clip_id"), "style_ref": style_ref,
            "font_asset_id": font_asset_id, "evidence_ids": list(evidence_ids),
            "decided_by": decided_by, "timestamp": timestamp, "rationale": rationale,
        })

    report = {
        "schema_version": "draftcut-caption-decisions-report/v1",
        "plan_id": out.get("plan_id"),
        "mechanism": ("Links each caption to its add action and target timeline clip."),
        "not_done": ("this tool does not compute caption timing (D-95: a fixed marker endpoint "
                     "reproduces 3 of 7 and 4 of 7 reference captions with up to 4.73s residual error), "
                     "does not choose the target segment (7 of the canonical 8 captions overlap "
                     "two or three clips), and does not emit act_style_caption_segment"),
        "human_provenance_location": ("Reviewer metadata is stored in this report because the caption record has no fields for it."),
        "asset_map_verified": asset_map is not None,
        "render_guarantee": ("Referenced font files and hashes were verified; full rendering was not tested." if asset_map is not None
                             else "NOT verified: without --asset-map this plan is not shown to "
                                  "render; --validate-only checks no asset map, font file, "
                                  "SHA-256, PNG renderer or ffmpeg"),
        "fonts_verified": fonts_verified,
        "captions_recorded": len(applied),
        "captions_total": len(captions),
        "applied": applied,
    }
    return out, report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", required=True, type=Path, help="merged EditPlan JSON")
    parser.add_argument("--index", required=True, type=Path, help="paired Index JSON")
    parser.add_argument("--captions", required=True, type=Path,
                        help="JSON array of caption decisions, one object per caption with keys: "
                             + ", ".join(sorted(DECISION_REQUIRED)))
    parser.add_argument("--asset-map", type=Path,
                        help="asset_id -> real path map; without it the font file and its "
                             "SHA-256 are NOT verified and the plan is not shown to render")
    parser.add_argument("--out-plan", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        index = json.loads(args.index.read_text(encoding="utf-8"))
        decisions = json.loads(args.captions.read_text(encoding="utf-8"))
        if not isinstance(decisions, list) or not decisions:
            raise CaptionError("captions: non-empty JSON array required")
        asset_map = None
        if args.asset_map is not None:
            asset_map = json.loads(args.asset_map.read_text(encoding="utf-8"))
            if not isinstance(asset_map, dict):
                raise CaptionError("asset map: JSON object required")
        out, report = record(plan, index, decisions, asset_map)
    except (CaptionError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        print(f"CAPTION RECORD ERROR: {exc}", file=sys.stderr)
        return 2
    args.out_plan.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.out_plan.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"status": "recorded", "plan": str(args.out_plan),
                      "report": str(args.report),
                      "captions_recorded": report["captions_recorded"],
                      "captions_total": report["captions_total"],
                      "asset_map_verified": report["asset_map_verified"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
