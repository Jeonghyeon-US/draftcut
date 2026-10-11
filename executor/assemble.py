#!/usr/bin/env python3
"""Render an EditPlan as an MP4 and write execution evidence.
``--validate-only`` checks plan-internal values; it does not inspect media or assets,
verify file hashes, or execute ffmpeg."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import time
import wave
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .concat_check import check_concat
    from .normalize import (
        AUDIO_BITRATE, AUDIO_CHANNEL_LAYOUT, AUDIO_CHANNELS, AUDIO_CODEC, AUDIO_SAMPLE_RATE,
        _require_policy, build_normalize_argv, build_normalized_source_argv,
        needs_normalized_copy, require_present_audio_stream,
    )
    from .policy import RationalValueError, contract_fraction, ffmpeg_rational
    from .probe import ffmpeg_path, ffprobe_path, probe_format_name, probe_json, run_command
except ImportError: # Direct execution from executor/.
    from concat_check import check_concat
    from normalize import (
        AUDIO_BITRATE, AUDIO_CHANNEL_LAYOUT, AUDIO_CHANNELS, AUDIO_CODEC, AUDIO_SAMPLE_RATE,
        _require_policy, build_normalize_argv, build_normalized_source_argv,
        needs_normalized_copy, require_present_audio_stream,
    )
    from policy import RationalValueError, contract_fraction, ffmpeg_rational
    from probe import ffmpeg_path, ffprobe_path, probe_format_name, probe_json, run_command


class AssemblyError(ValueError):
    """Raised before an unsupported or unverified plan can render media."""


# measured this fixed AAC output: every audio packet spans 1024
# samples at the executor's fixed 48 kHz output rate. D must measure packet
# counts from rendered pieces; planned clip lengths are not a substitute.
AAC_AUDIO_PACKET_SAMPLES = 1024
AAC_AUDIO_SAMPLE_RATE = 48_000


# The only two measured render points: 360s at 35 masks, 720s at 88 masks.
# So the slope is (720 - 360) / (88 - 35) = 360 / 53 seconds per mask and the
# intercept is 360 - 35 * (360 / 53) = 6480 / 53 seconds. No other variable has
# a measured coefficient.
ESTIMATE_MODEL_VERSION = "structure-range-v1"
_MASK_SECONDS_PER_ASSET = 360.0 / 53.0
_MASK_SECONDS_INTERCEPT = 6480.0 / 53.0
_REFERENCE_OUTPUT_PIXELS = 3024 * 1522
_REFERENCE_OUTPUT_SECONDS = 354.0

# Reference resolution.
ESTIMATE_REFERENCE_PIXELS = 1920 * 1080
# 102.9s per 741.407s of output. Observed range 0.139-1.241 (an 8.9x load spread).
NORMALIZE_SECONDS_PER_OUTPUT_SECOND = 0.139
# 0.0043, 0.0039, 0.0036 - three measurements in agreement.
CONCAT_CHECK_SECONDS_PER_OUTPUT_SECOND = 0.0043
# Chain path, intercept: 129.15s per 741.407s of output.
CAPTION_CHAIN_PASS_SECONDS_PER_OUTPUT_SECOND = 0.174
# Chain path, slope. Not linear - an interior check came out 1.28x.
CAPTION_CHAIN_SECONDS_PER_CAPTION = 10.42
# Track path, intercept: 862.5s per 741.407s of output. Includes load noise.
CAPTION_TRACK_PASS_SECONDS_PER_OUTPUT_SECOND = 1.164
# Track path, slope.
CAPTION_TRACK_SECONDS_PER_CAPTION = 1.5125
# The full overlay pass when there are no captions and at least one mask.
# Agrees with the mask intercept (0.156) to within 12%.
OVERLAY_PASS_SECONDS_PER_OUTPUT_SECOND = 0.174
# Reuse the existing mask slope from those two points. Do not copy a new value
# here: two constants that can drift apart is the defect this avoids.
MASK_SECONDS_PER_OVERLAY_ASSET = _MASK_SECONDS_PER_ASSET
# In-domain band, wide enough to cover the observed 8.9x load spread.
ESTIMATE_BAND_FACTOR_IN_DOMAIN = 3.0
# Out-of-domain band.
ESTIMATE_BAND_FACTOR_OUT_OF_DOMAIN = 5.0
ESTIMATE_DOMAIN = {
    "captions": (0, 248), "masks": (0, 76), "pieces": (1, 25),
    "output_seconds": (99.93, 3060.0), "output_pixels": (2073600, 4602528),
}
ESTIMATE_BREAKDOWN_OUTPUT_KEYS = frozenset({
    "estimated_seconds", "band_seconds", "terms_seconds", "structure", "provenance", "domain",
})

#: shape A is faster at N=2 and the independent-piece track (shape B)
# wins at N=25 and above. The crossover between 2 and 25 was not measured, so
# this is a deliberately fixed conservative threshold, not an inferred optimum.
CAPTION_TRACK_THRESHOLD = 25

# Public L2 dispatch boundary. `_validate_actions` uses this exact object, so
# consumers that inspect this export and the dispatcher cannot drift apart.
L2_DISPATCH_ACTIONS = frozenset({
    "act_trim_segment", "act_assemble_segment", "act_scale_position", "act_crop_reframe",
    "act_add_caption_segment", "act_speed_change", "act_overlay_asset",
    "act_reorder_segments", "act_freeze_frame", "act_zoom_keyframes", "act_blur_region",
})

# Public executability-claim axes. `executability_claim_axis_errors` uses
# these exact exports, so consumers that inspect this export and the dispatcher cannot drift apart.
# Declaration actions (the contract), terminal output, output_policy, and the evidence probe are handled outside L2 dispatch.
HANDLED_OUTSIDE_L2_DISPATCH = frozenset({
    "act_repeat_segment", "act_ripple_delete", "act_insert_segment", "act_export_media", "act_remux_media", "act_change_canvas",
    "act_extract_capture_evidence_frames", "act_style_caption_segment",
    # Configuration D ran end to end from a single plan, and on real material all
    # four acceptance criteria were green: (1) the window matched the design with
    # correlation 1.0000 and 0 samples of drift,
    # ② audio_final_check passed (video 11.6116 / audio 11.611) ·
    # (3) correlation outside the window 0.9994, (4) no clipping and no new silence.
    # This is not a dispatch entry: it is one audio pass after concat, so it belongs here.
    "act_crossfade_audio",
    # the contract: fixed-order tail on the post-concat audio pass, not L2 dispatch.
    "act_gain", "act_normalize_loudness", "act_noise_reduce", "act_fade_audio",
    # the contract: one post-concat, length-preserving amix pass, not L2 dispatch.
    "act_mix_audio", "act_add_sfx", "act_add_ambience", "act_add_music", "act_duck_audio",
})
# These effects are ffmpeg-verified, but this v1 slice has no path from plan to render for them.
NO_L2_PATH_ACTIONS = frozenset({
    "act_j_cut", "act_l_cut",
})


def executability_claim_axis_errors(
    entries: Sequence[Mapping[str, Any]],
    l2_dispatch_actions: frozenset[str] = L2_DISPATCH_ACTIONS,
    handled_outside_l2_dispatch: frozenset[str] = HANDLED_OUTSIDE_L2_DISPATCH,
    no_l2_path_actions: frozenset[str] = NO_L2_PATH_ACTIONS,
) -> list[str]:
    """Return claim-axis drift errors without changing the exported constants.

    ``yes(effect-verified)`` reports that ffmpeg can produce an effect; these
    three axes separately report whether this v1 executor has a render path.
    """
    registered_actions = {
        entry.get("action") for entry in entries if isinstance(entry.get("action"), str)
    }
    effect_verified_actions = {
        entry["action"]
        for entry in entries
        if isinstance(entry.get("action"), str)
        and isinstance(entry.get("v1_executable"), str)
        and entry["v1_executable"].startswith("yes(effect-verified")
    }
    axes = {
        "L2 dispatch": l2_dispatch_actions,
        "handled elsewhere": handled_outside_l2_dispatch,
        "no L2 path": no_l2_path_actions,
    }
    errors: list[str] = []
    if len(effect_verified_actions) != 31:
        errors.append(
            f"effect-verified registry count is {len(effect_verified_actions)}, expected 31"
        )
    for label, actions in axes.items():
        unknown = actions - registered_actions
        if unknown:
            errors.append(f"{label} contains unregistered action(s): {sorted(unknown)}")
    for left_label, left_actions in axes.items():
        for right_label, right_actions in axes.items():
            if left_label >= right_label:
                continue
            overlap = left_actions & right_actions
            if overlap:
                errors.append(
                    f"{left_label} and {right_label} overlap: {sorted(overlap)}"
                )
    classified_actions = set().union(*axes.values())
    missing = effect_verified_actions - classified_actions
    extra = classified_actions - effect_verified_actions
    if missing:
        errors.append(f"effect-verified actions missing an axis: {sorted(missing)}")
    if extra:
        errors.append(f"claim-axis actions are not effect-verified: {sorted(extra)}")
    return errors

VALIDATE_ONLY_NOTICE = (
    "Passing these plan-internal checks does not establish that the executor can render the plan. Media, asset files, hashes, and rendering are not checked."
)
_VALIDATE_ONLY_SOURCE_PATH = "/nonexistent/draftcut-validate-only-source.mov"
_VALIDATE_ONLY_OUTPUT_PATH = "/nonexistent/draftcut-validate-only-output.mp4"


def _nonnegative_count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AssemblyError(f"{label} must be a non-negative integer")
    return value


def estimate_assembly_seconds(
    piece_count: int,
    overlay_asset_count: int,
    caption_count: int,
    output_pixels: int,
    output_duration_seconds: float,
) -> float:
    """Legacy gate-compatible mask estimate; the CLI uses the path.

    ``piece_count`` and ``caption_count`` are retained as plan facts for the
    caller's report, but do not affect the estimate: no measurement separates
    their cost.  Only the mask count comes from measurement; scaling by resolution
    and duration is unverified extrapolation.
    """
    _nonnegative_count(piece_count, "piece_count")
    masks = _nonnegative_count(overlay_asset_count, "overlay_asset_count")
    _nonnegative_count(caption_count, "caption_count")
    if isinstance(output_pixels, bool) or not isinstance(output_pixels, int) or output_pixels <= 0:
        raise AssemblyError("output_pixels must be a positive integer")
    if (isinstance(output_duration_seconds, bool) or
            not isinstance(output_duration_seconds, (int, float)) or
            not math.isfinite(output_duration_seconds) or output_duration_seconds <= 0):
        raise AssemblyError("output_duration_seconds must be positive")
    # Resolution and final-timeline duration have no independent measurement.
    # They are deliberately only proportional extrapolations from the measured point.
    mask_model_seconds = _MASK_SECONDS_INTERCEPT + _MASK_SECONDS_PER_ASSET * masks
    return mask_model_seconds * (output_pixels / _REFERENCE_OUTPUT_PIXELS) * (
        float(output_duration_seconds) / _REFERENCE_OUTPUT_SECONDS
    )


def _estimate_inputs_from_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Read the estimate inputs without resolving media or invoking ffmpeg."""
    policy = plan.get("output_policy")
    if not isinstance(policy, Mapping):
        raise AssemblyError("plan.output_policy must be an object")
    width, height = policy.get("width"), policy.get("height")
    if (isinstance(width, bool) or not isinstance(width, int) or width <= 0 or
            isinstance(height, bool) or not isinstance(height, int) or height <= 0):
        raise AssemblyError("output_policy.width and output_policy.height must be positive integers")
    clips = plan.get("timeline_clips")
    if not isinstance(clips, Sequence) or isinstance(clips, (str, bytes)) or not clips:
        raise AssemblyError("plan.timeline_clips must be a non-empty array")
    timeline_ends: list[Fraction] = []
    for clip in clips:
        if not isinstance(clip, Mapping):
            raise AssemblyError("timeline_clips must contain objects")
        timeline_range = clip.get("timeline_range")
        if not isinstance(timeline_range, Mapping):
            raise AssemblyError("timeline_range must be an object")
        end = _time_fraction(timeline_range.get("end"), "timeline_range.end")
        if end <= 0:
            raise AssemblyError("timeline_range.end must be positive")
        timeline_ends.append(end)
    actions = plan.get("actions")
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
        raise AssemblyError("actions must be an array")
    overlay_asset_count = sum(
        isinstance(action, Mapping) and action.get("action") == "act_overlay_asset"
        for action in actions
    )
    caption_count = sum(
        isinstance(action, Mapping) and action.get("action") == "act_add_caption_segment"
        for action in actions
    )
    duration = max(timeline_ends)
    return {
        "piece_count": len(clips),
        "overlay_asset_count": overlay_asset_count,
        "caption_count": caption_count,
        "width": width,
        "height": height,
        "output_pixels": width * height,
        "output_duration_seconds": float(duration),
    }


def _estimate_caption_track_segments(plan: Mapping[str, Any], policy: Mapping[str, Any], caption_count: int) -> int:
    """Count the actual transparent-track pieces from plan timing, without media."""
    captions = plan.get("captions")
    if (not isinstance(captions, Sequence) or isinstance(captions, (str, bytes))
            or len(captions) != caption_count):
        raise AssemblyError(
            "track-caption estimate requires captions[] whose count matches act_add_caption_segment actions"
        )
    try:
        frame_rate = contract_fraction(policy.get("frame_rate"))
    except (RationalValueError, TypeError) as exc:
        raise AssemblyError("output_policy.frame_rate is invalid for track-caption estimate") from exc
    if frame_rate <= 0:
        raise AssemblyError("output_policy.frame_rate must be positive for track-caption estimate")
    records: list[dict[str, Any]] = []
    for order, caption in enumerate(captions):
        if not isinstance(caption, Mapping) or not isinstance(caption.get("caption_id"), str):
            raise AssemblyError("track-caption estimate requires caption objects with caption_id")
        timeline_range = caption.get("timeline_range")
        if not isinstance(timeline_range, Mapping):
            raise AssemblyError("track-caption estimate requires caption.timeline_range")
        start = _time_fraction(timeline_range.get("start"), "caption.timeline_range.start")
        end = _time_fraction(timeline_range.get("end"), "caption.timeline_range.end")
        records.append({
            "caption_id": caption["caption_id"], "start": start, "end": end,
            # _caption_track_segments only needs these placeholders in addition to time.
            "png": f"/estimate-only/caption-{order}.png", "overlay": {"x": 0, "y": 0},
        })
    return len(_caption_track_segments(records, frame_rate))


def _estimate_assembly_breakdown(plan: Mapping[str, Any], inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Apply sealed range model and count the executor's render structure."""
    pieces = _nonnegative_count(inputs["piece_count"], "piece_count")
    masks = _nonnegative_count(inputs["overlay_asset_count"], "overlay_asset_count")
    captions = _nonnegative_count(inputs["caption_count"], "caption_count")
    pixels = inputs["output_pixels"]
    duration = inputs["output_duration_seconds"]
    if isinstance(pixels, bool) or not isinstance(pixels, int) or pixels <= 0:
        raise AssemblyError("output_pixels must be a positive integer")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not math.isfinite(duration) or duration <= 0):
        raise AssemblyError("output_duration_seconds must be positive")
    policy = plan.get("output_policy")
    if not isinstance(policy, Mapping):
        raise AssemblyError("plan.output_policy must be an object")

    resolution_ratio = float(pixels) / ESTIMATE_REFERENCE_PIXELS
    duration_float = float(duration)
    if captions == 0:
        caption_shape = "none"
        caption_track_segments = 0
        overlay_pass = OVERLAY_PASS_SECONDS_PER_OUTPUT_SECOND * duration_float * resolution_ratio if masks else 0.0
        caption_seconds = 0.0
    elif captions < CAPTION_TRACK_THRESHOLD:
        caption_shape = "chain"
        caption_track_segments = 0
        overlay_pass = CAPTION_CHAIN_PASS_SECONDS_PER_OUTPUT_SECOND * duration_float * resolution_ratio
        caption_seconds = CAPTION_CHAIN_SECONDS_PER_CAPTION * captions
    else:
        caption_shape = "track"
        caption_track_segments = _estimate_caption_track_segments(plan, policy, captions)
        overlay_pass = CAPTION_TRACK_PASS_SECONDS_PER_OUTPUT_SECOND * duration_float * resolution_ratio
        caption_seconds = CAPTION_TRACK_SECONDS_PER_CAPTION * captions

    normalize = NORMALIZE_SECONDS_PER_OUTPUT_SECOND * duration_float * resolution_ratio
    concat_check = CONCAT_CHECK_SECONDS_PER_OUTPUT_SECOND * duration_float * resolution_ratio
    # The mask intercept is one whole-video overlay pass. `overlay_pass`
    # already counts that pass, so applying it to masks here would double count it.
    mask_seconds = MASK_SECONDS_PER_OVERLAY_ASSET * masks
    estimated_seconds = normalize + concat_check + overlay_pass + caption_seconds + mask_seconds
    overlay_passes = 1 if captions > 0 or masks > 0 else 0
    if caption_shape == "chain":
        overlay_filter_inputs, overlay_filter_depth = 1 + captions + masks, captions + masks
    elif caption_shape == "track":
        overlay_filter_inputs, overlay_filter_depth = 1 + 1 + masks, 1 + masks
    else:
        overlay_filter_inputs, overlay_filter_depth = 1 + masks, masks

    values = {
        "captions": captions, "masks": masks, "pieces": pieces,
        "output_seconds": duration_float, "output_pixels": pixels,
    }
    outside: list[dict[str, int | float | str]] = []
    for axis, value in values.items():
        lower, upper = ESTIMATE_DOMAIN[axis]
        if value < lower:
            outside.append({"axis": axis, "value": value, "measured_min": lower})
        elif value > upper:
            outside.append({"axis": axis, "value": value, "measured_max": upper})
    in_domain = not outside
    factor = ESTIMATE_BAND_FACTOR_IN_DOMAIN if in_domain else ESTIMATE_BAND_FACTOR_OUT_OF_DOMAIN
    return {
        "estimated_seconds": float(estimated_seconds),
        "band_seconds": {"low": float(estimated_seconds / factor), "high": float(estimated_seconds * factor)},
        "terms_seconds": {
            "normalize": float(normalize), "concat_check": float(concat_check),
            "overlay_pass": float(overlay_pass), "captions": float(caption_seconds), "masks": float(mask_seconds),
        },
        "structure": {
            "normalize_passes": int(pieces), "overlay_passes": int(overlay_passes),
            "full_encode_passes": int(pieces + overlay_passes), "caption_shape": caption_shape,
            "caption_track_segments": int(caption_track_segments),
            "overlay_filter_inputs": int(overlay_filter_inputs), "overlay_filter_depth": int(overlay_filter_depth),
        },
        "provenance": {
            "normalize": "1 measured point; load spread 8.9x", "concat_check": "3 measured points",
            "overlay_pass": "caption pass and mask intercept measurements", "captions": (
                "2 measured points; nonlinear" if caption_shape == "chain" else
                "2 measured points; load noise" if caption_shape == "track" else "no captions"
            ), "masks": "2 measured points (slope)",
        },
        "domain": {"in_domain": in_domain, "outside": outside},
    }


def _format_estimate_line(inputs: Mapping[str, Any], estimated_seconds: float) -> str:
    duration = inputs["output_duration_seconds"]
    duration_text = f"{duration:.3f}".rstrip("0").rstrip(".")
    return (
        f"ASSEMBLY ESTIMATE: ~{estimated_seconds / 60:.1f} min "
        f"(pieces {inputs['piece_count']} · masks {inputs['overlay_asset_count']} · "
        f"captions {inputs['caption_count']} · {inputs['width']}x{inputs['height']} · {duration_text}s) "
        "- from structure and measured coefficients; scaling by resolution and duration is unverified extrapolation"
    )


def _format_estimate_breakdown_lines(breakdown: Mapping[str, Any]) -> tuple[str, str, str]:
    """Render the sealed range, deterministic structure, and evidence basis."""
    if set(breakdown) != ESTIMATE_BREAKDOWN_OUTPUT_KEYS:
        raise AssemblyError("estimate_breakdown keys do not match its output contract")
    estimated_seconds = float(breakdown["estimated_seconds"])
    # Read each JSON section here so the formatter and producer have one checked shape.
    band = breakdown["band_seconds"]
    terms = breakdown["terms_seconds"]
    structure = breakdown["structure"]
    provenance = breakdown["provenance"]
    domain = breakdown["domain"]
    if abs(estimated_seconds - sum(float(terms[name]) for name in ("normalize", "concat_check", "overlay_pass", "captions", "masks"))) > 1e-6:
        raise AssemblyError("estimate_breakdown terms do not sum to estimated_seconds")
    range_line = (
        f"ASSEMBLY ESTIMATE RANGE: {float(band['low']) / 60:.1f}~{float(band['high']) / 60:.1f} min "
        "(judge budgets against the upper bound)"
    )
    structure_line = (
        f"ASSEMBLY ESTIMATE STRUCTURE: full encode passes {structure['full_encode_passes']} "
        f"(normalize {structure['normalize_passes']} + overlay {structure['overlay_passes']}) · "
        f"caption path {structure['caption_shape']} · track segments {structure['caption_track_segments']} · "
        f"overlay inputs {structure['overlay_filter_inputs']} · depth {structure['overlay_filter_depth']}"
    )
    caption_provenance = provenance["captions"]
    if structure["caption_shape"] == "track":
        if "load noise" not in caption_provenance:
            raise AssemblyError("track caption estimate provenance is incomplete")
        caption_basis = "2 measured points (load noise)"
    elif structure["caption_shape"] == "chain":
        if "nonlinear" not in caption_provenance:
            raise AssemblyError("chain caption estimate provenance is incomplete")
        caption_basis = "2 measured points (nonlinear)"
    else:
        caption_basis = "none"
    basis_line = (
        "ASSEMBLY ESTIMATE BASIS: normalize 1 measured point (load 8.9x) · concat 3 measured points · "
        f"captions-{structure['caption_shape']} {caption_basis} · "
        "masks 2 measured points · resolution/duration scaling is unverified extrapolation"
    )
    if domain["in_domain"]:
        basis_line += " · in domain"
    else:
        labels = {"captions": "captions", "masks": "masks", "pieces": "pieces", "output_seconds": "output duration", "output_pixels": "output pixels"}
        warnings = []
        for item in domain["outside"]:
            if "measured_max" in item:
                warnings.append(f"{labels[item['axis']]} {item['value']} > measured max {item['measured_max']}")
            else:
                warnings.append(f"{labels[item['axis']]} {item['value']} < measured min {item['measured_min']}")
        basis_line += " · WARNING out of domain: " + ", ".join(warnings)
    return range_line, structure_line, basis_line


def _format_actual_line(inputs: Mapping[str, Any], estimated_seconds: float, actual_seconds: float) -> str:
    duration = inputs["output_duration_seconds"]
    duration_text = f"{duration:.3f}".rstrip("0").rstrip(".")
    return (
        f"ASSEMBLY ACTUAL: ~{actual_seconds / 60:.1f} min "
        f"(pieces {inputs['piece_count']} · masks {inputs['overlay_asset_count']} · "
        f"captions {inputs['caption_count']} · {inputs['width']}x{inputs['height']} · {duration_text}s) "
        f"- against the ~{estimated_seconds / 60:.1f} min estimate; from structure and measured coefficients"
    )


