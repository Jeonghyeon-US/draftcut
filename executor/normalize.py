#!/usr/bin/env python3
"""Build and run Draftcut's contract-driven H.264 normalisation command."""

from __future__ import annotations

import argparse
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .policy import contract_fraction, ffmpeg_rational
    from .probe import command_string, ffmpeg_path, probe_json, run_command
except ImportError:  # Direct execution from executor/.
    from policy import contract_fraction, ffmpeg_rational
    from probe import command_string, ffmpeg_path, probe_json, run_command


REQUIRED_POLICY_FIELDS = (
    "width", "height", "pixel_format", "sar", "frame_rate", "frame_rate_mode",
    "encoder_timebase", "color_primaries", "color_transfer", "color_matrix",
    "color_range", "audio_policy", "time_axis",
)
ENCODER_CODEC = "libx264"
ENCODER_PRESET = "medium"
ENCODER_CRF = "18"

# The concat demuxer copies the first audio stream description to the joined
# MP4.  AAC-LC at 48 kHz stereo / 192 kbit/s is explicitly encoded for every
# piece so codec, rate, channel count, layout, and bitrate cannot vary by
# source.  48 kHz stereo AAC is broadly supported by MP4 players.
AUDIO_CODEC = "aac"
AUDIO_SAMPLE_RATE = "48000"
AUDIO_CHANNELS = "2"
AUDIO_CHANNEL_LAYOUT = "stereo"
AUDIO_BITRATE = "192k"


def require_present_audio_stream(source_path: str | Path) -> None:
    """Fail clearly before a ``present`` render tries to map absent audio."""
    observation = probe_json([
        "-v", "error", "-select_streams", "a", "-show_entries",
        "stream=index,codec_type", "-of", "json",
    ], Path(source_path))
    payload = observation.get("json")
    streams = payload.get("streams") if isinstance(payload, Mapping) else None
    if not observation.get("returncode") == 0 or not isinstance(streams, list):
        raise ValueError(
            f"audio_policy=present could not inspect input audio stream: {source_path}"
        )
    audio_streams = [
        stream for stream in streams
        if isinstance(stream, Mapping) and stream.get("codec_type") == "audio"
    ]
    if not audio_streams:
        raise ValueError(
            f"audio_policy=present requires an input audio stream: {source_path}"
        )


def is_vfr_source(source_path: str | Path) -> bool:
    """Detect VFR from the first video's rate headers or packet PTS spacing."""
    from fractions import Fraction

    observation = probe_json([
        "-v", "error", "-select_streams", "v:0", "-show_packets", "-show_streams",
        "-show_entries", "stream=r_frame_rate,avg_frame_rate:packet=pts", "-of", "json",
    ], Path(source_path))
    payload = observation.get("json")
    streams = payload.get("streams") if isinstance(payload, Mapping) else None
    if observation.get("returncode") != 0 or not isinstance(streams, list) or not streams:
        raise ValueError(f"could not inspect first video stream: {source_path}")
    stream = streams[0]
    if not isinstance(stream, Mapping) or not isinstance(stream.get("r_frame_rate"), str) or not isinstance(stream.get("avg_frame_rate"), str):
        raise ValueError(f"first video stream has no frame-rate headers: {source_path}")
    header_vfr = stream["r_frame_rate"] != stream["avg_frame_rate"]
    packets = payload.get("packets")
    if not isinstance(packets, list):
        return header_vfr
    pts = sorted(int(packet["pts"]) for packet in packets
                 if isinstance(packet, Mapping) and isinstance(packet.get("pts"), (int, str))
                 and str(packet["pts"]).lstrip("-").isdigit())
    gaps = [right - left for left, right in zip(pts, pts[1:])]
    if not gaps:
        return header_vfr
    mean = Fraction(sum(gaps), len(gaps))
    tolerance = max(Fraction(1), mean / 100)
    return header_vfr or any(abs(gap - mean) > tolerance for gap in gaps)


def _on_grid(pts: Sequence[int], time_base: Fraction, fps: Fraction) -> bool:
    """Test presentation-order PTS against the plan grid with strict tick rounding."""
    if len(pts) < 2:
        return True
    step = 1 / (fps * time_base)
    return all(abs((pt - pts[0]) - i * step) < Fraction(1, 2)
               for i, pt in enumerate(pts))


