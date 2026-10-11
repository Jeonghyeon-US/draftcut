#!/usr/bin/env python3
"""Check an EditPlan, an Index or a skill output against the contracts.

    python3 validate_plan.py --plan plan.json --index index.json
    python3 validate_plan.py --contracts          # check the contracts themselves

Deterministic and standard-library only.  This is a stricter check than
`executor/assemble.py --validate-only`: it validates against the JSON Schema in
contracts/ and the structural rules built on top of it.  A plan can pass
--validate-only and still fail here, so run this before you render.
"""
import argparse
import ast
import json
import re
import sys
from collections import Counter
from fractions import Fraction
from pathlib import Path

def _find_root(start: Path) -> Path:
    """Walk up until the directory that holds contracts/. Works whether this file
    lives in the development tree or at the root of a distribution."""
    for candidate in (start, *start.parents):
        if (candidate / "contracts" / "edit-plan.schema.json").is_file():
            return candidate
    raise SystemExit("could not locate the repository root (no contracts/edit-plan.schema.json above this file)")


ROOT = _find_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:  # Development-record locations; absent in a distribution (see _dev_records).
    from _dev_records import CATALOG_RELPATH, PROBE_RELPATH
except ImportError:  # pragma: no cover - the distributed copy takes this path
    CATALOG_RELPATH = PROBE_RELPATH = None
RULES = (
    "schema-instance-validation", "action-registry-parity", "action-registry-probe-parity", "action-registry-probe-v1-executable",
    "l2-execution-scope-parity",
    "canonical-time-integer", "canonical-time-positive-base", "half-open-range", "index-source-id",
    "index-source-bound", "index-evidence-id", "index-evidence-bound", "index-marker-enum",
    "index-marker-evidence", "transcript-provenance", "transcript-confidence", "plan-index-id",
    "plan-segment-id", "plan-segment-bound", "timeline-clip-reference", "timeline-clip-order",
    "timeline-global-source-order", "action-id", "action-status", "action-verification-kind",
    "action-executable-gate", "action-evidence", "preserve-priority", "human-release-record",
    "lock-order", "asset-id", "asset-approved", "blocking-review-render-gate", "track-closed-kind",
    "caption-timeline-track", "caption-approved-raster", "reproducibility-slots", "qc-no-auto-retry",
    "skill-output-kind", "skill-output-scope-evidence", "dependency-missing-behaviour", "skill-review-blocking",
    "released-review-human-record", "asset-consuming-action-reference",
    "chunk-reassembly-consistency", "paid-backend-call-presence",
    "capture-pass-zero-off-target-frames", "capture-refilm-reason", "capture-frame-evidence",
    "concat-stage-verdict", "concat-review-reference", "concat-record-integrity",
    "caption-style-integrity", "action-params-integrity", "repeat-segment-declaration",
    "style-caption-declaration",
    "caption-clip-binding", "crossfade-audio-policy", "crossfade-join-index",
    "crossfade-join-unique", "crossfade-duration", "crossfade-window-nonoverlap",
    "audio-cleanup-audio-policy", "loudnorm-global", "loudnorm-two-pass",
    "audio-bed-audio-policy", "audio-bed-mix-pairing", "audio-bed-mix-inputs", "audio-bed-mix-policy",
    "audio-bed-timeline-start",
    "duck-audio-policy", "duck-targets", "duck-depth", "duck-ramp", "duck-window",
    "duck-window-nonoverlap", "duck-program-bounds", "duck-sample-grid",
    "freeze-frame-contract", "zoom-keyframes-contract", "blur-region-contract",
    "ripple-delete-declaration", "insert-segment-declaration",
)
MARKERS = {"transcript_segment", "shot_scene_boundary", "silence_interval", "loudness_peak", "onscreen_text", "sensitive_information_candidate", "behavior_reaction_candidate", "screen_activity"}
PROVENANCE = {"provided", "generated", "human_reviewed", "unconfirmed"}
ALLOWED_ACTION_STATUS = {"yes(effect-verified)", "yes(container-only)", "plan_only"}
DESTRUCTIVE = {"act_trim_segment", "act_ripple_delete", "act_speed_change", "act_reorder_segments", "act_repeat_segment"}
SCHEMA_KEYWORDS = {
    "$schema", "$id", "title", "description", "type", "required", "properties", "items",
    "minItems", "enum", "additionalProperties", "$defs", "$ref", "const", "pattern",
    "minimum", "maximum", "minLength", "allOf", "if", "then", "else", "not",
}
SCHEMA_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}

class Gate:
    def __init__(self): self.errors = []
    def require(self, rule_id, condition, message):
        if not condition: self.errors.append(message)

def static_rule_bindings():
    """Collect literal IDs attached to real checks without running their branches."""
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(Path(__file__)))
    bindings = Counter()
    errors = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "require"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "gate"
        ):
            continue
        if len(node.args) != 3 or node.keywords:
            errors.append(f"rule binding: line {node.lineno}: gate.require must use positional rule_id, condition, message")
            continue
        rule_id = node.args[0]
        if not isinstance(rule_id, ast.Constant) or not isinstance(rule_id.value, str):
            errors.append(f"rule binding: line {node.lineno}: gate.require rule_id must be a string literal")
            continue
        bindings[rule_id.value] += 1
    return bindings, errors

def validate_rule_bindings(gate, bindings, binding_errors):
    gate.errors.extend(binding_errors)
    declared = set(RULES)
    unbound = sorted(declared - set(bindings))
    undeclared = sorted(set(bindings) - declared)
    if unbound:
        gate.errors.append(f"rule binding: unbound declared RULES IDs: {', '.join(unbound)}")
    if undeclared:
        gate.errors.append(f"rule binding: undeclared gate.require IDs: {', '.join(undeclared)}")

def read_json(path):
    try:
        with Path(path).open(encoding="utf-8") as f: return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}")

def read_l2_execution_scope_actions(path):
    """Read the declared action-name boundary as a non-duplicated string set."""
    data = read_json(path)
    actions = data.get("actions")
    if not isinstance(actions, list) or not all(isinstance(action, str) and action for action in actions):
        raise ValueError("L2 execution scope: actions must be a non-empty array of non-empty action names")
    if len(actions) != len(set(actions)):
        raise ValueError("L2 execution scope: actions must not contain duplicates")
    return set(actions)

def read_executor_l2_dispatch_actions(path):
    """Read the public literal export without importing renderer dependencies.

    This is deliberately AST-only: the checker stays standard-library-only and mutation
    tests can pass a copied executor source without importing or executing it.
    """
    source_path = Path(path)
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    except (OSError, SyntaxError) as exc:
        raise ValueError(f"cannot read executor L2 dispatch export {source_path}: {exc}") from exc
    definitions = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "L2_DISPATCH_ACTIONS":
            definitions.append(node.value)
    if len(definitions) != 1:
        raise ValueError("executor L2 dispatch export: L2_DISPATCH_ACTIONS must have exactly one module-level definition")
    value = definitions[0]
    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "frozenset"
        and len(value.args) == 1
        and not value.keywords
        and isinstance(value.args[0], ast.Set)
    ):
        raise ValueError("executor L2 dispatch export: L2_DISPATCH_ACTIONS must be a literal frozenset")
    elements = value.args[0].elts
    if not all(isinstance(element, ast.Constant) and isinstance(element.value, str) and element.value for element in elements):
        raise ValueError("executor L2 dispatch export: L2_DISPATCH_ACTIONS must contain non-empty string literals")
    actions = [element.value for element in elements]
    if len(actions) != len(set(actions)):
        raise ValueError("executor L2 dispatch export: L2_DISPATCH_ACTIONS must not contain duplicates")
    return set(actions)

def validate_l2_execution_scope(gate, scope_path, executor_path):
    declared = read_l2_execution_scope_actions(scope_path)
    dispatched = read_executor_l2_dispatch_actions(executor_path)
    for action in sorted(declared - dispatched):
        gate.require(
            "l2-execution-scope-parity",
            False,
            f"L2 execution scope: declaration contains action not dispatched: {action}",
        )
    for action in sorted(dispatched - declared):
        gate.require(
            "l2-execution-scope-parity",
            False,
            f"L2 execution scope: dispatch contains action not declared: {action}",
        )

def json_path(path, part):
    encoded = str(part).replace("~", "~0").replace("/", "~1")
    return f"{path}/{encoded}" if path else f"/{encoded}"

def schema_error(label, path, message):
    return f"{label} schema: {path or '/'}: {message}"