def _print_stage(started: float, detail: str) -> None:
    """Emit one flush-safe, stdout-only assembly boundary for background logs."""
    elapsed = time.monotonic() - started
    print(f"ASSEMBLY STAGE: +{elapsed:.1f}s {detail}", flush=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _time_fraction(value: Any, label: str) -> Fraction:
    if not isinstance(value, Mapping) or set(value) != {"value", "timebase"}:
        raise AssemblyError(f"{label} must contain exactly value and timebase")
    tick = value["value"]
    if isinstance(tick, bool) or not isinstance(tick, int):
        raise AssemblyError(f"{label}.value must be an integer tick")
    try:
        return tick * contract_fraction(value["timebase"])
    except (RationalValueError, TypeError) as exc:
        raise AssemblyError(f"{label}.timebase is invalid: {exc}") from exc


def _floor_decimal(value: Fraction, places: int = 6) -> str:
    """Format a non-negative rational as a decimal truncated at ``places``."""
    if value < 0:
        raise AssemblyError("internal error: cannot format a negative seek value")
    scale = 10 ** places
    units = value.numerator * scale // value.denominator
    whole, remainder = divmod(units, scale)
    return f"{whole}.{remainder:0{places}d}"


def _source_timing(source: Path) -> Fraction:
    """Return the first video's start offset from the container, without floats."""
    try:
        observation = probe_json([
            "-v", "error", "-select_streams", "v:0", "-show_entries",
            "stream=start_time:format=start_time", "-of", "json",
        ], source)
    except Exception as exc:
        raise AssemblyError(f"cannot probe source start offset for {source}") from exc
    try:
        if observation.get("returncode") != 0:
            raise ValueError("ffprobe failed")
        payload = observation.get("json")
        video_start = payload["streams"][0]["start_time"]
        format_start = payload["format"]["start_time"]
        if not isinstance(video_start, str) or not isinstance(format_start, str):
            raise ValueError("ffprobe start_time values must be strings")
        return Fraction(video_start) - Fraction(format_start)
    except (AttributeError, KeyError, IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise AssemblyError(f"cannot probe source start offset for {source}") from exc


def _source_audio_start_gap(source: Path) -> Fraction:
    """Return first-audio minus first-video start, preserving rational headers."""
    try:
        observation = probe_json([
            "-v", "error", "-show_entries", "stream=codec_type,start_time", "-of", "json",
        ], source)
    except Exception as exc:
        raise AssemblyError(f"cannot probe source audio start gap for {source}") from exc
    try:
        if observation.get("returncode") != 0:
            raise ValueError("ffprobe failed")
        streams = observation["json"]["streams"]
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        audio = next(stream for stream in streams if stream["codec_type"] == "audio")
        video_start, audio_start = video["start_time"], audio["start_time"]
        if not isinstance(video_start, str) or not isinstance(audio_start, str):
            raise ValueError("ffprobe start_time values must be strings")
        return Fraction(audio_start) - Fraction(video_start)
    except (AttributeError, KeyError, TypeError, ValueError, StopIteration, ZeroDivisionError) as exc:
        raise AssemblyError(f"cannot probe source audio start gap for {source}") from exc


def cut_timing(source_range: Mapping[str, Any], frame_rate: Fraction) -> dict[str, Any]:
    """Apply without ever converting contract time through float."""
    if frame_rate <= 0:
        raise AssemblyError("output_policy.frame_rate must be positive")
    start_seconds = _time_fraction(source_range.get("start"), "source_range.start")
    end_seconds = _time_fraction(source_range.get("end"), "source_range.end")
    if start_seconds < 0:
        raise AssemblyError("source_range.start must not be negative")
    if end_seconds <= start_seconds:
        raise AssemblyError("source_range.end must be greater than source_range.start")
    start_coordinate = start_seconds * frame_rate
    start_index = start_coordinate.numerator // start_coordinate.denominator
    frame_coordinate = (end_seconds - start_seconds) * frame_rate
    if frame_coordinate.denominator != 1:
        raise AssemblyError(
            "D-25 requires (end_seconds - start_seconds) * frame_rate to be an integer; "
            f"calculated {frame_coordinate.numerator}/{frame_coordinate.denominator}"
        )
    frame_count = frame_coordinate.numerator
    end_index = start_index + frame_count
    if frame_count <= 0:
        raise AssemblyError("frame_count must be positive")
    # Aim at the frame BOUNDARY, not the frame centre.  `-ss` does not give "the frame
    # containing that instant"; it gives "the first frame whose pts is at or after the
    # target" (after rounding to stream ticks).  Aiming at the centre lands one frame
    # late; measurement confirmed the boundary +/-1us yields the same frame.
    # The 6-digit truncation error (<=1us) is under half a tick interval
    # (1/15360 ~= 65us), so it cannot change the verdict.
    seek_target = Fraction(start_index, 1) / frame_rate
    duration = Fraction(frame_count, 1) / frame_rate
    return {
        "start_seconds": {"num": start_seconds.numerator, "den": start_seconds.denominator},
        "end_seconds": {"num": end_seconds.numerator, "den": end_seconds.denominator},
        "start_index": start_index,
        "end_index": end_index,
        "frame_count": frame_count,
        "seek_seconds": _floor_decimal(seek_target),
        "duration_seconds": _floor_decimal(duration),
    }


def _validate_crop_reframe_params(params: Any) -> Mapping[str, Any]:
    required = {"x", "y", "width", "height", "coordinate_origin"}
    if not isinstance(params, Mapping) or set(params) != required:
        raise AssemblyError("act_crop_reframe.params must contain exactly x, y, width, height, and coordinate_origin")
    for field in ("x", "y", "width", "height"):
        value = params[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise AssemblyError(f"act_crop_reframe.params.{field} must be an integer")
    if params["coordinate_origin"] != "top_left":
        raise AssemblyError("act_crop_reframe.params.coordinate_origin must be top_left")
    return params


def _fraction_evidence(value: Fraction) -> dict[str, int]:
    return {"num": value.numerator, "den": value.denominator}


def _fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def _validate_speed_change_params(params: Any, action_id: Any) -> dict[str, Any]:
    required = {"range", "factor", "audio_handling"}
    if not isinstance(params, Mapping) or set(params) != required:
        raise AssemblyError(
            "act_speed_change.params must contain exactly range, factor, and audio_handling"
        )
    if params["audio_handling"] not in {"drop", "resample"}:
        raise AssemblyError("act_speed_change.params.audio_handling must be drop or resample")
    start = _time_fraction(params["range"].get("start") if isinstance(params["range"], Mapping) else None,
                           f"act_speed_change {action_id!r}.params.range.start")
    end = _time_fraction(params["range"].get("end") if isinstance(params["range"], Mapping) else None,
                         f"act_speed_change {action_id!r}.params.range.end")
    if start < 0 or end <= start:
        raise AssemblyError(f"act_speed_change {action_id!r}.params.range must be non-negative and non-empty")
    try:
        factor = contract_fraction(params["factor"])
    except (RationalValueError, TypeError) as exc:
        raise AssemblyError(f"act_speed_change {action_id!r}.params.factor is invalid: {exc}") from exc
    if factor <= 0:
        raise AssemblyError(f"act_speed_change {action_id!r}.params.factor must be positive")
    if params["audio_handling"] == "resample" and not Fraction(1, 2) <= factor <= Fraction(100, 1):
        raise AssemblyError(
            f"act_speed_change {action_id!r}.params.audio_handling=resample requires "
            "factor between 1/2 and 100/1 for atempo"
        )
    return {
        "action_id": action_id,
        "range_start": start,
        "range_end": end,
        "factor": factor,
        "factor_ffmpeg": ffmpeg_rational(params["factor"]),
        "audio_handling": params["audio_handling"],
    }


def _validate_freeze_frame_params(params: Any, action_id: Any) -> dict[str, Any]:
    """Validate the declared values; frame-derived speed stays executor-owned."""
    required = {"source_time", "timeline_duration"}
    if not isinstance(params, Mapping) or set(params) != required:
        raise AssemblyError(
            "act_freeze_frame.params must contain exactly source_time and timeline_duration"
        )
    source_time = _time_fraction(params["source_time"],
                                 f"act_freeze_frame {action_id!r}.params.source_time")
    timeline_duration = _time_fraction(
        params["timeline_duration"],
        f"act_freeze_frame {action_id!r}.params.timeline_duration",
    )
    if source_time < 0 or timeline_duration <= 0:
        raise AssemblyError(
            f"act_freeze_frame {action_id!r}.params source_time must be non-negative and "
            "timeline_duration must be positive"
        )
    return {
        "action_id": action_id,
        "source_time": source_time,
        "timeline_duration": timeline_duration,
    }


def _freeze_speed_for_clip(action: Mapping[str, Any], clip_id: Any, source_start: Fraction,
                           source_end: Fraction, timeline_start: Fraction, timeline_end: Fraction,
                           frame_rate: Fraction) -> dict[str, Any]:
    """Turn a validated freeze declaration into the existing speed representation."""
    source_duration = source_end - source_start
    frame_duration = Fraction(1, 1) / frame_rate
    if source_duration != frame_duration:
        raise AssemblyError(f"clip {clip_id!r} act_freeze_frame source_range must be exactly one frame")
    if action["source_time"] != source_start:
        raise AssemblyError(f"clip {clip_id!r} act_freeze_frame source_time must equal clip source_range.start")
    output_frames = action["timeline_duration"] * frame_rate
    if output_frames.denominator != 1:
        raise AssemblyError(
            f"clip {clip_id!r} act_freeze_frame timeline_duration * frame_rate "
            f"must be an integer; calculated {_fraction_text(output_frames)} frames"
        )
    timeline_duration = timeline_end - timeline_start
    if timeline_duration != action["timeline_duration"]:
        raise AssemblyError(
            f"clip {clip_id!r} act_freeze_frame timeline duration mismatch: "
            f"declared {_fraction_text(action['timeline_duration'])} seconds, "
            f"timeline {_fraction_text(timeline_duration)} seconds"
        )
    factor = frame_duration / action["timeline_duration"]
    return {
        "action_id": action["action_id"], "factor": factor, "factor_ffmpeg": _fraction_text(factor),
        "audio_handling": "drop", "source_duration": source_duration,
        "output_duration": action["timeline_duration"], "output_frame_count": output_frames.numerator,
        "freeze_frame": True,
    }


def _validate_zoom_keyframes_params(params: Any, action_id: Any) -> dict[str, Any]:
    required = {"interpolation", "keyframes"}
    if not isinstance(params, Mapping) or set(params) != required:
        raise AssemblyError(
            "act_zoom_keyframes.params must contain exactly interpolation and keyframes"
        )
    if params["interpolation"] != "linear":
        raise AssemblyError("act_zoom_keyframes.params.interpolation must be linear")
    keyframes = params["keyframes"]
    if not isinstance(keyframes, Sequence) or isinstance(keyframes, (str, bytes)) or len(keyframes) < 2:
        raise AssemblyError("act_zoom_keyframes.params.keyframes must contain at least two records")
    parsed: list[dict[str, Any]] = []
    previous_time: Fraction | None = None
    for index, keyframe in enumerate(keyframes):
        if not isinstance(keyframe, Mapping) or set(keyframe) != {"time", "zoom", "center_x", "center_y"}:
            raise AssemblyError(
                f"act_zoom_keyframes {action_id!r}.params.keyframes[{index}] must contain exactly "
                "time, zoom, center_x, and center_y"
            )
        time_value = _time_fraction(keyframe["time"],
                                    f"act_zoom_keyframes {action_id!r}.params.keyframes[{index}].time")
        try:
            zoom = contract_fraction(keyframe["zoom"])
        except (RationalValueError, TypeError) as exc:
            raise AssemblyError(
                f"act_zoom_keyframes {action_id!r}.params.keyframes[{index}].zoom is invalid: {exc}"
            ) from exc
        if zoom <= 0:
            raise AssemblyError(
                f"act_zoom_keyframes {action_id!r}.params.keyframes[{index}].zoom must be positive"
            )
        if any(isinstance(keyframe[field], bool) or not isinstance(keyframe[field], int)
               for field in ("center_x", "center_y")):
            raise AssemblyError(
                f"act_zoom_keyframes {action_id!r}.params.keyframes[{index}] centers must be integers"
            )
        if previous_time is not None and time_value <= previous_time:
            raise AssemblyError(
                f"act_zoom_keyframes {action_id!r}.params.keyframes times must be strictly increasing"
            )
        previous_time = time_value
        parsed.append({"time": time_value, "zoom": zoom,
                       "center_x": keyframe["center_x"], "center_y": keyframe["center_y"]})
    return {"action_id": action_id, "keyframes": parsed}


def _validate_blur_region_params(params: Any, action_id: Any) -> dict[str, Any]:
    """Validate the contract's declared filter and fixed output-canvas rectangle."""
    required = {"filter", "strength", "x", "y", "width", "height"}
    if not isinstance(params, Mapping) or set(params) != required:
        raise AssemblyError(
            "act_blur_region.params must contain exactly filter, strength, x, y, width, and height"
        )
    filter_name = params["filter"]
    if filter_name not in {"gaussian", "box"}:
        raise AssemblyError("act_blur_region.params.filter must be gaussian or box")
    try:
        strength = contract_fraction(params["strength"])
    except (RationalValueError, TypeError) as exc:
        raise AssemblyError(f"act_blur_region {action_id!r}.params.strength is invalid: {exc}") from exc
    if strength <= 0:
        raise AssemblyError(f"act_blur_region {action_id!r}.params.strength must be positive")
    rectangle: dict[str, int] = {}
    for field in ("x", "y", "width", "height"):
        value = params[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise AssemblyError(f"act_blur_region.params.{field} must be an integer")
        rectangle[field] = value
    if rectangle["x"] < 0 or rectangle["y"] < 0:
        raise AssemblyError("act_blur_region.params.x and y must be non-negative")
    if rectangle["width"] < 2 or rectangle["height"] < 2:
        raise AssemblyError("act_blur_region.params.width and height must be at least 2")
    if any(value % 2 for value in rectangle.values()):
        raise AssemblyError("act_blur_region.params x, y, width, and height must all be even")
    return {
        "action_id": action_id, "filter": filter_name, "strength": strength,
        "strength_ffmpeg": _fraction_text(strength), **rectangle,
    }


def _validate_blur_region_policy(
    blur_by_segment: Mapping[str, Sequence[Mapping[str, Any]]], policy: Mapping[str, Any],
) -> None:
    """Reject declared rectangles outside the contract's output-policy coordinate system."""
    width, height = policy.get("width"), policy.get("height")
    if isinstance(width, bool) or not isinstance(width, int) or isinstance(height, bool) or not isinstance(height, int):
        raise AssemblyError("act_blur_region requires integer output_policy.width and height")
    for actions in blur_by_segment.values():
        for action in actions:
            if action["x"] + action["width"] > width or action["y"] + action["height"] > height:
                raise AssemblyError(
                    f"act_blur_region {action['action_id']!r} rectangle exceeds output_policy canvas {width}x{height}"
                )


def _validate_speed_audio_policy(
    speed_by_segment: Mapping[str, Sequence[Mapping[str, Any]]], policy: Mapping[str, Any],
) -> None:
    """`resample` needs the source audio that `audio_policy: none` omits."""
    if policy.get("audio_policy") != "none":
        return
    for actions in speed_by_segment.values():
        for action in actions:
            if action["audio_handling"] == "resample":
                raise AssemblyError(
                    "act_speed_change.params.audio_handling=resample requires "
                    "output_policy.audio_policy=present"
                )


def _timeline_clips_in_concat_order(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return the one stable concat order used by validation and rendering."""
    clips = plan.get("timeline_clips")
    if not isinstance(clips, Sequence) or isinstance(clips, (str, bytes)):
        raise AssemblyError("act_crossfade_audio requires plan.timeline_clips to be an array")
    ordered: list[tuple[Fraction, Mapping[str, Any]]] = []
    for clip in clips:
        if not isinstance(clip, Mapping) or not isinstance(clip.get("timeline_range"), Mapping):
            raise AssemblyError("act_crossfade_audio requires timeline_clips.timeline_range objects")
        timeline_range = clip["timeline_range"]
        start = _time_fraction(timeline_range.get("start"), "timeline_range.start")
        end = _time_fraction(timeline_range.get("end"), "timeline_range.end")
        if end <= start:
            raise AssemblyError("act_crossfade_audio requires positive timeline clip durations")
        ordered.append((start, clip))
    # `sorted` is stable. Keeping equal-start clips in plan order is the same
    # tie-break the render loop uses; join_index must never select a different pair.
    return [clip for _, clip in sorted(ordered, key=lambda item: item[0])]


def _validate_timeline_clip_tiling(clips: Sequence[Mapping[str, Any]]) -> None:
    """Require concat-ordered clips to tile the final timeline exactly."""
    if not clips:
        return
    first = clips[0]
    first_start = _time_fraction(first["timeline_range"].get("start"), "timeline_range.start")
    if first_start != 0:
        raise AssemblyError(
            f"timeline clips must start at 0 seconds: first clip {first.get('clip_id')!r} "
            f"starts at {_fraction_text(first_start)} seconds"
        )
    for previous, following in zip(clips, clips[1:]):
        previous_end = _time_fraction(previous["timeline_range"].get("end"), "timeline_range.end")
        following_start = _time_fraction(following["timeline_range"].get("start"), "timeline_range.start")
        if following_start < previous_end:
            raise AssemblyError(
                f"timeline clips overlap: clip {previous.get('clip_id')!r} ends at "
                f"{_fraction_text(previous_end)} seconds, but clip {following.get('clip_id')!r} starts at "
                f"{_fraction_text(following_start)} seconds"
            )
        if following_start > previous_end:
            raise AssemblyError(
                f"timeline clips have a gap: clip {previous.get('clip_id')!r} ends at "
                f"{_fraction_text(previous_end)} seconds, but clip {following.get('clip_id')!r} starts at "
                f"{_fraction_text(following_start)} seconds"
            )


def _crossfade_clip_durations(plan: Mapping[str, Any]) -> list[Fraction]:
    """Return concat-order clip lengths from the plan, never from a media probe."""
    return [
        _time_fraction(clip["timeline_range"].get("end"), "timeline_range.end")
        - _time_fraction(clip["timeline_range"].get("start"), "timeline_range.start")
        for clip in _timeline_clips_in_concat_order(plan)
    ]


def _validated_piece_audio_packets(
    piece_audio_packets: Sequence[Any], piece_count: int, *, label: str,
) -> list[int]:
    """Require the one measured AAC packet count for every rendered piece."""
    if (not isinstance(piece_audio_packets, Sequence)
            or isinstance(piece_audio_packets, (str, bytes))
            or len(piece_audio_packets) != piece_count):
        raise AssemblyError(f"{label} requires one measured audio packet count per clip")
    packets: list[int] = []
    for packet_count in piece_audio_packets:
        if isinstance(packet_count, bool) or not isinstance(packet_count, int) or packet_count <= 0:
            raise AssemblyError(f"{label} requires positive integer audio packet counts")
        packets.append(packet_count)
    return packets


def _measured_piece_content_start_samples(piece_audio_packets: Sequence[int], piece_index: int) -> int:
    """Return a piece-content start on the real concat audio sample axis.

: content starts after the preceding *measured* packet counts plus
    one AAC packet. For a join after ``join_index``, use the right piece's
    content start (``piece_index = join_index + 1``).
    """
    if not 0 <= piece_index < len(piece_audio_packets):
        raise AssemblyError("measured piece content start is outside rendered pieces")
    return (sum(piece_audio_packets[:piece_index]) + 1) * AAC_AUDIO_PACKET_SAMPLES


def _measured_join_time(piece_audio_packets: Sequence[int], join_index: int) -> Fraction:
    """Return D's measured seam time, never a sum of planned clip durations."""
    return Fraction(
        _measured_piece_content_start_samples(piece_audio_packets, join_index + 1),
        AAC_AUDIO_SAMPLE_RATE,
    )


def _planned_piece_sample_boundaries(clip_durations: Sequence[Fraction]) -> list[int]:
    """Return planned-axis sample boundaries by exact cumulative rounding.

    The executor's frame counts are the duration authority. Their conversion
    to 48 kHz need not be integral per piece, so accumulate as ``Fraction``
    and round only at each boundary; never introduce float drift here.
    """
    if not clip_durations:
        raise AssemblyError("planned piece boundaries require one or more clip durations")
    cumulative = Fraction()
    boundaries = [0]
    for duration in clip_durations:
        if not isinstance(duration, Fraction) or duration <= 0:
            raise AssemblyError("planned piece boundaries require positive Fraction durations")
        cumulative += duration * AAC_AUDIO_SAMPLE_RATE
        boundaries.append(round(cumulative))
    return boundaries


def _planned_piece_content_start_samples(clip_durations: Sequence[Fraction], piece_index: int) -> int:
    """Return a piece-content start on the intended, frame-derived plan axis."""
    boundaries = _planned_piece_sample_boundaries(clip_durations)
    if not 0 <= piece_index < len(clip_durations):
        raise AssemblyError("planned piece content start is outside rendered pieces")
    return boundaries[piece_index]


def _planned_join_time(clip_durations: Sequence[Fraction], join_index: int) -> Fraction:
    """Return the planned-axis time for the right side of a declared join."""
    return Fraction(
        _planned_piece_content_start_samples(clip_durations, join_index + 1),
        AAC_AUDIO_SAMPLE_RATE,
    )


def _measured_left_cut(source_start: Fraction, packet_count: int) -> Fraction:
    """Return the left piece's actual original-source content end.

    A rendered AAC piece contains exactly ``packet_count * 1024`` samples of
    content. Its original-source end must therefore follow that measured
    duration, rather than the planned ``source_range.end``.
    """
    return source_start + Fraction(
        packet_count * AAC_AUDIO_PACKET_SAMPLES,
        AAC_AUDIO_SAMPLE_RATE,
    )


def _even_crossfade_duration_samples(duration: Fraction) -> int:
    """Return a blend length whose centred half-window lands on samples."""
    duration_samples = duration * AAC_AUDIO_SAMPLE_RATE
    if duration_samples.denominator != 1 or duration_samples.numerator % 2:
        raise AssemblyError(
            "act_crossfade_audio duration must span an even number of samples "
            "so its half-window boundaries are integer samples"
        )
    return duration_samples.numerator


def _piece_audio_packet_count(piece: Path) -> int:
    """Count AAC packets from a rendered piece with the required ffprobe probe."""
    observation = run_command([
        ffprobe_path(), "-v", "error", "-select_streams", "a:0", "-count_packets",
        "-show_entries", "stream=nb_read_packets", "-of", "default=nw=1:nk=1", piece,
    ])
    raw_count = observation.get("stdout", "").strip()
    if observation.get("returncode") != 0 or not raw_count.isdecimal():
        raise AssemblyError(f"could not measure audio packet count for rendered piece {piece}")
    packet_count = int(raw_count)
    if packet_count <= 0:
        raise AssemblyError(f"rendered piece {piece} has no audio packets")
    return packet_count


def _crossfade_audio_joins(
    plan: Mapping[str, Any], policy: Mapping[str, Any], piece_audio_packets: Sequence[Any] | None = None,
    clip_durations: Sequence[Fraction] | None = None,
) -> list[dict[str, Any]]:
    """Validate declared joins; add planned seam times only after piece rendering.

    ``--validate-only`` cannot open media, so it validates declarations without
    inventing a seam. Real assembly supplies measured packet counts only for
    the initial re-splice; its post-re-splice windows use the planned axis.
    """
    actions = plan.get("actions")
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
        raise AssemblyError("act_crossfade_audio requires actions to be an array")
    declared = [action for action in actions if isinstance(action, Mapping)
                and action.get("action") == "act_crossfade_audio"]
    if not declared:
        return []
    if policy.get("audio_policy") != "present":
        raise AssemblyError("act_crossfade_audio requires output_policy.audio_policy=present")
    durations = list(clip_durations) if clip_durations is not None else _crossfade_clip_durations(plan)
    _planned_piece_sample_boundaries(durations)
    packets = (
        _validated_piece_audio_packets(
            piece_audio_packets, len(durations), label="act_crossfade_audio measured seam",
        )
        if piece_audio_packets is not None else None
    )
    records: list[dict[str, Any]] = []
    seen: set[int] = set()
    for action in declared:
        params = action.get("params")
        required = {"join_index", "duration", "shape"}
        if not isinstance(params, Mapping) or set(params) != required:
            raise AssemblyError(
                "act_crossfade_audio.params must contain exactly join_index, duration, and shape"
            )
        join_index = params["join_index"]
        if isinstance(join_index, bool) or not isinstance(join_index, int) or not 0 <= join_index <= len(durations) - 2:
            raise AssemblyError(
                f"act_crossfade_audio.params.join_index {join_index!r} must be between 0 and {len(durations) - 2}"
            )
        if join_index in seen:
            raise AssemblyError(f"act_crossfade_audio.params.join_index {join_index} is declared more than once")
        seen.add(join_index)
        try:
            duration = contract_fraction(params["duration"])
        except (RationalValueError, TypeError) as exc:
            raise AssemblyError("act_crossfade_audio.params.duration is invalid") from exc
        if duration <= 0:
            raise AssemblyError("act_crossfade_audio.params.duration must be positive")
        _even_crossfade_duration_samples(duration)
        if duration >= durations[join_index] or duration >= durations[join_index + 1]:
            raise AssemblyError(
                f"act_crossfade_audio.params.duration must be shorter than both clips at join {join_index}"
            )
        if params["shape"] != "linear":
            raise AssemblyError("act_crossfade_audio.params.shape must be linear")
        record: dict[str, Any] = {"join_index": join_index, "duration": duration}
        if packets is not None:
            record["join_time"] = _planned_join_time(durations, join_index)
        records.append(record)
    records.sort(key=lambda item: item["join_index"])
    if packets is None:
        return records
    windows = [
        (item["join_time"] - item["duration"] / 2, item["join_time"] + item["duration"] / 2, item)
        for item in records
    ]
    for earlier, later in zip(windows, windows[1:]):
        if later[0] < earlier[1]:
            raise AssemblyError(
                "act_crossfade_audio blend windows overlap at joins "
                f"{earlier[2]['join_index']} and {later[2]['join_index']}"
            )
    return records


JOIN_AUDIO_CROSSFADE_RENDER_RECORD_KEYS = (
    "join_index", "duration", "join_time", "left_source_path", "right_source_path",
    "left_cut", "right_cut",
)
JOIN_AUDIO_CROSSFADE_AUDIO_PARAM_KEYS = (
    "codec", "sample_rate", "channels", "channel_layout", "bitrate", "clip_durations",
    "piece_audio_packets",
)


_AUDIO_CLEANUP_ACTION_ORDER = (
    "act_noise_reduce", "act_gain", "act_fade_audio", "act_normalize_loudness",
)

# Measured: ffmpeg 8.1.2's ``afftdn`` delays its output by exactly
# 1,200 samples (25 ms at the fixed 48 kHz rate). ``anull`` and
# ``highpass=f=80`` measured 0 samples in the same seeded-pink-noise probe.
# This is deliberately one named build property, rather than a number repeated
# through the graph: the audio-filter latency test remeasures
# afftdn on this build and fails if it ever ceases to agree.
AUDIO_CLEANUP_FILTER_LATENCY_SAMPLES = 1_200


def _audio_cleanup_actions(plan: Mapping[str, Any], policy: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate the contract F.1 declarations and make argv-safe records.

    Fractions stay internal to argv construction. Evidence is produced by the
    separate serializer below so no ``Fraction`` can leak into JSON output.
    """
    actions = plan.get("actions")
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
        raise AssemblyError("F.1 audio cleanup requires actions to be an array")
    declared = [
        action for action in actions if isinstance(action, Mapping)
        and action.get("action") in _AUDIO_CLEANUP_ACTION_ORDER
    ]
    if not declared:
        return []
    if policy.get("audio_policy") != "present":
        raise AssemblyError("F.1 audio cleanup actions require output_policy.audio_policy=present")
    records: list[dict[str, Any]] = []
    for action in declared:
        name, action_id, params = action.get("action"), action.get("action_id"), action.get("params")
        if not isinstance(action_id, str) or not action_id:
            raise AssemblyError(f"{name} requires a non-empty action_id")
        if not isinstance(params, Mapping):
            raise AssemblyError(f"{name}.params must be an object")
        record: dict[str, Any] = {"action": name, "action_id": action_id}
        if name in {"act_noise_reduce", "act_gain"}:
            value_key = "noise_reduction_db" if name == "act_noise_reduce" else "gain_db"
            required = {"timeline_range", value_key, "enable_axis"}
            # ⛔: `nr` alone is unreachable on real material (nr 12 vs 97 both move the
            # 6-8 kHz band by 0.16 dB). `nf` is the parameter that has the effect, so the
            # contract requires it rather than hiding ffmpeg's ineffective -50 default.
            if name == "act_noise_reduce":
                required = required | {"noise_floor_db"}
            if set(params) != required:
                raise AssemblyError(f"{name}.params must contain exactly {', '.join(sorted(required))}")
            timeline_range = params["timeline_range"]
            if not isinstance(timeline_range, Mapping):
                raise AssemblyError(f"{name}.params.timeline_range must be an object")
            start = _time_fraction(timeline_range.get("start"), f"{name}.params.timeline_range.start")
            end = _time_fraction(timeline_range.get("end"), f"{name}.params.timeline_range.end")
            value = params[value_key]
            if start < 0 or end <= start or isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AssemblyError(f"{name}.params requires a non-negative non-empty timeline_range and numeric {value_key}")
            if params["enable_axis"] != "final_timeline":
                raise AssemblyError(f"{name}.params.enable_axis must be final_timeline")
            record.update({"range_start": start, "range_end": end, value_key: value})
            if name == "act_noise_reduce":
                # ⛔: `nf` is the parameter with the effect (nr 12 and 97 both move the
                # 6-8 kHz band by only 0.16 dB on real material), so it must reach argv.
                floor = params["noise_floor_db"]
                if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not -80 <= floor <= -20:
                    raise AssemblyError(f"{name}.params.noise_floor_db must be a number in [-80, -20]")
                record["noise_floor_db"] = floor
        elif name == "act_fade_audio":
            required = {"direction", "start", "duration", "curve", "enable_axis"}
            if set(params) != required:
                raise AssemblyError(f"{name}.params must contain exactly {', '.join(sorted(required))}")
            start = _time_fraction(params["start"], f"{name}.params.start")
            try:
                duration = contract_fraction(params["duration"])
            except (RationalValueError, TypeError) as exc:
                raise AssemblyError(f"{name}.params.duration is invalid") from exc
            if start < 0 or duration <= 0 or params["direction"] not in {"in", "out"} or params["curve"] != "tri":
                raise AssemblyError(f"{name}.params requires non-negative start, positive duration, direction in/out, and curve tri")
            if params["enable_axis"] != "final_timeline":
                raise AssemblyError(f"{name}.params.enable_axis must be final_timeline")
            record.update({"start": start, "duration": duration, "direction": params["direction"], "curve": params["curve"]})
        elif name == "act_normalize_loudness":
            required = {"target_i", "target_lra", "target_tp"}
            if set(params) != required:
                raise AssemblyError("act_normalize_loudness is global and params must contain exactly target_i, target_lra, and target_tp")
            if any(isinstance(params[key], bool) or not isinstance(params[key], (int, float)) for key in required):
                raise AssemblyError("act_normalize_loudness targets must be numeric")
            two_pass = plan.get("reproducibility", {}).get("loudnorm_two_pass") if isinstance(plan.get("reproducibility"), Mapping) else None
            measured_keys = ("measured_I", "measured_LRA", "measured_TP", "measured_thresh", "offset")
            if not isinstance(two_pass, Mapping) or two_pass.get("status") != "measured" or any(
                isinstance(two_pass.get(key), bool) or not isinstance(two_pass.get(key), (int, float))
                for key in measured_keys
            ):
                raise AssemblyError("act_normalize_loudness requires reproducibility.loudnorm_two_pass measured_I, measured_LRA, measured_TP, measured_thresh, and offset")
            record.update({key: params[key] for key in required})
            record.update({key: two_pass[key] for key in measured_keys})
        records.append(record)
    return records


AUDIO_BED_PLAN_RECORD_KEYS = (
    "action", "action_id", "asset_id", "expected_sha256", "timeline_start",
    "adelay_samples", "gain_db", "coverage_samples", "program_samples",
)
AUDIO_BED_ASSET_RECORD_KEYS = (
    *AUDIO_BED_PLAN_RECORD_KEYS, "asset_path", "asset_sha256", "source_samples", "aloop_count",
    "play_samples", "truncated_samples",
)
AUDIO_BED_MIX_RECORD_KEYS = (
    "input_action_ids", "duration_policy", "normalize", "output_channel_layout",
)
# ffmpeg evaluates any audio automation that depends on `t` (both volume's
# eval=frame and timeline enable=) ONCE PER FRAME and applies it to the WHOLE frame.
# With the default 4,096-sample frame the automation is late by half of that on
# average (42.7 ms), which exceeds one video frame (33.4 ms).
# Measured: on real material the ducking envelope was 37.0 and 43.5 ms late, and a
# declared 2,000.00 ms gain span came out 1,962.67 ms.  At n=64 both fall under 0.5 ms.
# Safe: it does not change afftdn's 1,200-sample delay (the existing compensation
# still holds), adds no delay of its own, and preserves duration.
# Cost: 2.52s on 741s of audio (0.14s before), i.e. 0.13% of a 31.6-minute render.
# Do NOT lower it to n=32 - the gain shrinks from 0.018 to 0.011 dB while the cost doubles.
AUDIO_AUTOMATION_FRAME_SAMPLES = 64
_AUDIO_AUTOMATION_FRAME = f"asetnsamples=n={AUDIO_AUTOMATION_FRAME_SAMPLES}:p=0,"

AUDIO_DUCK_RECORD_KEYS = (
    "action_id", "target_action_ids", "windows", "depth_db", "attack", "release", "enable_axis",
)
_AUDIO_BED_ACTIONS = ("act_add_sfx", "act_add_ambience", "act_add_music")


def _audio_bed_actions(plan: Mapping[str, Any], policy: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the contract F.3 declarations before resolving their media assets.

    The plan supplies final-timeline coordinates and the length-preserving mix
    policy. The source WAV frame count needed for ``aloop`` is intentionally
    resolved later by ``_audio_bed_asset_records``: $defs.asset is closed and
    must not grow audio-only duration metadata.
    """
    actions = plan.get("actions")
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
        raise AssemblyError("F.3 audio beds require actions to be an array")
    declared = [
        action for action in actions if isinstance(action, Mapping)
        and action.get("action") in _AUDIO_BED_ACTIONS
    ]
    mixes = [
        action for action in actions if isinstance(action, Mapping)
        and action.get("action") == "act_mix_audio"
    ]
    if not declared and not mixes:
        return {"beds": [], "mix": None}
    if policy.get("audio_policy") != "present":
        raise AssemblyError("F.3 audio bed actions require output_policy.audio_policy=present")
    if not declared:
        raise AssemblyError("act_mix_audio is forbidden when no act_add_sfx or act_add_ambience is declared")
    if len(mixes) != 1:
        raise AssemblyError("one or more audio beds require exactly one act_mix_audio")
    assets = plan.get("assets")
    if not isinstance(assets, Sequence) or isinstance(assets, (str, bytes)):
        raise AssemblyError("F.3 audio beds require plan.assets to be an array")
    try:
        program_samples_fraction = sum(_crossfade_clip_durations(plan), Fraction()) * int(AUDIO_SAMPLE_RATE)
    except (ValueError, TypeError) as exc:
        raise AssemblyError("F.3 audio beds require valid final timeline clip durations") from exc
    if program_samples_fraction <= 0:
        raise AssemblyError("F.3 audio beds require a positive final timeline length")
    # ⛔⛔: an earlier revision required this product to land on a whole audio
    # sample and that REJECTED every real plan --- declared timeline sums
    # to 563563/15000 s, so x48000 has denominator 5. Every gate was green and
    # real material would have died at L2. The frame grid (1/30000) and the audio
    # rate (48000) simply do not share a common sample grid, so floor here.
    # ⭐ This count is DECLARED-timeline based, not measured. The rendered AAC audio
    # differs by up to one packet (measured 1,804,416 samples against
    # 1,803,401.6 declared = 1,014 samples = 21 ms), which is why it is only used
    # for the "cue starts before the program ends" rejection and for reporting
    # truncated_samples --- never as a length invariant.
    program_samples = program_samples_fraction.numerator // program_samples_fraction.denominator
    records_by_id: dict[str, dict[str, Any]] = {}
    for action in declared:
        name, action_id, asset_id, params = (
            action.get("action"), action.get("action_id"), action.get("asset_id"), action.get("params"),
        )
        if not isinstance(action_id, str) or not action_id or action_id in records_by_id:
            raise AssemblyError(f"{name} requires a unique non-empty action_id")
        if not isinstance(asset_id, str) or not asset_id:
            raise AssemblyError(f"{name}.asset_id must be a non-empty string")
        matching = [asset for asset in assets if isinstance(asset, Mapping) and asset.get("asset_id") == asset_id]
        if len(matching) != 1:
            raise AssemblyError(f"{name}.asset_id {asset_id!r} must resolve to exactly one assets[] record")
        required = {"timeline_start", "gain_db", "enable_axis"}
        if name == "act_add_ambience":
            required.add("coverage_samples")
        if not isinstance(params, Mapping) or set(params) != required:
            raise AssemblyError(f"{name}.params must contain exactly {', '.join(sorted(required))}")
        start = _time_fraction(params["timeline_start"], f"{name}.params.timeline_start")
        start_samples = start * int(AUDIO_SAMPLE_RATE)
        gain = params["gain_db"]
        if (start < 0 or start_samples.denominator != 1 or isinstance(gain, bool)
                or not isinstance(gain, (int, float)) or not -60 <= gain <= 60):
            raise AssemblyError(f"{name}.params requires sample-exact non-negative timeline_start and gain_db in [-60, 60]")
        if start_samples.numerator >= program_samples:
            raise AssemblyError(f"{name}.params.timeline_start must be before the program end")
        if params["enable_axis"] != "final_timeline":
            raise AssemblyError(f"{name}.params.enable_axis must be final_timeline")
        coverage_samples: int | None = None
        if name == "act_add_ambience":
            coverage_samples = params["coverage_samples"]
            if isinstance(coverage_samples, bool) or not isinstance(coverage_samples, int) or coverage_samples <= 0:
                raise AssemblyError("act_add_ambience.params.coverage_samples must be a positive integer")
        records_by_id[action_id] = {
            "action": name, "action_id": action_id, "asset_id": asset_id,
            "expected_sha256": matching[0].get("sha256"), "timeline_start": start,
            "adelay_samples": start_samples.numerator, "gain_db": gain,
            "coverage_samples": coverage_samples, "program_samples": program_samples,
        }
    mix_params = mixes[0].get("params")
    if not isinstance(mix_params, Mapping) or set(mix_params) != set(AUDIO_BED_MIX_RECORD_KEYS):
        raise AssemblyError("act_mix_audio.params must contain exactly duration_policy, input_action_ids, normalize, and output_channel_layout")
    input_action_ids = mix_params["input_action_ids"]
    if (not isinstance(input_action_ids, Sequence) or isinstance(input_action_ids, (str, bytes))
            or any(not isinstance(item, str) or not item for item in input_action_ids)
            or len(input_action_ids) != len(set(input_action_ids))
            or set(input_action_ids) != set(records_by_id)):
        raise AssemblyError("act_mix_audio.input_action_ids must exactly name the declared audio bed action_ids")
    if mix_params["duration_policy"] != "first" or mix_params["normalize"] != 0:
        raise AssemblyError("act_mix_audio requires duration_policy=first and normalize=0")
    if mix_params["output_channel_layout"] != AUDIO_CHANNEL_LAYOUT:
        raise AssemblyError(f"act_mix_audio.output_channel_layout must be {AUDIO_CHANNEL_LAYOUT!r}")
    return {
        "beds": [records_by_id[action_id] for action_id in input_action_ids],
        "mix": {key: mix_params[key] for key in AUDIO_BED_MIX_RECORD_KEYS},
    }


def _audio_bed_asset_records(
    plan_records: Sequence[Mapping[str, Any]], asset_map_path: Path | None,
) -> list[dict[str, Any]]:
    """Resolve F.3 assets by map, file identity, SHA-256, and WAV sample count."""
    if not plan_records:
        return []
    if asset_map_path is None:
        raise AssemblyError("--asset-map is required when act_add_sfx or act_add_ambience is present")
    asset_map = _load_json(asset_map_path, "asset map")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in asset_map.items()):
        raise AssemblyError("asset map must be an object of asset_id to file path strings")
    records: list[dict[str, Any]] = []
    for plan_record in plan_records:
        if set(plan_record) != set(AUDIO_BED_PLAN_RECORD_KEYS):
            raise AssemblyError("audio bed asset resolver received plan records outside its fixed contract")
        asset_id = plan_record["asset_id"]
        if asset_id not in asset_map:
            raise AssemblyError(f"asset map has no path for audio bed asset_id {asset_id!r}")
        asset_path = Path(asset_map[asset_id]).expanduser()
        if not asset_path.is_absolute():
            asset_path = asset_map_path.parent / asset_path
        asset_path = asset_path.resolve()
        if not asset_path.is_file():
            raise AssemblyError(f"resolved audio bed asset is not a file: {asset_path}")
        actual_sha = _sha256(asset_path)
        if actual_sha != plan_record["expected_sha256"]:
            raise AssemblyError(
                f"audio bed asset {asset_id!r} SHA-256 mismatch: expected {plan_record['expected_sha256']}, got {actual_sha}"
            )
        try:
            with wave.open(str(asset_path), "rb") as source:
                source_samples, source_rate = source.getnframes(), source.getframerate()
        except (wave.Error, OSError) as exc:
            raise AssemblyError(f"audio bed asset {asset_id!r} must be a readable WAV file") from exc
        if source_samples <= 0 or source_rate != int(AUDIO_SAMPLE_RATE):
            raise AssemblyError(f"audio bed asset {asset_id!r} must be non-empty {AUDIO_SAMPLE_RATE} Hz WAV audio")
        coverage = plan_record["coverage_samples"]
        if plan_record["action"] == "act_add_ambience":
            if not isinstance(coverage, int) or coverage <= 0:
                raise AssemblyError("act_add_ambience coverage_samples is invalid after asset resolution")
            aloop_count = math.ceil(coverage / source_samples) - 1
            play_samples = coverage
            if aloop_count < 0 or (aloop_count + 1) * source_samples < coverage:
                raise AssemblyError("act_add_ambience aloop and atrim arithmetic cannot cover coverage_samples exactly")
        else:
            aloop_count = None
            play_samples = source_samples
        records.append({
            **plan_record, "asset_path": str(asset_path), "asset_sha256": actual_sha,
            "source_samples": source_samples, "aloop_count": aloop_count,
            "play_samples": play_samples,
            "truncated_samples": max(0, plan_record["adelay_samples"] + play_samples - plan_record["program_samples"]),
        })
    return records


def _duck_number_fraction(value: Any, label: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AssemblyError(f"{label} must be a number")
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise AssemblyError(f"{label} must be a finite decimal number") from exc


def _audio_duck_actions(
    plan: Mapping[str, Any], policy: Mapping[str, Any], bed_records_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate the contract's declared, per-bed volume envelopes before argv work."""
    actions = plan.get("actions")
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
        raise AssemblyError("act_duck_audio requires actions to be an array")
    ducks = [action for action in actions if isinstance(action, Mapping) and action.get("action") == "act_duck_audio"]
    if not ducks:
        return []
    if policy.get("audio_policy") != "present":
        raise AssemblyError("act_duck_audio requires output_policy.audio_policy=present")
    if not bed_records_by_id:
        raise AssemblyError("act_duck_audio requires one or more declared audio beds")
    try:
        program_duration = sum(_crossfade_clip_durations(plan), Fraction())
    except (ValueError, TypeError) as exc:
        raise AssemblyError("act_duck_audio requires valid final timeline clip durations") from exc
    if program_duration <= 0:
        raise AssemblyError("act_duck_audio requires a positive final timeline length")
    records: list[dict[str, Any]] = []
    intervals_by_bed: dict[str, list[tuple[Fraction, Fraction, str]]] = {
        action_id: [] for action_id in bed_records_by_id
    }
    for action in ducks:
        action_id, params = action.get("action_id"), action.get("params")
        if not isinstance(action_id, str) or not action_id:
            raise AssemblyError("act_duck_audio requires a non-empty action_id")
        required = {"target_action_ids", "windows", "depth_db", "attack", "release", "enable_axis"}
        if not isinstance(params, Mapping) or set(params) != required:
            raise AssemblyError("act_duck_audio.params must contain exactly attack, depth_db, enable_axis, release, target_action_ids, and windows")
        target_ids = params["target_action_ids"]
        if (not isinstance(target_ids, Sequence) or isinstance(target_ids, (str, bytes))
                or not target_ids or any(not isinstance(value, str) or not value for value in target_ids)
                or len(target_ids) != len(set(target_ids))
                or not set(target_ids) <= set(bed_records_by_id)):
            raise AssemblyError("act_duck_audio.target_action_ids must be a non-empty subset of declared audio bed action_ids")
        depth = params["depth_db"]
        if isinstance(depth, bool) or not isinstance(depth, (int, float)) or depth >= 0:
            raise AssemblyError("act_duck_audio.depth_db must be negative")
        attack = _duck_number_fraction(params["attack"], "act_duck_audio.attack")
        release = _duck_number_fraction(params["release"], "act_duck_audio.release")
        if attack <= 0 or release <= 0:
            raise AssemblyError("act_duck_audio.attack and release must be positive")
        if params["enable_axis"] != "final_timeline":
            raise AssemblyError("act_duck_audio.enable_axis must be final_timeline")
        raw_windows = params["windows"]
        if not isinstance(raw_windows, Sequence) or isinstance(raw_windows, (str, bytes)) or not raw_windows:
            raise AssemblyError("act_duck_audio.windows must be a non-empty array")
        windows: list[dict[str, Fraction]] = []
        for index, window in enumerate(raw_windows):
            if not isinstance(window, Mapping) or set(window) != {"start", "end"}:
                raise AssemblyError("act_duck_audio.windows items must contain exactly start and end")
            start = _time_fraction(window["start"], f"act_duck_audio.windows[{index}].start")
            end = _time_fraction(window["end"], f"act_duck_audio.windows[{index}].end")
            if end <= start:
                raise AssemblyError("act_duck_audio.windows must be non-empty half-open ranges")
            if (start * int(AUDIO_SAMPLE_RATE)).denominator != 1 or (end * int(AUDIO_SAMPLE_RATE)).denominator != 1:
                raise AssemblyError("act_duck_audio window times must be sample-exact at 48000 Hz")
            if start - attack < 0 or end + release > program_duration:
                raise AssemblyError("act_duck_audio window including attack/release must stay within the program timeline")
            windows.append({"start": start, "end": end})
        record = {
            "action_id": action_id, "target_action_ids": list(target_ids), "windows": windows,
            "depth_db": depth, "attack": attack, "release": release, "enable_axis": params["enable_axis"],
        }
        for target_id in target_ids:
            intervals_by_bed[target_id].extend(
                (window["start"] - attack, window["end"] + release, action_id) for window in windows
            )
        records.append(record)
    for target_id, intervals in intervals_by_bed.items():
        for earlier, later in zip(sorted(intervals), sorted(intervals)[1:]):
            if later[0] < earlier[1]:
                raise AssemblyError(
                    f"act_duck_audio envelope windows overlap for audio bed {target_id!r}: "
                    f"{earlier[2]!r} and {later[2]!r}"
                )
    return records


def _duck_envelope_expression(duck_records: Sequence[Mapping[str, Any]]) -> str:
    """Return the contract decision 4's complete, fixed-decimal piecewise envelope."""
    expression = "1"
    windows: list[tuple[Fraction, Fraction, Fraction, Fraction, float]] = []
    for record in duck_records:
        if set(record) != set(AUDIO_DUCK_RECORD_KEYS):
            raise AssemblyError("audio duck argv records do not match the producer contract")
        for window in record["windows"]:
            windows.append((window["start"], window["end"], record["attack"], record["release"], record["depth_db"]))
    for start, end, attack, release, depth_db in reversed(sorted(windows)):
        gain = f"{10 ** (float(depth_db) / 20):.12f}"
        attack_start, release_end = start - attack, end + release
        s0, s1, e0, e1 = (
            _fraction_decimal(attack_start), _fraction_decimal(start),
            _fraction_decimal(end), _fraction_decimal(release_end),
        )
        attack_text, release_text = _fraction_decimal(attack), _fraction_decimal(release)
        expression = (
            f"if(lt(t,{s0}),1,if(lt(t,{s1}),1-(1-{gain})*(t-({s0}))/{attack_text},"
            f"if(lt(t,{e0}),{gain},if(lt(t,{e1}),{gain}+(1-{gain})*(t-({e0}))/{release_text},{expression}))))"
        )
    return expression


def _audio_bed_filter_chain(
    program_label: str, records: Sequence[Mapping[str, Any]], mix_record: Mapping[str, Any], input_indices: Sequence[int],
    duck_records: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[str], str]:
    """Build the contract's one amix node; input ordering is plan-declared order."""
    if set(mix_record) != set(AUDIO_BED_MIX_RECORD_KEYS):
        raise AssemblyError("audio bed argv mix record keys do not match the producer contract")
    if (mix_record["duration_policy"] != "first" or mix_record["normalize"] != 0
            or mix_record["output_channel_layout"] != AUDIO_CHANNEL_LAYOUT):
        raise AssemblyError("Unsupported audio-bed mix policy.")
    if len(records) != len(input_indices) or not records:
        raise AssemblyError("audio bed argv requires one resolved input index per bed")
    filters: list[str] = []
    labels: list[str] = []
    ducks_by_target = {
        record["action_id"]: [duck for duck in duck_records if record["action_id"] in duck["target_action_ids"]]
        for record in records
    }
    for order, (record, input_index) in enumerate(zip(records, input_indices)):
        if set(record) != set(AUDIO_BED_ASSET_RECORD_KEYS):
            raise AssemblyError("audio bed argv asset record keys do not match the producer contract")
        label = f"abed{order}"
        prefix = f"[{input_index}:a]"
        if record["action"] == "act_add_ambience":
            if (not isinstance(record["aloop_count"], int) or record["aloop_count"] < 0
                    or not isinstance(record["source_samples"], int) or record["source_samples"] <= 0
                    or not isinstance(record["coverage_samples"], int) or record["coverage_samples"] <= 0
                    or (record["aloop_count"] + 1) * record["source_samples"] < record["coverage_samples"]
                    or record["play_samples"] != record["coverage_samples"]):
                raise AssemblyError("act_add_ambience aloop and atrim arithmetic cannot cover coverage_samples exactly")
            prefix += (
                f"aloop=loop={record['aloop_count']}:size={record['source_samples']},"
                f"atrim=end_sample={record['coverage_samples']},asetpts=N/SR/TB,"
            )
        elif record["action"] not in {"act_add_sfx", "act_add_music"}:
            raise AssemblyError("audio bed argv received an unsupported bed action")
        duck_tail = ""
        if ducks_by_target[record["action_id"]]:
            duck_tail = (f",{_AUDIO_AUTOMATION_FRAME}"
                         f"volume=volume='{_duck_envelope_expression(ducks_by_target[record['action_id']])}':eval=frame")
        filters.append(
            f"{prefix}volume={record['gain_db']}dB,"
            f"adelay=delays={record['adelay_samples']}S:all=1{duck_tail}[{label}]"
        )
        labels.append(label)
    mixed = "amixed"
    filters.append(
        f"[{program_label}]" + "".join(f"[{label}]" for label in labels)
        + f"amix=inputs={len(labels) + 1}:duration=first:normalize=0[{mixed}]"
    )
    return filters, mixed


def _audio_bed_evidence_records(
    records: Sequence[Mapping[str, Any]], applied: bool, duck_records: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Serialize F.3 evidence without leaking Fraction into assemble JSON."""
    evidence: list[dict[str, Any]] = []
    for record in records:
        item = {key: record[key] for key in (
            "action_id", "action", "asset_id", "asset_sha256", "adelay_samples", "gain_db",
            "coverage_samples", "source_samples", "aloop_count", "play_samples", "truncated_samples",
        )}
        item["timeline_start"] = _fraction_evidence(record["timeline_start"])
        item["duck"] = []
        for duck in duck_records:
            if record["action_id"] not in duck["target_action_ids"]:
                continue
            expression = _duck_envelope_expression([duck])
            item["duck"].append({
                "action_id": duck["action_id"],
                "windows": [{"start": _fraction_evidence(window["start"]), "end": _fraction_evidence(window["end"])} for window in duck["windows"]],
                "depth_db": duck["depth_db"], "attack": _fraction_evidence(duck["attack"]),
                "release": _fraction_evidence(duck["release"]),
                "envelope_sha256": hashlib.sha256(expression.encode("utf-8")).hexdigest(),
            })
        item["applied"] = applied
        evidence.append(item)
    return evidence


def _audio_cleanup_filter_tail(
    input_label: str, records: Sequence[Mapping[str, Any]], stages: Sequence[str] | None = None,
    *, program_samples: int | None = None,
) -> tuple[list[str], str]:
    """Append the contract's fixed category order, then the contract's sample trim."""
    filters: list[str] = []
    current = input_label
    sequence = 0
    selected = tuple(_AUDIO_CLEANUP_ACTION_ORDER if stages is None else stages)
    if any(action_name not in _AUDIO_CLEANUP_ACTION_ORDER for action_name in selected):
        raise AssemblyError("audio cleanup filter tail received an unknown fixed-order stage")
    for action_name in _AUDIO_CLEANUP_ACTION_ORDER:
        if action_name not in selected:
            continue
        for record in sorted((item for item in records if item["action"] == action_name), key=lambda item: item["action_id"]):
            output = f"acleanup{sequence}"; sequence += 1
            if action_name == "act_noise_reduce":
                expression = (
                    f"afftdn=nr={record['noise_reduction_db']}:nf={record['noise_floor_db']}:"
                    f"enable='gte(t,{_fraction_decimal(record['range_start'])})*"
                    f"lt(t,{_fraction_decimal(record['range_end'])})'"
                )
            elif action_name == "act_gain":
                expression = (
                    f"{_AUDIO_AUTOMATION_FRAME}"
                    f"volume={record['gain_db']}dB:enable='gte(t,{_fraction_decimal(record['range_start'])})*"
                    f"lt(t,{_fraction_decimal(record['range_end'])})'"
                )
            elif action_name == "act_fade_audio":
                expression = (
                    f"afade=t={record['direction']}:st={_fraction_decimal(record['start'])}:"
                    f"d={_fraction_decimal(record['duration'])}:curve={record['curve']}"
                )
            else:
                expression = (
                    f"loudnorm=I={record['target_i']}:LRA={record['target_lra']}:TP={record['target_tp']}:"
                    f"measured_I={record['measured_I']}:measured_LRA={record['measured_LRA']}:"
                    f"measured_TP={record['measured_TP']}:measured_thresh={record['measured_thresh']}:"
                    f"offset={record['offset']}:linear=true"
                )
            filters.append(f"[{current}]{expression}[{output}]")
            current = output
    latency_samples = _audio_cleanup_latency_samples(records, selected)
    if latency_samples:
        if isinstance(program_samples, bool) or not isinstance(program_samples, int) or program_samples <= 0:
            raise AssemblyError("noise-reduction cleanup requires positive planned program_samples")
        # the contract A: this sits *after* the fixed-order cleanup tail. Its input
        # was admitted through program_samples + L below, so [L, L + planned)
        # restores programme time without padding its lost tail with silence.
        output = "acleanuptrim"
        filters.append(
            f"[{current}]atrim=start_sample={latency_samples}:"
            f"end_sample={latency_samples + program_samples},asetpts=PTS-STARTPTS[{output}]"
        )
        current = output
    return filters, current


def _audio_cleanup_latency_samples(
    records: Sequence[Mapping[str, Any]], stages: Sequence[str] | None = None,
) -> int:
    """Return the measured afftdn delay only when *this* emission carries it.

    ⛔ The stage subset matters. ``join_audio_crossfade_argv`` emits the tail
    twice when beds exist -- pre-stages before the mix, loudnorm after it -- and
    a records-only test compensates in both, which double-trims and emits the
    same ``acleanuptrim`` label twice. The delay belongs to the emission that
    actually contains ``afftdn``; compensating there also returns the dialogue
    to programme time *before* the beds are mixed, which is what keeps the
    (undelayed) beds aligned with it.
    """
    selected = None if stages is None else set(stages)
    return AUDIO_CLEANUP_FILTER_LATENCY_SAMPLES if any(
        record.get("action") == "act_noise_reduce"
        and (selected is None or "act_noise_reduce" in selected)
        for record in records
    ) else 0


def _audio_cleanup_program_input(
    input_label: str, records: Sequence[Mapping[str, Any]], program_samples: int | None,
) -> tuple[list[str], str]:
    """Admit exactly the planned programme plus the contract's real-source tail."""
    latency_samples = _audio_cleanup_latency_samples(records)
    if not latency_samples:
        return [], input_label
    if isinstance(program_samples, bool) or not isinstance(program_samples, int) or program_samples <= 0:
        raise AssemblyError("noise-reduction cleanup requires positive planned program_samples")
    # Do not apad: this is an atrim of the already-rendered programme, whose
    # source tail must contain the actual L samples needed by afftdn.
    label = "acleanupinput"
    return [
        f"[{input_label}]atrim=start_sample=0:end_sample={program_samples + latency_samples},"
        f"asetpts=PTS-STARTPTS[{label}]"
    ], label


def _resplice_program_labels(program: str, consumers: int) -> tuple[list[str], list[str]]:
    """Return extra filters and the labels each later consumer must use.

    ffmpeg 8 does not auto-asplit an intermediate pad. A second named consume
    rebinds to the file input (``0:0``) instead. Input-file pads such as
    ``0:a`` do auto-split, so they are repeated as-is.
    """
    if consumers == 1:
        return [], [program]
    if program == "0:a":
        return [], ["0:a"] * consumers
    split_labels = [f"arespliced{index}" for index in range(consumers)]
    return [
        f"[{program}]asplit={consumers}" + "".join(f"[{label}]" for label in split_labels)
    ], split_labels


def _resplice_concat_audio_input(
    clip_durations: Sequence[Fraction] | None, piece_audio_packets: Sequence[Any] | None, *,
    tail_samples: int = 0, consumers: int = 1,
) -> tuple[list[str], list[str]]:
    """Return the contract's measured-to-planned concat audio re-splice.

    The incoming concat has AAC priming and packet-grid padding at every piece.
    Slice each piece at its measured content start, concatenate exactly the
    frame-derived planned spans, and expose one planned-axis program label to
    every later audio consumer. A final real-source tail is admitted only for
    the contract's afftdn latency compensation.

    ``consumers`` is how many later filters will read the planned axis. One
    consumer (the cleanup path) keeps a single ``[arespliced]`` pad. Two or
    more append ``[arespliced]asplit=N[arespliced0]…`` here so each consumer
    gets its own pad — ffmpeg 8 will not insert that split itself.
    """
    if isinstance(consumers, bool) or not isinstance(consumers, int) or consumers < 1:
        raise AssemblyError("audio re-splice consumers must be a positive integer")
    if clip_durations is None and piece_audio_packets is None:
        extra, labels = _resplice_program_labels("0:a", consumers)
        return extra, labels
    if clip_durations is None or piece_audio_packets is None:
        raise AssemblyError("audio re-splice requires both planned clip durations and measured packet counts")
    if isinstance(tail_samples, bool) or not isinstance(tail_samples, int) or tail_samples < 0:
        raise AssemblyError("audio re-splice tail_samples must be a non-negative integer")
    durations = list(clip_durations)
    boundaries = _planned_piece_sample_boundaries(durations)
    packets = _validated_piece_audio_packets(piece_audio_packets, len(durations), label="audio re-splice")
    # A promotion has no concat-program priming boundary. Keep its pre-ADR
    # path exactly intact, including the admission filter when needed.
    if len(durations) == 1:
        extra, labels = _resplice_program_labels("0:a", consumers)
        return extra, labels
    filters: list[str] = []
    labels: list[str] = []
    for piece_index, packet_count in enumerate(packets):
        planned_samples = boundaries[piece_index + 1] - boundaries[piece_index]
        if piece_index == len(packets) - 1:
            planned_samples += tail_samples
        start_sample = _measured_piece_content_start_samples(packets, piece_index)
        label = f"resplice{piece_index}"
        filters.append(
            f"[0:a]atrim=start_sample={start_sample}:end_sample={start_sample + planned_samples},"
            f"asetpts=N/SR/TB[{label}]"
        )
        labels.append(label)
    filters.append("".join(f"[{label}]" for label in labels) + f"concat=n={len(labels)}:v=0:a=1[arespliced]")
    extra, program_labels = _resplice_program_labels("arespliced", consumers)
    filters.extend(extra)
    return filters, program_labels


def _audio_cleanup_evidence_records(records: Sequence[Mapping[str, Any]], applied: bool) -> list[dict[str, Any]]:
    """Serialize the internal cleanup records without Fraction values."""
    evidence: list[dict[str, Any]] = []
    for record in records:
        item = {"action_id": record["action_id"], "action": record["action"], "applied": applied}
        for key in ("range_start", "range_end", "start", "duration"):
            if key in record:
                item[key] = _fraction_evidence(record[key])
        for key in ("noise_reduction_db", "noise_floor_db", "gain_db", "direction", "curve", "target_i", "target_lra", "target_tp", "measured_I", "measured_LRA", "measured_TP", "measured_thresh", "offset"):
            if key in record:
                item[key] = record[key]
        evidence.append(item)
    return evidence


def audio_cleanup_argv(
    input_path: str | Path, output_path: str | Path, records: Sequence[Mapping[str, Any]],
    bed_records: Sequence[Mapping[str, Any]] = (), mix_record: Mapping[str, Any] | None = None,
    duck_records: Sequence[Mapping[str, Any]] = (), *, program_samples: int | None = None,
    clip_durations: Sequence[Fraction] | None = None, piece_audio_packets: Sequence[Any] | None = None,
) -> list[str]:
    """Build the single post-concat the contract/F.1/F.3 pass without crossfades."""
    filters, program_labels = _resplice_concat_audio_input(
        clip_durations, piece_audio_packets,
        tail_samples=_audio_cleanup_latency_samples(records),
    )
    program_input = program_labels[0]
    if not bed_records:
        cleanup_input_filters, cleanup_input = _audio_cleanup_program_input(program_input, records, program_samples)
        filters.extend(cleanup_input_filters)
        cleanup_filters, final_label = _audio_cleanup_filter_tail(
            cleanup_input, records, program_samples=program_samples,
        )
        filters.extend(cleanup_filters)
        if not filters:
            raise AssemblyError("audio cleanup argv requires a re-splice or one F.1 action")
        inputs: list[str] = []
    else:
        if mix_record is None:
            raise AssemblyError("audio bed argv requires its act_mix_audio record")
        pre_stages = _AUDIO_CLEANUP_ACTION_ORDER[:-1]
        cleanup_input_filters, cleanup_input = _audio_cleanup_program_input(program_input, records, program_samples)
        filters.extend(cleanup_input_filters)
        cleanup_filters, dialogue_label = _audio_cleanup_filter_tail(
            cleanup_input, records, pre_stages, program_samples=program_samples,
        )
        filters.extend(cleanup_filters)
        bed_filters, mixed_label = _audio_bed_filter_chain(
            dialogue_label, bed_records, mix_record, list(range(1, len(bed_records) + 1)), duck_records,
        )
        filters.extend(bed_filters)
        loudnorm_filters, final_label = _audio_cleanup_filter_tail(
            mixed_label, records, ("act_normalize_loudness",), program_samples=program_samples,
        )
        filters.extend(loudnorm_filters)
        inputs = [item for record in bed_records for item in ("-i", str(record["asset_path"]))]
    return [
        ffmpeg_path(), "-y", "-hide_banner", "-nostdin", "-i", str(input_path), *inputs,
        "-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", f"[{final_label}]",
        "-c:v", "copy",
        *(["-bsf:v", "setts=pts=PTS-STARTPTS:dts=DTS-STARTPTS"]
          if clip_durations is not None and len(clip_durations) > 1 else []),
        "-c:a", AUDIO_CODEC, "-ar", str(AUDIO_SAMPLE_RATE),
        "-ac", str(AUDIO_CHANNELS), "-channel_layout", AUDIO_CHANNEL_LAYOUT,
        "-b:a", AUDIO_BITRATE, str(output_path),
    ]


def _join_audio_crossfade_audio_params(
    plan: Mapping[str, Any], piece_audio_packets: Sequence[Any], clip_durations: Sequence[Fraction] | None = None,
) -> dict[str, Any]:
    """Produce the exact audio record that the D argv consumer requires."""
    durations = list(clip_durations) if clip_durations is not None else _crossfade_clip_durations(plan)
    _planned_piece_sample_boundaries(durations)
    packets = _validated_piece_audio_packets(
        piece_audio_packets, len(durations), label="join audio crossfade audio params",
    )
    return {
        "codec": AUDIO_CODEC, "sample_rate": AUDIO_SAMPLE_RATE, "channels": AUDIO_CHANNELS,
        "channel_layout": AUDIO_CHANNEL_LAYOUT, "bitrate": AUDIO_BITRATE,
        "clip_durations": durations, "piece_audio_packets": packets,
    }


def _crossfade_render_joins(
    plan: Mapping[str, Any], joins: Sequence[Mapping[str, Any]], sources: Mapping[str, Path],
    index_by_source: Mapping[str, Mapping[str, Any]],
    speed_by_segment: Mapping[str, Sequence[Mapping[str, Any]]], piece_audio_packets: Sequence[Any],
    clip_durations: Sequence[Fraction] | None = None,
) -> list[dict[str, Any]]:
    """Resolve validated joins to D's two source handles, before any ffmpeg work."""
    clips = _timeline_clips_in_concat_order(plan)
    durations = list(clip_durations) if clip_durations is not None else _crossfade_clip_durations(plan)
    _planned_piece_sample_boundaries(durations)
    packets = _validated_piece_audio_packets(
        piece_audio_packets, len(clips), label="act_crossfade_audio render joins",
    )
    total = Fraction(_planned_piece_sample_boundaries(durations)[-1], AAC_AUDIO_SAMPLE_RATE)
    segments = plan.get("source_segments")
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)):
        raise AssemblyError("act_crossfade_audio requires plan.source_segments to be an array")
    segment_by_id = {
        segment.get("segment_id"): segment for segment in segments if isinstance(segment, Mapping)
    }
    records: list[dict[str, Any]] = []
    windows: list[tuple[Fraction, Fraction, int]] = []
    for join in joins:
        join_index = join.get("join_index")
        duration = join.get("duration")
        join_time = join.get("join_time")
        if (isinstance(join_index, bool) or not isinstance(join_index, int)
                or not isinstance(duration, Fraction) or not isinstance(join_time, Fraction)
                or not 0 <= join_index < len(clips) - 1):
            raise AssemblyError("act_crossfade_audio internal join record is invalid")
        if join_time != _planned_join_time(durations, join_index):
            raise AssemblyError("act_crossfade_audio internal join record has a non-planned seam time")
        half = duration / 2
        window_start, window_end = join_time - half, join_time + half
        if window_start < 0 or window_end > total:
            raise AssemblyError(
                f"act_crossfade_audio blend window at join {join_index} is outside the program timeline"
            )
        windows.append((window_start, window_end, join_index))
        left_clip, right_clip = clips[join_index], clips[join_index + 1]
        left_segment = segment_by_id.get(left_clip.get("source_segment_id"))
        right_segment = segment_by_id.get(right_clip.get("source_segment_id"))
        if not isinstance(left_segment, Mapping) or not isinstance(right_segment, Mapping):
            raise AssemblyError(f"act_crossfade_audio join {join_index} references an unresolved source segment")
        left_source_id, right_source_id = left_segment.get("source_id"), right_segment.get("source_id")
        if left_source_id not in sources or right_source_id not in sources:
            raise AssemblyError(f"act_crossfade_audio join {join_index} has an unresolved original source path")
        if left_source_id not in index_by_source or right_source_id not in index_by_source:
            raise AssemblyError(f"act_crossfade_audio join {join_index} has an unresolved original source duration")
        left_source_range = left_clip.get("source_range")
        if not isinstance(left_source_range, Mapping):
            raise AssemblyError("act_crossfade_audio requires timeline clip source_range objects")
        left_start = _time_fraction(left_source_range.get("start"), "source_range.start")
        left_cut = left_start + durations[join_index]
        right_cut = _time_fraction(right_clip.get("source_range", {}).get("start"), "source_range.start")
        left_duration = _time_fraction(index_by_source[left_source_id].get("duration"), "index source duration")
        if left_cut + half > left_duration or right_cut - half < 0:
            raise AssemblyError(
                f"act_crossfade_audio join {join_index} lacks original-source handle for duration "
                f"{_fraction_text(duration)}"
            )
        for clip, segment in ((left_clip, left_segment), (right_clip, right_segment)):
            source_range = clip.get("source_range")
            if not isinstance(source_range, Mapping):
                raise AssemblyError("act_crossfade_audio requires timeline clip source_range objects")
            start = _time_fraction(source_range.get("start"), "source_range.start")
            end = _time_fraction(source_range.get("end"), "source_range.end")
            if any(action["range_start"] < end and start < action["range_end"]
                   for action in speed_by_segment.get(segment.get("segment_id"), [])):
                raise AssemblyError(
                    f"act_crossfade_audio join {join_index} is adjacent to act_speed_change; "
                    "v1 does not apply time-axis scaling to original-source handles"
                )
        records.append({
            "join_index": join_index, "duration": duration, "join_time": join_time,
            "left_source_path": str(sources[left_source_id]), "right_source_path": str(sources[right_source_id]),
            "left_cut": left_cut, "right_cut": right_cut,
        })
    for earlier, later in zip(sorted(windows), sorted(windows)[1:]):
        if later[0] < earlier[1]:
            raise AssemblyError(
                "act_crossfade_audio blend windows overlap at joins "
                f"{earlier[2]} and {later[2]}"
            )
    return records


def join_audio_crossfade_argv(
    input_path: str | Path, output_path: str | Path, joins: Sequence[Mapping[str, Any]],
    audio_params: Mapping[str, Any], cleanup_records: Sequence[Mapping[str, Any]] = (),
    bed_records: Sequence[Mapping[str, Any]] = (), mix_record: Mapping[str, Any] | None = None,
    duck_records: Sequence[Mapping[str, Any]] = (),
    source_start_offsets: Mapping[Path, Fraction] | None = None,
) -> list[str]:
    """Build the contract D argv, with the contract's optional fixed-order tail."""
    raw_durations = audio_params.get("clip_durations")
    if not isinstance(raw_durations, Sequence) or isinstance(raw_durations, (str, bytes)) or not raw_durations:
        raise AssemblyError("join audio crossfade argv requires non-empty plan clip_durations")
    try:
        clip_durations = [value if isinstance(value, Fraction) else Fraction(str(value)) for value in raw_durations]
    except (ValueError, ZeroDivisionError) as exc:
        raise AssemblyError("join audio crossfade argv has invalid plan clip_durations") from exc
    if any(value <= 0 for value in clip_durations):
        raise AssemblyError("join audio crossfade argv requires positive plan clip_durations")
    packets = _validated_piece_audio_packets(
        audio_params.get("piece_audio_packets"), len(clip_durations),
        label="join audio crossfade argv",
    )
    if str(audio_params.get("sample_rate")) != str(AAC_AUDIO_SAMPLE_RATE):
        raise AssemblyError("join audio crossfade argv requires the measured 48000 Hz AAC sample rate")
    join_by_index: dict[int, Mapping[str, Any]] = {}
    for join in joins:
        if set(join) != set(JOIN_AUDIO_CROSSFADE_RENDER_RECORD_KEYS):
            raise AssemblyError("join audio crossfade argv join record keys do not match the render contract")
        join_index, duration = join["join_index"], join["duration"]
        if (isinstance(join_index, bool) or not isinstance(join_index, int)
                or join_index in join_by_index or not isinstance(duration, Fraction)
                or not isinstance(join["join_time"], Fraction) or not isinstance(join["left_cut"], Fraction)
                or not isinstance(join["right_cut"], Fraction)
                or not isinstance(join["left_source_path"], str) or not isinstance(join["right_source_path"], str)):
            raise AssemblyError("join audio crossfade argv has an invalid render join record")
        if not 0 <= join_index < len(clip_durations) - 1 or duration <= 0:
            raise AssemblyError("join audio crossfade argv join is outside plan bounds or non-positive")
        if join["join_time"] != _planned_join_time(clip_durations, join_index):
            raise AssemblyError("join audio crossfade argv join time does not match planned piece boundary")
        join_by_index[join_index] = join
    if not join_by_index:
        raise AssemblyError("join audio crossfade argv requires at least one declared join")
    if set(audio_params) != set(JOIN_AUDIO_CROSSFADE_AUDIO_PARAM_KEYS):
        raise AssemblyError("join audio crossfade argv audio_params must use the fixed normalizer audio fields")
    source_inputs: dict[str, int] = {}
    for join in join_by_index.values():
        for key in ("left_source_path", "right_source_path"):
            path = join[key]
            if path not in source_inputs:
                source_inputs[path] = len(source_inputs) + 1
    cleanup_latency_samples = _audio_cleanup_latency_samples(cleanup_records)
    cursor_samples = 0
    plan_total_samples = _planned_piece_sample_boundaries(clip_durations)[-1]
    input_program_samples = plan_total_samples + cleanup_latency_samples
    program_consumes = 0
    for join_index in sorted(join_by_index):
        join = join_by_index[join_index]
        duration_samples = _even_crossfade_duration_samples(join["duration"])
        join_samples = _planned_piece_content_start_samples(clip_durations, join_index + 1)
        half_samples = duration_samples // 2
        window_start_samples, window_end_samples = join_samples - half_samples, join_samples + half_samples
        if window_start_samples < cursor_samples or window_end_samples > plan_total_samples:
            raise AssemblyError("join audio crossfade argv has overlapping or out-of-program windows")
        if cursor_samples < window_start_samples:
            program_consumes += 1
        cursor_samples = window_end_samples
    if cursor_samples < input_program_samples:
        program_consumes += 1
    filters, program_labels = _resplice_concat_audio_input(
        clip_durations, packets, tail_samples=cleanup_latency_samples,
        consumers=max(program_consumes, 1),
    )
    program_at = iter(program_labels)
    cursor_samples = 0
    labels: list[str] = []
    piece_index = 0
    for join_index in sorted(join_by_index):
        join = join_by_index[join_index]
        duration = join["duration"]
        duration_samples = _even_crossfade_duration_samples(duration)
        join_samples = _planned_piece_content_start_samples(clip_durations, join_index + 1)
        half_samples = duration_samples // 2
        window_start_samples, window_end_samples = join_samples - half_samples, join_samples + half_samples
        if window_start_samples < cursor_samples or window_end_samples > plan_total_samples:
            raise AssemblyError("join audio crossfade argv has overlapping or out-of-program windows")
        if cursor_samples < window_start_samples:
            label = f"p{piece_index}"; piece_index += 1
            filters.append(
                f"[{next(program_at)}]atrim=start_sample={cursor_samples}:end_sample={window_start_samples},"
                f"asetpts=PTS-STARTPTS[{label}]"
            )
            labels.append(label)
        left_input = source_inputs[join["left_source_path"]]
        right_input = source_inputs[join["right_source_path"]]
        left_start, left_end = join["left_cut"] - duration / 2, join["left_cut"] + duration / 2
        right_start, right_end = join["right_cut"] - duration / 2, join["right_cut"] + duration / 2
        # Source handles use container time; reuse each original's cached offset.
        if source_start_offsets is not None:
            left_offset = source_start_offsets[Path(join["left_source_path"]).resolve()]
            right_offset = source_start_offsets[Path(join["right_source_path"]).resolve()]
            if left_offset != 0:
                left_start, left_end = left_start + left_offset, left_end + left_offset
            if right_offset != 0:
                right_start, right_end = right_start + right_offset, right_end + right_offset
        filters.extend([
            f"[{left_input}:a]atrim=start={_fraction_decimal(left_start)}:end={_fraction_decimal(left_end)},"
            f"asetpts=PTS-STARTPTS,afade=t=out:st=0:d={_fraction_decimal(duration)}:curve=tri[L{join_index}]",
            f"[{right_input}:a]atrim=start={_fraction_decimal(right_start)}:end={_fraction_decimal(right_end)},"
            f"asetpts=PTS-STARTPTS,afade=t=in:st=0:d={_fraction_decimal(duration)}:curve=tri[R{join_index}]",
            f"[L{join_index}][R{join_index}]amix=inputs=2:duration=longest:normalize=0[w{join_index}]",
        ])
        labels.append(f"w{join_index}")
        cursor_samples = window_end_samples
    # the contract A(1): afftdn must receive L samples of the *real* programme
    # tail. the contract admits that tail from the final measured piece before
    # returning the entire programme to its planned axis.
    if cursor_samples < input_program_samples:
        label = f"p{piece_index}"
        filters.append(
            f"[{next(program_at)}]atrim=start_sample={cursor_samples}:end_sample={input_program_samples},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )
        labels.append(label)
    filters.append("".join(f"[{label}]" for label in labels) + f"concat=n={len(labels)}:v=0:a=1[aout]")
    if not bed_records:
        # Preserve the contract's exact F.3-free graph.
        cleanup_input_filters, cleanup_input = _audio_cleanup_program_input(
            "aout", cleanup_records, plan_total_samples,
        )
        filters.extend(cleanup_input_filters)
        cleanup_filters, final_label = _audio_cleanup_filter_tail(
            cleanup_input, cleanup_records, program_samples=plan_total_samples,
        )
        filters.extend(cleanup_filters)
        bed_inputs: list[str] = []
    else:
        if mix_record is None:
            raise AssemblyError("audio bed argv requires its act_mix_audio record")
        pre_stages = _AUDIO_CLEANUP_ACTION_ORDER[:-1]
        cleanup_input_filters, cleanup_input = _audio_cleanup_program_input(
            "aout", cleanup_records, plan_total_samples,
        )
        filters.extend(cleanup_input_filters)
        cleanup_filters, dialogue_label = _audio_cleanup_filter_tail(
            cleanup_input, cleanup_records, pre_stages, program_samples=plan_total_samples,
        )
        filters.extend(cleanup_filters)
        bed_filters, mixed_label = _audio_bed_filter_chain(
            dialogue_label, bed_records, mix_record,
            list(range(len(source_inputs) + 1, len(source_inputs) + len(bed_records) + 1)),
            duck_records,
        )
        filters.extend(bed_filters)
        loudnorm_filters, final_label = _audio_cleanup_filter_tail(
            mixed_label, cleanup_records, ("act_normalize_loudness",), program_samples=plan_total_samples,
        )
        filters.extend(loudnorm_filters)
        bed_inputs = [item for record in bed_records for item in ("-i", str(record["asset_path"]))]
    return [
        ffmpeg_path(), "-y", "-hide_banner", "-nostdin", "-i", str(input_path),
        *[item for path in source_inputs for item in ("-i", path)],
        *bed_inputs,
        "-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", f"[{final_label}]",
        "-c:v", "copy", "-bsf:v", "setts=pts=PTS-STARTPTS:dts=DTS-STARTPTS",
        "-c:a", str(audio_params["codec"]), "-ar", str(audio_params["sample_rate"]),
        "-ac", str(audio_params["channels"]), "-channel_layout", str(audio_params["channel_layout"]),
        "-b:a", str(audio_params["bitrate"]), str(output_path),
    ]


def _validate_reorder_segments(plan: Mapping[str, Any], action: Mapping[str, Any]) -> None:
    """Accept only a reorder declaration that exactly describes rendered order."""
    if "ordered_segment_ids" not in action:
        raise AssemblyError("act_reorder_segments requires ordered_segment_ids")
    declared = action["ordered_segment_ids"]
    if not isinstance(declared, Sequence) or isinstance(declared, (str, bytes)):
        raise AssemblyError("act_reorder_segments.ordered_segment_ids must be an array")
    if any(not isinstance(segment_id, str) or not segment_id for segment_id in declared):
        raise AssemblyError("act_reorder_segments.ordered_segment_ids must contain non-empty strings")

    segments = plan.get("source_segments")
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)):
        raise AssemblyError("act_reorder_segments requires plan.source_segments to be an array")
    known_segment_ids = {
        segment.get("segment_id")
        for segment in segments
        if isinstance(segment, Mapping) and isinstance(segment.get("segment_id"), str)
    }
    unknown = [segment_id for segment_id in declared if segment_id not in known_segment_ids]
    if unknown:
        raise AssemblyError(
            "act_reorder_segments.ordered_segment_ids references unknown source segment IDs: "
            f"{unknown!r}"
        )

    clips = plan.get("timeline_clips")
    if not isinstance(clips, Sequence) or isinstance(clips, (str, bytes)):
        raise AssemblyError("act_reorder_segments requires plan.timeline_clips to be an array")
    ordered_clips: list[tuple[Fraction, str]] = []
    for clip in clips:
        if not isinstance(clip, Mapping):
            raise AssemblyError("act_reorder_segments requires timeline_clips to contain objects")
        source_segment_id = clip.get("source_segment_id")
        if not isinstance(source_segment_id, str) or not source_segment_id:
            raise AssemblyError("act_reorder_segments requires timeline_clips.source_segment_id strings")
        timeline_range = clip.get("timeline_range")
        if not isinstance(timeline_range, Mapping):
            raise AssemblyError("act_reorder_segments requires timeline_clips.timeline_range objects")
        ordered_clips.append((
            _time_fraction(timeline_range.get("start"), "timeline_range.start"), source_segment_id,
        ))
    actual = [source_segment_id for _, source_segment_id in sorted(ordered_clips, key=lambda item: item[0])]
    if list(declared) != actual:
        raise AssemblyError(
            "act_reorder_segments.ordered_segment_ids does not match rendered timeline order: "
            f"declared {list(declared)!r}; actual {actual!r}"
        )


def _validate_repeat_segment(plan: Mapping[str, Any], action: Mapping[str, Any]) -> None:
    """Validate an the contract repeat declaration without changing assembly input."""
    params = action.get("params")
    if not isinstance(params, Mapping) or set(params) != {"contiguous"} or type(params.get("contiguous")) is not bool:
        raise AssemblyError("act_repeat_segment.params must contain exactly boolean contiguous")
    segment_id = action.get("source_segment_id")
    if not isinstance(segment_id, str) or not segment_id:
        raise AssemblyError("act_repeat_segment.source_segment_id must be a non-empty string")
    clips = plan.get("timeline_clips")
    if not isinstance(clips, Sequence) or isinstance(clips, (str, bytes)):
        raise AssemblyError("act_repeat_segment requires plan.timeline_clips to be an array")
    # Key reuse by SOURCE IDENTITY, not by segment_id.  The contract checker's
    # repeat-segment-declaration rule keys on (source_id, source_range), so a plan
    # that splits one source range into two differently named segments passes there.
    # If the executor counted by segment_id it would reject that same plan, and the
    # two gates would disagree.
    segments = plan.get("source_segments")
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)):
        raise AssemblyError("act_repeat_segment requires plan.source_segments to be an array")
    segment_identity: dict[str, tuple[Any, Fraction, Fraction]] = {}
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise AssemblyError("act_repeat_segment requires source_segments to contain objects")
        candidate_id = segment.get("segment_id")
        source_range = segment.get("source_range")
        if not isinstance(candidate_id, str) or not isinstance(source_range, Mapping):
            raise AssemblyError("act_repeat_segment requires source_segments segment_id and source_range")
        segment_identity[candidate_id] = (
            segment.get("source_id"),
            _time_fraction(source_range.get("start"), "source_range.start"),
            _time_fraction(source_range.get("end"), "source_range.end"),
        )
    if segment_id not in segment_identity:
        raise AssemblyError(f"act_repeat_segment.source_segment_id {segment_id!r} is not in plan.source_segments")
    declared_source_id, declared_start, declared_end = segment_identity[segment_id]
    ordered_clips: list[tuple[Fraction, str]] = []
    for clip in clips:
        if not isinstance(clip, Mapping):
            raise AssemblyError("act_repeat_segment requires timeline_clips to contain objects")
        candidate_segment_id = clip.get("source_segment_id")
        timeline_range = clip.get("timeline_range")
        if not isinstance(candidate_segment_id, str) or not isinstance(timeline_range, Mapping):
            raise AssemblyError("act_repeat_segment requires timeline clip source_segment_id and timeline_range")
        ordered_clips.append((_time_fraction(timeline_range.get("start"), "timeline_range.start"), candidate_segment_id))
    actual = [candidate for _, candidate in sorted(ordered_clips, key=lambda item: item[0])]
    # the contract: reuse means an OVERLAPPING source range, not an identical one.
    # ⛔ the contract checker grades on that same axis; showed that a different axis here
    # makes the two gates disagree on a legitimate plan. Touching ranges
    # (zero-length overlap) are consecutive trims, not reuse.
    positions = []
    for position, candidate in enumerate(actual):
        identity = segment_identity.get(candidate)
        if identity is None or identity[0] != declared_source_id:
            continue
        if min(identity[2], declared_end) - max(identity[1], declared_start) > 0:
            positions.append(position)
    if len(positions) < 2:
        raise AssemblyError("act_repeat_segment declaration has no reused source segment in rendered timeline")
    actual_contiguous = positions == list(range(positions[0], positions[-1] + 1))
    if params["contiguous"] is not actual_contiguous:
        raise AssemblyError(
            "act_repeat_segment.contiguous does not match rendered timeline reuse: "
            f"declared {params['contiguous']!r}; actual {actual_contiguous!r}"
        )


def _structural_declaration_context(
    plan: Mapping[str, Any], action_name: str,
) -> tuple[dict[str, tuple[Any, Fraction, Fraction]], list[tuple[Fraction, Fraction, str]]]:
    """Return the final timeline identities used by the contract declarations."""
    segments = plan.get("source_segments")
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)):
        raise AssemblyError(f"{action_name} requires plan.source_segments to be an array")
    identities: dict[str, tuple[Any, Fraction, Fraction]] = {}
    for segment in segments:
        if not isinstance(segment, Mapping):
            raise AssemblyError(f"{action_name} requires source_segments to contain objects")
        segment_id = segment.get("segment_id")
        source_range = segment.get("source_range")
        if not isinstance(segment_id, str) or not isinstance(source_range, Mapping):
            raise AssemblyError(f"{action_name} requires source_segments segment_id and source_range")
        identities[segment_id] = (
            segment.get("source_id"),
            _time_fraction(source_range.get("start"), "source_range.start"),
            _time_fraction(source_range.get("end"), "source_range.end"),
        )
    clips = plan.get("timeline_clips")
    if not isinstance(clips, Sequence) or isinstance(clips, (str, bytes)):
        raise AssemblyError(f"{action_name} requires plan.timeline_clips to be an array")
    ordered_clips: list[tuple[Fraction, Fraction, str]] = []
    for clip in clips:
        if not isinstance(clip, Mapping):
            raise AssemblyError(f"{action_name} requires timeline_clips to contain objects")
        segment_id = clip.get("source_segment_id")
        timeline_range = clip.get("timeline_range")
        if not isinstance(segment_id, str) or not isinstance(timeline_range, Mapping):
            raise AssemblyError(
                f"{action_name} requires timeline clip source_segment_id and timeline_range"
            )
        ordered_clips.append((
            _time_fraction(timeline_range.get("start"), "timeline_range.start"),
            _time_fraction(timeline_range.get("end"), "timeline_range.end"), segment_id,
        ))
    return identities, sorted(ordered_clips, key=lambda item: item[0])


def _validate_ripple_delete(plan: Mapping[str, Any], action: Mapping[str, Any]) -> None:
    """Validate the contract's declaration against the final, already-rippled timeline."""
    params = action.get("params")
    if not isinstance(params, Mapping) or set(params) != {"scope"} or params.get("scope") != "all_tracks":
        raise AssemblyError("act_ripple_delete.params must contain exactly scope=all_tracks")
    segment_id = action.get("source_segment_id")
    if not isinstance(segment_id, str) or not segment_id:
        raise AssemblyError("act_ripple_delete.source_segment_id must be a non-empty string")
    identities, clips = _structural_declaration_context(plan, "act_ripple_delete")
    if segment_id not in identities:
        raise AssemblyError(
            f"act_ripple_delete.source_segment_id {segment_id!r} is not in plan.source_segments"
        )
    positions = [index for index, (_, _, candidate) in enumerate(clips) if candidate == segment_id]
    if not positions:
        raise AssemblyError(
            "act_ripple_delete.source_segment_id must resolve to a timeline clip"
        )
    pairs = [
        (position, clips[position + 1]) for position in positions if position + 1 < len(clips)
    ]
    if not pairs:
        raise AssemblyError("act_ripple_delete requires a following timeline clip")
    left_source_id, left_start, left_end = identities[segment_id]
    same_source = [
        (position, right) for position, right in pairs
        if right[2] in identities and identities[right[2]][0] == left_source_id
    ]
    if not same_source:
        raise AssemblyError(
            "act_ripple_delete requires the following timeline clip to have the same source_id"
        )
    source_gap = [
        (position, right) for position, right in same_source
        if identities[right[2]][1] > left_end
    ]
    if not source_gap:
        raise AssemblyError(
            "act_ripple_delete requires a positive source-range gap after source_segment_id"
        )
    if not any(right[0] == clips[position][1] for position, right in source_gap):
        raise AssemblyError(
            "act_ripple_delete requires no timeline gap after source_segment_id"
        )


def _validate_insert_segment(plan: Mapping[str, Any], action: Mapping[str, Any]) -> None:
    """Validate the contract's inserted-piece declaration without replaying an edit."""
    params = action.get("params")
    if not isinstance(params, Mapping) or set(params) != {"mode"} or params.get("mode") != "ripple":
        raise AssemblyError("act_insert_segment.params must contain exactly mode=ripple")
    segment_id = action.get("source_segment_id")
    if not isinstance(segment_id, str) or not segment_id:
        raise AssemblyError("act_insert_segment.source_segment_id must be a non-empty string")
    identities, clips = _structural_declaration_context(plan, "act_insert_segment")
    if segment_id not in identities:
        raise AssemblyError(
            f"act_insert_segment.source_segment_id {segment_id!r} is not in plan.source_segments"
        )
    positions = [index for index, (_, _, candidate) in enumerate(clips) if candidate == segment_id]
    if not positions:
        raise AssemblyError("act_insert_segment.source_segment_id must resolve to a timeline clip")
    triples = [
        (clips[position - 1], clips[position], clips[position + 1])
        for position in positions if 0 < position < len(clips) - 1
    ]
    if not triples:
        raise AssemblyError("act_insert_segment requires timeline clips on both sides")
    same_source = [
        (before, inserted, after) for before, inserted, after in triples
        if before[2] in identities and after[2] in identities
        and identities[before[2]][0] == identities[after[2]][0]
    ]
    if not same_source:
        raise AssemblyError(
            "act_insert_segment requires its adjacent timeline clips to have the same source_id"
        )
    source_touching = [
        (before, inserted, after) for before, inserted, after in same_source
        if identities[after[2]][1] == identities[before[2]][2]
    ]
    if not source_touching:
        raise AssemblyError(
            "act_insert_segment requires its adjacent source ranges to touch"
        )
    if not any(
        inserted[0] == before[1] and after[0] == inserted[1]
        for before, inserted, after in source_touching
    ):
        raise AssemblyError(
            "act_insert_segment requires no timeline gaps around source_segment_id"
        )


def _validate_style_caption_segment(plan: Mapping[str, Any], action: Mapping[str, Any]) -> None:
    """Validate the contract caption-style declaration against its final plan."""
    params = action.get("params")
    if not isinstance(params, Mapping) or set(params) != {"caption_id", "style_id"}:
        raise AssemblyError("act_style_caption_segment.params must contain exactly caption_id and style_id")
    caption_id = params.get("caption_id")
    style_id = params.get("style_id")
    captions = plan.get("captions")
    if not isinstance(captions, Sequence) or isinstance(captions, (str, bytes)):
        raise AssemblyError("act_style_caption_segment requires plan.captions to be an array")
    matching_captions = [
        caption for caption in captions
        if isinstance(caption, Mapping) and caption.get("caption_id") == caption_id
    ]
    if len(matching_captions) != 1:
        raise AssemblyError(
            f"act_style_caption_segment.params.caption_id {caption_id!r} must resolve to exactly one plan.captions record"
        )
    caption_style_ref = matching_captions[0].get("style_ref")
    if style_id != caption_style_ref:
        raise AssemblyError(
            f"act_style_caption_segment.params.style_id {style_id!r} does not match "
            f"caption {caption_id!r}.style_ref {caption_style_ref!r}"
        )
    styles = plan.get("caption_styles")
    if not isinstance(styles, Sequence) or isinstance(styles, (str, bytes)):
        raise AssemblyError("act_style_caption_segment requires plan.caption_styles to be an array")
    if sum(isinstance(style, Mapping) and style.get("style_id") == style_id for style in styles) != 1:
        raise AssemblyError(
            f"act_style_caption_segment.params.style_id {style_id!r} must resolve to exactly one plan.caption_styles record"
        )
    actions = plan.get("actions")
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
        raise AssemblyError("act_style_caption_segment requires plan.actions to be an array")
    style_declarations = [
        candidate for candidate in actions
        if isinstance(candidate, Mapping)
        and candidate.get("action") == "act_style_caption_segment"
        and isinstance(candidate.get("params"), Mapping)
        and candidate["params"].get("caption_id") == caption_id
    ]
    if len(style_declarations) != 1:
        raise AssemblyError(f"act_style_caption_segment declares caption_id {caption_id!r} more than once")
    has_add_action = any(
        isinstance(candidate, Mapping)
        and candidate.get("action") == "act_add_caption_segment"
        and isinstance(candidate.get("params"), Mapping)
        and candidate["params"].get("caption_id") == caption_id
        for candidate in actions
    )
    if not has_add_action:
        raise AssemblyError(
            f"act_style_caption_segment caption_id {caption_id!r} requires an act_add_caption_segment action"
        )


def _validate_actions(
    plan: Mapping[str, Any],
    errors: list[str] | None = None,
) -> tuple[
    dict[str, Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]],
    dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]],
]:
    actions = plan.get("actions")
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
        exc = AssemblyError("actions must be an array")
        if errors is None:
            raise exc
        errors.append(str(exc))
        return {}, [], [], {}, {}, {}, {}, [], []
    crop_by_segment: dict[str, Mapping[str, Any]] = {}
    caption_actions: list[Mapping[str, Any]] = []
    overlay_actions: list[Mapping[str, Any]] = []
    speed_by_segment: dict[str, list[dict[str, Any]]] = {}
    freeze_by_segment: dict[str, list[dict[str, Any]]] = {}
    blur_by_segment: dict[str, list[dict[str, Any]]] = {}
    zoom_by_segment: dict[str, list[dict[str, Any]]] = {}
    for action in actions:
        try:
            if not isinstance(action, Mapping):
                raise AssemblyError("actions must contain objects")
            name = action.get("action")
            # the contract D has an assembly post-pass but is deliberately not L2
            # dispatch until its actual-material verification is accepted. Its
            # plan fields are checked by _crossfade_audio_joins below.
            if name not in L2_DISPATCH_ACTIONS and name not in {
                "act_repeat_segment", "act_ripple_delete", "act_insert_segment", "act_style_caption_segment", "act_crossfade_audio", *_AUDIO_CLEANUP_ACTION_ORDER,
                *_AUDIO_BED_ACTIONS, "act_mix_audio", "act_duck_audio",
            }:
                raise AssemblyError(f"unsupported action for this slice: {name!r}")
            if name == "act_trim_segment" and "params" in action:
                raise AssemblyError("act_trim_segment must not contain params")
            if name == "act_assemble_segment":
                params = action.get("params")
                if not isinstance(params, Mapping) or params.get("join_mode") != "concat_copy":
                    raise AssemblyError("only act_assemble_segment.params.join_mode=concat_copy is executable")
            if name == "act_reorder_segments":
                _validate_reorder_segments(plan, action)
            if name == "act_repeat_segment":
                _validate_repeat_segment(plan, action)
            if name == "act_ripple_delete":
                _validate_ripple_delete(plan, action)
            if name == "act_insert_segment":
                _validate_insert_segment(plan, action)
            if name == "act_style_caption_segment":
                _validate_style_caption_segment(plan, action)
            if name == "act_crop_reframe":
                source_segment_id = action.get("source_segment_id")
                if not isinstance(source_segment_id, str):
                    raise AssemblyError("act_crop_reframe.source_segment_id must be a string")
                if source_segment_id in crop_by_segment:
                    raise AssemblyError(
                        f"multiple act_crop_reframe actions target source_segment_id {source_segment_id!r}"
                    )
                crop_by_segment[source_segment_id] = _validate_crop_reframe_params(action.get("params"))
            if name == "act_add_caption_segment":
                params = action.get("params")
                required = {"caption_id", "overlay_x", "overlay_y", "enable_axis"}
                if not isinstance(params, Mapping) or set(params) != required:
                    raise AssemblyError(
                        "act_add_caption_segment.params must contain exactly caption_id, overlay_x, overlay_y, and enable_axis"
                    )
                if not isinstance(params["caption_id"], str) or not params["caption_id"]:
                    raise AssemblyError("act_add_caption_segment.params.caption_id must be a non-empty string")
                for field in ("overlay_x", "overlay_y"):
                    if isinstance(params[field], bool) or not isinstance(params[field], int):
                        raise AssemblyError(f"act_add_caption_segment.params.{field} must be an integer")
                if params["enable_axis"] != "final_timeline":
                    raise AssemblyError("act_add_caption_segment.params.enable_axis must be final_timeline")
                caption_actions.append(action)
            if name == "act_overlay_asset":
                if not isinstance(action.get("asset_id"), str) or not action["asset_id"]:
                    raise AssemblyError("act_overlay_asset.asset_id must be a non-empty string")
                params = action.get("params")
                required = {"overlay_x", "overlay_y", "enable_axis", "timeline_range"}
                if not isinstance(params, Mapping) or set(params) != required:
                    raise AssemblyError(
                        "act_overlay_asset.params must contain exactly overlay_x, overlay_y, enable_axis, and timeline_range"
                    )
                for field in ("overlay_x", "overlay_y"):
                    if isinstance(params[field], bool) or not isinstance(params[field], int):
                        raise AssemblyError(f"act_overlay_asset.params.{field} must be an integer")
                if params["enable_axis"] != "final_timeline":
                    raise AssemblyError("act_overlay_asset.params.enable_axis must be final_timeline")
                if not isinstance(params["timeline_range"], Mapping):
                    raise AssemblyError("act_overlay_asset.params.timeline_range must be an object")
                overlay_actions.append(action)
            if name == "act_speed_change":
                source_segment_id = action.get("source_segment_id")
                if not isinstance(source_segment_id, str):
                    raise AssemblyError("act_speed_change.source_segment_id must be a string")
                speed_by_segment.setdefault(source_segment_id, []).append(
                    _validate_speed_change_params(action.get("params"), action.get("action_id"))
                )
            if name == "act_freeze_frame":
                source_segment_id = action.get("source_segment_id")
                if not isinstance(source_segment_id, str):
                    raise AssemblyError("act_freeze_frame.source_segment_id must be a string")
                freeze_by_segment.setdefault(source_segment_id, []).append(
                    _validate_freeze_frame_params(action.get("params"), action.get("action_id"))
                )
            if name == "act_blur_region":
                source_segment_id = action.get("source_segment_id")
                if not isinstance(source_segment_id, str):
                    raise AssemblyError("act_blur_region.source_segment_id must be a string")
                blur_by_segment.setdefault(source_segment_id, []).append(
                    _validate_blur_region_params(action.get("params"), action.get("action_id"))
                )
            if name == "act_zoom_keyframes":
                source_segment_id = action.get("source_segment_id")
                if not isinstance(source_segment_id, str):
                    raise AssemblyError("act_zoom_keyframes.source_segment_id must be a string")
                zoom_by_segment.setdefault(source_segment_id, []).append(
                    _validate_zoom_keyframes_params(action.get("params"), action.get("action_id"))
                )
        except AssemblyError as exc:
            if errors is None:
                raise
            errors.append(str(exc))
            continue
    for source_segment_id in sorted(set(blur_by_segment) & set(zoom_by_segment)):
        message = (
            "act_blur_region and act_zoom_keyframes cannot coexist on source_segment_id "
            f"{source_segment_id!r}"
        )
        if errors is None:
            raise AssemblyError(message)
        errors.append(message)
    caption_plan_records = _caption_plan_records(plan, caption_actions, errors)
    overlay_plan_records = _overlay_plan_records(plan, overlay_actions, errors)
    return (
        crop_by_segment, caption_actions, overlay_actions, speed_by_segment, freeze_by_segment, blur_by_segment, zoom_by_segment,
        caption_plan_records, overlay_plan_records,
    )


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssemblyError(f"cannot read {label}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise AssemblyError(f"{label} must be a JSON object")
    return payload


def _source_records(index: Mapping[str, Any], source_map: Mapping[str, Any]) -> dict[str, Path]:
    sources = index.get("sources")
    if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)):
        raise AssemblyError("index.sources must be an array")
    resolved: dict[str, Path] = {}
    for source in sources:
        if not isinstance(source, Mapping) or not isinstance(source.get("source_id"), str):
            raise AssemblyError("each index source must have source_id")
        source_id = source["source_id"]
        mapped = source_map.get(source_id)
        if not isinstance(mapped, str):
            raise AssemblyError(f"source map has no path for source_id {source_id!r}")
        path = Path(mapped)
        if not path.is_file():
            raise AssemblyError(f"resolved source path is not a file for source_id {source_id!r}: {path}")
        actual = _sha256(path)
        expected = source.get("sha256")
        if actual != expected:
            raise AssemblyError(f"source SHA-256 mismatch for source_id {source_id!r}: expected {expected}, got {actual}")
        resolved[source_id] = path
    return resolved


