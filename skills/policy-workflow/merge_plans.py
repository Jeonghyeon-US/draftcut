#!/usr/bin/env python3
"""Merge an ordered list of single-source draft EditPlans into one multi-source draft plan.

This tool does not approve the draft, choose speed settings, or verify
media compatibility."""
from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
L2_EXECUTION_SCOPE_PATH = ROOT / "contracts" / "l2-execution-scope.json"
L2_EXECUTION_SCOPE_DECLARATION = "contracts/l2-execution-scope.json"
ACTION_EXECUTABILITY_PATH = ROOT / "contracts" / "action-executability.json"
FORBIDDEN_ACTIONS_SOURCE = "draftcut-l4-project-definition/v1"
FORBIDDEN_ACTIONS_NOTICE = "This is a name-set comparison only: it does not judge whether the merged plan realises the scenario, and passing it does not mean the edit suits the scenario."


class MergeError(ValueError):
    """A project definition or its inputs cannot be safely merged."""


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MergeError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MergeError(f"{label} {path}: JSON root must be an object")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def l2_dryrun(plan_path: Path) -> dict[str, Any]:
    """Run the public L2 plan-only CLI and retain its boundary report.

    This is intentionally reporting-only: L4's successfully written outputs do
    not become invalid merely because a downstream executor cannot accept them.
    """
    entrypoint = "executor/assemble.py --validate-only"
    notice = (
        "A passing dry run does not mean the plan will render. "
        "If stages_not_reached is non-empty, fix the plan and run the dry run again."
    )
    try:
        assemble = Path(__file__).resolve().parents[2] / "executor" / "assemble.py"
        completed = subprocess.run(
            [sys.executable, str(assemble), "--plan", str(plan_path), "--validate-only"],
            text=True,
            capture_output=True,
        )
    except Exception as exc: # noqa: BLE001
        # -(f): the merge outputs are already written and remain valid. A
        # reporting tool must never turn a successful merge into a failure, so
        # every way this call can fail is recorded rather than raised.
        return {"entrypoint": entrypoint, "status": "unavailable",
                "error": f"{type(exc).__name__}: {exc}",
                "errors": [], "stages_not_reached": [], "notice": notice}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        error = f"dry-run stdout was not JSON (exit {completed.returncode}): {exc}"
        if completed.stderr.strip():
            error += f"; stderr: {completed.stderr.strip()}"
        return {
            "entrypoint": entrypoint,
            "status": "unavailable",
            "error": error,
            "errors": [],
            "stages_not_reached": [],
            "notice": notice,
        }
    if not isinstance(payload, dict):
        return {"entrypoint": entrypoint, "status": "unavailable",
                "error": "dry-run stdout JSON root was not an object", "errors": [],
                "stages_not_reached": [], "notice": notice}
    raw_status = payload.get("status")
    if raw_status == "validated":
        status = "ok"
    elif raw_status == "rejected":
        status = "rejected"
    else:
        return {"entrypoint": entrypoint, "status": "unavailable",
                "error": f"dry-run returned unknown status: {raw_status!r}", "errors": [],
                "stages_not_reached": [], "notice": notice}
    result: dict[str, Any] = {
        "entrypoint": entrypoint,
        "status": status,
        "error": payload.get("error"),
        "errors": payload.get("errors", []),
        # executor emits this only for a rejected plan. A successful run has
        # no unreached stage, which L4 records explicitly for a stable report.
        "stages_not_reached": payload.get("stages_not_reached", []),
        "notice": notice,
    }
    # Do not invent a not_checked list for rejected output: executor does not
    # emit one there. When it does emit the field, preserve it verbatim.
    if "not_checked" in payload:
        result["not_checked"] = payload["not_checked"]
    return result


def fraction_time(value: Any, label: str) -> Fraction:
    if not isinstance(value, dict) or set(value) != {"value", "timebase"}:
        raise MergeError(f"{label}: canonical time must contain value and timebase")
    tick, base = value.get("value"), value.get("timebase")
    if type(tick) is not int or not isinstance(base, dict) or set(base) != {"num", "den"}:
        raise MergeError(f"{label}: invalid canonical time")
    numerator, denominator = base.get("num"), base.get("den")
    if type(numerator) is not int or type(denominator) is not int or numerator <= 0 or denominator <= 0:
        raise MergeError(f"{label}: timebase must be a positive integer rational")
    return Fraction(tick * numerator, denominator)