def supported_schema(schema, schema_path="$"):
    """Reject schema keywords outside the small, explicit local validator."""
    if not isinstance(schema, dict):
        raise ValueError(f"schema {schema_path}: schema must be an object")
    unknown = set(schema) - SCHEMA_KEYWORDS
    if unknown:
        raise ValueError(f"schema {schema_path}: unsupported keyword(s): {', '.join(sorted(unknown))}")
    for keyword in ("$schema", "$id", "title", "description", "$ref", "pattern"):
        if keyword in schema and not isinstance(schema[keyword], str):
            raise ValueError(f"schema {schema_path}: {keyword} must be a string")
    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not kinds or any(kind not in SCHEMA_TYPES for kind in kinds):
            raise ValueError(f"schema {schema_path}: unsupported type declaration")
    if "required" in schema and (not isinstance(schema["required"], list) or any(not isinstance(key, str) for key in schema["required"])):
        raise ValueError(f"schema {schema_path}: required must be a string array")
    if "properties" in schema:
        if not isinstance(schema["properties"], dict):
            raise ValueError(f"schema {schema_path}: properties must be an object")
        for name, child in schema["properties"].items():
            supported_schema(child, f"{schema_path}.properties.{name}")
    if "items" in schema:
        supported_schema(schema["items"], f"{schema_path}.items")
    if "allOf" in schema:
        if not isinstance(schema["allOf"], list):
            raise ValueError(f"schema {schema_path}: allOf must be an array")
        for index, child in enumerate(schema["allOf"]):
            supported_schema(child, f"{schema_path}.allOf[{index}]")
    for keyword in ("if", "then", "else", "not"):
        if keyword in schema:
            supported_schema(schema[keyword], f"{schema_path}.{keyword}")
    if "$defs" in schema:
        if not isinstance(schema["$defs"], dict):
            raise ValueError(f"schema {schema_path}: $defs must be an object")
        for name, child in schema["$defs"].items():
            supported_schema(child, f"{schema_path}.$defs.{name}")
    if "additionalProperties" in schema and type(schema["additionalProperties"]) is not bool:
        raise ValueError(f"schema {schema_path}: additionalProperties must be boolean")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise ValueError(f"schema {schema_path}: enum must be an array")
    # minItems/minLength must be non-negative, but minimum/maximum must NOT be forced
    # non-negative: an earlier blanket guard left eight dB/LUFS parameters (gain_db,
    # target_i, target_tp, measured_I/LRA/TP/thresh, offset) with no bounds at all.
    for keyword in ("minItems", "minLength"):
        if keyword in schema and (type(schema[keyword]) not in {int, float} or schema[keyword] < 0):
            raise ValueError(f"schema {schema_path}: {keyword} must be a non-negative number")
    for keyword in ("minimum", "maximum"):
        if keyword in schema and type(schema[keyword]) not in {int, float}:
            raise ValueError(f"schema {schema_path}: {keyword} must be a number")
    if "minimum" in schema and "maximum" in schema and schema["minimum"] > schema["maximum"]:
        raise ValueError(f"schema {schema_path}: minimum must not exceed maximum")
    if schema.get("type") == "number" and ("minimum" not in schema or "maximum" not in schema):
        raise ValueError(f"schema {schema_path}: number schemas must bound both minimum and maximum")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except re.error as exc:
            raise ValueError(f"schema {schema_path}: invalid pattern: {exc}") from exc

def instance_type_matches(value, kind):
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": type(value) in {int, float},
        "integer": type(value) is int,
        "boolean": type(value) is bool,
        "null": value is None,
    }[kind]

def resolve_ref(root, ref):
    if not ref.startswith("#/$defs/"):
        raise ValueError(f"unsupported $ref: {ref}")
    name = ref.removeprefix("#/$defs/")
    target = root.get("$defs", {}).get(name)
    if target is None:
        raise ValueError(f"unresolved $ref: {ref}")
    return target

def validate_schema_instance(value, schema, root, label, path, errors):
    if "$ref" in schema:
        validate_schema_instance(value, resolve_ref(root, schema["$ref"]), root, label, path, errors)
    for child in schema.get("allOf", []):
        validate_schema_instance(value, child, root, label, path, errors)
    if "if" in schema:
        condition_errors = []
        validate_schema_instance(value, schema["if"], root, label, path, condition_errors)
        branch = "then" if not condition_errors else "else"
        if branch in schema:
            validate_schema_instance(value, schema[branch], root, label, path, errors)
    if "not" in schema:
        negated_errors = []
        validate_schema_instance(value, schema["not"], root, label, path, negated_errors)
        if not negated_errors:
            errors.append(schema_error(label, path, "must not match forbidden schema"))
    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(instance_type_matches(value, kind) for kind in kinds):
            errors.append(schema_error(label, path, f"type must be {' or '.join(kinds)}"))
            return
    if "const" in schema and value != schema["const"]:
        errors.append(schema_error(label, path, f"const must be {schema['const']!r}"))
    if "enum" in schema and value not in schema["enum"]:
        errors.append(schema_error(label, path, f"enum must be one of {schema['enum']!r}"))
    if type(value) in {int, float}:
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(schema_error(label, path, f"minimum {schema['minimum']}"))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(schema_error(label, path, f"maximum {schema['maximum']}"))
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(schema_error(label, path, f"minLength {schema['minLength']}"))
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(schema_error(label, path, f"pattern {schema['pattern']!r}"))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(schema_error(label, path, f"minItems {schema['minItems']}"))
        if "items" in schema:
            for index, item in enumerate(value):
                validate_schema_instance(item, schema["items"], root, label, json_path(path, index), errors)
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(schema_error(label, path, f"required property {key!r} is missing"))
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                validate_schema_instance(item, properties[key], root, label, json_path(path, key), errors)
            elif schema.get("additionalProperties") is False:
                errors.append(schema_error(label, json_path(path, key), "additional property is not allowed"))

def load_contract_schemas(gate):
    schemas = {}
    for filename in ("index.schema.json", "edit-plan.schema.json", "skill-output.schema.json"):
        schema = read_json(ROOT / "contracts" / filename)
        try:
            supported_schema(schema)
            schemas[filename] = schema
        except ValueError as exc:
            gate.errors.append(str(exc))
    return schemas

def validate_against_contract_schema(data, name, schemas, gate):
    schema = schemas.get(name)
    if schema is None:
        return False
    before = len(gate.errors)
    validate_schema_instance(data, schema, schema, name.removesuffix(".schema.json"), "", gate.errors)
    return len(gate.errors) == before

def ids(items, name, gate, key):
    values = [x.get(key) for x in items if isinstance(x, dict)]
    gate.require("index-source-id", len(values) == len(set(values)) and all(isinstance(x, str) and x for x in values), f"{name}: IDs must be unique non-empty strings")
    return set(values)

def time_value(obj, gate, where):
    gate.require("canonical-time-integer", isinstance(obj, dict) and set(obj) == {"value", "timebase"}, f"{where}: canonical time must contain only value and timebase")
    if not isinstance(obj, dict): return Fraction(0)
    value, base = obj.get("value"), obj.get("timebase")
    gate.require("canonical-time-integer", type(value) is int, f"{where}: time value must be an integer, never float")
    gate.require("canonical-time-positive-base", isinstance(base, dict) and set(base) == {"num", "den"}, f"{where}: timebase must contain num and den")
    if not isinstance(base, dict): return Fraction(0)
    num, den = base.get("num"), base.get("den")
    gate.require("canonical-time-positive-base", type(num) is int and type(den) is int and num > 0 and den > 0, f"{where}: timebase integers must be positive")
    if type(value) is not int or type(num) is not int or type(den) is not int or num <= 0 or den <= 0: return Fraction(0)
    return Fraction(value * num, den)

def range_value(obj, gate, where):
    gate.require("half-open-range", isinstance(obj, dict) and set(obj) == {"start", "end"}, f"{where}: range requires exactly start/end")
    if not isinstance(obj, dict): return Fraction(0), Fraction(0)
    start, end = time_value(obj.get("start"), gate, where + ".start"), time_value(obj.get("end"), gate, where + ".end")
    gate.require("half-open-range", start < end, f"{where}: must be a non-empty half-open range")
    return start, end

def ranges_overlap(left, right): return left[0] < right[1] and right[0] < left[1]

def validate_contract_files(gate, schemas):
    for name, data in schemas.items():
        gate.require("schema-instance-validation", data.get("$schema") == "https://json-schema.org/draft/2020-12/schema", f"{name}: must declare draft 2020-12")
        gate.require("schema-instance-validation", isinstance(data.get("$id"), str) and data.get("type") == "object", f"{name}: must be an object schema with ID")
    index_schema = schemas["index.schema.json"]
    gate.require("index-marker-enum", set(index_schema.get("$defs", {}).get("marker", {}).get("properties", {}).get("marker_type", {}).get("enum", [])) == MARKERS, "index schema: marker enum must match normative closed set")
    gate.require("transcript-provenance", {"utterance_confidence", "word_confidence", "speaker_confidence", "provenance_status", "word_timestamps_available"} <= set(index_schema.get("$defs", {}).get("transcript", {}).get("required", [])), "index schema: transcript provenance/confidence/word availability fields required")
    plan_schema = schemas["edit-plan.schema.json"]
    expected_defs = {
        "rational", "positive_rational", "rgba", "canonical_time", "range", "segment", "clip",
        "crop_reframe_params", "scale_position_params", "speed_change_params", "freeze_frame_params",
        "zoom_keyframe", "zoom_keyframes_params", "blur_region_params", "crossfade_audio_params",
        "gain_params", "noise_reduce_params", "fade_audio_params", "normalize_loudness_params",
        "add_sfx_params", "add_music_params", "add_ambience_params", "mix_audio_params", "duck_audio_params",
        "add_caption_segment_params", "style_caption_segment_params", "assemble_segment_params", "repeat_segment_params",
        "ripple_delete_params", "insert_segment_params",
        "action", "constraints", "asset", "review", "track", "caption", "output_policy",
        "caption_style", "concat_compatibility", "backend_selection", "cost_estimate",
        "cost_summary", "cost_call", "cost_approval", "reproducibility", "qc", "qc_result",
    }
    gate.require("schema-instance-validation", expected_defs <= set(plan_schema.get("$defs", {})), "edit-plan schema: contract record definitions required")
    gate.require("schema-instance-validation", plan_schema.get("properties", {}).get("timeline_clips", {}).get("items", {}).get("$ref") == "#/$defs/clip" and plan_schema.get("properties", {}).get("actions", {}).get("items", {}).get("$ref") == "#/$defs/action", "edit-plan schema: clips/actions must use closed item definitions")
    output_schema = schemas["skill-output.schema.json"]
    gate.require("skill-output-scope-evidence", "review" in output_schema.get("required", []) and {"source_id", "range"} <= set(output_schema.get("properties", {}).get("scope", {}).get("required", [])), "skill-output schema: review and bounded scope required")
    registry = action_registry(gate)
    # Registry-parity rules compare the registry against the action catalogue and the
    # measurement results.  Those two files are development records that are not part of
    # a distribution, so the rules are skipped when they are absent rather than crashing.
    if CATALOG_RELPATH is None or PROBE_RELPATH is None:
        print("SKIP: action-registry-probe-parity (development records not present)")
        return
    catalog_path = ROOT / CATALOG_RELPATH
    probe_path = ROOT / PROBE_RELPATH
    if not (catalog_path.is_file() and probe_path.is_file()):
        print("SKIP: action-registry-probe-parity (catalogue/measurement records not present)")
        return
    catalog_rows = re.findall(r"^### (act_[a-z0-9_]+)$", catalog_path.read_text(encoding="utf-8"), re.M)
    catalog = set(catalog_rows)
    probe = read_json(probe_path)["results"]
    probe_ids = {x["action"] for x in probe}
    gate.require("action-registry-probe-parity", len(registry) == len(catalog_rows) == len(probe) and set(registry) == catalog == probe_ids, "action registry: must contain each catalog/probe action exactly once")
    probe_status = {row["action"]: row.get("v1_executable") for row in probe}
    for action in sorted(set(registry) & probe_ids):
        registry_status = registry[action]["v1_executable"]
        measured_status = probe_status[action]
        gate.require("action-registry-probe-v1-executable", registry_status == measured_status, f"action registry {action}: v1_executable mismatch (registry={registry_status!r}, probe={measured_status!r})")