def _prepare_indexed_sources(
    sources: Mapping[str, Path], index: Mapping[str, Any], work: Path,
) -> tuple[dict[str, Path], list[dict[str, Any]]]:
    """Resolve transport streams to one seekable stream copy per verified SHA."""
    resolved = dict(sources)
    records: list[dict[str, Any]] = []
    source_shas = {item["source_id"]: item["sha256"] for item in index["sources"]}
    by_sha: dict[str, Path] = {}
    formats_by_sha: dict[str, str] = {}
    for source_id, original in sources.items():
        source_sha = source_shas[source_id]
        if source_sha in by_sha:
            resolved[source_id] = by_sha[source_sha]
            continue
        if source_sha not in formats_by_sha:
            observation = probe_format_name(original)
            format_name = observation["stdout"].strip()
            if observation["returncode"] != 0 or not format_name:
                raise AssemblyError(
                    f"cannot probe source format for {original}: argv={observation['argv']!r}; "
                    f"stderr={observation['stderr'][-2000:]}"
                )
            formats_by_sha[source_sha] = format_name
        format_name = formats_by_sha[source_sha]
        if "mpegts" not in format_name.split(","):
            # Preserve each caller-supplied non-transport path, including aliases.
            continue
        directory = work / "indexed-sources"
        directory.mkdir(parents=True, exist_ok=True)
        indexed_copy = directory / f"{source_sha}.mp4"
        argv = [
            ffmpeg_path(), "-y", "-hide_banner", "-nostdin",
            "-copyts", "-start_at_zero", "-i", str(original),
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-avoid_negative_ts", "disabled", str(indexed_copy),
        ]
        command = run_command(argv)
        failure = (
            f"indexed source creation failed for {original}: argv={argv!r}; "
            f"stderr={command['stderr'][-2000:]}"
        )
        if command["returncode"] != 0 or not indexed_copy.is_file():
            raise AssemblyError(failure)
        video = probe_json([
            "-v", "error", "-select_streams", "v:0", "-show_entries",
            "stream=codec_type", "-of", "json",
        ], indexed_copy)
        payload = video.get("json")
        streams = payload.get("streams", []) if isinstance(payload, Mapping) else []
        if video["returncode"] != 0 or not any(
            isinstance(stream, Mapping) and stream.get("codec_type") == "video"
            for stream in streams
        ):
            raise AssemblyError(f"{failure}; no video stream; probe stderr={video['stderr'][-2000:]}")
        by_sha[source_sha] = indexed_copy
        resolved[source_id] = indexed_copy
        records.append({
            "source_path": str(original), "source_sha256": source_sha,
            "copy_path": str(indexed_copy), "copy_sha256": _sha256(indexed_copy),
            "argv": argv, "format_name": format_name,
        })
    return resolved, records