def canonical_time(value: Fraction) -> dict[str, Any]:
    if value < 0:
        raise MergeError("cannot emit a negative timeline time")
    return {"value": value.numerator, "timebase": {"num": 1, "den": value.denominator}}


def range_times(value: Any, label: str) -> tuple[Fraction, Fraction]:
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise MergeError(f"{label}: range must contain start and end")
    start = fraction_time(value["start"], f"{label}.start")
    end = fraction_time(value["end"], f"{label}.end")
    if end <= start:
        raise MergeError(f"{label}: range must be non-empty")
    return start, end


def shifted_range(value: Any, offset: Fraction, label: str) -> dict[str, Any]:
    start, end = range_times(value, label)
    return {"start": canonical_time(start + offset), "end": canonical_time(end + offset)}


def require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise MergeError(f"{label}: non-empty string required")
    return value


def read_l2_execution_scope(path: Path) -> tuple[set[str], str]:
    """Read the declared L2 dispatch-name boundary needed for L4 reporting."""
    declaration = read_json(path, "L2 execution scope declaration")
    actions = declaration.get("actions")
    if not isinstance(actions, list) or not all(isinstance(action, str) and action for action in actions):
        raise MergeError(f"L2 execution scope declaration {path}: actions must be a non-empty array of non-empty action names")
    if len(actions) != len(set(actions)):
        raise MergeError(f"L2 execution scope declaration {path}: actions must not contain duplicates")
    notice = declaration.get("dispatch_boundary_notice")
    if not isinstance(notice, str):
        raise MergeError(f"L2 execution scope declaration {path}: dispatch_boundary_notice must be a string")
    return set(actions), notice


def read_action_executability_actions(path: Path) -> set[str]:
    """Read the authoritative action-name set for project forbidden-actions declarations."""
    declaration = read_json(path, "action executability declaration")
    entries = declaration.get("entries")
    if not isinstance(entries, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("action"), str) and entry["action"]
        for entry in entries
    ):
        raise MergeError(f"action executability declaration {path}: entries must be an array of non-empty action names")
    actions = [entry["action"] for entry in entries]
    if len(actions) != len(set(actions)):
        raise MergeError(f"action executability declaration {path}: action names must not contain duplicates")
    return set(actions)