def action_registry(gate):
    rows = read_json(ROOT / "contracts/action-executability.json").get("entries", [])
    ids(rows, "action registry", gate, "action")
    result = {row.get("action"): row for row in rows}
    for action, row in result.items():
        status = row.get("v1_executable")
        gate.require("action-status", status in ALLOWED_ACTION_STATUS or str(status).startswith("blocked(") or status == "no(out-of-scope)", f"action registry {action}: illegal executable status")
        kind = row.get("verification_kind")
        gate.require("action-verification-kind", kind in {"plan_contract", "container_property", "effect_measurement", "rejection"}, f"action registry {action}: illegal verification kind")
        if kind in {"container_property", "effect_measurement"}: gate.require("action-verification-kind", isinstance(row.get("t2_effect_requirement"), str) and row["t2_effect_requirement"], f"action registry {action}: missing T2 effect requirement")
    return result

def validate_index(data, gate):
    gate.require("schema-instance-validation", data.get("schema_version") == "1.0", "index: schema version required")
    gate.require("index-source-id", bool(data.get("sources")), "index: at least one source is required")
    source_ids = ids(data.get("sources", []), "index sources", gate, "source_id")
    durations = {}
    for source in data.get("sources", []):
        gate.require("index-source-id", re.fullmatch(r"[a-f0-9]{64}", str(source.get("sha256"))) is not None, f"source {source.get('source_id')}: SHA-256 required")
        duration = time_value(source.get("duration"), gate, f"source {source.get('source_id')}.duration")
        gate.require("index-source-bound", duration > 0, f"source {source.get('source_id')}: duration must be positive")
        durations[source.get("source_id")] = duration
    evidence_ids = ids(data.get("evidence", []), "index evidence", gate, "evidence_id")
    evidence_ranges = {}
    for e in data.get("evidence", []):
        sid = e.get("source_id"); r = range_value(e.get("range"), gate, f"evidence {e.get('evidence_id')}.range")
        gate.require("index-evidence-bound", sid in source_ids and r[0] >= 0 and r[1] <= durations.get(sid, 0), f"evidence {e.get('evidence_id')}: range outside source")
        p = e.get("provenance", {}); kind = e.get("kind")
        gate.require("index-evidence-id", kind in {"frame", "signal", "human"}, f"evidence {e.get('evidence_id')}: closed evidence kind")
        if kind == "frame":
            frame_time = time_value(p.get("frame_time"), gate, f"frame evidence {e.get('evidence_id')}.frame_time")
            gate.require("index-evidence-bound", re.fullmatch(r"[a-f0-9]{64}", str(p.get("frame_sha256"))) is not None and r[0] <= frame_time < r[1], f"frame evidence {e.get('evidence_id')}: hash/time in evidence range required")
        if kind == "signal": gate.require("index-evidence-id", isinstance(p.get("tool"), str) and isinstance(p.get("version"), str), f"signal evidence {e.get('evidence_id')}: tool/version required")
        if kind == "human": gate.require("index-evidence-id", isinstance(p.get("reviewer"), str) and isinstance(p.get("rationale"), str), f"human evidence {e.get('evidence_id')}: reviewer/rationale required")
        evidence_ranges[e.get("evidence_id")] = r
    evidence_by_id = {e.get("evidence_id"): e for e in data.get("evidence", [])}
    for source in data.get("sources", []):
        capture = source.get("capture")
        if not isinstance(capture, dict):
            continue
        validation = capture.get("validation", {})
        verdict = validation.get("verdict")
        off_target_frame_count = validation.get("off_target_frame_count")
        source_id = source.get("source_id")
        gate.require(
            "capture-pass-zero-off-target-frames",
            verdict != "pass" or off_target_frame_count == 0,
            f"capture {source_id}: pass verdict requires off_target_frame_count 0; Ω.4 forbids pass when any off-target window frame is visible",
        )
        refilm_reason = validation.get("refilm_reason")
        gate.require(
            "capture-refilm-reason",
            verdict != "refilm" or isinstance(refilm_reason, str) and bool(refilm_reason.strip()),
            f"capture {source_id}: refilm verdict requires a non-empty refilm_reason",
        )
        for evidence_id in validation.get("evidence_ids", []):
            evidence = evidence_by_id.get(evidence_id)
            gate.require(
                "capture-frame-evidence",
                isinstance(evidence, dict) and evidence.get("kind") == "frame",
                f"capture {source_id}: validation evidence {evidence_id!r} must reference existing frame evidence; Ω.4 verdicts require extracted frames",
            )
    ids(data.get("markers", []), "markers", gate, "marker_id")
    for marker in data.get("markers", []):
        sid = marker.get("source_id"); r = range_value(marker.get("range"), gate, f"marker {marker.get('marker_id')}.range")
        gate.require("index-marker-enum", marker.get("marker_type") in MARKERS, f"marker {marker.get('marker_id')}: marker type is not closed enum")
        gate.require("index-source-bound", sid in source_ids and r[0] >= 0 and r[1] <= durations.get(sid, 0), f"marker {marker.get('marker_id')}: range outside source")
        gate.require("index-marker-evidence", bool(marker.get("evidence_ids")) and set(marker.get("evidence_ids", [])) <= evidence_ids, f"marker {marker.get('marker_id')}: evidence must exist")
    ids(data.get("transcript", []), "transcript", gate, "utterance_id")
    for utterance in data.get("transcript", []):
        sid = utterance.get("source_id"); r = range_value(utterance.get("range"), gate, f"utterance {utterance.get('utterance_id')}.range")
        gate.require("index-source-bound", sid in source_ids and r[0] >= 0 and r[1] <= durations.get(sid, 0), f"utterance {utterance.get('utterance_id')}: range outside source")
        gate.require("transcript-provenance", utterance.get("provenance_status") in PROVENANCE, f"utterance {utterance.get('utterance_id')}: provenance status invalid")
        for field in ("utterance_confidence", "word_confidence", "speaker_confidence"):
            value = utterance.get(field); gate.require("transcript-confidence", type(value) in {int, float} and 0 <= value <= 1, f"utterance {utterance.get('utterance_id')}: {field} must be 0..1")
    for observation in data.get("onscreen_text", []):
        sid = observation.get("source_id"); time = time_value(observation.get("time"), gate, f"onscreen text {observation.get('text')!r}.time")
        gate.require("index-source-bound", sid in source_ids and 0 <= time < durations.get(sid, 0), f"onscreen text {observation.get('text')!r}: time outside source")
        confidence = observation.get("confidence"); gate.require("transcript-confidence", type(confidence) in {int, float} and 0 <= confidence <= 1, f"onscreen text {observation.get('text')!r}: confidence must be 0..1")
    chunks = []
    for chunk in data.get("transcription_chunks", []):
        original_range = range_value(chunk.get("original_range"), gate, f"transcription chunk {chunk.get('chunk_id')}.original_range")
        actual_offset = time_value(chunk.get("actual_offset"), gate, f"transcription chunk {chunk.get('chunk_id')}.actual_offset")
        gate.require("chunk-reassembly-consistency", actual_offset == original_range[0], f"transcription chunk {chunk.get('chunk_id')}: actual_offset must exactly equal original_range.start")
        gate.require(
            "chunk-reassembly-consistency",
            set(chunk.get("reassembly_evidence_ids", [])) <= evidence_ids,
            f"transcription chunk {chunk.get('chunk_id')}: reassembly_evidence_ids must reference existing evidence",
        )
        chunks.append((chunk.get("chunk_id"), original_range))
    for position, (chunk_id, original_range) in enumerate(chunks):
        for other_chunk_id, other_range in chunks[position + 1:]:
            gate.require("chunk-reassembly-consistency", not ranges_overlap(original_range, other_range), f"transcription chunks {chunk_id}/{other_chunk_id}: original_range must not overlap")
    return {"index_id": data.get("index_id"), "source_ids": source_ids, "durations": durations, "evidence_ids": evidence_ids, "evidence_ranges": evidence_ranges}