def _prepare_normalized_sources(
    sources: Mapping[str, Path], source_ids: set[str], index_by_source: Mapping[str, Mapping[str, Any]],
    policy: Mapping[str, Any], work: Path,
) -> tuple[dict[str, Path], list[dict[str, Any]]]:
    """Derive and reuse CFR copies for header VFR or sources off the planned fps grid."""
    video_sources: dict[str, Path] = {}
    records: list[dict[str, Any]] = []
    fps = ffmpeg_rational(policy["frame_rate"])
    for source_id in sorted(source_ids):
        original = sources[source_id]
        try:
            vfr = needs_normalized_copy(original, policy["frame_rate"])
        except ValueError as exc:
            raise AssemblyError(str(exc)) from exc
        if not vfr:
            continue
        source_sha = index_by_source[source_id]["sha256"]
        # The two paths are excluded from the recipe digest; the input identity
        # is supplied separately by its already-checked source SHA-256.
        recipe = build_normalized_source_argv(original, work / "unused.mp4", policy)
        recipe_argv = recipe[:2] + recipe[3:-1]
        recipe_sha = hashlib.sha256(json.dumps(recipe_argv, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        key = hashlib.sha256(json.dumps([source_sha, fps, recipe_sha], separators=(",", ":")).encode("utf-8")).hexdigest()
        directory = work / "normalized-sources"
        copy = directory / f"{key}.mp4"
        sidecar = directory / f"{key}.json"
        argv = build_normalized_source_argv(original, copy, policy)
        reused = False
        if copy.exists() or sidecar.exists():
            if not copy.is_file() or not sidecar.is_file():
                raise AssemblyError(f"incomplete normalized source for {source_id!r}: {copy}")
            metadata = _load_json(sidecar, "normalized source sidecar")
            recorded_argv = metadata.get("argv")
            if (metadata.get("source_sha256") != source_sha or metadata.get("key") != key
                    or not isinstance(recorded_argv, list)
                    or recorded_argv[:2] + recorded_argv[3:-1] != recipe_argv
                    or recorded_argv[-1] != str(copy)
                    or metadata.get("copy_sha256") != _sha256(copy)):
                raise AssemblyError(f"normalized source sidecar mismatch for {source_id!r}: {sidecar}")
            # Retain the command that actually made this copy; the current
            # source may have moved while its verified bytes stayed identical.
            argv = recorded_argv
            reused = True
        else:
            directory.mkdir(parents=True, exist_ok=True)
            command = run_command(argv)
            if command["returncode"] != 0 or not copy.is_file():
                raise AssemblyError(f"normalized source creation failed for {source_id!r}: {command['stderr'][-2000:]}")
            sidecar.write_text(json.dumps({
                "source_sha256": source_sha, "key": key, "argv": argv,
                "copy_sha256": _sha256(copy),
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        video_sources[source_id] = copy
        records.append({
            "source_id": source_id, "source_path": str(original), "source_sha256": source_sha,
            "copy_path": str(copy), "copy_sha256": _sha256(copy), "key": key,
            "sidecar_path": str(sidecar), "argv": argv, "reused": reused,
        })
    return video_sources, records


def _caption_plan_records(
    plan: Mapping[str, Any], caption_actions: Sequence[Mapping[str, Any]], errors: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Validate and select caption records using plan values only."""
    if not caption_actions:
        return []
    captions = plan.get("captions")
    styles = plan.get("caption_styles")
    assets = plan.get("assets")
    if not isinstance(captions, Sequence) or isinstance(captions, (str, bytes)):
        exc = AssemblyError("plan.captions must be an array")
        if errors is None:
            raise exc
        errors.append(str(exc))
        return []
    if not isinstance(styles, Sequence) or isinstance(styles, (str, bytes)):
        exc = AssemblyError("plan.caption_styles must be an array")
        if errors is None:
            raise exc
        errors.append(str(exc))
        return []
    if not isinstance(assets, Sequence) or isinstance(assets, (str, bytes)):
        exc = AssemblyError("plan.assets must be an array")
        if errors is None:
            raise exc
        errors.append(str(exc))
        return []
    action_by_caption: dict[str, Mapping[str, Any]] = {}
    for action in caption_actions:
        try:
            params = action["params"]
            caption_id = params["caption_id"]
            if caption_id in action_by_caption:
                raise AssemblyError(f"multiple act_add_caption_segment actions reference caption_id {caption_id!r}")
            action_by_caption[caption_id] = action
        except AssemblyError as exc:
            if errors is None:
                raise
            errors.append(str(exc))
            continue
    selected: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    found_caption_ids: set[str] = set()
    for caption in captions:
        try:
            if not isinstance(caption, Mapping):
                raise AssemblyError("plan.captions must contain objects")
            caption_id = caption.get("caption_id")
            if caption_id in action_by_caption:
                if caption_id in found_caption_ids:
                    raise AssemblyError(f"caption_id {caption_id!r} must resolve to exactly one captions[] record")
                found_caption_ids.add(caption_id)
                selected.append((caption, action_by_caption[caption_id]))
        except AssemblyError as exc:
            if errors is None:
                raise
            errors.append(str(exc))
            continue
    for caption_id in sorted(set(action_by_caption) - found_caption_ids):
        exc = AssemblyError(f"act_add_caption_segment references unknown caption_id {caption_id!r}")
        if errors is None:
            raise exc
        errors.append(str(exc))
    policy = plan.get("output_policy")
    output_width = policy.get("width") if isinstance(policy, Mapping) else None
    records: list[dict[str, Any]] = []
    for order, (caption, action) in enumerate(selected):
        try:
            caption_id = caption.get("caption_id")
            text = caption.get("text")
            style_ref = caption.get("style_ref")
            if not isinstance(text, str) or not text:
                raise AssemblyError(f"caption {caption_id!r}.text must be a non-empty string")
            if not isinstance(style_ref, str) or not style_ref:
                raise AssemblyError(f"caption {caption_id!r}.style_ref must be a non-empty string")
            matching_styles = [style for style in styles if isinstance(style, Mapping) and style.get("style_id") == style_ref]
            if len(matching_styles) != 1:
                raise AssemblyError(f"caption {caption_id!r}.style_ref {style_ref!r} must resolve to exactly one caption style")
            style = matching_styles[0]
            font_asset_id = style.get("font_asset_id")
            matching_assets = [asset for asset in assets if isinstance(asset, Mapping) and asset.get("asset_id") == font_asset_id]
            if not isinstance(font_asset_id, str) or len(matching_assets) != 1:
                raise AssemblyError(f"caption style {style_ref!r}.font_asset_id must resolve to exactly one asset")
            asset = matching_assets[0]
            timeline_range = caption.get("timeline_range")
            if not isinstance(timeline_range, Mapping):
                raise AssemblyError(f"caption {caption_id!r}.timeline_range must be an object")
            start = _time_fraction(timeline_range.get("start"), f"caption {caption_id!r}.timeline_range.start")
            end = _time_fraction(timeline_range.get("end"), f"caption {caption_id!r}.timeline_range.end")
            if start < 0 or end <= start:
                raise AssemblyError(f"caption {caption_id!r}.timeline_range must be non-negative and non-empty")
            params = action["params"]
            records.append({
                "order": order, "caption": caption, "action": action, "style": style, "asset": asset,
                "caption_id": caption_id, "text": text, "style_ref": style_ref, "font_asset_id": font_asset_id,
                "expected_sha": asset.get("sha256"), "start": start, "end": end,
                "overlay": {"x": params["overlay_x"], "y": params["overlay_y"]},
                "assets": assets, "output_width": output_width, "output_policy": policy,
            })
        except AssemblyError as exc:
            if errors is None:
                raise
            errors.append(str(exc))
            continue
    # v1 intentionally turns the observed non-overlap into an executable
    # contract. A transparent concat track has one visual layer, so accepting
    # overlap would silently choose a composition/rounding order for it.
    ordered = sorted(records, key=lambda record: (record["start"], record["end"], record["caption_id"]))
    for earlier, later in zip(ordered, ordered[1:]):
        if later["start"] < earlier["end"]:
            def interval(record: Mapping[str, Any]) -> str:
                start, end = record["start"], record["end"]
                return f"[{start.numerator}/{start.denominator}, {end.numerator}/{end.denominator})"
            exc = AssemblyError(
                "caption timeline ranges overlap: "
                f"{earlier['caption_id']!r} {interval(earlier)} and "
                f"{later['caption_id']!r} {interval(later)}"
            )
            if errors is None:
                raise exc
            errors.append(str(exc))
            break
    # Catch "does every caption have at least one displayable frame" at the CONTRACT
    # layer.  This check is what states the bound N <= timeline frame count.  Before
    # it existed the bound lived only in code, and `--validate-only` passed 30,000
    # captions of 0.0197s each as `validated`.
    try:
        frame_rate = contract_fraction(policy.get("frame_rate")) if isinstance(policy, Mapping) else None
    except (RationalValueError, TypeError):
        frame_rate = None
    if frame_rate is not None and frame_rate > 0:
        for record in ordered:
            start_frame, end_frame = _caption_frame_bounds(record["start"], record["end"], frame_rate)
            if end_frame <= start_frame:
                exc = AssemblyError(
                    f"caption {record['caption_id']!r} has no displayable frame at output_policy.frame_rate"
                )
                if errors is None:
                    raise exc
                errors.append(str(exc))
                break
    return records


def _caption_records(
    plan_records: Sequence[Mapping[str, Any]], asset_map_path: Path | None, work: Path,
) -> list[dict[str, Any]]:
    """Resolve and render plan-validated captions before ffmpeg overlays."""
    if not plan_records:
        return []
    if asset_map_path is None:
        raise AssemblyError("--asset-map is required when act_add_caption_segment is present")
    asset_map = _load_json(asset_map_path, "asset map")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in asset_map.items()):
        raise AssemblyError("asset map must be an object of asset_id to file path strings")
    assets = plan_records[0]["assets"]
    contract_dir = work / "caption-contract"
    contract_dir.mkdir(parents=True, exist_ok=True)
    assets_path = contract_dir / "assets.json"
    assets_path.write_text(json.dumps(list(assets), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rendered: list[dict[str, Any]] = []
    for plan_record in plan_records:
        order = plan_record["order"]
        style = plan_record["style"]
        caption_id = plan_record["caption_id"]
        text = plan_record["text"]
        style_ref = plan_record["style_ref"]
        font_asset_id = plan_record["font_asset_id"]
        expected_sha = plan_record["expected_sha"]
        start = plan_record["start"]
        end = plan_record["end"]
        if font_asset_id not in asset_map:
            raise AssemblyError(f"asset map has no path for font_asset_id {font_asset_id!r}")
        font_path = Path(asset_map[font_asset_id]).expanduser()
        if not font_path.is_absolute():
            font_path = asset_map_path.parent / font_path
        font_path = font_path.resolve()
        if not font_path.is_file():
            raise AssemblyError(f"resolved font asset is not a file: {font_path}")
        actual_sha = _sha256(font_path)
        if actual_sha != expected_sha:
            raise AssemblyError(
                f"font asset {font_asset_id!r} SHA-256 mismatch: expected {expected_sha}, got {actual_sha}"
            )
        style_path = contract_dir / f"style-{order:03d}.json"
        style_path.write_text(json.dumps(style, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        png = contract_dir / f"caption-{order:03d}.png"
        renderer_argv = [
            sys.executable, str(Path(__file__).with_name("caption_png.py")), "--text", text, "--out", str(png),
            "--width", str(plan_record["output_width"]), "--caption-style-json", str(style_path),
            "--assets-json", str(assets_path), "--asset-map", str(asset_map_path),
        ]
        renderer = run_command(renderer_argv)
        if renderer.get("returncode") != 0 or not png.is_file():
            raise AssemblyError(f"caption PNG rendering failed for caption_id {caption_id!r}")
        try:
            renderer_result = json.loads(renderer["stdout"].splitlines()[-1])
            png_width, png_height = renderer_result["width"], renderer_result["height"]
            if (isinstance(png_width, bool) or not isinstance(png_width, int) or png_width < 1
                    or isinstance(png_height, bool) or not isinstance(png_height, int) or png_height < 1):
                raise ValueError("width and height must be positive integers")
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AssemblyError(
                f"caption PNG renderer did not report valid dimensions for caption_id {caption_id!r}"
            ) from exc
        geometry_errors = caption_overlay_geometry_errors(
            plan_record["overlay"], png_width, png_height, plan_record["output_policy"],
        )
        if geometry_errors:
            raise AssemblyError(f"caption_id {caption_id!r}: {'; '.join(geometry_errors)}")
        enable = f"gte(t,{_floor_decimal(start)})*lt(t,{_floor_decimal(end)})"
        rendered.append({
            "order": order, "caption_id": caption_id, "style_ref": style_ref, "png": str(png),
            "png_sha256": _sha256(png), "enable": enable,
            #: the transparent-track path needs the exact rational bounds, not the
            # decimal-rounded "enable" string. Keeping them on the record is what lets
            # _caption_track_segments cut frame-accurate pieces. ⛔ Do not drop these:
            # shipped the track path without them and the real render died
            # with KeyError while every gate was green (the gate built its own records).
            "start": start, "end": end,
            "overlay": plan_record["overlay"],
            "renderer": renderer,
        })
    return rendered


def caption_overlay_geometry_errors(
    overlay: Mapping[str, Any], png_width: int, png_height: int, policy: Mapping[str, Any],
) -> list[str]:
    """Return the contract screen-boundary violations for one rendered caption PNG.

    This deliberately depends only on supplied geometry, so regression tests do
    not require Pillow, ffmpeg, or a rendered media fixture.
    """
    x, y = overlay["x"], overlay["y"]
    width, height = policy["width"], policy["height"]
    errors = []
    if x < 0:
        errors.append(f"caption overlay x {x} must be >= 0")
    if x + png_width > width:
        errors.append(f"caption overlay right edge {x + png_width} exceeds output width {width}")
    if y < 0:
        errors.append(f"caption overlay y {y} must be >= 0")
    if y + png_height > height:
        errors.append(f"caption overlay bottom edge {y + png_height} exceeds output height {height}")
    return errors


def _overlay_plan_records(
    plan: Mapping[str, Any], overlay_actions: Sequence[Mapping[str, Any]], errors: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Validate overlay actions and calculate their plan-only render inputs."""
    if not overlay_actions:
        return []
    assets = plan.get("assets")
    if not isinstance(assets, Sequence) or isinstance(assets, (str, bytes)):
        exc = AssemblyError("plan.assets must be an array")
        if errors is None:
            raise exc
        errors.append(str(exc))
        return []
    policy = plan.get("output_policy")
    if not isinstance(policy, Mapping):
        exc = AssemblyError("plan.output_policy must be an object")
        if errors is None:
            raise exc
        errors.append(str(exc))
        return []
    try:
        frame_rate = contract_fraction(policy.get("frame_rate"))
    except (RationalValueError, TypeError) as exc:
        error = AssemblyError(f"output_policy.frame_rate is invalid: {exc}")
        if errors is None:
            raise error from exc
        errors.append(str(error))
        return []
    if frame_rate <= 0:
        exc = AssemblyError("output_policy.frame_rate must be positive")
        if errors is None:
            raise exc
        errors.append(str(exc))
        return []
    records: list[dict[str, Any]] = []
    for order, action in enumerate(overlay_actions):
        try:
            asset_id = action["asset_id"]
            matching = [asset for asset in assets if isinstance(asset, Mapping) and asset.get("asset_id") == asset_id]
            if len(matching) != 1:
                raise AssemblyError(f"act_overlay_asset.asset_id {asset_id!r} must resolve to exactly one assets[] record")
            asset = matching[0]
            params = action["params"]
            timeline_range = params["timeline_range"]
            start = _time_fraction(timeline_range.get("start"), f"act_overlay_asset {asset_id!r}.timeline_range.start")
            end = _time_fraction(timeline_range.get("end"), f"act_overlay_asset {asset_id!r}.timeline_range.end")
            if start < 0 or end <= start:
                raise AssemblyError(f"act_overlay_asset {asset_id!r}.timeline_range must be non-negative and non-empty")
            # t is a frame PTS, so a mid-frame start is snapped down to that
            # containing frame; the end remains the declared half-open boundary.
            start_frame = start * frame_rate
            start_snapped = Fraction(start_frame.numerator // start_frame.denominator, 1) / frame_rate
            records.append({
                "order": order, "asset_id": asset_id, "asset": asset,
                "enable": f"gte(t,{_floor_decimal(start_snapped)})*lt(t,{_floor_decimal(end)})",
                "overlay": {"x": params["overlay_x"], "y": params["overlay_y"]},
            })
        except AssemblyError as exc:
            if errors is None:
                raise
            errors.append(str(exc))
            continue
    return records


def _overlay_asset_records(
    plan_records: Sequence[Mapping[str, Any]], asset_map_path: Path | None,
) -> list[dict[str, Any]]:
    """Resolve approved image assets that are overlaid on the final timeline.

    An opaque mask is an overlay, not a caption: it carries no text and no
    style. The asset is verified by the plan-declared SHA-256 exactly as a
    caption font is, so a swapped mask file is rejected rather than rendered.
    """
    if not plan_records:
        return []
    if asset_map_path is None:
        raise AssemblyError("--asset-map is required when act_overlay_asset is present")
    asset_map = _load_json(asset_map_path, "asset map")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in asset_map.items()):
        raise AssemblyError("asset map must be an object of asset_id to file path strings")
    records: list[dict[str, Any]] = []
    for plan_record in plan_records:
        order = plan_record["order"]
        asset_id = plan_record["asset_id"]
        asset = plan_record["asset"]
        if asset_id not in asset_map:
            raise AssemblyError(f"asset map has no path for asset_id {asset_id!r}")
        overlay_path = Path(asset_map[asset_id]).expanduser()
        if not overlay_path.is_absolute():
            overlay_path = asset_map_path.parent / overlay_path
        overlay_path = overlay_path.resolve()
        if not overlay_path.is_file():
            raise AssemblyError(f"resolved overlay asset is not a file: {overlay_path}")
        actual_sha = _sha256(overlay_path)
        if actual_sha != asset.get("sha256"):
            raise AssemblyError(
                f"overlay asset {asset_id!r} SHA-256 mismatch: expected {asset.get('sha256')}, got {actual_sha}"
            )
        records.append({
            "order": order, "asset_id": asset_id, "png": str(overlay_path), "png_sha256": actual_sha,
            "enable": plan_record["enable"], "overlay": plan_record["overlay"],
        })
    return records


def _caption_overlay_argv(staged: Path, output: Path, policy: Mapping[str, Any], captions: Sequence[Mapping[str, Any]]) -> list[str]:
    """Reuse normalise's canonical encoder arguments for one final overlay pass."""
    canonical = build_normalize_argv(staged, output, policy)
    vf_index = canonical.index("-vf")
    argv = canonical[:vf_index]
    for caption in captions:
        argv += ["-loop", "1", "-framerate", ffmpeg_rational(policy["frame_rate"]), "-i", str(caption["png"])]
    filter_parts: list[str] = []
    current = "[0:v]"
    for index, caption in enumerate(captions, start=1):
        next_label = f"[caption{index}]"
        overlay = caption["overlay"]
        filter_parts.append(
            f"{current}[{index}:v]overlay={overlay['x']}:{overlay['y']}:"
            f"enable='{caption['enable']}':shortest=1{next_label}"
        )
        current = next_label
    filter_parts.append(f"{current}setsar={ffmpeg_rational(policy['sar'])}[vout]")
    # ⛔: build_normalize_argv appends "-map 0:v:0 -map 0:a:0" — but ONLY when
    # audio_policy == "present". Inheriting that tail verbatim put a second
    # "-map 0:v:0" alongside this pass's own "-map [vout]", so a captioned plan
    # WITH SOUND wrote TWO video streams: v:0 captioned, v:1 the raw copy. The
    # file was ~2x its size and ffmpeg's own default stream selection picked the
    # UNCAPTIONED v:1, so the captions looked absent depending on the reader.
    # Why it hid for so long: earlier probes had captions but no sound (audio_policy
    # "none" emits "-an" and no video map), or sound but zero captions.  The
    # combination "sound x captions" only appeared later.
    # The audio map must stay; only the duplicate video map is dropped.
    tail: list[str] = []
    index = vf_index + 2
    while index < len(canonical):
        if canonical[index] == "-map" and index + 1 < len(canonical) and canonical[index + 1] == "0:v:0":
            index += 2
            continue
        tail.append(canonical[index])
        index += 1
    return argv + ["-filter_complex", ";".join(filter_parts), "-map", "[vout]"] + tail


def _ceil_fraction(value: Fraction) -> int:
    """Return ceil(value) without sending contract time through float."""
    return -(-value.numerator // value.denominator)


def _fraction_decimal(value: Fraction, places: int = 12) -> str:
    """Render a non-negative rational for concat-demuxer ``duration`` records."""
    if value < 0:
        raise AssemblyError("internal error: cannot format a negative duration")
    scale = 10 ** places
    units = value.numerator * scale // value.denominator
    whole, remainder = divmod(units, scale)
    return f"{whole}.{remainder:0{places}d}"


CAPTION_TRACK_RECORD_KEYS = ("start", "end", "caption_id", "png", "overlay")
JOIN_AUDIO_CROSSFADE_EVIDENCE_KEYS = ("join_index", "duration_seconds", "applied")


def _caption_evidence_records(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Serialise caption records for evidence without losing the exact rationals.

    ⛔: the working record carries `start`/`end` as `Fraction` because the
    transparent track needs frame-accurate bounds. Evidence is JSON, so those two
    must go through the contract's rational spelling. shipped the track
    path without this and the real render finished every stage, then died writing
    evidence with "Object of type Fraction is not JSON serializable".
    """
    serialised: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        for key in ("start", "end"):
            value = item.get(key)
            if isinstance(value, Fraction):
                item[key] = _fraction_evidence(value)
        serialised.append(item)
    return serialised


def _join_audio_crossfade_evidence_records(
    joins: Sequence[Mapping[str, Any]], *, applied: bool,
) -> list[dict[str, Any]]:
    """Write only JSON primitives into the new, append-only crossfade evidence key."""
    return [
        {"join_index": int(item["join_index"]), "duration_seconds": float(item["duration"]), "applied": applied}
        for item in joins
    ]


def _caption_frame_bounds(start: Fraction, end: Fraction, frame_rate: Fraction) -> tuple[int, int]:
    """Output-grid frame bounds of one caption.

    The pre-render check and the track segments use the SAME predicate on purpose.
    When it lived in only one place, `--validate-only` passed 30,000 sub-frame
    captions and the rejection only arrived during the render itself.
    """
    return _ceil_fraction(start * frame_rate), _ceil_fraction(end * frame_rate)


def _caption_track_segments(
    captions: Sequence[Mapping[str, Any]], frame_rate: Fraction,
) -> list[dict[str, Any]]:
    """Make chronological opaque/gap segments for one non-overlapping caption track."""
    cursor = 0
    segments: list[dict[str, Any]] = []
    for record in captions:
        missing = [key for key in CAPTION_TRACK_RECORD_KEYS if key not in record]
        if missing:
            raise AssemblyError(
                "caption record is missing "
                + ", ".join(missing)
                + " required by the transparent caption track"
            )
    for caption in sorted(captions, key=lambda record: (record["start"], record["end"], record["caption_id"])):
        start_frame, end_frame = _caption_frame_bounds(caption["start"], caption["end"], frame_rate)
        if start_frame < cursor:
            raise AssemblyError("internal error: overlapping captions reached transparent-track composition")
        if start_frame > cursor:
            segments.append({"kind": "gap", "frames": start_frame - cursor})
        if end_frame <= start_frame:
            raise AssemblyError(
                f"caption {caption['caption_id']!r} has no displayable frame at output_policy.frame_rate"
            )
        segments.append({"kind": "caption", "frames": end_frame - start_frame, "caption": caption})
        cursor = end_frame
    return segments


def _caption_track_timescale(policy: Mapping[str, Any]) -> str:
    """Return a MOV time scale that exactly represents the policy timebase."""
    try:
        timebase = contract_fraction(policy.get("encoder_timebase"))
    except (RationalValueError, TypeError) as exc:
        raise AssemblyError(f"output_policy.encoder_timebase is invalid: {exc}") from exc
    if timebase <= 0 or timebase.denominator % timebase.numerator:
        raise AssemblyError(
            "transparent caption-track requires encoder_timebase with an integer reciprocal for MOV"
        )
    return str(timebase.denominator // timebase.numerator)


def _caption_track_piece_argv(
    output: Path, policy: Mapping[str, Any], frames: int, caption: Mapping[str, Any] | None = None,
) -> list[str]:
    """Render one independent, lossless yuva420p track piece (or transparent gap)."""
    if frames <= 0:
        raise AssemblyError("internal error: transparent-track piece must contain frames")
    width, height = policy["width"], policy["height"]
    frame_rate = ffmpeg_rational(policy["frame_rate"])
    sar = ffmpeg_rational(policy["sar"])
    argv = [
        ffmpeg_path(), "-y", "-hide_banner", "-nostdin",
        "-f", "lavfi", "-i", f"color=c=black@0.0:s={width}x{height}:r={frame_rate},format=yuva420p",
    ]
    if caption is None:
        argv += ["-vf", f"format=yuva420p,setsar={sar}"]
    else:
        overlay = caption["overlay"]
        argv += ["-loop", "1", "-framerate", frame_rate, "-i", str(caption["png"])]
        #: overlay's straight-alpha blend onto a fully transparent background is
        # non-deterministic in the chroma planes (Y and alpha are stable; U/V differ by up
        # to 4 on some caption images). The background contributes nothing at alpha 0, so
        # pad places the caption directly and skips the blend entirely. Geometry is already
        # bounded by _validate_caption_overlay, so pad can never overflow the canvas.
        argv += [
            "-filter_complex",
            f"[1:v]format=yuva420p,pad={width}:{height}:{overlay['x']}:{overlay['y']}:"
            f"color=black@0.0,setsar={sar}[vout]",
            "-map", "[vout]",
        ]
    # FFV1 is lossless and retains alpha in yuva420p. Every piece gets the
    # policy's geometry, CFR rate, and encoder timebase before concat-copy.
    return argv + [
        "-frames:v", str(frames), "-c:v", "ffv1", "-pix_fmt", "yuva420p",
        "-enc_time_base", ffmpeg_rational(policy["encoder_timebase"]), "-fps_mode", "passthrough",
        "-video_track_timescale", _caption_track_timescale(policy), "-an", str(output),
    ]


def _caption_track_concat_argv(list_path: Path, output: Path, policy: Mapping[str, Any]) -> list[str]:
    """Concat independently rendered yuva420p pieces without a filter chain."""
    return [
        ffmpeg_path(), "-y", "-hide_banner", "-nostdin", "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c", "copy", "-video_track_timescale", _caption_track_timescale(policy), "-an", str(output),
    ]


def _caption_track_overlay_argv(
    staged: Path, output: Path, policy: Mapping[str, Any], track: Path, overlays: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Overlay one finite caption track, then retain the existing mask z-order."""
    canonical = build_normalize_argv(staged, output, policy)
    vf_index = canonical.index("-vf")
    argv = canonical[:vf_index] + ["-i", str(track)]
    for overlay_record in overlays:
        argv += ["-loop", "1", "-framerate", ffmpeg_rational(policy["frame_rate"]), "-i", str(overlay_record["png"])]
    filter_parts = ["[0:v][1:v]overlay=0:0:eof_action=pass[caption_track]"]
    current = "[caption_track]"
    for index, overlay_record in enumerate(overlays, start=2):
        next_label = f"[overlay{index}]"
        overlay = overlay_record["overlay"]
        filter_parts.append(
            f"{current}[{index}:v]overlay={overlay['x']}:{overlay['y']}:"
            f"enable='{overlay_record['enable']}':shortest=1{next_label}"
        )
        current = next_label
    filter_parts.append(f"{current}setsar={ffmpeg_rational(policy['sar'])}[vout]")
    tail: list[str] = []
    index = vf_index + 2
    while index < len(canonical):
        if canonical[index] == "-map" and index + 1 < len(canonical) and canonical[index + 1] == "0:v:0":
            index += 2
            continue
        tail.append(canonical[index])
        index += 1
    return argv + ["-filter_complex", ";".join(filter_parts), "-map", "[vout]"] + tail


def _render_caption_track(
    captions: Sequence[Mapping[str, Any]], policy: Mapping[str, Any], work: Path,
) -> tuple[Path, list[dict[str, Any]]]:
    """Render independent caption/gap pieces and concat-copy them into one track."""
    try:
        frame_rate = contract_fraction(policy.get("frame_rate"))
    except (RationalValueError, TypeError) as exc:
        raise AssemblyError(f"output_policy.frame_rate is invalid: {exc}") from exc
    if frame_rate <= 0:
        raise AssemblyError("output_policy.frame_rate must be positive")
    track_work = work / "caption-track"
    track_work.mkdir(parents=True, exist_ok=True)
    pieces: list[tuple[Path, int]] = []
    commands: list[dict[str, Any]] = []
    for index, segment in enumerate(_caption_track_segments(captions, frame_rate)):
        piece = track_work / f"piece-{index:04d}.mov"
        command = run_command(_caption_track_piece_argv(piece, policy, segment["frames"], segment.get("caption")))
        commands.append(command)
        if command.get("returncode") != 0 or not piece.is_file():
            raise AssemblyError("transparent caption-track piece rendering failed")
        pieces.append((piece, segment["frames"]))
    concat_list = track_work / "pieces.txt"
    concat_list.write_text(
        "".join(
            f"file '{str(piece).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'\n"
            f"duration {_fraction_decimal(Fraction(frames, 1) / frame_rate)}\n"
            for piece, frames in pieces
        ),
        encoding="utf-8",
    )
    track = track_work / "caption-track.mov"
    concat_command = run_command(_caption_track_concat_argv(concat_list, track, policy))
    commands.append(concat_command)
    if concat_command.get("returncode") != 0 or not track.is_file():
        raise AssemblyError("transparent caption-track concat failed")
    return track, commands


def _source_dimensions(source: Path) -> tuple[int, int]:
    observation = probe_json([
        "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json",
    ], source)
    payload = observation.get("json")
    streams = payload.get("streams") if isinstance(payload, Mapping) else None
    stream = streams[0] if isinstance(streams, list) and streams else None
    if observation.get("returncode") != 0 or not isinstance(stream, Mapping):
        raise AssemblyError(f"cannot probe source dimensions for {source}: {observation.get('stderr')}")
    width, height = stream.get("width"), stream.get("height")
    if (isinstance(width, bool) or not isinstance(width, int) or width <= 0 or
            isinstance(height, bool) or not isinstance(height, int) or height <= 0):
        raise AssemblyError(f"ffprobe returned invalid source dimensions for {source}")
    return width, height


def _validate_crop_bounds(
    crop_reframe: Mapping[str, Any], source: Path, clip_id: Any, dimensions: tuple[int, int]
) -> None:
    width, height = dimensions
    x, y = crop_reframe["x"], crop_reframe["y"]
    crop_width, crop_height = crop_reframe["width"], crop_reframe["height"]
    if (x < 0 or y < 0 or crop_width < 1 or crop_height < 1 or
            x + crop_width > width or y + crop_height > height):
        raise AssemblyError(
            f"clip {clip_id!r} act_crop_reframe is outside source dimensions {width}x{height}"
        )


def _inject_input_cut(argv: list[str], seek: str, duration: str) -> list[str]:
    """Insert the input seek and clip length into canonical normalisation argv."""
    input_index = argv.index("-i")
    # Both options are input options. In particular, putting -t after -i
    # limits the *post-setpts output* duration and causes fps to refill a
    # speed-adjusted piece, rather than cutting the requested source interval.
    argv[input_index:input_index] = ["-ss", seek, "-t", duration]
    return argv


def _inject_speed_filter(argv: list[str], factor: Fraction) -> tuple[list[str], str]:
    """Place exact source-to-timeline PTS scaling before the canonical CFR filter."""
    vf_index = argv.index("-vf")
    filter_chain = argv[vf_index + 1]
    setpts = f"PTS*{factor.denominator}/{factor.numerator}"
    marker = ",fps="
    if marker not in filter_chain:
        raise AssemblyError("canonical normalisation filter has no fps stage for act_speed_change")
    argv[vf_index + 1] = filter_chain.replace(marker, f",setpts={setpts}{marker}", 1)
    return argv, setpts


def _zoom_expression(keyframes: Sequence[Mapping[str, Any]], timeline_start: Fraction,
                     timeline_end: Fraction, output_frame_count: int, field: str) -> str:
    """Compile declared linear keyframes without inventing a time or a value."""
    duration = timeline_end - timeline_start
    last_frame = output_frame_count - 1
    if last_frame <= 0:
        raise AssemblyError("act_zoom_keyframes requires a clip with at least two output frames")
    positions = [
        (keyframe["time"] - timeline_start) * last_frame / duration
        for keyframe in keyframes
    ]
    values = [keyframe[field] for keyframe in keyframes]
    def scalar(value: Any) -> str:
        return _fraction_text(value) if isinstance(value, Fraction) else str(value)
    pieces = []
    for index in range(len(keyframes) - 1):
        left_n, right_n = positions[index], positions[index + 1]
        left_value, right_value = values[index], values[index + 1]
        ramp = (
            f"{scalar(left_value)}+({scalar(right_value)}-{scalar(left_value)})*"
            f"(on-{_fraction_text(left_n)})/({_fraction_text(right_n)}-{_fraction_text(left_n)})"
        )
        pieces.append(ramp)
    expression = pieces[-1]
    for index in range(len(pieces) - 2, -1, -1):
        expression = f"if(lt(on,{_fraction_text(positions[index + 1])}),{pieces[index]},{expression})"
    return expression


def _inject_zoom_filter(
    argv: list[str], keyframes: Sequence[Mapping[str, Any]], timeline_start: Fraction,
    timeline_end: Fraction, output_frame_count: int, policy: Mapping[str, Any],
) -> tuple[list[str], str]:
    """Tail-place zoompan after canonical CFR, before setsar (the contract )."""
    vf_index = argv.index("-vf")
    filter_chain = argv[vf_index + 1]
    marker = ",setsar="
    if marker not in filter_chain or ",fps=" not in filter_chain:
        raise AssemblyError("canonical normalisation filter has no fps/setsar tail for act_zoom_keyframes")
    width, height = policy["width"], policy["height"]
    frame_rate = ffmpeg_rational(policy["frame_rate"])
    z = _zoom_expression(keyframes, timeline_start, timeline_end, output_frame_count, "zoom")
    cx = _zoom_expression(keyframes, timeline_start, timeline_end, output_frame_count, "center_x")
    cy = _zoom_expression(keyframes, timeline_start, timeline_end, output_frame_count, "center_y")
    # zoompan's x/y denote the zoomed crop's top-left, while the contract names
    # the output-canvas centre. Preserve the declared centre by subtracting
    # half the crop extent at the declared zoom; passing cx/cy through would
    # silently place the crop centre at cx + iw/(2*z), not at cx/cy.
    x = f"({cx})-iw/(2*({z}))"
    y = f"({cy})-ih/(2*({z}))"
    zoompan = f"zoompan=z='{z}':d=1:s={width}x{height}:fps={frame_rate}:x='{x}':y='{y}'"
    argv[vf_index + 1] = filter_chain.replace(marker, f",{zoompan}{marker}", 1)
    return argv, zoompan


def _inject_blur_region_filter(argv: list[str], blur: Mapping[str, Any]) -> tuple[list[str], str]:
    """Insert the contract's patch graph before the canonical fps/setsar tail."""
    vf_index = argv.index("-vf")
    filter_chain = argv[vf_index + 1]
    marker = ",fps="
    if marker not in filter_chain or ",setsar=" not in filter_chain:
        raise AssemblyError("canonical normalisation filter has no fps/setsar tail for act_blur_region")
    if blur["filter"] == "gaussian":
        # gblur's sigma option is an ffmpeg float, unlike boxblur's expression-valued radius.
        # This changes only the transport spelling, never the declared strength's unit.
        blur_filter = f"gblur=sigma={format(float(blur['strength']), '.17g')}"
    else:
        blur_filter = f"boxblur=luma_radius={blur['strength_ffmpeg']}:luma_power=1"
    patch_graph = (
        f"split[base][patch];[patch]crop={blur['width']}:{blur['height']}:{blur['x']}:{blur['y']},"
        f"{blur_filter}[patch];[base][patch]overlay={blur['x']}:{blur['y']}"
    )
    argv[vf_index + 1] = filter_chain.replace(marker, f",{patch_graph}{marker}", 1)
    return argv, patch_graph


def _inject_speed_audio_filter(argv: list[str], atempo: str) -> list[str]:
    """Add the one permitted speed-audio filter before the mapped source audio."""
    if "-af" in argv:
        raise AssemblyError("speed audio filter cannot replace an existing audio filter")
    map_index = argv.index("-map")
    argv[map_index:map_index] = ["-af", f"atempo={atempo}"]
    return argv


def _inject_leading_audio_gap_filter(argv: list[str], audio_input: str) -> list[str]:
    """Prepend silence filling to the sole mapped original audio's simple chain.

    Complex audio graphs need their input pad identified before any PTS-changing
    stage. They belong to the post-concat passes, not this piece normalizer;
    reject them here rather than attach a simple filter to the wrong stream.
    """
    error = "unsupported audio filter shape for audio starts after video"
    if audio_input not in ("0:a:0", "1:a:0") or "-an" in argv:
        raise AssemblyError(error)
    inputs = [index for index, token in enumerate(argv) if token == "-i"]
    if len(inputs) != (1 if audio_input == "0:a:0" else 2):
        raise AssemblyError(error)
    maps = [index for index, token in enumerate(argv) if token == "-map"]
    if (any(index + 1 >= len(argv) for index in maps)
            or [argv[index + 1] for index in maps] != ["0:v:0", audio_input]):
        raise AssemblyError(error)
    simple_options = {"-af", "-filter:a", "-filter:a:0"}
    audio_filters = []
    for index, token in enumerate(argv):
        if (token in ("-filter_complex", "-filter_complex_script", "-lavfi")
                or token.startswith("-filter_script:a")
                or (token.startswith("-filter:a") and token not in simple_options)):
            raise AssemblyError(error)
        if token in simple_options:
            audio_filters.append(index)
    if len(audio_filters) > 1:
        raise AssemblyError(error)
    prefix = "aresample=async=1:first_pts=0"
    if audio_filters:
        index = audio_filters[0]
        if (index <= inputs[-1] + 1 or index + 1 >= len(argv)
                or not argv[index + 1] or argv[index + 1].startswith("-")
                or any(marker in argv[index + 1] for marker in (";", "[", "]"))):
            raise AssemblyError(error)
        argv[index + 1] = prefix + "," + argv[index + 1]
    else:
        argv[maps[0]:maps[0]] = ["-af", prefix]
    return argv


def _inject_output_cut(argv: list[str], output_frame_count: int, frame_rate: Fraction) -> list[str]:
    """Trim the CFR output axis to the piece's already-calculated frame count."""
    output_duration = Fraction(output_frame_count, 1) / frame_rate
    vf_index = argv.index("-vf")
    # Post-fps trimming excludes a round=near endpoint frame outside [0, D).
    argv[vf_index + 1] += f",trim=end={_floor_decimal(output_duration)},setpts=PTS-STARTPTS"
    return argv


def _final_probe(output: Path) -> dict[str, Any]:
    stream = probe_json([
        "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
        "stream=nb_read_packets,sample_aspect_ratio,extradata_size,extradata_hash:format=duration",
        "-show_data_hash", "sha256", "-of", "json",
    ], output)
    packets = probe_json([
        "-v", "error", "-select_streams", "v:0", "-show_packets", "-show_entries", "packet=dts", "-of", "json",
    ], output)
    audio = probe_json([
        "-v", "error", "-select_streams", "a", "-show_entries",
        "stream=index,codec_name,duration:format=duration", "-of", "json",
    ], output)
    # Stream/container duration is not decoded programme length for AAC MP4.
    # Read every decoded audio frame and sum its nb_samples instead (the contract C).
    audio_frames = probe_json([
        "-v", "error", "-select_streams", "a:0", "-show_frames", "-show_entries",
        "frame=media_type,nb_samples", "-of", "json",
    ], output)
    values = []
    nb_read_packets = None
    if isinstance(stream.get("json"), Mapping):
        streams = stream["json"].get("streams")
        if isinstance(streams, list) and streams and isinstance(streams[0], Mapping):
            raw_packet_count = streams[0].get("nb_read_packets")
            if isinstance(raw_packet_count, int) and not isinstance(raw_packet_count, bool):
                nb_read_packets = raw_packet_count
            elif isinstance(raw_packet_count, str) and raw_packet_count.isdecimal():
                nb_read_packets = int(raw_packet_count)
    if isinstance(packets.get("json"), Mapping):
        raw_packets = packets["json"].get("packets")
        if isinstance(raw_packets, list):
            values = [int(packet["dts"]) for packet in raw_packets if isinstance(packet, Mapping) and packet.get("dts") is not None]
    intervals = [later - earlier for earlier, later in zip(values, values[1:])]
    audio_streams = []
    container_duration_s = None
    if isinstance(audio.get("json"), Mapping):
        raw_audio_streams = audio["json"].get("streams")
        if isinstance(raw_audio_streams, list):
            audio_streams = [stream for stream in raw_audio_streams if isinstance(stream, Mapping)]
            if len(audio_streams) == 1:
                raw_duration = audio_streams[0].get("duration")
                if raw_duration in (None, "N/A"):
                    raw_duration = audio["json"].get("format", {}).get("duration") if isinstance(audio["json"].get("format"), Mapping) else None
                try:
                    container_duration_s = float(raw_duration) if raw_duration is not None else None
                except (TypeError, ValueError):
                    container_duration_s = None
    decoded_sample_count = None
    if isinstance(audio_frames.get("json"), Mapping):
        raw_frames = audio_frames["json"].get("frames")
        if isinstance(raw_frames, list):
            samples: list[int] = []
            for frame in raw_frames:
                if not isinstance(frame, Mapping) or frame.get("media_type") != "audio":
                    samples = []
                    break
                raw_samples = frame.get("nb_samples")
                if isinstance(raw_samples, int) and not isinstance(raw_samples, bool) and raw_samples >= 0:
                    samples.append(raw_samples)
                elif isinstance(raw_samples, str) and raw_samples.isdecimal():
                    samples.append(int(raw_samples))
                else:
                    samples = []
                    break
            if samples:
                decoded_sample_count = sum(samples)
    return {"stream_format": stream, "packet_dts": packets,
            "packet_count": nb_read_packets if nb_read_packets is not None else len(values),
            "dts_intervals": intervals, "dts_even": bool(intervals) and len(set(intervals)) == 1,
            "audio_stream": audio, "audio_stream_count": len(audio_streams),
            "audio_decoded_frames": audio_frames, "audio_present": bool(audio_streams),
            "container_duration_s": container_duration_s,
            # Compatibility evidence only; final acceptance below never reads it.
            "audio_duration_s": container_duration_s,
            "decoded_sample_count": decoded_sample_count}


def _audio_final_length_check(final_output: Mapping[str, Any], expected_samples: int) -> dict[str, Any]:
    """Apply the contract C to decoded samples, retaining container time as evidence."""
    if isinstance(expected_samples, bool) or not isinstance(expected_samples, int) or expected_samples <= 0:
        raise AssemblyError("audio final check requires positive expected sample count")
    actual_samples = final_output.get("decoded_sample_count")
    tolerance_samples = 2 * AAC_AUDIO_PACKET_SAMPLES
    passed = (
        final_output.get("audio_stream_count") == 1
        and isinstance(actual_samples, int) and not isinstance(actual_samples, bool)
        and abs(actual_samples - expected_samples) <= tolerance_samples
    )
    return {
        "expected_samples": expected_samples,
        "actual_decoded_samples": actual_samples,
        "container_duration_s": final_output.get("container_duration_s"),
        "tolerance_samples": tolerance_samples,
        "stream_count": final_output.get("audio_stream_count"),
        "passed": passed,
    }


def assemble(
    plan_path: Path, index_path: Path, source_map_path: Path, output: Path, work: Path, asset_map_path: Path | None = None,
    started: float | None = None,
) -> dict[str, Any]:
    """Run the through vertical slice and retain process evidence."""
    stage_started = time.monotonic() if started is None else started
    plan = _load_json(plan_path, "plan")
    index = _load_json(index_path, "index")
    source_map = _load_json(source_map_path, "source map")
    evidence: dict[str, Any] = {"plan": str(plan_path), "index": str(index_path), "source_map": str(source_map_path),
                                "output": str(output), "pieces": [], "concat_check": None, "final_concat": None,
                                "single_piece_promotion": None,
                                "final_output": None, "crop": {"applied_pieces": 0, "total_pieces": 0},
                                "speed": {"applied_pieces": 0, "total_pieces": 0},
                                "blur_region": {"applied_pieces": 0, "total_pieces": 0},
                                "join_audio_crossfade": [], "audio_cleanup": [], "audio_beds": [],
                                "caption": {"pass": "not_requested", "captions": [], "overlay_assets": [],
                                            "staged_input": None, "command": None, "track_commands": [], "composition": None}}
    (
        crop_by_segment, _caption_actions, _overlay_actions, speed_by_segment, freeze_by_segment, blur_by_segment, zoom_by_segment,
        caption_plan_records, overlay_plan_records,
    ) = _validate_actions(plan)
    policy = plan.get("output_policy")
    if not isinstance(policy, Mapping):
        raise AssemblyError("plan.output_policy must be an object")
    _validate_speed_audio_policy(speed_by_segment, policy)
    _validate_blur_region_policy(blur_by_segment, policy)
    cleanup_records = _audio_cleanup_actions(plan, policy)
    audio_bed_plan = _audio_bed_actions(plan, policy)
    duck_records = _audio_duck_actions(
        plan, policy, {record["action_id"]: record for record in audio_bed_plan["beds"]},
    )
    evidence["audio_cleanup"] = _audio_cleanup_evidence_records(cleanup_records, applied=False)
    # Media-free validation deliberately has no join_time: the correct seam
    # only exists after every rendered piece has been ffprobe-measured.
    crossfade_joins = _crossfade_audio_joins(plan, policy)
    evidence["join_audio_crossfade"] = _join_audio_crossfade_evidence_records(
        crossfade_joins, applied=False,
    )
    try:
        frame_rate = contract_fraction(policy.get("frame_rate"))
    except (RationalValueError, TypeError) as exc:
        raise AssemblyError(f"output_policy.frame_rate is invalid: {exc}") from exc
    # ⛔⛔ [2026-09-16 · R5] Concat uses piece durations, not their
    # declared absolute endpoints. Therefore a duration-matched overlap
    # lengthens the result and a gap shortens it without either value changing.
    # Check the assembled concat order here—not in `_timeline_clips_in_concat_order`,
    # which also serves crossfade declaration validation and has its own contract.
    concat_clips = _timeline_clips_in_concat_order(plan)
    _validate_timeline_clip_tiling(concat_clips)
    sources = _source_records(index, source_map)
    sources, evidence["indexed_sources"] = _prepare_indexed_sources(sources, index, work)
    segments = plan.get("source_segments")
    clips = plan.get("timeline_clips")
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)):
        raise AssemblyError("plan.source_segments must be an array")
    if not isinstance(clips, Sequence) or isinstance(clips, (str, bytes)):
        raise AssemblyError("plan.timeline_clips must be an array")
    segment_by_id = {item.get("segment_id"): item for item in segments if isinstance(item, Mapping)}
    prepared: list[tuple[
        Fraction, Mapping[str, Any], Mapping[str, Any], Path, dict[str, Any], Mapping[str, Any] | None,
        dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None
    ]] = []
    index_by_source = {item.get("source_id"): item for item in index.get("sources", []) if isinstance(item, Mapping)}
    # `_timeline_clips_in_concat_order` is the sole ordering authority for both
    # join_index validation and piece rendering (including equal-start ties).
    for clip in concat_clips:
        if not isinstance(clip, Mapping):
            raise AssemblyError("timeline_clips must contain objects")
        segment = segment_by_id.get(clip.get("source_segment_id"))
        if not isinstance(segment, Mapping):
            raise AssemblyError(f"clip references unknown source_segment_id {clip.get('source_segment_id')!r}")
        source_id = segment.get("source_id")
        if source_id not in sources or source_id not in index_by_source:
            raise AssemblyError(f"segment references unresolved source_id {source_id!r}")
        timing = cut_timing(clip.get("source_range"), frame_rate)
        duration = _time_fraction(index_by_source[source_id].get("duration"), f"index source {source_id!r}.duration")
        if Fraction(timing["end_seconds"]["num"], timing["end_seconds"]["den"]) > duration:
            raise AssemblyError(f"clip {clip.get('clip_id')!r} source_range.end exceeds index source duration")
        timeline_start = _time_fraction(clip.get("timeline_range", {}).get("start"), "timeline_range.start")
        timeline_end = _time_fraction(clip.get("timeline_range", {}).get("end"), "timeline_range.end")
        source_start = Fraction(timing["start_seconds"]["num"], timing["start_seconds"]["den"])
        source_end = Fraction(timing["end_seconds"]["num"], timing["end_seconds"]["den"])
        matching_speed = [
            action for action in speed_by_segment.get(segment.get("segment_id"), [])
            if action["range_start"] < source_end and source_start < action["range_end"]
        ]
        if len(matching_speed) > 1:
            raise AssemblyError(f"multiple act_speed_change actions apply to clip {clip.get('clip_id')!r}")
        matching_freeze = freeze_by_segment.get(segment.get("segment_id"), [])
        if len(matching_freeze) > 1:
            raise AssemblyError(f"multiple act_freeze_frame actions apply to clip {clip.get('clip_id')!r}")
        if matching_speed and matching_freeze:
            raise AssemblyError(f"clip {clip.get('clip_id')!r} cannot combine act_speed_change and act_freeze_frame")
        speed: dict[str, Any] | None = None
        if matching_speed:
            action = matching_speed[0]
            if action["range_start"] != source_start or action["range_end"] != source_end:
                raise AssemblyError(
                    f"clip {clip.get('clip_id')!r} act_speed_change partial overlap unsupported: "
                    f"action range {_fraction_text(action['range_start'])}-{_fraction_text(action['range_end'])}, "
                    f"clip range {_fraction_text(source_start)}-{_fraction_text(source_end)}"
                )
            source_duration = source_end - source_start
            output_duration = source_duration / action["factor"]
            output_frames = output_duration * frame_rate
            if output_frames.denominator != 1:
                raise AssemblyError(
                    f"clip {clip.get('clip_id')!r} act_speed_change output_duration * frame_rate "
                    f"must be an integer; calculated {_fraction_text(output_frames)} frames"
                )
            timeline_duration = timeline_end - timeline_start
            if timeline_duration != output_duration:
                raise AssemblyError(
                    f"clip {clip.get('clip_id')!r} act_speed_change timeline duration mismatch: "
                    f"calculated output {_fraction_text(output_duration)} seconds, "
                    f"timeline {_fraction_text(timeline_duration)} seconds"
                )
            speed = {
                "action_id": action["action_id"], "factor": action["factor"],
                "factor_ffmpeg": action["factor_ffmpeg"], "audio_handling": action["audio_handling"],
                "source_duration": source_duration, "output_duration": output_duration,
                "output_frame_count": output_frames.numerator,
            }
        if matching_freeze:
            speed = _freeze_speed_for_clip(
                matching_freeze[0], clip.get("clip_id"), source_start, source_end,
                timeline_start, timeline_end, frame_rate,
            )
        if speed is None:
            # A clip with no speed change and no freeze must have equal source and
            # timeline spans.  Nothing checked this before: lengthening
            # `timeline_range.end` by a second produced byte-identical argv, because
            # the piece duration is taken from `source_range` alone.  The declaration
            # was silently ignored.
            # Speed (above) and freeze (`_freeze_speed_for_clip`) already make the same
            # check; only the ordinary clip was missing it.
            # Every existing plan already satisfies this rule, so tightening costs nothing.
            declared = timeline_end - timeline_start
            realized = source_end - source_start
            if declared != realized:
                raise AssemblyError(
                    f"clip {clip.get('clip_id')!r} timeline duration mismatch: "
                    f"source range {_fraction_text(realized)} seconds, "
                    f"timeline {_fraction_text(declared)} seconds "
                    "(no act_speed_change or act_freeze_frame declares the difference)"
                )
        matching_zoom = zoom_by_segment.get(segment.get("segment_id"), [])
        if len(matching_zoom) > 1:
            raise AssemblyError(f"multiple act_zoom_keyframes actions apply to clip {clip.get('clip_id')!r}")
        zoom = matching_zoom[0] if matching_zoom else None
        if zoom is not None and (zoom["keyframes"][0]["time"] != timeline_start
                                 or zoom["keyframes"][-1]["time"] != timeline_end):
            raise AssemblyError(
                f"clip {clip.get('clip_id')!r} act_zoom_keyframes first/last keyframe must equal clip timeline range"
            )
        matching_blur = blur_by_segment.get(segment.get("segment_id"), [])
        if len(matching_blur) > 1:
            raise AssemblyError(f"multiple act_blur_region actions apply to clip {clip.get('clip_id')!r}")
        blur = matching_blur[0] if matching_blur else None
        crop_reframe = crop_by_segment.get(segment.get("segment_id"))
        prepared.append((timeline_start, clip, segment, sources[source_id], timing, crop_reframe, speed, blur, zoom))
    # Source aliases and repeated pieces share one original timing observation.
    source_start_offsets = {
        source: _source_timing(source)
        for source in sorted({item[3].resolve() for item in prepared}, key=str)
    }
    # The leading-audio-gap probe is separate so the start-offset probe argv stays intact. Present-audio
    # originals get one additional observation shared by aliases and pieces.
    source_audio_start_gaps = {}
    if policy.get("audio_policy") == "present":
        source_audio_start_gaps = {
            source: _source_audio_start_gap(source)
            for source in sorted({item[3].resolve() for item in prepared}, key=str)
        }
        for _, clip, _, source, timing, _, speed, _, _ in prepared:
            gap = (source_audio_start_gaps[source.resolve()]
                   - Fraction(timing["start_index"], 1) / frame_rate)
            if gap > 0 and speed is not None:
                raise AssemblyError(
                    f"clip {clip.get('clip_id')!r} audio starts after video; "
                    "leading audio gap with speed change or freeze frame is unsupported"
                )
    source_dimensions: dict[Path, tuple[int, int]] = {}
    for _, clip, _, source, _, crop_reframe, _, _, _ in prepared:
        if crop_reframe is None:
            continue
        dimensions = source_dimensions.get(source)
        if dimensions is None:
            dimensions = _source_dimensions(source)
            source_dimensions[source] = dimensions
        _validate_crop_bounds(crop_reframe, source, clip.get("clip_id"), dimensions)
    evidence["crop"] = {
        "applied_pieces": sum(item[5] is not None for item in prepared),
        "total_pieces": len(prepared),
    }
    evidence["speed"] = {
        "applied_pieces": sum(item[6] is not None for item in prepared),
        "total_pieces": len(prepared),
    }
    evidence["blur_region"] = {
        "applied_pieces": sum(item[7] is not None for item in prepared),
        "total_pieces": len(prepared),
    }
    if policy.get("audio_policy") == "present":
        for source in sorted({item[3] for item in prepared}, key=str):
            try:
                require_present_audio_stream(source)
            except ValueError as exc:
                raise AssemblyError(str(exc)) from exc
    work.mkdir(parents=True, exist_ok=True)
    video_sources, evidence["normalized_sources"] = _prepare_normalized_sources(
        sources, {item[2]["source_id"] for item in prepared}, index_by_source, policy, work,
    )
    caption_records = _caption_records(caption_plan_records, asset_map_path, work)
    overlays = _overlay_asset_records(overlay_plan_records, asset_map_path)
    audio_bed_records = _audio_bed_asset_records(audio_bed_plan["beds"], asset_map_path)
    evidence["audio_beds"] = _audio_bed_evidence_records(audio_bed_records, applied=False, duck_records=duck_records)
    # Masks are composited after captions so an approved mask can never be
    # left uncovered by a caption drawn on top of it.
    captions = list(caption_records) + overlays
    evidence["caption"]["captions"] = _caption_evidence_records(captions)
    evidence["caption"]["overlay_assets"] = overlays
    pieces: list[Path] = []
    for order, (_, clip, segment, source, timing, crop_reframe, speed, blur, zoom) in enumerate(prepared):
        _print_stage(stage_started, f"normalize {order + 1}/{len(prepared)}")
        piece = work / f"piece-{order:03d}.mp4"
        expected_frame_count = speed["output_frame_count"] if speed is not None else timing["frame_count"]
        silence_audio_until = None
        resample_audio_atempo = None
        if (speed is not None and speed["audio_handling"] == "drop"
                and policy.get("audio_policy") == "present"):
            silence_audio_until = _floor_decimal(Fraction(expected_frame_count, 1) / frame_rate)
        elif speed is not None and speed["audio_handling"] == "resample":
            resample_audio_atempo = speed["factor_ffmpeg"]
        if segment["source_id"] in video_sources:
            argv = build_normalize_argv(
                video_sources[segment["source_id"]], piece, policy,
                actions=plan.get("actions"), crop_reframe=crop_reframe,
                silence_audio_until=silence_audio_until,
            )
        else:
            argv = build_normalize_argv(
                source, piece, policy, actions=plan.get("actions"), crop_reframe=crop_reframe,
                silence_audio_until=silence_audio_until,
            )
        offset = source_start_offsets[source.resolve()]
        original_seek = timing["seek_seconds"]
        if offset != 0:
            original_seek = _floor_decimal(
                Fraction(timing["start_index"], 1) / frame_rate + offset
            )
        video_seek = (timing["seek_seconds"] if segment["source_id"] in video_sources
                      else original_seek)
        argv = _inject_input_cut(argv, video_seek, timing["duration_seconds"])
        if segment["source_id"] in video_sources and policy.get("audio_policy") == "present":
            # The copy starts at the first video frame; original audio seeks
            # from the container origin and needs the video's start offset.
            second_input = argv.index("-i") + 2
            argv[second_input:second_input] = [
                "-ss", original_seek, "-t", timing["duration_seconds"], "-i", str(source),
            ]
            try:
                audio_map = argv.index("0:a:0")
            except ValueError as exc:
                raise AssemblyError("unsupported original audio map for normalized source copy") from exc
            argv[audio_map] = "1:a:0"
        if policy.get("audio_policy") == "present":
            gap = (source_audio_start_gaps[source.resolve()]
                   - Fraction(timing["start_index"], 1) / frame_rate)
            if gap > 0:
                audio_input = "1:a:0" if segment["source_id"] in video_sources else "0:a:0"
                argv = _inject_leading_audio_gap_filter(argv, audio_input)
        setpts = None
        if speed is not None:
            argv, setpts = _inject_speed_filter(argv, speed["factor"])
        blur_filter = None
        if blur is not None:
            argv, blur_filter = _inject_blur_region_filter(argv, blur)
        zoompan = None
        if zoom is not None:
            argv, zoompan = _inject_zoom_filter(
                argv, zoom["keyframes"],
                _time_fraction(clip["timeline_range"]["start"], "timeline_range.start"),
                _time_fraction(clip["timeline_range"]["end"], "timeline_range.end"),
                expected_frame_count, policy,
            )
        if resample_audio_atempo is not None:
            argv = _inject_speed_audio_filter(argv, resample_audio_atempo)
        argv = _inject_output_cut(argv, expected_frame_count, frame_rate)
        command = run_command(argv)
        speed_evidence = None
        if speed is not None:
            speed_evidence = {
                "action_id": speed["action_id"], "factor": _fraction_evidence(speed["factor"]),
                "source_duration_seconds": _fraction_evidence(speed["source_duration"]),
                "output_duration_seconds": _fraction_evidence(speed["output_duration"]),
                "output_frame_count": speed["output_frame_count"], "setpts": setpts,
                "audio_handling": speed["audio_handling"],
                "audio": "silenced" if silence_audio_until is not None else (
                    "resampled" if resample_audio_atempo is not None else "not_applicable"
                ),
                "audio_output_duration": silence_audio_until,
                "atempo": resample_audio_atempo,
                "freeze_frame": bool(speed.get("freeze_frame")),
            }
        record = {"order": order, "clip_id": clip.get("clip_id"), "source_segment_id": segment.get("segment_id"),
                  "source_id": segment.get("source_id"), "source_path": str(source), "piece": str(piece),
                  "timing": timing, "crop": dict(crop_reframe) if crop_reframe is not None else None,
                  "speed": speed_evidence,
                  "expected_frame_count": expected_frame_count, "actual_packet_count": None,
                  "command": command}
        if blur_filter is not None:
            record["blur_region"] = {"action_id": blur["action_id"], "filter": blur_filter}
        if zoompan is not None:
            record["zoom_keyframes"] = {"action_id": zoom["action_id"], "filter": zoompan}
        evidence["pieces"].append(record)
        if command["returncode"] != 0 or not piece.is_file():
            raise AssemblyError(f"normalisation failed for clip {clip.get('clip_id')!r}")
        actual_packet_count = _final_probe(piece)["packet_count"]
        record["actual_packet_count"] = actual_packet_count
        if actual_packet_count != expected_frame_count:
            error = AssemblyError(
                f"clip {clip.get('clip_id')!r} frame count mismatch: expected frame_count "
                f"{expected_frame_count}, actual packet count {actual_packet_count}"
            )
            # Preserve the measured rejected piece when invoked through main().
            error.evidence = evidence # type: ignore[attr-defined]
            raise error
        pieces.append(piece)
    planned_clip_durations = [
        Fraction(piece["expected_frame_count"], 1) / frame_rate for piece in evidence["pieces"]
    ]
    planned_program_samples = _planned_piece_sample_boundaries(planned_clip_durations)[-1]
    piece_audio_packets: list[int] = []
    if policy.get("audio_policy") == "present" and len(pieces) > 1:
        # the contract: the re-splice starts from rendered-piece packet observations,
        # never a duration estimate. It is needed even with no declared action.
        piece_audio_packets = [_piece_audio_packet_count(piece) for piece in pieces]
    if crossfade_joins:
        crossfade_joins = _crossfade_audio_joins(
            plan, policy, piece_audio_packets, planned_clip_durations,
        )
        crossfade_render_joins = _crossfade_render_joins(
            plan, crossfade_joins, sources, index_by_source, speed_by_segment, piece_audio_packets,
            planned_clip_durations,
        )
        evidence["join_audio_crossfade"] = _join_audio_crossfade_evidence_records(
            crossfade_joins, applied=False,
        )
    check_output = work / "concat-check.mp4"
    _print_stage(stage_started, "concat compatibility check started")
    concat_evidence = check_concat(pieces, policy, concat_output=check_output)
    evidence["concat_check"] = concat_evidence
    _print_stage(stage_started, "concat compatibility check completed")
    if concat_evidence.get("verdict") != "compatible":
        error = AssemblyError(f"concat compatibility verdict is {concat_evidence.get('verdict')!r}; final output was not assembled")
        error.evidence = evidence # type: ignore[attr-defined]
        raise error
    # A compatible multi-piece plan has a checker-created concat stage. A
    # compatible single-piece plan has no join, so its already-normalized piece
    # is the honest promotion input instead (and the checker intentionally has
    # no concat artifact for it).
    if len(pieces) == 1:
        promotion_input = pieces[0]
        evidence["single_piece_promotion"] = {
            "concat_performed": False,
            "input": {"path": str(promotion_input), "sha256": _sha256(promotion_input)},
            "reason": "single normalized piece promoted without concat",
        }
    else:
        # check_concat has already executed the required concat demuxer / -c copy
        # command into a staged path as part of its R3 evidence. Promotion occurs
        # only after its compatible verdict, so a rejected plan never creates --out.
        if not check_output.is_file():
            raise AssemblyError("concat checker reported compatible without its staged output")
        promotion_input = check_output
    if crossfade_joins:
        # the contract §8 D: retain concat audio outside each centered window and
        # replace each window with original-source handles of exactly the same d.
        # This is not `acrossfade`: that filter shortens audio by Σd.
        crossfade_staged = work / "join-audio-crossfade.mp4"
        crossfade_audio_params = _join_audio_crossfade_audio_params(
            plan, piece_audio_packets, planned_clip_durations,
        )
        crossfade_command = run_command(join_audio_crossfade_argv(
            promotion_input, crossfade_staged, crossfade_render_joins, crossfade_audio_params, cleanup_records,
            audio_bed_records, audio_bed_plan["mix"], duck_records,
            source_start_offsets=source_start_offsets,
        ))
        evidence["join_audio_crossfade"] = _join_audio_crossfade_evidence_records(
            crossfade_joins, applied=crossfade_command.get("returncode") == 0 and crossfade_staged.is_file(),
        )
        evidence["join_audio_crossfade_command"] = crossfade_command
        if crossfade_command.get("returncode") != 0 or not crossfade_staged.is_file():
            error = AssemblyError("act_crossfade_audio length-preserving post-concat pass failed")
            error.evidence = evidence # type: ignore[attr-defined]
            raise error
        evidence["audio_cleanup"] = _audio_cleanup_evidence_records(
            cleanup_records, applied=bool(cleanup_records),
        )
        evidence["audio_beds"] = _audio_bed_evidence_records(
            audio_bed_records, applied=bool(audio_bed_records), duck_records=duck_records,
        )
        promotion_input = crossfade_staged
    elif (policy.get("audio_policy") == "present" and len(pieces) > 1) or cleanup_records or audio_bed_records or duck_records:
        cleanup_staged = work / "audio-cleanup.mp4"
        cleanup_command = run_command(audio_cleanup_argv(
            promotion_input, cleanup_staged, cleanup_records, audio_bed_records, audio_bed_plan["mix"], duck_records,
            program_samples=planned_program_samples,
            clip_durations=planned_clip_durations if len(pieces) > 1 else None,
            piece_audio_packets=piece_audio_packets if len(pieces) > 1 else None,
        ))
        evidence["audio_cleanup"] = _audio_cleanup_evidence_records(
            cleanup_records, applied=cleanup_command.get("returncode") == 0 and cleanup_staged.is_file(),
        )
        evidence["audio_cleanup_command"] = cleanup_command
        evidence["audio_beds"] = _audio_bed_evidence_records(
            audio_bed_records, applied=cleanup_command.get("returncode") == 0 and cleanup_staged.is_file(),
            duck_records=duck_records,
        )
        if cleanup_command.get("returncode") != 0 or not cleanup_staged.is_file():
            error = AssemblyError("Post-concat audio processing failed.")
            error.evidence = evidence # type: ignore[attr-defined]
            raise error
        promotion_input = cleanup_staged
    evidence["caption"]["staged_input"] = {"path": str(promotion_input), "sha256": _sha256(promotion_input)}
    _print_stage(stage_started, f"caption/overlay composition started (masks {len(overlays)})")
    if captions:
        #: the overlay pass writes to a staged path, then promotes. Writing
        # straight to --out broke the guarantee stated eight lines above -- a caption
        # plan whose overlay pass died mid-encode left a truncated file at --out that
        # ffprobe still reads as a playable duration (measured it).
        caption_staged = work / "caption-overlay.mp4"
        if len(caption_records) < CAPTION_TRACK_THRESHOLD:
            # This is intentionally the pre- argv and includes any masks.
            overlay_command = run_command(_caption_overlay_argv(promotion_input, caption_staged, policy, captions))
            # ⛔: keep the contract value "applied" —— (and any caller outside the
            # five gates) asserts on it. The new information goes in a NEW field instead.
            # shipped "applied_legacy" here and broke test 30 silently.
            evidence["caption"]["pass"] = "applied"
            evidence["caption"]["composition"] = "legacy_overlay_chain"
            evidence["caption"]["command"] = overlay_command
            if overlay_command.get("returncode") != 0 or not caption_staged.is_file():
                raise AssemblyError("caption overlay pass failed")
            os.replace(caption_staged, output)
        else:
            track_work = work / "caption-track"
            try:
                track, track_commands = _render_caption_track(caption_records, policy, work)
                overlay_command = run_command(
                    _caption_track_overlay_argv(promotion_input, caption_staged, policy, track, overlays)
                )
                evidence["caption"]["pass"] = "applied"
                evidence["caption"]["composition"] = "transparent_track"
                evidence["caption"]["track_commands"] = track_commands
                evidence["caption"]["command"] = overlay_command
                if overlay_command.get("returncode") != 0 or not caption_staged.is_file():
                    raise AssemblyError("transparent caption-track overlay pass failed")
                os.replace(caption_staged, output)
            finally:
                # 1080p transparent tracks can exceed 0.8 GB. This directory is
                # private to this pass; retain neither its pieces nor its concat track.
                shutil.rmtree(track_work, ignore_errors=True)
    else:
        #: no caption action means the approved promotion input is promoted byte-for-byte.
        os.replace(promotion_input, output)
    # ⛔ Both promotions are os.replace, so --work and --out must sit on one
    # filesystem; rename(2) returns EXDEV across them. That was already true of the
    # no-caption branch before -- it is written down here, not newly imposed.
    _print_stage(stage_started, f"caption/overlay composition completed (masks {len(overlays)})")
    evidence["final_concat"] = concat_evidence.get("concat")
    _print_stage(stage_started, "final probe started")
    evidence["final_output"] = _final_probe(output)
    if policy.get("audio_policy") == "present":
        # AAC packet duration is quantised to 1024 samples (about 21.34 ms at
        # the fixed 48 kHz rate). Two packets is a conservative mux/priming
        # allowance, while still rejecting a missing or materially truncated
        # audio track.
        # Do NOT subtract the crossfade length from this value.  A crossfade replaces
        # a window of the same length, so the expected duration stays the sum of the
        # pieces.  An `acrossfade`-style subtraction desynchronises A/V against the
        # video `-c:v copy` path and hides the defect from the evidence.
        expected_audio_samples = planned_program_samples
        evidence["audio_final_check"] = _audio_final_length_check(
            evidence["final_output"], expected_audio_samples,
        )
        audio_ok = evidence["audio_final_check"]["passed"]
        if not audio_ok:
            speed_piece_orders = [
                str(piece["order"]) for piece in evidence["pieces"]
                if isinstance(piece, Mapping) and piece.get("speed") is not None
            ]
            speed_detail = (
                f"; speed-applied piece order(s): {', '.join(speed_piece_orders)}"
                if speed_piece_orders else ""
            )
            error = AssemblyError(
                "audio_policy=present final output must contain one audio stream "
                f"with duration matching the assembled pieces{speed_detail}"
            )
            error.evidence = evidence # type: ignore[attr-defined]
            raise error
    _print_stage(stage_started, "final probe completed")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path, help="EditPlan JSON to render or validate.")
    parser.add_argument("--index", type=Path, help="Paired Observation Index JSON; required for rendering.")
    parser.add_argument("--source-map", type=Path, help="JSON object mapping source IDs to media file paths; required for rendering.")
    parser.add_argument("--asset-map", type=Path, help="JSON object mapping asset IDs to file paths; required for actions that reference assets.")
    parser.add_argument("--out", type=Path, help="Output MP4 path; required for rendering.")
    parser.add_argument("--work", type=Path, help="Directory for intermediate media and execution evidence; required for rendering.")
    parser.add_argument("--estimate-only", action="store_true",
                        help="print the estimate to stdout and exit without reading ffmpeg or input media")
    parser.add_argument("--validate-only", action="store_true",
                        help="validate plan-internal values only and exit without media or an asset map")
    arguments = parser.parse_args()
    if arguments.validate_only and arguments.estimate_only:
        parser.error("--validate-only and --estimate-only cannot be used together")
    if arguments.validate_only:
        errors: list[str] = []
        stages_not_reached: list[str] = []
        try:
            plan = _load_json(arguments.plan, "plan")
            (
                crop_by_segment, _caption_actions, _overlay_actions, _speed_by_segment, _freeze_by_segment, _blur_by_segment, _zoom_by_segment,
                caption_plan_records, overlay_plan_records,
            ) = _validate_actions(plan, errors)
            actions = plan.get("actions")
            if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
                stages_not_reached = ["captions", "overlays", "normalizer"]
            else:
                # _validate_actions has already run both plan-record passes when actions is
                # available. The normalizer, however, only accepts records produced before
                # any plan-only rejection.
                policy = plan.get("output_policy")
                if not isinstance(policy, Mapping):
                    errors.append("plan.output_policy must be an object")
                else:
                    try:
                        _validate_speed_audio_policy(_speed_by_segment, policy)
                        _validate_blur_region_policy(_blur_by_segment, policy)
                        _crossfade_audio_joins(plan, policy)
                        _audio_cleanup_actions(plan, policy)
                        audio_bed_plan = _audio_bed_actions(plan, policy)
                        _audio_duck_actions(
                            plan, policy, {record["action_id"]: record for record in audio_bed_plan["beds"]},
                        )
                    except AssemblyError as exc:
                        errors.append(str(exc))
                if not errors:
                    try:
                        _require_policy(policy)
                    except ValueError as exc:
                        errors.append(str(exc))
                if not errors:
                    for crop_reframe in crop_by_segment.values():
                        try:
                            build_normalize_argv(
                                _VALIDATE_ONLY_SOURCE_PATH, _VALIDATE_ONLY_OUTPUT_PATH, policy,
                                actions=actions, crop_reframe=crop_reframe,
                            )
                        except (AssemblyError, ValueError) as exc:
                            errors.append(str(exc))
                    try:
                        build_normalize_argv(
                            _VALIDATE_ONLY_SOURCE_PATH, _VALIDATE_ONLY_OUTPUT_PATH, policy,
                            actions=actions, crop_reframe=None,
                        )
                    except (AssemblyError, ValueError) as exc:
                        errors.append(str(exc))
                if not errors:
                    _validate_timeline_clip_tiling(_timeline_clips_in_concat_order(plan))
                if errors and not stages_not_reached:
                    stages_not_reached = ["normalizer"]
        except (AssemblyError, ValueError) as exc:
            errors.append(str(exc))
            stages_not_reached = ["actions", "captions", "overlays", "normalizer"]
        if errors:
            print(json.dumps({
                "status": "rejected", "error": errors[0], "errors": errors,
                "stages_not_reached": stages_not_reached,
            }, ensure_ascii=False))
            return 1
        print(json.dumps({
            "status": "validated",
            "checked": {
                "l2_dispatch_names": len(L2_DISPATCH_ACTIONS),
                "actions": len(plan["actions"]),
                "crop_reframe_variants": len(crop_by_segment) + 1,
                "caption_plan_records": len(caption_plan_records),
                "overlay_plan_records": len(overlay_plan_records),
                "audio_bed_plan_records": len(_audio_bed_actions(plan, policy)["beds"]),
            },
            "not_checked": [
                "asset map contents (keys and paths)", "asset file existence and SHA-256",
                "caption PNG renderer", "ffmpeg execution",
            ],
            "notice": VALIDATE_ONLY_NOTICE,
        }, ensure_ascii=False))
        return 0
    try:
        estimate_plan = _load_json(arguments.plan, "plan")
        estimate_inputs = _estimate_inputs_from_plan(estimate_plan)
        estimate_breakdown = _estimate_assembly_breakdown(estimate_plan, estimate_inputs)
        estimated_seconds = float(estimate_breakdown["estimated_seconds"])
    except AssemblyError as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(_format_estimate_line(estimate_inputs, estimated_seconds))
    for line in _format_estimate_breakdown_lines(estimate_breakdown):
        print(line)
    if arguments.estimate_only:
        return 0
    missing = [name for name, value in (("--index", arguments.index), ("--source-map", arguments.source_map),
                                        ("--out", arguments.out), ("--work", arguments.work)) if value is None]
    if missing:
        parser.error(f"{' '.join(missing)} required unless --estimate-only is used")
    assert arguments.index is not None and arguments.source_map is not None
    assert arguments.out is not None and arguments.work is not None
    evidence_path = arguments.work / "assemble-evidence.json"
    started = time.monotonic()
    try:
        evidence = assemble(
            arguments.plan, arguments.index, arguments.source_map, arguments.out, arguments.work, arguments.asset_map,
            started=started,
        )
        actual_seconds = time.monotonic() - started
        evidence["timing"] = {
            "estimated_seconds": estimated_seconds,
            "actual_seconds": actual_seconds,
            "model_version": ESTIMATE_MODEL_VERSION,
        }
        evidence["estimate_breakdown"] = estimate_breakdown
        evidence["status"] = "assembled"
        exit_code = 0
    except AssemblyError as exc:
        actual_seconds = time.monotonic() - started
        partial_evidence = getattr(exc, "evidence", None)
        evidence = dict(partial_evidence) if isinstance(partial_evidence, Mapping) else {
            "plan": str(arguments.plan), "output": str(arguments.out)
        }
        evidence["timing"] = {
            "estimated_seconds": estimated_seconds,
            "actual_seconds": actual_seconds,
            "model_version": ESTIMATE_MODEL_VERSION,
        }
        evidence["estimate_breakdown"] = estimate_breakdown
        evidence.update({"status": "rejected", "error": str(exc)})
        exit_code = 1
    arguments.work.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(_format_actual_line(estimate_inputs, estimated_seconds, actual_seconds))
    print(json.dumps({"status": evidence["status"], "evidence": str(evidence_path),
                      **({"error": evidence["error"]} if "error" in evidence else {})}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