def needs_normalized_copy(
    source_path: str | Path, frame_rate: Mapping[str, Any] | Fraction,
) -> bool:
    """Require a copy for rate-header VFR or PTS outside the planned fps grid."""
    try:
        fps = frame_rate if isinstance(frame_rate, Fraction) else contract_fraction(frame_rate)
        if fps <= 0:
            raise ValueError("frame rate must be positive")
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"invalid planned frame rate: {frame_rate!r}") from exc
    observation = probe_json([
        "-v", "error", "-select_streams", "v:0", "-show_packets", "-show_streams",
        "-show_entries", "stream=r_frame_rate,avg_frame_rate,time_base:packet=pts", "-of", "json",
    ], Path(source_path))
    payload = observation.get("json")
    streams = payload.get("streams") if isinstance(payload, Mapping) else None
    if observation.get("returncode") != 0 or not isinstance(streams, list) or not streams:
        raise ValueError(f"could not inspect first video stream: {source_path}")
    stream = streams[0]
    if not isinstance(stream, Mapping) or not isinstance(stream.get("r_frame_rate"), str) or not isinstance(stream.get("avg_frame_rate"), str):
        raise ValueError(f"first video stream has no frame-rate headers: {source_path}")
    header_vfr = stream["r_frame_rate"] != stream["avg_frame_rate"]
    try:
        value = stream.get("time_base")
        if not isinstance(value, str):
            raise ValueError("time base must be a rational string")
        time_base = Fraction(value)
        if time_base <= 0:
            raise ValueError("time base must be positive")
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"first video stream has no valid time base: {source_path}") from exc
    packets = payload.get("packets")
    if not isinstance(packets, list):
        return header_vfr
    pts = sorted(int(packet["pts"]) for packet in packets
                 if isinstance(packet, Mapping) and isinstance(packet.get("pts"), (int, str))
                 and str(packet["pts"]).lstrip("-").isdigit())
    return header_vfr or not _on_grid(pts, time_base, fps)


def build_normalized_source_argv(
    source_path: str | Path, output_path: str | Path, output_policy: Mapping[str, Any]
) -> list[str]:
    """Make a whole-file video-only CFR copy with the canonical fps string."""
    frame_rate = ffmpeg_rational(output_policy["frame_rate"])
    return [
        ffmpeg_path(), "-i", str(source_path), "-map", "0:v:0", "-vf",
        f"setpts=PTS-STARTPTS,fps={frame_rate}:round=near",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-an", str(output_path),
    ]


def _require_policy(output_policy: Mapping[str, Any]) -> None:
    missing = [field for field in REQUIRED_POLICY_FIELDS if field not in output_policy]
    if missing:
        raise ValueError(f"output_policy is missing required fields: {', '.join(missing)}")
    if output_policy["frame_rate_mode"] != "cfr":
        raise ValueError("only output_policy.frame_rate_mode=cfr is executable")
    if output_policy["time_axis"] != "final_timeline":
        raise ValueError("only output_policy.time_axis=final_timeline is executable")
    if output_policy["audio_policy"] not in ("none", "present"):
        raise ValueError("unsupported output_policy.audio_policy")
    if not isinstance(output_policy["width"], int) or output_policy["width"] <= 0:
        raise ValueError("output_policy.width must be a positive integer")
    if not isinstance(output_policy["height"], int) or output_policy["height"] <= 0:
        raise ValueError("output_policy.height must be a positive integer")


def _validate_geometry_actions(
    output_policy: Mapping[str, Any], actions: Sequence[Mapping[str, Any]] | None
) -> None:
    """Reject geometry action values this normalisation slice cannot honour silently."""
    if not actions:
        return
    for action in actions:
        if action.get("action") != "act_scale_position":
            continue
        params = action.get("params")
        if not isinstance(params, Mapping):
            raise ValueError("act_scale_position requires params")
        if params.get("fit") != "contain":
            raise ValueError("only act_scale_position.params.fit=contain is executable")
        if params.get("width") != output_policy["width"] or params.get("height") != output_policy["height"]:
            raise ValueError("scale canvas must equal output_policy width and height")


def _validate_crop_reframe(crop_reframe: Mapping[str, Any]) -> None:
    """Require the contract crop rectangle before rendering it into a filter."""
    required = {"x", "y", "width", "height", "coordinate_origin"}
    if set(crop_reframe) != required:
        raise ValueError("crop_reframe must contain exactly x, y, width, height, and coordinate_origin")
    for field in ("x", "y", "width", "height"):
        value = crop_reframe[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"crop_reframe.{field} must be an integer")
    if crop_reframe["coordinate_origin"] != "top_left":
        raise ValueError("crop_reframe.coordinate_origin must be top_left")