def validate_plan(plan, index, gate):
    gate.require("schema-instance-validation", plan.get("schema_version") == "1.0", "plan: schema version required")
    gate.require("plan-index-id", plan.get("index_id") == index["index_id"], "plan: index_id must match paired Index")
    segments = plan.get("source_segments", []); segment_ids = ids(segments, "source segments", gate, "segment_id")
    segs = {}
    for segment in segments:
        sid = segment.get("source_id"); r = range_value(segment.get("source_range"), gate, f"segment {segment.get('segment_id')}.source_range")
        gate.require("plan-segment-bound", sid in index["source_ids"] and r[0] >= 0 and r[1] <= index["durations"].get(sid, 0), f"segment {segment.get('segment_id')}: source range outside source")
        segs[segment.get("segment_id")] = (sid, r)
    tracks = plan.get("tracks", []); track_ids = ids(tracks, "tracks", gate, "track_id")
    track_kinds = {t.get("track_id"): t.get("kind") for t in tracks}
    for track in tracks:
        kind = track.get("kind")
        gate.require("track-closed-kind", kind in {"video", "dialogue", "music", "sfx", "graphics", "captions"}, f"track {track.get('track_id')}: closed track kind")
        if kind in {"video", "graphics", "captions"}: gate.require("track-closed-kind", set(track) == {"track_id", "kind", "z_order"} and type(track.get("z_order")) is int, f"track {track.get('track_id')}: visual track requires only integer z_order")
        else: gate.require("track-closed-kind", set(track) == {"track_id", "kind", "mix_order"} and type(track.get("mix_order")) is int, f"track {track.get('track_id')}: audio track requires only integer mix_order")
    clips = plan.get("timeline_clips", []); ids(clips, "timeline clips", gate, "clip_id")
    ordered = sorted(clips, key=lambda c: range_value(c.get("timeline_range"), gate, f"clip {c.get('clip_id')}.timeline_range")[0])
    clip_segment_ids = []
    for clip in ordered:
        r = range_value(clip.get("timeline_range"), gate, f"clip {clip.get('clip_id')}.timeline_range")
        segment_id = clip.get("source_segment_id")
        gate.require("timeline-clip-reference", set(clip) == {"clip_id", "source_segment_id", "track_id", "rounding", "source_range", "timeline_range"}, f"clip {clip.get('clip_id')}: explicit source/timeline mapping fields required")
        source_r = range_value(clip.get("source_range"), gate, f"clip {clip.get('clip_id')}.source_range")
        gate.require("timeline-clip-reference", segment_id in segment_ids and clip.get("track_id") in track_ids and track_kinds.get(clip.get("track_id")) == "video" and clip.get("rounding") == "toward_zero" and segment_id in segs and source_r == segs[segment_id][1] and r[0] >= 0, f"clip {clip.get('clip_id')}: source segment/video track/exact mapping required")
        clip_segment_ids.append(clip.get("source_segment_id"))
    # Timeline clips on the same track may not overlap.
    by_track = {}
    for clip in ordered:
        by_track.setdefault(clip.get("track_id"), []).append(clip)
    for track_id, track_clips in by_track.items():
        for earlier, later in zip(track_clips, track_clips[1:]):
            a = range_value(earlier.get("timeline_range"), gate, f"clip {earlier.get('clip_id')}.timeline_range")
            b = range_value(later.get("timeline_range"), gate, f"clip {later.get('clip_id')}.timeline_range")
            gate.require("timeline-clip-order", not ranges_overlap(a, b), f"clips {earlier.get('clip_id')}/{later.get('clip_id')} on track {track_id}: timeline ranges must not overlap")
    source_positions = {sid: seg[1][0] for sid, seg in segs.items()}
    # Compare source_range order only within one source_id: times from different
    # sources have no ordering relation to each other.
    positions_by_source = {}
    for sid in clip_segment_ids:
        seg = segs.get(sid)
        positions_by_source.setdefault(seg[0] if seg else None, []).append(source_positions.get(sid, Fraction(-1)))
    registry = action_registry(gate)
    caption_ids = {caption.get("caption_id") for caption in plan.get("captions", [])}
    caption_style_ids = {style.get("style_id") for style in plan.get("caption_styles", [])}
    ids(plan.get("actions", []), "actions", gate, "action_id")
    speed_change_ranges = []
    freeze_segment_ids = set()
    zoom_segment_ids = {
        action.get("source_segment_id") for action in plan.get("actions", [])
        if isinstance(action, dict) and action.get("action") == "act_zoom_keyframes"
    }
    clips_by_segment = {}
    for clip in ordered:
        clips_by_segment.setdefault(clip.get("source_segment_id"), []).append(clip)
    repeat_actions = []
    for action in plan.get("actions", []):
        name = action.get("action"); row = registry.get(name)
        gate.require("action-id", row is not None, f"action {action.get('action_id')}: unknown action")
        if row:
            gate.require("action-registry-parity", action.get("v1_executable") == row["v1_executable"], f"action {action.get('action_id')}: status must equal registry")
            gate.require("action-verification-kind", action.get("verification_kind") == row["verification_kind"], f"action {action.get('action_id')}: verification kind must equal registry")
            gate.require("action-executable-gate", row["v1_executable"] in ALLOWED_ACTION_STATUS, f"action {name}: execution blocked or out of scope")
        segment_id = action.get("source_segment_id"); gate.require("plan-segment-id", segment_id in segment_ids, f"action {action.get('action_id')}: segment must exist")
        evidence = set(action.get("evidence_ids", [])); gate.require("action-evidence", bool(evidence) and evidence <= index["evidence_ids"], f"action {action.get('action_id')}: evidence must exist")
        if segment_id in segs:
            gate.require("action-evidence", all(ranges_overlap(segs[segment_id][1], index["evidence_ranges"][eid]) for eid in evidence if eid in index["evidence_ranges"]), f"action {action.get('action_id')}: evidence must overlap action segment")
        if name == "act_add_caption_segment" and isinstance(action.get("params"), dict):
            caption_id = action["params"].get("caption_id")
            gate.require(
                "action-params-integrity",
                caption_id in caption_ids,
                f"action {action.get('action_id')}: params.caption_id {caption_id!r} must reference an existing caption",
            )
        if name == "act_style_caption_segment" and isinstance(action.get("params"), dict):
            caption_id = action["params"].get("caption_id")
            style_id = action["params"].get("style_id")
            gate.require(
                "action-params-integrity",
                caption_id in caption_ids,
                f"action {action.get('action_id')}: params.caption_id {caption_id!r} must reference an existing caption",
            )
            gate.require(
                "action-params-integrity",
                style_id in caption_style_ids,
                f"action {action.get('action_id')}: params.style_id {style_id!r} must reference an existing caption style",
            )
        if name == "act_assemble_segment":
            ordered_segment_ids = action.get("ordered_segment_ids", [])
            gate.require(
                "action-params-integrity",
                all(ordered_segment_id in segment_ids for ordered_segment_id in ordered_segment_ids),
                f"action {action.get('action_id')}: ordered_segment_ids must reference existing source segments",
            )
        if name == "act_repeat_segment":
            params = action.get("params")
            gate.require(
                "repeat-segment-declaration",
                isinstance(params, dict) and set(params) == {"contiguous"} and type(params.get("contiguous")) is bool,
                f"action {action.get('action_id')}: act_repeat_segment.params must contain exactly boolean contiguous",
            )
            repeat_actions.append(action)
        if name == "act_ripple_delete":
            params = action.get("params") if isinstance(action.get("params"), dict) else {}
            positions = [
                position for position, clip in enumerate(ordered)
                if clip.get("source_segment_id") == segment_id
            ]
            target_ok = (
                set(params) == {"scope"} and params.get("scope") == "all_tracks"
                and segment_id in segs and bool(positions)
            )
            gate.require(
                "ripple-delete-declaration", target_ok,
                f"action {action.get('action_id')}: act_ripple_delete requires closed scope=all_tracks and a source segment in the timeline",
            )
            pairs = [
                (position, ordered[position + 1]) for position in positions
                if position + 1 < len(ordered)
            ]
            same_source = [
                (position, right) for position, right in pairs
                if segment_id in segs and right.get("source_segment_id") in segs
                and segs[segment_id][0] == segs[right.get("source_segment_id")][0]
            ]
            gate.require(
                "ripple-delete-declaration", bool(same_source),
                f"action {action.get('action_id')}: act_ripple_delete requires the immediate right timeline piece to share source_id",
            )
            source_gap = [
                (position, right) for position, right in same_source
                if segs[right.get("source_segment_id")][1][0] > segs[segment_id][1][1]
            ]
            gate.require(
                "ripple-delete-declaration", bool(source_gap),
                f"action {action.get('action_id')}: act_ripple_delete requires a positive source-range gap to the immediate right piece",
            )
            timeline_touching = any(
                range_value(right.get("timeline_range"), gate, f"clip {right.get('clip_id')}.timeline_range")[0]
                == range_value(ordered[position].get("timeline_range"), gate, f"clip {ordered[position].get('clip_id')}.timeline_range")[1]
                for position, right in source_gap
            )
            gate.require(
                "ripple-delete-declaration", timeline_touching,
                f"action {action.get('action_id')}: act_ripple_delete requires no timeline gap to the immediate right piece",
            )
        if name == "act_insert_segment":
            params = action.get("params") if isinstance(action.get("params"), dict) else {}
            positions = [
                position for position, clip in enumerate(ordered)
                if clip.get("source_segment_id") == segment_id
            ]
            target_ok = (
                set(params) == {"mode"} and params.get("mode") == "ripple"
                and segment_id in segs and bool(positions)
            )
            gate.require(
                "insert-segment-declaration", target_ok,
                f"action {action.get('action_id')}: act_insert_segment requires closed mode=ripple and an inserted segment in the timeline",
            )
            triples = [
                (ordered[position - 1], ordered[position], ordered[position + 1])
                for position in positions if 0 < position < len(ordered) - 1
            ]
            same_source = [
                (before, inserted, after) for before, inserted, after in triples
                if before.get("source_segment_id") in segs and after.get("source_segment_id") in segs
                and segs[before.get("source_segment_id")][0] == segs[after.get("source_segment_id")][0]
            ]
            gate.require(
                "insert-segment-declaration", bool(same_source),
                f"action {action.get('action_id')}: act_insert_segment requires both adjacent timeline pieces to share source_id",
            )
            source_touching = [
                (before, inserted, after) for before, inserted, after in same_source
                if segs[after.get("source_segment_id")][1][0] == segs[before.get("source_segment_id")][1][1]
            ]
            gate.require(
                "insert-segment-declaration", bool(source_touching),
                f"action {action.get('action_id')}: act_insert_segment requires adjacent source ranges to touch",
            )
            timeline_touching = any(
                range_value(inserted.get("timeline_range"), gate, f"clip {inserted.get('clip_id')}.timeline_range")[0]
                == range_value(before.get("timeline_range"), gate, f"clip {before.get('clip_id')}.timeline_range")[1]
                and range_value(after.get("timeline_range"), gate, f"clip {after.get('clip_id')}.timeline_range")[0]
                == range_value(inserted.get("timeline_range"), gate, f"clip {inserted.get('clip_id')}.timeline_range")[1]
                for before, inserted, after in source_touching
            )
            gate.require(
                "insert-segment-declaration", timeline_touching,
                f"action {action.get('action_id')}: act_insert_segment requires no timeline gaps around the inserted piece",
            )
        if name == "act_speed_change" and segment_id in segs and isinstance(action.get("params"), dict):
            speed_change_ranges.append((segs[segment_id][0], range_value(action["params"].get("range"), gate, f"action {action.get('action_id')}.params.range")))
        if name == "act_freeze_frame":
            params = action.get("params") if isinstance(action.get("params"), dict) else {}
            source_time = time_value(params.get("source_time"), gate, f"action {action.get('action_id')}.params.source_time")
            timeline_duration = time_value(params.get("timeline_duration"), gate, f"action {action.get('action_id')}.params.timeline_duration")
            matching_clips = clips_by_segment.get(segment_id, [])
            frame_rate_data = plan.get("output_policy", {}).get("frame_rate")
            try:
                frame_rate = Fraction(frame_rate_data["num"], frame_rate_data["den"])
            except (KeyError, TypeError, ZeroDivisionError):
                frame_rate = Fraction(0)
            one_frame = Fraction(1, 1) / frame_rate if frame_rate > 0 else Fraction(0)
            clip_timeline_range = (
                range_value(matching_clips[0].get("timeline_range"), gate,
                            f"clip {matching_clips[0].get('clip_id')}.timeline_range")
                if len(matching_clips) == 1 else None
            )
            good = (
                len(matching_clips) == 1 and segment_id in segs and source_time == segs[segment_id][1][0]
                and segs[segment_id][1][1] - segs[segment_id][1][0] == one_frame
                and timeline_duration > 0 and frame_rate > 0
                and (timeline_duration * frame_rate).denominator == 1
                and clip_timeline_range is not None
                and timeline_duration == clip_timeline_range[1] - clip_timeline_range[0]
            )
            gate.require(
                "freeze-frame-contract", good,
                f"action {action.get('action_id')}: act_freeze_frame requires one source frame, its source_range.start, and an integral output-frame duration equal to its clip",
            )
            freeze_segment_ids.add(segment_id)
        if name == "act_zoom_keyframes":
            params = action.get("params") if isinstance(action.get("params"), dict) else {}
            keyframes = params.get("keyframes") if isinstance(params.get("keyframes"), list) else []
            matching_clips = clips_by_segment.get(segment_id, [])
            times = [time_value(keyframe.get("time") if isinstance(keyframe, dict) else None, gate,
                                f"action {action.get('action_id')}.params.keyframes[{index}].time")
                     for index, keyframe in enumerate(keyframes)]
            good = (
                params.get("interpolation") == "linear" and len(keyframes) >= 2 and len(matching_clips) == 1
                and all(later > earlier for earlier, later in zip(times, times[1:]))
                and times and times[0] == range_value(matching_clips[0].get("timeline_range"), gate,
                                                       f"clip {matching_clips[0].get('clip_id')}.timeline_range")[0]
                and times[-1] == range_value(matching_clips[0].get("timeline_range"), gate,
                                              f"clip {matching_clips[0].get('clip_id')}.timeline_range")[1]
            )
            gate.require(
                "zoom-keyframes-contract", good,
                f"action {action.get('action_id')}: act_zoom_keyframes requires linear strict keyframes at clip timeline endpoints",
            )
        if name == "act_blur_region":
            params = action.get("params") if isinstance(action.get("params"), dict) else {}
            strength = params.get("strength")
            try:
                strength_value = Fraction(strength["num"], strength["den"])
            except (KeyError, TypeError, ZeroDivisionError):
                strength_value = Fraction(0)
            canvas = plan.get("output_policy") if isinstance(plan.get("output_policy"), dict) else {}
            fields = ("x", "y", "width", "height")
            values = [params.get(field) for field in fields]
            integer_values = all(type(value) is int for value in values)
            x, y, width, height = values if integer_values else (0, 0, 0, 0)
            good = (
                set(params) == {"filter", "strength", "x", "y", "width", "height"}
                and params.get("filter") in {"gaussian", "box"}
                and strength_value > 0
                and integer_values and x >= 0 and y >= 0 and width >= 2 and height >= 2
                and all(value % 2 == 0 for value in values)
                and type(canvas.get("width")) is int and type(canvas.get("height")) is int
                and x + width <= canvas["width"] and y + height <= canvas["height"]
                and segment_id not in zoom_segment_ids
            )
            gate.require(
                "blur-region-contract", good,
                f"action {action.get('action_id')}: act_blur_region requires gaussian/box positive strength, an even rectangle inside output_policy canvas, and no act_zoom_keyframes on the same segment",
            )
    style_caption_actions = [
        action for action in plan.get("actions", [])
        if isinstance(action, dict) and action.get("action") == "act_style_caption_segment"
    ]
    add_caption_ids = {
        action["params"].get("caption_id") for action in plan.get("actions", [])
        if isinstance(action, dict) and action.get("action") == "act_add_caption_segment"
        and isinstance(action.get("params"), dict)
    }
    for action in style_caption_actions:
        params = action.get("params")
        params = params if isinstance(params, dict) else {}
        caption_id, style_id = params.get("caption_id"), params.get("style_id")
        matching_captions = [
            caption for caption in plan.get("captions", [])
            if isinstance(caption, dict) and caption.get("caption_id") == caption_id
        ]
        caption_style_ref = matching_captions[0].get("style_ref") if len(matching_captions) == 1 else None
        matching_styles = [
            style for style in plan.get("caption_styles", [])
            if isinstance(style, dict) and style.get("style_id") == style_id
        ]
        declarations_for_caption = sum(
            isinstance(candidate, dict) and candidate.get("action") == "act_style_caption_segment"
            and isinstance(candidate.get("params"), dict)
            and candidate["params"].get("caption_id") == caption_id
            for candidate in style_caption_actions
        )
        gate.require(
            "style-caption-declaration",
            set(params) == {"caption_id", "style_id"},
            f"action {action.get('action_id')}: act_style_caption_segment.params must contain exactly caption_id and style_id",
        )
        gate.require(
            "style-caption-declaration", len(matching_captions) == 1,
            f"action {action.get('action_id')}: params.caption_id {caption_id!r} must resolve to exactly one captions[] record",
        )
        gate.require(
            "style-caption-declaration", style_id == caption_style_ref,
            f"action {action.get('action_id')}: params.style_id {style_id!r} must match caption {caption_id!r}.style_ref {caption_style_ref!r}",
        )
        gate.require(
            "style-caption-declaration", len(matching_styles) == 1,
            f"action {action.get('action_id')}: params.style_id {style_id!r} must resolve to exactly one caption_styles[] record",
        )
        gate.require(
            "style-caption-declaration", declarations_for_caption == 1,
            f"action {action.get('action_id')}: caption_id {caption_id!r} must have exactly one act_style_caption_segment declaration",
        )
        gate.require(
            "style-caption-declaration", caption_id in add_caption_ids,
            f"action {action.get('action_id')}: caption_id {caption_id!r} requires act_add_caption_segment",
        )
    crossfade_actions = [
        action for action in plan.get("actions", [])
        if isinstance(action, dict) and action.get("action") == "act_crossfade_audio"
    ]
    if crossfade_actions:
        gate.require(
            "crossfade-audio-policy",
            plan.get("output_policy", {}).get("audio_policy") == "present",
            "act_crossfade_audio requires output_policy.audio_policy=present",
        )
        clip_durations = [
            end - start for start, end in
            (range_value(clip.get("timeline_range"), gate, f"clip {clip.get('clip_id')}.timeline_range") for clip in ordered)
        ]
        seen_join_indices = set()
        crossfade_windows = []
        for action in crossfade_actions:
            params = action.get("params") if isinstance(action.get("params"), dict) else {}
            join_index = params.get("join_index")
            join_ok = (
                type(join_index) is int and 0 <= join_index <= len(clip_durations) - 2
            )
            gate.require(
                "crossfade-join-index", join_ok,
                f"action {action.get('action_id')}: act_crossfade_audio join_index must be within timeline clip joins",
            )
            gate.require(
                "crossfade-join-unique", join_ok and join_index not in seen_join_indices,
                f"action {action.get('action_id')}: act_crossfade_audio join_index must not be declared twice",
            )
            if join_ok:
                seen_join_indices.add(join_index)
            duration_data = params.get("duration")
            duration = Fraction(0)
            duration_ok = False
            if isinstance(duration_data, dict):
                numerator, denominator = duration_data.get("num"), duration_data.get("den")
                if type(numerator) is int and type(denominator) is int and numerator > 0 and denominator > 0:
                    duration = Fraction(numerator, denominator)
                    duration_ok = join_ok and duration < clip_durations[join_index] and duration < clip_durations[join_index + 1]
            gate.require(
                "crossfade-duration", duration_ok,
                f"action {action.get('action_id')}: act_crossfade_audio duration must be positive and shorter than both adjacent clips",
            )
            if duration_ok:
                join_time = sum(clip_durations[:join_index + 1], Fraction())
                crossfade_windows.append((join_time - duration / 2, join_time + duration / 2, join_index))
        for earlier, later in zip(sorted(crossfade_windows), sorted(crossfade_windows)[1:]):
            gate.require(
                "crossfade-window-nonoverlap", earlier[1] <= later[0],
                f"act_crossfade_audio blend windows overlap at joins {earlier[2]} and {later[2]}",
            )
    audio_cleanup_actions = [
        action for action in plan.get("actions", [])
        if isinstance(action, dict)
        and action.get("action") in {
            "act_gain", "act_noise_reduce", "act_fade_audio", "act_normalize_loudness",
        }
    ]
    if audio_cleanup_actions:
        gate.require(
            "audio-cleanup-audio-policy",
            plan.get("output_policy", {}).get("audio_policy") == "present",
            "F.1 audio cleanup actions require output_policy.audio_policy=present",
        )
    loudnorm_actions = [
        action for action in audio_cleanup_actions
        if action.get("action") == "act_normalize_loudness"
    ]
    for action in loudnorm_actions:
        params = action.get("params")
        gate.require(
            "loudnorm-global",
            isinstance(params, dict) and "timeline_range" not in params,
            f"action {action.get('action_id')}: act_normalize_loudness is global and must not specify timeline_range",
        )
    if loudnorm_actions:
        two_pass = plan.get("reproducibility", {}).get("loudnorm_two_pass")
        measured_keys = {"measured_I", "measured_LRA", "measured_TP", "measured_thresh", "offset"}
        gate.require(
            "loudnorm-two-pass",
            isinstance(two_pass, dict)
            and two_pass.get("status") == "measured"
            and all(type(two_pass.get(key)) in {int, float} for key in measured_keys),
            "act_normalize_loudness requires reproducibility.loudnorm_two_pass measured_I, measured_LRA, measured_TP, measured_thresh, and offset",
        )
    audio_bed_actions = [
        action for action in plan.get("actions", [])
        if isinstance(action, dict) and action.get("action") in {"act_add_sfx", "act_add_music", "act_add_ambience"}
    ]
    mix_actions = [
        action for action in plan.get("actions", [])
        if isinstance(action, dict) and action.get("action") == "act_mix_audio"
    ]
    if audio_bed_actions or mix_actions:
        gate.require(
            "audio-bed-audio-policy",
            plan.get("output_policy", {}).get("audio_policy") == "present",
            "F.3 audio bed actions require output_policy.audio_policy=present",
        )
    gate.require(
        "audio-bed-mix-pairing",
        (not audio_bed_actions and not mix_actions) or (bool(audio_bed_actions) and len(mix_actions) == 1),
        "F.3 requires exactly one act_mix_audio when and only when one or more beds are declared",
    )
    if len(mix_actions) == 1:
        params = mix_actions[0].get("params") if isinstance(mix_actions[0].get("params"), dict) else {}
        action_ids = [action.get("action_id") for action in audio_bed_actions]
        input_ids = params.get("input_action_ids")
        gate.require(
            "audio-bed-mix-inputs",
            isinstance(input_ids, list) and len(input_ids) == len(set(input_ids)) and set(input_ids) == set(action_ids),
            "act_mix_audio.input_action_ids must exactly name the declared audio bed action_ids",
        )
        gate.require(
            "audio-bed-mix-policy",
            params.get("duration_policy") == "first" and params.get("normalize") == 0 and params.get("output_channel_layout") == "stereo",
            "act_mix_audio requires duration_policy=first, normalize=0, and output_channel_layout=stereo",
        )
    if audio_bed_actions:
        program_duration = Fraction(0)
        for clip in ordered:
            clip_start, clip_end = range_value(
                clip.get("timeline_range"), gate, f"clip {clip.get('clip_id')}.timeline_range",
            )
            program_duration += clip_end - clip_start
        for action in audio_bed_actions:
            params = action.get("params") if isinstance(action.get("params"), dict) else {}
            start = time_value(params.get("timeline_start"), gate, f"action {action.get('action_id')}.params.timeline_start")
            gate.require(
                "audio-bed-timeline-start", start < program_duration,
                f"action {action.get('action_id')}: audio bed timeline_start must be before program end",
            )
    duck_actions = [
        action for action in plan.get("actions", [])
        if isinstance(action, dict) and action.get("action") == "act_duck_audio"
    ]
    if duck_actions:
        gate.require(
            "duck-audio-policy",
            plan.get("output_policy", {}).get("audio_policy") == "present",
            "act_duck_audio requires output_policy.audio_policy=present",
        )
        bed_ids = {action.get("action_id") for action in audio_bed_actions}
        program_duration = sum(
            (range_value(clip.get("timeline_range"), gate, f"clip {clip.get('clip_id')}.timeline_range")[1]
             - range_value(clip.get("timeline_range"), gate, f"clip {clip.get('clip_id')}.timeline_range")[0])
            for clip in ordered
        )
        intervals_by_bed = {action_id: [] for action_id in bed_ids}
        for action in duck_actions:
            params = action.get("params") if isinstance(action.get("params"), dict) else {}
            targets = params.get("target_action_ids")
            gate.require(
                "duck-targets",
                bool(bed_ids) and isinstance(targets, list) and bool(targets)
                and len(targets) == len(set(targets)) and set(targets) <= bed_ids,
                f"action {action.get('action_id')}: target_action_ids must be a non-empty subset of declared audio bed action_ids",
            )
            depth = params.get("depth_db")
            gate.require(
                "duck-depth", type(depth) in {int, float} and depth < 0,
                f"action {action.get('action_id')}: depth_db must be negative",
            )
            attack, release = params.get("attack"), params.get("release")
            gate.require(
                "duck-ramp", type(attack) in {int, float} and attack > 0 and type(release) in {int, float} and release > 0,
                f"action {action.get('action_id')}: attack and release must be positive",
            )
            attack_f = Fraction(str(attack)) if type(attack) in {int, float} else Fraction(0)
            release_f = Fraction(str(release)) if type(release) in {int, float} else Fraction(0)
            windows = params.get("windows")
            gate.require(
                "duck-window", isinstance(windows, list) and bool(windows),
                f"action {action.get('action_id')}: windows must be a non-empty array",
            )
            if not isinstance(windows, list):
                continue
            for window_index, window in enumerate(windows):
                start, end = range_value(window, gate, f"action {action.get('action_id')}.params.windows[{window_index}]")
                gate.require(
                    "duck-sample-grid",
                    (start * 48000).denominator == 1 and (end * 48000).denominator == 1,
                    f"action {action.get('action_id')}: duck window times must be sample-exact at 48000 Hz",
                )
                gate.require(
                    "duck-program-bounds",
                    start - attack_f >= 0 and end + release_f <= program_duration,
                    f"action {action.get('action_id')}: duck window including attack/release must stay within the program timeline",
                )
                if isinstance(targets, list):
                    for target in targets:
                        if target in intervals_by_bed:
                            intervals_by_bed[target].append((start - attack_f, end + release_f))
        for target, intervals in intervals_by_bed.items():
            ordered_intervals = sorted(intervals)
            gate.require(
                "duck-window-nonoverlap",
                all(later[0] >= earlier[1] for earlier, later in zip(ordered_intervals, ordered_intervals[1:])),
                f"audio bed {target}: duck windows including ramps must not overlap",
            )
    for clip in ordered:
        segment_id = clip.get("source_segment_id")
        source_r = range_value(clip.get("source_range"), gate, f"clip {clip.get('clip_id')}.source_range")
        timeline_r = range_value(clip.get("timeline_range"), gate, f"clip {clip.get('clip_id')}.timeline_range")
        source_id = segs.get(segment_id, (None, None))[0]
        speed_change_applies = any(
            action_source_id == source_id and ranges_overlap(source_r, action_range)
            for action_source_id, action_range in speed_change_ranges
        )
        gate.require(
            "timeline-clip-reference",
            speed_change_applies or clip.get("source_segment_id") in freeze_segment_ids or source_r[1] - source_r[0] == timeline_r[1] - timeline_r[0],
            f"clip {clip.get('clip_id')}: source/timeline durations must match unless an act_speed_change or act_freeze_frame applies",
        )
    constraints = plan.get("constraints", {}); gate.require("preserve-priority", set(constraints) == {"preserve", "lock_orders", "releases"}, "constraints: closed preserve/lock_orders/releases required")
    preserve = {x.get("segment_id") for x in constraints.get("preserve", [])}
    for item in constraints.get("preserve", []): gate.require("preserve-priority", item.get("segment_id") in segment_ids and set(item.get("evidence_ids", [])) <= index["evidence_ids"], "preserve: segment and evidence must exist")
    releases = {x.get("segment_id"): x for x in constraints.get("releases", [])}
    for sid, release in releases.items():
        gate.require("human-release-record", release.get("released_by") == "human" and isinstance(release.get("timestamp"), str) and bool(release.get("rationale")) and release.get("evidence_id") in index["evidence_ids"], f"release {sid}: human/evidence/rationale/timestamp required")
    for action in plan.get("actions", []):
        if action.get("action") in DESTRUCTIVE and action.get("source_segment_id") in preserve:
            gate.require("preserve-priority", action.get("source_segment_id") in releases, f"action {action.get('action_id')}: may not modify preserved segment without human release")
    for locked in constraints.get("lock_orders", []):
        gate.require("lock-order", all(s in segment_ids for s in locked) and [s for s in clip_segment_ids if s in locked] == locked, "lock_order: timeline order must equal locked source order")
    reordered = any(p != sorted(p) for p in positions_by_source.values())
    declared_reorders = [a for a in plan.get("actions", []) if a.get("action") == "act_reorder_segments"]
    if reordered:
        gate.require("timeline-global-source-order", any(a.get("ordered_segment_ids") == clip_segment_ids for a in declared_reorders), "timeline clips: source-segment order reversed without a permitted reorder action")
    # A repeated source range is an explicit editorial declaration, not
    # a rendering instruction.  Index by source identity/range so separately named
    # source segments cannot conceal reuse of the same source material.
    # "The same source range" means an OVERLAPPING range, not an identical one.
    # Real material duplicates by overlap - a cold open can overlap part of a
    # longer body segment - so keying on exact equality lets such a plan pass
    # undeclared.  Touching ranges
    # (zero-length overlap) are ordinary consecutive trims, not reuse.
    clip_identity = []
    for position, clip in enumerate(ordered):
        source_id, source_range = segs.get(clip.get("source_segment_id"), (None, None))
        if source_range is not None:
            clip_identity.append((position, source_id, source_range))
    declared_repeat = []
    for action in repeat_actions:
        source_id, source_range = segs.get(action.get("source_segment_id"), (None, None))
        params = action.get("params")
        if source_range is not None and isinstance(params, dict) and type(params.get("contiguous")) is bool:
            declared_repeat.append((source_id, source_range, params["contiguous"]))

    def _overlaps(left, right):
        return min(left[1], right[1]) - max(left[0], right[0]) > 0

    def _declared_for(source_id, first, second, contiguous):
        return any(
            declared_source_id == source_id
            and declared_contiguous is contiguous
            and declared_range in (first, second)
            for declared_source_id, declared_range, declared_contiguous in declared_repeat
        )

    reused_pairs = []
    # ⛔ `index` is this function's Index parameter — never shadow it here.
    for offset, (position, source_id, source_range) in enumerate(clip_identity):
        for other_position, other_source_id, other_range in clip_identity[offset + 1:]:
            if source_id != other_source_id or not _overlaps(source_range, other_range):
                continue
            actual_contiguous = abs(other_position - position) == 1
            reused_pairs.append((source_id, source_range, other_range, actual_contiguous))
    for source_id, first, second, actual_contiguous in reused_pairs:
        gate.require(
            "repeat-segment-declaration",
            _declared_for(source_id, first, second, actual_contiguous),
            f"timeline clips: reused source range {(source_id, first, second)!r} requires matching act_repeat_segment declaration",
        )
    assets = plan.get("assets", []); asset_ids = ids(assets, "assets", gate, "asset_id")
    for asset in assets:
        gate.require("asset-id", re.fullmatch(r"[a-f0-9]{64}", str(asset.get("sha256"))) is not None, f"asset {asset.get('asset_id')}: SHA-256 required")
        gate.require("asset-id", asset.get("source_class") in {"project_provided", "external_candidate"}, f"asset {asset.get('asset_id')}: source class invalid")
        gate.require("asset-approved", asset.get("approval_status") == "approved", f"asset {asset.get('asset_id')}: unapproved asset cannot be rendered")
        gate.require("asset-approved", bool(asset.get("rights_status")) and bool(asset.get("usage_scope")), f"asset {asset.get('asset_id')}: rights and usage scope required")
    asset_actions = {"act_overlay_asset", "act_apply_lut", "act_add_music", "act_add_sfx", "act_add_ambience", "act_animate_graphic"}
    for action in plan.get("actions", []):
        if action.get("action") in asset_actions: gate.require("asset-consuming-action-reference", action.get("asset_id") in asset_ids, f"action {action.get('action_id')}: asset-consuming action requires approved asset_id")
    ids(plan.get("reviews", []), "reviews", gate, "review_id")
    reviews_by_id = {review.get("review_id"): review for review in plan.get("reviews", [])}
    for review in plan.get("reviews", []):
        gate.require("schema-instance-validation", review.get("severity") in {"info", "warning", "critical"} and type(review.get("blocking")) is bool and review.get("state") in {"open", "approved", "released"}, f"review {review.get('review_id')}: severity/blocking/state required")
        gate.require("blocking-review-render-gate", set(review.get("evidence_ids", [])) <= index["evidence_ids"], f"review {review.get('review_id')}: evidence must exist")
        if plan.get("stage") in {"ready_for_render", "rendered"} and review.get("blocking"):
            gate.require("blocking-review-render-gate", review.get("state") in {"approved", "released"}, f"review {review.get('review_id')}: unresolved blocking review forbids render")
        if review.get("state") == "released":
            release = review.get("release", {})
            gate.require("released-review-human-record", set(release) == {"released_by", "evidence_id", "rationale", "timestamp"} and release.get("released_by") == "human" and release.get("evidence_id") in index["evidence_ids"] and bool(release.get("rationale")) and isinstance(release.get("timestamp"), str), f"review {review.get('review_id')}: released review needs human evidence/rationale/timestamp")
    concat = plan.get("concat_compatibility", {})
    verdict = concat.get("verdict")
    if plan.get("stage") in {"ready_for_render", "rendered"}:
        gate.require(
            "concat-stage-verdict",
            verdict != "unverified",
            f"concat_compatibility: unverified verdict is forbidden at {plan.get('stage')}",
        )
        gate.require(
            "concat-stage-verdict",
            verdict != "incompatible",
            f"concat_compatibility: incompatible verdict forbids {plan.get('stage')}",
        )
    review_id = concat.get("review_id")
    gate.require(
        "concat-review-reference",
        review_id is None or review_id in reviews_by_id,
        f"concat_compatibility: review_id {review_id!r} must reference an existing review when non-null",
    )
    linked_review = reviews_by_id.get(review_id)
    gate.require(
        "concat-review-reference",
        verdict != "incompatible"
        or isinstance(linked_review, dict) and linked_review.get("blocking") is True,
        "concat_compatibility: incompatible verdict requires review_id referencing an existing blocking review",
    )
    # Cross-check against the Index, as the five other evidence_ids sites do.
    # Without that check any string could promote a verdict to compatible.
    concat_evidence_ids = concat.get("evidence_ids")
    gate.require(
        "concat-record-integrity",
        verdict != "compatible"
        or bool(concat_evidence_ids)
        and isinstance(concat_evidence_ids, list)
        and set(concat_evidence_ids) <= index["evidence_ids"],
        "concat_compatibility: compatible verdict requires at least one evidence_id that exists in the paired Index",
    )
    # One relational gate reuses review(blocking): every paid call has one matching approval record.
    ids(plan.get("cost_calls", []), "cost calls", gate, "call_id")
    selected_backends = {selection.get("backend_id") for selection in plan.get("backend_selections", [])}
    called_backends = {call.get("backend_id") for call in plan.get("cost_calls", [])}
    for selection in plan.get("backend_selections", []):
        if selection.get("cost_tier") == "유료":
            backend_id = selection.get("backend_id")
            gate.require("paid-backend-call-presence", backend_id in called_backends, f"backend {backend_id} selected as 유료 but no matching cost_calls record")
    for call in plan.get("cost_calls", []):
        gate.require("paid-backend-call-presence", call.get("backend_id") in selected_backends, f"cost call {call.get('call_id')}: backend must be selected")
        if call.get("cost_tier") == "유료":
            review = reviews_by_id.get(call.get("review_id"))
            approval = review.get("cost_approval", {}) if isinstance(review, dict) else {}
            gate.require("paid-backend-call-presence", isinstance(review, dict) and review.get("blocking") is True and approval.get("call_id") == call.get("call_id"), f"cost call {call.get('call_id')}: paid call requires matching review(blocking) approval")
    style_ids = ids(plan.get("caption_styles", []), "caption styles", gate, "style_id")
    for style in plan.get("caption_styles", []):
        font_asset_id = style.get("font_asset_id")
        gate.require(
            "caption-style-integrity",
            font_asset_id in asset_ids,
            f"caption style {style.get('style_id')}: font_asset_id {font_asset_id!r} must reference an existing asset",
        )
        max_width_ratio = style.get("max_width_ratio") if isinstance(style, dict) else None
        ratio_num = max_width_ratio.get("num") if isinstance(max_width_ratio, dict) else None
        ratio_den = max_width_ratio.get("den") if isinstance(max_width_ratio, dict) else None
        gate.require(
            "caption-style-integrity",
            type(ratio_num) is int and type(ratio_den) is int and ratio_num <= ratio_den,
            f"caption style {style.get('style_id')}: max_width_ratio must have integer num <= den",
        )
    ids(plan.get("captions", []), "captions", gate, "caption_id")
    for caption in plan.get("captions", []):
        caption_range = range_value(caption.get("timeline_range"), gate, f"caption {caption.get('caption_id')}.timeline_range")
        gate.require("caption-timeline-track", caption.get("language") and caption.get("text") and caption.get("safe_area") and caption.get("review_status") and caption.get("track_id") in track_ids and track_kinds.get(caption.get("track_id")) == "captions" and caption_range[0] >= 0, f"caption {caption.get('caption_id')}: renderer-neutral time/track fields required")
        gate.require(
            "caption-style-integrity",
            caption.get("style_ref") in style_ids,
            f"caption {caption.get('caption_id')}: style_ref {caption.get('style_ref')!r} must reference caption_styles.style_id",
        )
        raster = caption.get("raster_asset_id"); gate.require("caption-approved-raster", raster is None or raster in asset_ids, f"caption {caption.get('caption_id')}: raster asset must be approved registry asset")
    # The clip a caption describes is uniquely determined by back-reference.
    # caption_id -> act_add_caption_segment.params.caption_id -> action.source_segment_id
    # -> timeline_clips[].source_segment_id. Do not invent fields.
    # This does not ask about timing ("is this a good moment"), only about structure:
    # whether the target is uniquely determined.
    # evidence_ids is not an event selector; events are not asked about here.
    caption_adds = [a for a in plan.get("actions", []) if a.get("action") == "act_add_caption_segment"]
    add_by_caption = {}
    for action in caption_adds:
        params = action.get("params")
        add_by_caption.setdefault(params.get("caption_id") if isinstance(params, dict) else None, []).append(action)
    clips_by_segment = {}
    for clip in clips:
        clips_by_segment.setdefault(clip.get("source_segment_id"), []).append(clip)
    for caption in plan.get("captions", []):
        cid = caption.get("caption_id")
        # Assertion 1 - caption to action is unique.
        gate.require("caption-clip-binding", len(add_by_caption.get(cid, [])) == 1, f"caption {cid}: exactly one act_add_caption_segment must reference it")
    for action in caption_adds:
        params = action.get("params"); cid = params.get("caption_id") if isinstance(params, dict) else None
        matched = [c for c in plan.get("captions", []) if c.get("caption_id") == cid]
        # Assertion 2 - the action points at exactly one caption record.
        gate.require("caption-clip-binding", len(matched) == 1, f"caption action {action.get('action_id')}: params.caption_id {cid!r} must resolve to exactly one captions[] record")
        # Assertion 3 - that action's source_segment_id resolves to exactly one timeline clip.
        #   (clip_segment_ids is used only for ordering, so this was not guaranteed before.)
        segment_id = action.get("source_segment_id")
        source_id, source_range = segs.get(segment_id, (None, None))
        # The caption exception follows the same overlap axis.
        declared_noncontiguous_repeat = any(
            declared_source_id == source_id
            and declared_contiguous is False
            and source_range is not None
            and min(declared_range[1], source_range[1]) - max(declared_range[0], source_range[0]) > 0
            for declared_source_id, declared_range, declared_contiguous in declared_repeat
        )
        gate.require("caption-clip-binding", len(clips_by_segment.get(segment_id, [])) == 1 or declared_noncontiguous_repeat, f"caption action {action.get('action_id')}: source_segment_id {segment_id!r} must resolve to exactly one timeline clip unless its reused source range has declared act_repeat_segment.contiguous=false")
    reproducibility = plan.get("reproducibility", {})
    gate.require("reproducibility-slots", {"ffmpeg_version", "filter_arguments", "loudnorm_two_pass", "render_log_id", "output_sha256"} <= set(reproducibility), "reproducibility: all slots required")
    for phase in ("pre_render", "post_render"):
        qc = plan.get("qc", {}).get(phase, {}); gate.require("qc-no-auto-retry", qc.get("severity") in {"info", "warning", "critical"} and qc.get("state") in {"pending", "approved", "failed", "escalate_review"} and qc.get("auto_retry") is False, f"QC {phase}: severity/state and no automatic retry required")

