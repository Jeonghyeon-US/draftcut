#!/usr/bin/env python3
"""Record a human's speed-ramp decisions into an EditPlan's `constraints`.

This is the fourth piece of the speed slot (L4 merge design §2-2): the
product recommends (the draft plan's `act_speed_change` actions), the human looks
(`make_contact_sheet.py`), and this tool writes down what they decided.

 proved the mechanism is already in the contract, so nothing here
invents a field:

  * `constraints.preserve[]` marks a segment as needing human judgement. The contract checker then
    rejects any destructive action on it -- `act_speed_change` included -- unless a
    release exists-465`).
  * `constraints.releases[]` is that release: `released_by: "human"`, a timestamp,
    a rationale, and an `evidence_id` that must exist in the paired Index.

⛔ WHAT THIS TOOL DOES NOT DO: remove a speed action ("drop"). Removing one
changes the clip's own timeline duration -- The contract checker requires source and timeline
durations to match unless a speed action overlaps the range (mutation 28/29) --
and every later clip would have to shift. That is re-planning, not recording, and
nothing in the repo grounds a re-planning rule here. Dropping a segment belongs
to whatever re-runs L3/L4, and this tool refuses to pretend otherwise.

⚠️ The rationale and the evidence_id come FROM THE HUMAN. This tool will not
invent either; a decision missing them is rejected.

Standard library only.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

DESTRUCTIVE = {"act_trim_segment", "act_ripple_delete", "act_speed_change",
               "act_reorder_segments", "act_repeat_segment"}
DECISIONS = {"preserve", "release"}


class DecisionError(RuntimeError):
    pass


def require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecisionError(f"{label}: non-empty string required")
    return value


def index_evidence_ids(index: dict[str, Any]) -> set[str]:
    out = set()
    for item in index.get("evidence", []):
        if isinstance(item, dict) and isinstance(item.get("evidence_id"), str):
            out.add(item["evidence_id"])
    return out


def apply(plan: dict[str, Any], index: dict[str, Any],
          decisions: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    out = copy.deepcopy(plan)
    constraints = out.setdefault("constraints", {})
    preserve = constraints.setdefault("preserve", [])
    releases = constraints.setdefault("releases", [])
    constraints.setdefault("lock_orders", [])
    actions = {a.get("action_id"): a for a in out.get("actions", []) if isinstance(a, dict)}
    segment_ids = {s.get("segment_id") for s in out.get("source_segments", [])}
    evidence = index_evidence_ids(index)
    preserved = {item.get("segment_id") for item in preserve if isinstance(item, dict)}
    released = {item.get("segment_id") for item in releases if isinstance(item, dict)}
    applied: list[dict[str, Any]] = []

    for position, decision in enumerate(decisions, start=1):
        label = f"decisions[{position}]"
        if not isinstance(decision, dict):
            raise DecisionError(f"{label}: object required")
        kind = decision.get("decision")
        if kind not in DECISIONS:
            raise DecisionError(f"{label}.decision: one of {sorted(DECISIONS)} required "
                                f"(got {kind!r}). 'drop' is re-planning, not recording -- "
                                f"see the module docstring")
        action_id = require_text(decision.get("action_id"), f"{label}.action_id")
        action = actions.get(action_id)
        if action is None:
            raise DecisionError(f"{label}: no action {action_id!r} in the plan")
        if action.get("action") not in DESTRUCTIVE:
            raise DecisionError(f"{label}: {action_id} is {action.get('action')!r}, which is not "
                                f"destructive, so preserve/release does not apply to it")
        segment_id = action.get("source_segment_id")
        if segment_id not in segment_ids:
            raise DecisionError(f"{label}: {action_id} points at unknown segment {segment_id!r}")
        decision_evidence = [e for e in action.get("evidence_ids", []) if isinstance(e, str)]

        if kind == "preserve":
            if segment_id in preserved:
                raise DecisionError(f"{label}: segment {segment_id} is already preserved")
            ids = decision.get("evidence_ids") or decision_evidence
            if not ids:
                raise DecisionError(f"{label}: evidence_ids required (the action carries none)")
            missing = [e for e in ids if e not in evidence]
            if missing:
                raise DecisionError(f"{label}: evidence not in the paired Index: {missing}")
            preserve.append({"segment_id": segment_id, "evidence_ids": list(ids)})
            preserved.add(segment_id)
            applied.append({"decision": "preserve", "action_id": action_id,
                            "segment_id": segment_id, "evidence_ids": list(ids)})
        else:
            if segment_id not in preserved:
                raise DecisionError(f"{label}: segment {segment_id} must be preserved before it "
                                    f"can be released -- a release on an unpreserved segment "
                                    f"records nothing")
            if segment_id in released:
                raise DecisionError(f"{label}: segment {segment_id} is already released")
            rationale = require_text(decision.get("rationale"), f"{label}.rationale")
            timestamp = require_text(decision.get("timestamp"), f"{label}.timestamp")
            evidence_id = require_text(decision.get("evidence_id"), f"{label}.evidence_id")
            if evidence_id not in evidence:
                raise DecisionError(f"{label}.evidence_id {evidence_id!r} is not in the paired Index")
            releases.append({"segment_id": segment_id, "released_by": "human",
                             "timestamp": timestamp, "rationale": rationale,
                             "evidence_id": evidence_id})
            released.add(segment_id)
            applied.append({"decision": "release", "action_id": action_id,
                            "segment_id": segment_id, "evidence_id": evidence_id,
                            "timestamp": timestamp, "rationale": rationale})

    report = {
        "schema_version": "draftcut-speed-decisions-report/v1",
        "plan_id": out.get("plan_id"),
        "mechanism": "Records preservation constraints and human releases for the selected speed-change actions.",
        "not_done": ("dropping a speed action is re-planning, not recording -- it changes the "
                     "clip's timeline duration and shifts every later clip"),
        "decisions_applied": len(applied),
        "preserve_total": len(preserve),
        "releases_total": len(releases),
        "applied": applied,
    }
    return out, report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", required=True, type=Path, help="EditPlan JSON")
    parser.add_argument("--index", required=True, type=Path, help="paired Index JSON")
    parser.add_argument("--decisions", required=True, type=Path,
                        help='JSON array of {action_id, decision: "preserve"|"release", '
                             'rationale, timestamp, evidence_id}')
    parser.add_argument("--out-plan", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        index = json.loads(args.index.read_text(encoding="utf-8"))
        decisions = json.loads(args.decisions.read_text(encoding="utf-8"))
        if not isinstance(decisions, list) or not decisions:
            raise DecisionError("decisions: non-empty JSON array required")
        out, report = apply(plan, index, decisions)
    except (DecisionError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        print(f"SPEED DECISIONS ERROR: {exc}", file=sys.stderr)
        return 2
    args.out_plan.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.out_plan.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"status": "recorded", "plan": str(args.out_plan),
                      "report": str(args.report),
                      "decisions_applied": report["decisions_applied"],
                      "preserve_total": report["preserve_total"],
                      "releases_total": report["releases_total"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