def build_normalize_argv(
    source_path: str | Path,
    output_path: str | Path,
    output_policy: Mapping[str, Any],
    *,
    actions: Sequence[Mapping[str, Any]] | None = None,
    crop_reframe: Mapping[str, Any] | None = None,
    silence_audio_until: str | None = None,
) -> list[str]:
    """Construct the canonical argv from policy values and fixed encoder settings."""
    _require_policy(output_policy)
    _validate_geometry_actions(output_policy, actions)
    if crop_reframe is not None:
        _validate_crop_reframe(crop_reframe)
    width = output_policy["width"]
    height = output_policy["height"]
    frame_rate = ffmpeg_rational(output_policy["frame_rate"])
    encoder_timebase = ffmpeg_rational(output_policy["encoder_timebase"])
    sar = ffmpeg_rational(output_policy["sar"])
    contract_crop = ""
    if crop_reframe is not None:
        contract_crop = (
            f"crop={crop_reframe['width']}:{crop_reframe['height']}:"
            f"{crop_reframe['x']}:{crop_reframe['y']},"
        )
    filter_chain = contract_crop + (
        "crop=trunc(iw/2)*2:trunc(ih/2)*2,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        "setpts=PTS-STARTPTS,"
        f"fps={frame_rate}:round=near,"
        f"setsar={sar}"
    )
    x264_parameters = ":".join((
        f"colorprim={output_policy['color_primaries']}",
        f"transfer={output_policy['color_transfer']}",
        f"colormatrix={output_policy['color_matrix']}",
    ))
    argv = [
        ffmpeg_path(), "-y", "-hide_banner", "-nostdin", "-i", str(source_path),
        "-vf", filter_chain,
        "-c:v", ENCODER_CODEC, "-preset", ENCODER_PRESET, "-crf", ENCODER_CRF,
        "-pix_fmt", str(output_policy["pixel_format"]),
        "-enc_time_base", encoder_timebase,
        "-fps_mode", "passthrough",
        "-color_primaries", str(output_policy["color_primaries"]),
        "-color_trc", str(output_policy["color_transfer"]),
        "-colorspace", str(output_policy["color_matrix"]),
        "-color_range", str(output_policy["color_range"]),
        "-x264-params", x264_parameters,
    ]
    if output_policy["audio_policy"] == "none":
        argv.append("-an")
    else:
        if silence_audio_until is not None:
            argv.extend([
                "-af",
                f"volume=0,apad,atrim=end={silence_audio_until},asetpts=N/SR/TB",
            ])
        argv.extend([
            "-map", "0:v:0", "-map", "0:a:0",
            "-c:a", AUDIO_CODEC,
            "-ar", AUDIO_SAMPLE_RATE,
            "-ac", AUDIO_CHANNELS,
            "-channel_layout", AUDIO_CHANNEL_LAYOUT,
            "-b:a", AUDIO_BITRATE,
        ])
    argv.append(str(output_path))
    return argv


def build_normalize_command(
    source_path: str | Path,
    output_path: str | Path,
    output_policy: Mapping[str, Any],
    *,
    actions: Sequence[Mapping[str, Any]] | None = None,
    crop_reframe: Mapping[str, Any] | None = None,
) -> str:
    """Return the independently reproducible command string without executing it."""
    return command_string(build_normalize_argv(
        source_path, output_path, output_policy, actions=actions, crop_reframe=crop_reframe
    ))


def normalize_file(
    source_path: str | Path,
    output_path: str | Path,
    output_policy: Mapping[str, Any],
    *,
    actions: Sequence[Mapping[str, Any]] | None = None,
    crop_reframe: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the canonical command and return its complete process evidence."""
    if output_policy.get("audio_policy") == "present":
        require_present_audio_stream(source_path)
    result = run_command(build_normalize_argv(
        source_path, output_path, output_policy, actions=actions, crop_reframe=crop_reframe
    ))
    result["fixed_encoder_settings"] = {
        "codec": ENCODER_CODEC,
        "preset": ENCODER_PRESET,
        "crf": ENCODER_CRF,
    }
    return result


def _load_plan(path: Path) -> tuple[Mapping[str, Any], Sequence[Mapping[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "output_policy" in payload:
        return payload["output_policy"], payload.get("actions", [])
    return payload, []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path, help="EditPlan or output_policy JSON")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--print-only", action="store_true", help="Print the ffmpeg command without executing it.")
    arguments = parser.parse_args()
    policy, actions = _load_plan(arguments.plan)
    argv = build_normalize_argv(arguments.input, arguments.output, policy, actions=actions)
    if arguments.print_only:
        print(command_string(argv))
        return 0
    if policy.get("audio_policy") == "present":
        require_present_audio_stream(arguments.input)
    result = run_command(argv)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["returncode"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