def validate_skill_output(data, index, gate):
    gate.require("schema-instance-validation", data.get("schema_version") == "1.0", "skill output: schema version required")
    gate.require("skill-output-kind", data.get("output_kind") in {"selection", "proposal", "constraint", "review", "failure"}, "skill output: closed output kind")
    confidence = data.get("confidence"); gate.require("schema-instance-validation", type(confidence) in {int, float} and 0 <= confidence <= 1, "skill output: confidence must be 0..1")
    scope = data.get("scope", {}); gate.require("skill-output-scope-evidence", scope.get("source_id") in index["source_ids"], "skill output: scope source must exist")
    scope_range = range_value(scope.get("range"), gate, "skill output scope")
    gate.require("skill-output-scope-evidence", scope_range[0] >= 0 and scope_range[1] <= index["durations"].get(scope.get("source_id"), 0), "skill output: scope range must be inside source")
    gate.require("skill-output-scope-evidence", bool(data.get("impact")) and set(data.get("evidence_ids", [])) <= index["evidence_ids"], "skill output: impact/evidence required")
    for dep in data.get("dependencies", []): gate.require("dependency-missing-behaviour", type(dep.get("required")) is bool and dep.get("on_missing") in {"skip", "empty_result", "escalate_review"} and bool(dep.get("skill_id")), "dependency: required and on_missing must be explicit")
    review = data.get("review", {}); gate.require("skill-review-blocking", review.get("severity") in {"info", "warning", "critical"} and type(review.get("blocking")) is bool and review.get("state") in {"open", "approved", "released"}, "skill output review: severity/blocking/state required")