def json_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def unique_preserving(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        marker = json_key(value)
        if marker not in seen:
            result.append(copy.deepcopy(value))
            seen.add(marker)
    return result


def rewrite_known_references(value: Any, maps: dict[str, dict[str, str]]) -> Any:
    """Rewrite references, but never free text or source_id values.

    The allowed key set is intentional: IDs in prose/rationales are not contract
    references and must not be altered as a side effect of merging.
    """
    singular = {
        "evidence_id": "evidence",
        "source_segment_id": "segment",
        "segment_id": "segment",
        "track_id": "track",
        "clip_id": "clip",
        "action_id": "action",
        "marker_id": "marker",
    }
    plural = {
        "evidence_ids": "evidence",
        "reassembly_evidence_ids": "evidence",
        "ordered_segment_ids": "segment",
    }
    if isinstance(value, list):
        return [rewrite_known_references(item, maps) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in singular and isinstance(item, str):
            result[key] = maps[singular[key]].get(item, item)
        elif key == "lock_orders" and isinstance(item, list):
            mapping = maps["segment"]
            result[key] = [
                [mapping.get(entry, entry) if isinstance(entry, str) else rewrite_known_references(entry, maps)
                 for entry in group]
                if isinstance(group, list) else rewrite_known_references(group, maps)
                for group in item
            ]
        elif key in plural and isinstance(item, list):
            mapping = maps[plural[key]]
            result[key] = [mapping.get(entry, entry) if isinstance(entry, str) else rewrite_known_references(entry, maps)
                           for entry in item]
        else:
            result[key] = rewrite_known_references(item, maps)
    return result


def prefixed_map(items: Any, id_key: str, prefix: str, label: str) -> dict[str, str]:
    if not isinstance(items, list):
        raise MergeError(f"{label}: array required")
    mapping: dict[str, str] = {}
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise MergeError(f"{label}[{position}]: object required")
        old = require_string(item.get(id_key), f"{label}[{position}].{id_key}")
        if old in mapping:
            raise MergeError(f"{label}: duplicate ID {old!r} inside one input")
        mapping[old] = prefix + old
    return mapping


def material_paths(raw: dict[str, Any], project_path: Path) -> list[dict[str, Any]]:
    if raw.get("schema_version") != "draftcut-l4-project-definition/v1":
        raise MergeError("project.schema_version must be draftcut-l4-project-definition/v1")
    require_string(raw.get("project_id"), "project.project_id")
    materials = raw.get("materials")
    if not isinstance(materials, list) or not materials:
        raise MergeError("project.materials must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    for number, item in enumerate(materials, start=1):
        if not isinstance(item, dict) or set(item) != {"source_id", "plan", "index", "media_path"}:
            raise MergeError(f"project.materials[{number}]: exactly source_id, plan, index, media_path are required")
        source_id = require_string(item.get("source_id"), f"project.materials[{number}].source_id")
        if source_id in source_ids:
            raise MergeError(f"project.materials: duplicate source_id {source_id!r}")
        source_ids.add(source_id)
        paths: dict[str, Any] = {"source_id": source_id}
        for key in ("plan", "index", "media_path"):
            text = require_string(item.get(key), f"project.materials[{number}].{key}")
            path = Path(text)
            if key == "media_path" and not path.is_absolute():
                raise MergeError(f"project.materials[{number}].media_path must be absolute")
            if key != "media_path" and not path.is_absolute():
                path = project_path.parent / path
            paths[key] = path
        normalized.append(paths)
    return normalized


def project_forbidden_actions(raw: dict[str, Any]) -> list[str] | None:
    """Validate the optional project-level forbidden action-name declaration."""
    if "forbidden_actions" not in raw:
        return None
    actions = raw["forbidden_actions"]
    if not isinstance(actions, list):
        raise MergeError("project.forbidden_actions must be an array")
    if not actions:
        raise MergeError("project.forbidden_actions must not be empty")
    if not all(isinstance(action, str) and action for action in actions):
        raise MergeError("project.forbidden_actions must contain only non-empty strings")
    if len(actions) != len(set(actions)):
        raise MergeError("project.forbidden_actions must not contain duplicates")
    known_actions = read_action_executability_actions(ACTION_EXECUTABILITY_PATH)
    unknown_actions = sorted(set(actions) - known_actions)
    if unknown_actions:
        raise MergeError(f"project.forbidden_actions contains unknown action names: {unknown_actions}")
    return sorted(actions)


def validate_material(plan: dict[str, Any], index: dict[str, Any], source_id: str, label: str) -> None:
    if plan.get("schema_version") != "1.0" or index.get("schema_version") != "1.0":
        raise MergeError(f"{label}: plan and index schema_version must be 1.0")
    if plan.get("index_id") != index.get("index_id"):
        raise MergeError(f"{label}: plan.index_id must equal its input index.index_id")
    sources = index.get("sources")
    if not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict):
        raise MergeError(f"{label}: L4 v1 requires an input Index with exactly one source")
    if sources[0].get("source_id") != source_id:
        raise MergeError(f"{label}: project source_id does not match input Index source_id")
    segments = plan.get("source_segments")
    clips = plan.get("timeline_clips")
    if not isinstance(segments, list) or not isinstance(clips, list) or not clips:
        raise MergeError(f"{label}: source_segments and non-empty timeline_clips are required")
    if any(not isinstance(segment, dict) or segment.get("source_id") != source_id for segment in segments):
        raise MergeError(f"{label}: every source segment must belong to its declared source_id")
    # These ID-bearing Plan families are intentionally not assigned a v1 L4
    # merge rule. Silently concatenating them would make their references (and
    # caption tracks after M-1) invalid, which is worse than an explicit refusal.
    for key in ("assets", "reviews", "captions", "caption_styles", "cost_calls"):
        if plan.get(key):
            raise MergeError(f"{label}: non-empty {key} is outside the narrow L4 v1 merge scope")
    if any(isinstance(action, dict) and action.get("action") == "act_reorder_segments" for action in plan.get("actions", [])):
        raise MergeError(f"{label}: M-7 forbids input act_reorder_segments")
    starts_ends = [range_times(clip.get("timeline_range"), f"{label}.timeline_clips[{n}].timeline_range")
                   for n, clip in enumerate(clips, start=1) if isinstance(clip, dict)]
    if len(starts_ends) != len(clips):
        raise MergeError(f"{label}: timeline clips must be objects")
    if min(start for start, _ in starts_ends) != 0:
        raise MergeError(f"{label}: L4 v1 requires each input timeline to begin at exactly zero")


def merge_index(items: list[tuple[dict[str, Any], dict[str, str]]], index_id: str) -> dict[str, Any]:
    """Concatenate every list field, while retaining only the merged scalar identity."""
    merged: dict[str, Any] = {}
    all_keys = set().union(*(index.keys() for index, _ in items))
    for key in sorted(all_keys):
        if key in {"schema_version", "index_id"}:
            continue
        values = [index.get(key) for index, _ in items]
        if any(isinstance(value, list) for value in values):
            if not all(isinstance(value, list) for value in values):
                raise MergeError(f"Index field {key!r} is list-shaped in only some inputs")
            merged[key] = [entry for index, _ in items for entry in index[key]]
        elif all(value == values[0] for value in values):
            merged[key] = copy.deepcopy(values[0])
        else:
            raise MergeError(f"Index scalar field {key!r} differs across inputs and has no L4 v1 merge rule")
    merged["schema_version"] = "1.0"
    merged["index_id"] = index_id
    # Stable key order is useful for human review, while object member order remains immaterial.
    return {"schema_version": merged.pop("schema_version"), "index_id": merged.pop("index_id"), **merged}


def input_cost_summary(plans: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [plan.get("cost_summary") for plan in plans]
    if not all(isinstance(summary, dict) for summary in summaries):
        raise MergeError("all input plans require cost_summary objects")
    totals = [summary.get("estimated_total") for summary in summaries]
    if not all(isinstance(total, dict) and type(total.get("amount")) in {int, float} for total in totals):
        raise MergeError("all input cost_summary.estimated_total.amount values must be numbers")
    currencies = {total.get("currency") for total in totals}
    if len(currencies) != 1 or not all(isinstance(currency, str) and currency for currency in currencies):
        raise MergeError("L4 v1 cannot aggregate estimated costs with different currencies")
    amount = sum((total["amount"] for total in totals), 0)
    return {"estimated_total": {"amount": amount, "currency": next(iter(currencies)),
                                "basis": "L4 v1 sum of input estimated totals; detailed input bases are in the project report"}}


def merge_project(project: dict[str, Any], project_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    declared_actions, dispatch_boundary_notice = read_l2_execution_scope(L2_EXECUTION_SCOPE_PATH)
    forbidden_actions = project_forbidden_actions(project)
    materials = material_paths(project, project_path)
    loaded: list[dict[str, Any]] = []
    policies: list[Any] = []
    rejection: list[str] = []
    for number, material in enumerate(materials, start=1):
        plan = read_json(material["plan"], f"material {number} plan")
        index = read_json(material["index"], f"material {number} index")
        validate_material(plan, index, material["source_id"], f"material {number} ({material['source_id']})")
        loaded.append({**material, "plan_data": plan, "index_data": index})
        policies.append(plan.get("output_policy"))
    if any(json_key(policy) != json_key(policies[0]) for policy in policies[1:]):
        rejection.append("M-6: input output_policy objects differ; no policy was selected")
    project_id = project["project_id"]
    report_base: dict[str, Any] = {
        "schema_version": "draftcut-l4-project-report/v1",
        "project_id": project_id,
        "status": "rejected" if rejection else "merged",
        "materials": [],
        "id_prefixes": [],
        "output_policy": {"all_equal": not rejection},
        "rejection_reasons": rejection,
    }
    if rejection:
        for number, item in enumerate(loaded, start=1):
            report_base["materials"].append({"order": number, "source_id": item["source_id"],
                                             "plan": str(item["plan"]), "index": str(item["index"]),
                                             "media_path": str(item["media_path"])})
        raise RejectedMerge(report_base)

    merged_index_id = f"{project_id}-merged-index"
    merged_plan_id = f"{project_id}-merged-plan"
    remapped_indexes: list[tuple[dict[str, Any], dict[str, str]]] = []
    remapped_plans: list[dict[str, Any]] = []
    cursor = Fraction(0)
    source_map: dict[str, Any] = {}
    all_backend_selections: list[Any] = []
    all_cost_calls: list[Any] = []
    for number, item in enumerate(loaded, start=1):
        prefix = f"p{number:02d}-"
        plan = copy.deepcopy(item["plan_data"])
        index = copy.deepcopy(item["index_data"])
        maps = {
            "evidence": prefixed_map(index.get("evidence"), "evidence_id", prefix, "index evidence"),
            "marker": prefixed_map(index.get("markers"), "marker_id", prefix, "index markers"),
            "segment": prefixed_map(plan.get("source_segments"), "segment_id", prefix, "source_segments"),
            "track": prefixed_map(plan.get("tracks"), "track_id", prefix, "tracks"),
            "clip": prefixed_map(plan.get("timeline_clips"), "clip_id", prefix, "timeline_clips"),
            "action": prefixed_map(plan.get("actions"), "action_id", prefix, "actions"),
        }
        index = rewrite_known_references(index, maps)
        plan = rewrite_known_references(plan, maps)
        # M-1: input tracks are intentionally replaced, rather than concatenated.
        for clip in plan["timeline_clips"]:
            clip["track_id"] = "v1"
            clip["timeline_range"] = shifted_range(clip["timeline_range"], cursor,
                                                    f"{item['source_id']} clip {clip.get('clip_id')}")
        ends = [range_times(clip["timeline_range"], "shifted timeline_range")[1] for clip in plan["timeline_clips"]]
        material_length = max(ends) - cursor
        if material_length <= 0:
            raise MergeError(f"{item['source_id']}: merged material timeline length must be positive")
        report_base["materials"].append({
            "order": number, "source_id": item["source_id"], "plan": str(item["plan"]), "index": str(item["index"]),
            "media_path": str(item["media_path"]), "timeline_offset": canonical_time(cursor),
            "timeline_length": canonical_time(material_length),
        })
        report_base["id_prefixes"].append({"order": number, "source_id": item["source_id"], "prefix": prefix,
                                           "categories": ["index_evidence", "index_marker", "source_segments", "tracks",
                                                          "timeline_clips", "actions"],
                                           "track_resolution": "each pNN-prefixed input track reference is replaced by the single M-1 track v1"})
        source_map[item["source_id"]] = str(item["media_path"])
        all_backend_selections.extend(plan.get("backend_selections", []))
        all_cost_calls.extend(plan.get("cost_calls", []))
        remapped_indexes.append((index, maps["evidence"]))
        remapped_plans.append(plan)
        cursor += material_length

    merged_index = merge_index(remapped_indexes, merged_index_id)
    base = copy.deepcopy(remapped_plans[0])
    list_keys = {"source_segments", "timeline_clips", "actions", "assets", "reviews", "captions", "caption_styles"}
    for key in list_keys:
        base[key] = [entry for plan in remapped_plans for entry in plan.get(key, [])]
    constraints = {"preserve": [], "lock_orders": [], "releases": []}
    for key in constraints:
        constraints[key] = [entry for plan in remapped_plans for entry in plan.get("constraints", {}).get(key, [])]
    base.update({
        "schema_version": "1.0", "plan_id": merged_plan_id, "index_id": merged_index_id,
        "stage": "draft", "tracks": [{"track_id": "v1", "kind": "video", "z_order": 0}],
        "constraints": constraints, "output_policy": copy.deepcopy(policies[0]),
        "concat_compatibility": {"verdict": "unverified", "spec_id": "draftcut-concat-copy-compatibility",
                                  "spec_version": "1", "evidence_ids": [], "review_id": None},
        "backend_selections": unique_preserving(all_backend_selections),
        "cost_summary": input_cost_summary(remapped_plans), "cost_calls": all_cost_calls,
        "reproducibility": {"ffmpeg_version": "unverified-at-L4-draft",
                              "filter_arguments": ["L4 v1 concatenates L3 timeline clips without changing edit actions"],
                              "loudnorm_two_pass": {"status": "not_applicable"},
                              "render_log_id": merged_plan_id, "output_sha256": None},
        "qc": {"pre_render": {"severity": "info", "state": "pending", "auto_retry": False},
               "post_render": {"severity": "info", "state": "pending", "auto_retry": False}},
    })
    if any(action.get("action") == "act_reorder_segments" for action in base["actions"] if isinstance(action, dict)):
        raise MergeError("M-7: merged actions must not contain act_reorder_segments")
    out_of_scope: dict[str, list[str]] = {}
    forbidden_violations: dict[str, list[str]] = {}
    for action in base["actions"]:
        if not isinstance(action, dict):
            continue
        action_name = action.get("action")
        if isinstance(action_name, str) and action_name not in declared_actions:
            out_of_scope.setdefault(action_name, []).append(action["action_id"])
        if forbidden_actions is not None and isinstance(action_name, str) and action_name in forbidden_actions:
            forbidden_violations.setdefault(action_name, []).append(action["action_id"])
    report_base["l2_execution_scope"] = {
        "declaration": L2_EXECUTION_SCOPE_DECLARATION,
        "declared_action_count": len(declared_actions),
        "out_of_scope_actions": [
            {"action": action_name, "count": len(action_ids), "action_ids": action_ids}
            for action_name, action_ids in sorted(out_of_scope.items())
        ],
        "notice": dispatch_boundary_notice,
    }
    report_base["forbidden_actions"] = {
        "declared": forbidden_actions,
        "source": FORBIDDEN_ACTIONS_SOURCE,
        "violations": [
            {"action": action_name, "count": len(action_ids), "action_ids": action_ids}
            for action_name, action_ids in sorted(forbidden_violations.items())
        ],
        "notice": FORBIDDEN_ACTIONS_NOTICE,
    }
    report_base["total_timeline_length"] = canonical_time(cursor)
    report_base["merged_plan_id"] = merged_plan_id
    report_base["merged_index_id"] = merged_index_id
    report_base["chosen_unsettled_fields"] = {
        "plan_id": "<project_id>-merged-plan",
        "cost_summary": "sum input estimated totals only when all currencies match; preserve input cost_calls",
        "reproducibility": "new draft-level placeholders, because this merger has not rendered an output",
        "qc": "new draft-level pending records with auto_retry false",
        "project_definition": "draftcut-l4-project-definition/v1; ordered materials with source_id, plan, index, media_path",
    }
    return base, merged_index, source_map, report_base


class RejectedMerge(MergeError):
    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__("; ".join(report["rejection_reasons"]))
        self.report = report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path, help="Project JSON containing project_id and ordered materials; see the policy-workflow README for the required format.")
    parser.add_argument("--out-plan", required=True, type=Path)
    parser.add_argument("--out-index", required=True, type=Path)
    parser.add_argument("--out-source-map", required=True, type=Path)
    parser.add_argument("--out-report", required=True, type=Path)
    args = parser.parse_args()
    try:
        project = read_json(args.project, "project definition")
        plan, index, source_map, report = merge_project(project, args.project)
        write_json(args.out_plan, plan)
        write_json(args.out_index, index)
        write_json(args.out_source_map, source_map)
        report["l2_dryrun"] = l2_dryrun(args.out_plan)
        write_json(args.out_report, report)
    except RejectedMerge as exc:
        write_json(args.out_report, exc.report)
        print(f"L4 MERGE REJECTED: {exc}", file=sys.stderr)
        return 2
    except MergeError as exc:
        # A malformed definition may not identify individual materials, but it is
        # still important that the requested external report records the refusal.
        write_json(args.out_report, {"schema_version": "draftcut-l4-project-report/v1", "status": "rejected",
                                     "rejection_reasons": [str(exc)]})
        print(f"L4 MERGE ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "merged", "plan": str(args.out_plan), "index": str(args.out_index),
                      "source_map": str(args.out_source_map), "report": str(args.out_report)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