def main():
    parser = argparse.ArgumentParser(description="Validate Draftcut contract files, an Observation Index, an EditPlan, or a skill output. Plan and skill-output validation require the paired Index."); parser.add_argument("--contracts", action="store_true", help="Validate the bundled contract schemas and example fixtures."); parser.add_argument("--index", help="Observation Index JSON to validate; required with --plan or --skill-output."); parser.add_argument("--plan", help="EditPlan JSON to validate; requires --index."); parser.add_argument("--skill-output", help="Skill output JSON to validate; requires --index."); parser.add_argument("--l2-scope", type=Path, default=ROOT / "contracts/l2-execution-scope.json", help="Override the executor action-scope declaration; intended for development checks."); parser.add_argument("--executor-assemble", type=Path, default=ROOT / "executor/assemble.py", help="Executor Python source to inspect for action-scope consistency; intended for development checks.")
    args = parser.parse_args(); gate = Gate()
    bindings, binding_errors = static_rule_bindings()
    validate_rule_bindings(gate, bindings, binding_errors)
    try:
        validate_l2_execution_scope(gate, args.l2_scope, args.executor_assemble)
        schemas = load_contract_schemas(gate)
        if args.contracts: validate_contract_files(gate, schemas)
        index_data = read_json(args.index) if args.index else None
        index_schema_valid = validate_against_contract_schema(index_data, "index.schema.json", schemas, gate) if index_data is not None else False
        index = validate_index(index_data, gate) if index_schema_valid else None
        if args.plan:
            plan_data = read_json(args.plan)
            plan_schema_valid = validate_against_contract_schema(plan_data, "edit-plan.schema.json", schemas, gate)
            if index is None: gate.require("plan-index-id", False, "plan validation requires paired valid --index")
            elif plan_schema_valid:
                validate_plan(plan_data, index, gate)
        if args.skill_output:
            output_data = read_json(args.skill_output)
            output_schema_valid = validate_against_contract_schema(output_data, "skill-output.schema.json", schemas, gate)
            if index is None: gate.require("skill-output-scope-evidence", False, "skill output validation requires paired valid --index")
            elif output_schema_valid:
                validate_skill_output(output_data, index, gate)
    except ValueError as exc: gate.errors.append(str(exc))
    if gate.errors:
        for error in gate.errors: print("CONTRACT FAIL: " + error, file=sys.stderr)
        return 1
    print(f"CONTRACT PASS: {len(bindings)} rules bound to {sum(bindings.values())} checks")
    return 0

if __name__ == "__main__": sys.exit(main())
