#!/usr/bin/env python3
"""Evaluate concat-copy pieces against compatibility specification version 2."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .policy import RationalValueError, contract_fraction, probe_fraction
    from .probe import ffmpeg_path, probe_json, run_command, tool_versions
except ImportError:  # Direct execution from executor/.
    from policy import RationalValueError, contract_fraction, probe_fraction
    from probe import ffmpeg_path, probe_json, run_command, tool_versions


SPEC_ID = "draftcut-concat-copy-compatibility"
SPEC_VERSION = "2"
CONDITION_IDS = (
    "P1", "P2", "P3", "P4", "P5", "P6",
    "X1", "X2", "X3", "X4", "X5", "X6", "X7", "X8",
    "R1", "R2", "R3",
)
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_UNEVALUABLE = "unevaluable"
EXCLUDED_X8_TYPE = "H.26[45] User Data Unregistered SEI message"


OBSERVATIONS: dict[str, list[str]] = {
    "P1": ["-v", "error", "-show_optional_fields", "always", "-select_streams", "v:0",
           "-show_entries", "stream=width,height,coded_width,coded_height", "-of", "json"],
    "P2": ["-v", "error", "-show_optional_fields", "always", "-select_streams", "v:0",
           "-show_entries", "stream=pix_fmt,bits_per_raw_sample", "-of", "json"],
    "P3": ["-v", "error", "-show_optional_fields", "always", "-select_streams", "v:0",
           "-show_entries", "stream=sample_aspect_ratio,display_aspect_ratio,width,height", "-of", "json"],
    "P4": ["-v", "error", "-show_optional_fields", "always", "-select_streams", "v:0",
           "-show_entries", "stream=color_primaries,color_transfer,color_space,color_range", "-of", "json"],
    "P5-rate": ["-v", "error", "-select_streams", "v:0", "-show_entries",
                "stream=r_frame_rate,avg_frame_rate", "-of", "json"],
    "P5/R1/R2": ["-v", "error", "-select_streams", "v:0", "-show_packets",
                 "-show_entries", "packet=pts,dts,flags", "-of", "json"],
    "P6": ["-v", "error", "-select_streams", "v:0", "-show_entries", "stream=time_base", "-of", "json"],
    "X1": ["-v", "error", "-show_entries", "stream=index,codec_type", "-of", "json"],
    "X2": ["-v", "error", "-select_streams", "v:0", "-show_entries",
           "stream=codec_name,codec_tag_string", "-of", "json"],
    "X3": ["-v", "error", "-select_streams", "v:0", "-show_entries",
           "stream=extradata_size,extradata_hash", "-show_data_hash", "sha256", "-of", "json"],
    "X4": ["-v", "error", "-select_streams", "v:0", "-show_entries", "stream=profile,level", "-of", "json"],
    "X5": ["-v", "error", "-show_optional_fields", "always", "-select_streams", "v:0",
           "-show_entries", "stream=field_order", "-of", "json"],
    "X6": ["-v", "error", "-show_optional_fields", "always", "-select_streams", "v:0",
           "-show_entries", "stream=chroma_location", "-of", "json"],
    "X7": ["-v", "error", "-show_entries", "stream=index,codec_type:stream_side_data_list", "-of", "json"],
    "X8": ["-v", "error", "-select_streams", "v:0", "-show_frames", "-read_intervals", "%+#10",
           "-show_entries", "frame=pts,pts_time:frame_side_data_list", "-of", "json"],
}

SCOPE_OBSERVATION = [
    "-v", "error", "-show_optional_fields", "always", "-show_entries",
    "format=format_name:stream=index,codec_type,codec_name", "-of", "json",
]

AUDIO_OBSERVATION = [
    "-v", "error", "-select_streams", "a", "-show_entries",
    "stream=index,codec_name,sample_rate,channels,channel_layout,bit_rate", "-of", "json",
]
X7_VIDEO_OBSERVATION = [
    "-v", "error", "-select_streams", "v:0", "-show_entries",
    "stream=index,codec_type:stream_side_data_list", "-of", "json",
]
AAC_FRAME_SAMPLES = 1024


def _result(status: str, *, expected: Any = None, actual: Any = None,
            reason: str | None = None, per_piece: list[dict[str, Any]] | None = None,
            vacuous: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "expected": expected, "actual": actual}
    if reason is not None:
        result["reason"] = reason
    if per_piece is not None:
        result["per_piece"] = per_piece
    if vacuous:
        result["vacuous"] = True
    return result


def _probe_ok(observation: Mapping[str, Any]) -> bool:
    return (
        observation.get("returncode") == 0
        and observation.get("json_error") is None
        and isinstance(observation.get("json"), Mapping)
    )


def _one_stream(observation: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, str | None]:
    if not _probe_ok(observation):
        return None, "ffprobe command failed or did not return JSON"
    streams = observation["json"].get("streams")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], Mapping):
        return None, "expected exactly one selected video stream in probe output"
    return streams[0], None


def _packets(observation: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]] | None, str | None]:
    if not _probe_ok(observation):
        return None, "ffprobe packet command failed or did not return JSON"
    packets = observation["json"].get("packets")
    if not isinstance(packets, list) or not packets:
        return None, "packet list is missing or empty"
    if not all(isinstance(packet, Mapping) for packet in packets):
        return None, "packet list contains a non-object value"
    return packets, None


def _sha256(path: Path) -> tuple[str | None, str | None]:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        return None, str(exc)
    return digest.hexdigest(), None


def _scope_gate(pieces: Sequence[Path], output_policy: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    reasons: list[dict[str, Any]] = []
    audio_policy = output_policy.get("audio_policy")
    if audio_policy not in ("none", "present"):
        reasons.append({"reason": "output_policy.audio_policy is outside v1 scope",
                        "actual": audio_policy})
    for index, piece in enumerate(pieces):
        observation = probe_json(SCOPE_OBSERVATION, piece)
        records.append({"piece_index": index, "piece": str(piece), "probe": observation})
        if not _probe_ok(observation):
            reasons.append({"piece_index": index, "reason": "scope probe failed"})
            continue
        payload = observation["json"]
        formats = payload.get("format", {}).get("format_name") if isinstance(payload.get("format"), Mapping) else None
        streams = payload.get("streams")
        if not isinstance(formats, str) or "mp4" not in formats.split(","):
            reasons.append({"piece_index": index, "reason": "container is not MP4", "actual": formats})
        if not isinstance(streams, list):
            reasons.append({"piece_index": index, "reason": "stream list is missing", "actual": streams})
            continue
        video_streams = [stream for stream in streams if isinstance(stream, Mapping) and stream.get("codec_type") == "video"]
        audio_streams = [stream for stream in streams if isinstance(stream, Mapping) and stream.get("codec_type") == "audio"]
        if not video_streams:
            reasons.append({"piece_index": index, "reason": "video stream is missing"})
        elif any(stream.get("codec_name") != "h264" for stream in video_streams):
            reasons.append({"piece_index": index, "reason": "video codec is not H.264",
                            "actual": [stream.get("codec_name") for stream in video_streams]})
        if audio_policy == "none":
            # Keep the established silent path exactly as it was: any audio
            # stream is outside that policy's stream set.
            if audio_streams:
                reasons.append({"piece_index": index, "reason": "audio stream is outside v1 scope",
                                "actual": [{"index": stream.get("index"), "codec_name": stream.get("codec_name")}
                                           for stream in audio_streams]})
        elif audio_policy == "present":
            if len(video_streams) != 1 or len(audio_streams) != 1 or len(streams) != 2:
                reasons.append({
                    "piece_index": index,
                    "reason": "audio_policy=present requires exactly one video and one audio stream",
                    "actual": [{"index": stream.get("index"), "codec_type": stream.get("codec_type")}
                               for stream in streams if isinstance(stream, Mapping)],
                })
    return {
        "status": STATUS_PASS if not reasons else STATUS_UNEVALUABLE,
        "criterion": "MP4 / H.264 / no audio streams" if audio_policy == "none" else "MP4 / H.264 / one audio stream",
        "reasons": reasons,
    }, records


def _condition_audio_identity(observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare the audio description carried by concat-copy's first stream."""
    keys = ("codec_name", "sample_rate", "channels", "channel_layout")
    values: list[dict[str, Any] | None] = []
    per_piece: list[dict[str, Any]] = []
    unavailable = False
    for index, observation in enumerate(observations):
        if not _probe_ok(observation):
            value, reason = None, "ffprobe command failed or did not return JSON"
        else:
            streams = observation["json"].get("streams")
            if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], Mapping):
                value, reason = None, "expected exactly one selected audio stream in probe output"
            else:
                value = {key: streams[0].get(key) for key in keys}
                reason = ("audio identity field is missing, empty, or unavailable" if any(
                    value[key] in (None, "", "unknown", "N/A") for key in keys
                ) else None)
        values.append(value)
        per_piece.append({"piece_index": index, "status": STATUS_UNEVALUABLE if reason else STATUS_PASS,
                          "actual": value, **({"reason": reason} if reason else {})})
        unavailable = unavailable or reason is not None
    if unavailable:
        return _result(STATUS_UNEVALUABLE, expected=values[0] if values else None,
                       actual=values, per_piece=per_piece)
    # ffprobe's AAC bit_rate is derived from short-stream packet accounting,
    # rather than a stable encoded parameter. The normalizer fixes its nominal
    # 192k target; join compatibility compares the stream description fields.
    identity_keys = keys
    matches = all(
        all(value[key] == values[0][key] for key in identity_keys)
        for value in values[1:]
    )
    status = STATUS_PASS if matches else STATUS_FAIL
    for item in per_piece:
        actual = item["actual"]
        if item["status"] == STATUS_FAIL:
            continue
        item["status"] = STATUS_PASS if all(
            actual[key] == values[0][key] for key in identity_keys
        ) else STATUS_FAIL
    return _result(status, expected=values[0] if values else None, actual=values, per_piece=per_piece)


def _compare_scalar_per_piece(observations: Sequence[Mapping[str, Any]], key: str,
                              expected: Any) -> dict[str, Any]:
    per_piece: list[dict[str, Any]] = []
    status = STATUS_PASS
    for index, observation in enumerate(observations):
        stream, error = _one_stream(observation)
        if error:
            per_piece.append({"piece_index": index, "status": STATUS_UNEVALUABLE, "actual": None, "reason": error})
            status = STATUS_UNEVALUABLE if status != STATUS_FAIL else status
            continue
        if key not in stream or stream[key] in (None, ""):
            per_piece.append({"piece_index": index, "status": STATUS_UNEVALUABLE,
                              "actual": stream.get(key), "reason": f"{key} is missing or empty"})
            status = STATUS_UNEVALUABLE if status != STATUS_FAIL else status
            continue
        actual = stream[key]
        piece_status = STATUS_PASS if actual == expected else STATUS_FAIL
        per_piece.append({"piece_index": index, "status": piece_status, "actual": actual})
        if piece_status == STATUS_FAIL:
            status = STATUS_FAIL
    return _result(status, expected=expected, actual=[item["actual"] for item in per_piece], per_piece=per_piece)


def _condition_p1(observations: Sequence[Mapping[str, Any]], policy: Mapping[str, Any]) -> dict[str, Any]:
    per_piece: list[dict[str, Any]] = []
    expected = {"width": policy.get("width"), "height": policy.get("height")}
    status = STATUS_PASS
    if not isinstance(expected["width"], int) or not isinstance(expected["height"], int):
        return _result(STATUS_UNEVALUABLE, expected=expected, reason="policy width/height is missing or invalid")
    for index, observation in enumerate(observations):
        stream, error = _one_stream(observation)
        actual = None if stream is None else {key: stream.get(key) for key in ("width", "height", "coded_width", "coded_height")}
        if error or stream.get("width") is None or stream.get("height") is None:
            piece_status = STATUS_UNEVALUABLE
            reason = error or "width or height is missing"
        else:
            piece_status = STATUS_PASS if (stream["width"], stream["height"]) == (expected["width"], expected["height"]) else STATUS_FAIL
            reason = None
        per_piece.append({"piece_index": index, "status": piece_status, "actual": actual, **({"reason": reason} if reason else {})})
        if piece_status == STATUS_FAIL:
            status = STATUS_FAIL
        elif piece_status == STATUS_UNEVALUABLE and status != STATUS_FAIL:
            status = STATUS_UNEVALUABLE
    return _result(status, expected=expected, actual=[item["actual"] for item in per_piece], per_piece=per_piece)


def _condition_rational_per_piece(observations: Sequence[Mapping[str, Any]], policy_value: Any,
                                  key: str, separator: str) -> dict[str, Any]:
    try:
        expected_fraction = contract_fraction(policy_value)
    except (RationalValueError, TypeError) as exc:
        return _result(STATUS_UNEVALUABLE, expected=policy_value, reason=str(exc))
    expected = {"num": expected_fraction.numerator, "den": expected_fraction.denominator}
    per_piece: list[dict[str, Any]] = []
    status = STATUS_PASS
    for index, observation in enumerate(observations):
        stream, error = _one_stream(observation)
        raw = None if stream is None else stream.get(key)
        try:
            if error:
                raise RationalValueError(error)
            actual_fraction = probe_fraction(raw, separator)
            actual: Any = {"num": actual_fraction.numerator, "den": actual_fraction.denominator}
            piece_status = STATUS_PASS if actual_fraction == expected_fraction else STATUS_FAIL
            reason = None
        except RationalValueError as exc:
            actual = raw
            piece_status = STATUS_UNEVALUABLE
            reason = str(exc)
        per_piece.append({"piece_index": index, "status": piece_status, "actual": actual,
                          **({"reason": reason} if reason else {})})
        if piece_status == STATUS_FAIL:
            status = STATUS_FAIL
        elif piece_status == STATUS_UNEVALUABLE and status != STATUS_FAIL:
            status = STATUS_UNEVALUABLE
    return _result(status, expected=expected, actual=[item["actual"] for item in per_piece], per_piece=per_piece)


def _condition_p4(observations: Sequence[Mapping[str, Any]], policy: Mapping[str, Any]) -> dict[str, Any]:
    mapping = {
        "color_primaries": "color_primaries",
        "color_transfer": "color_transfer",
        "color_space": "color_matrix",
        "color_range": "color_range",
    }
    expected = {probe_key: policy.get(policy_key) for probe_key, policy_key in mapping.items()}
    if any(value in (None, "") for value in expected.values()):
        return _result(STATUS_UNEVALUABLE, expected=expected, reason="policy colour value is missing or empty")
    per_piece: list[dict[str, Any]] = []
    status = STATUS_PASS
    for index, observation in enumerate(observations):
        stream, error = _one_stream(observation)
        actual = None if stream is None else {key: stream.get(key) for key in mapping}
        if error or stream is None or any(key not in stream or stream.get(key) in (None, "") for key in mapping):
            piece_status = STATUS_UNEVALUABLE
            reason = error or "one or more colour fields are missing or empty"
        else:
            piece_status = STATUS_PASS if actual == expected else STATUS_FAIL
            reason = None
        per_piece.append({"piece_index": index, "status": piece_status, "actual": actual,
                          **({"reason": reason} if reason else {})})
        if piece_status == STATUS_FAIL:
            status = STATUS_FAIL
        elif piece_status == STATUS_UNEVALUABLE and status != STATUS_FAIL:
            status = STATUS_UNEVALUABLE
    return _result(status, expected=expected, actual=[item["actual"] for item in per_piece], per_piece=per_piece)


def _packet_dts(packets: Sequence[Mapping[str, Any]]) -> tuple[list[int] | None, str | None]:
    values: list[int] = []
    for packet in packets:
        raw = packet.get("dts")
        if isinstance(raw, bool):
            return None, "packet dts is boolean"
        try:
            values.append(int(raw))
        except (TypeError, ValueError):
            return None, f"packet dts is missing or non-integral: {raw!r}"
    return values, None


def _expected_delta(policy: Mapping[str, Any]) -> tuple[Fraction | None, str | None]:
    try:
        rate = contract_fraction(policy.get("frame_rate"))
        time_base = contract_fraction(policy.get("encoder_timebase"))
    except (RationalValueError, TypeError) as exc:
        return None, str(exc)
    if rate <= 0 or time_base <= 0:
        return None, "frame rate and encoder timebase must be positive"
    return Fraction(1, 1) / rate / time_base, None


def _condition_p5(rate_observations: Sequence[Mapping[str, Any]],
                  packet_observations: Sequence[Mapping[str, Any]], policy: Mapping[str, Any]) -> dict[str, Any]:
    try:
        expected_rate = contract_fraction(policy.get("frame_rate"))
    except (RationalValueError, TypeError) as exc:
        return _result(STATUS_UNEVALUABLE, expected=policy.get("frame_rate"), reason=str(exc))
    delta, delta_error = _expected_delta(policy)
    if delta_error:
        return _result(STATUS_UNEVALUABLE, expected=None, reason=delta_error)
    expected = {
        "r_frame_rate": {"num": expected_rate.numerator, "den": expected_rate.denominator},
        "avg_frame_rate": {"num": expected_rate.numerator, "den": expected_rate.denominator},
        "dts_delta": {"num": delta.numerator, "den": delta.denominator},
    }
    per_piece: list[dict[str, Any]] = []
    overall = STATUS_PASS
    for index, (rate_observation, packet_observation) in enumerate(zip(rate_observations, packet_observations)):
        stream, stream_error = _one_stream(rate_observation)
        packets, packet_error = _packets(packet_observation)
        actual: dict[str, Any] = {}
        reasons: list[str] = []
        rates_match = True
        if stream_error:
            reasons.append(stream_error)
            rates_match = False
        else:
            for key in ("r_frame_rate", "avg_frame_rate"):
                raw = stream.get(key)
                actual[key] = raw
                try:
                    parsed = probe_fraction(raw, "/")
                    if parsed != expected_rate:
                        rates_match = False
                except RationalValueError as exc:
                    reasons.append(str(exc))
                    rates_match = False
        deltas_match = True
        if packet_error:
            reasons.append(packet_error)
            deltas_match = False
        else:
            dts_values, dts_error = _packet_dts(packets)
            if dts_error:
                reasons.append(dts_error)
                deltas_match = False
            elif len(dts_values) < 2:
                reasons.append("at least two packets are required to evaluate CFR DTS deltas")
                deltas_match = False
            else:
                actual_deltas = [right - left for left, right in zip(dts_values, dts_values[1:])]
                actual["dts_deltas"] = actual_deltas
                deltas_match = all(Fraction(value, 1) == delta for value in actual_deltas)
        unevaluable = bool(reasons)
        piece_status = STATUS_UNEVALUABLE if unevaluable else (STATUS_PASS if rates_match and deltas_match else STATUS_FAIL)
        per_piece.append({"piece_index": index, "status": piece_status, "actual": actual,
                          **({"reason": "; ".join(reasons)} if reasons else {})})
        if piece_status == STATUS_FAIL:
            overall = STATUS_FAIL
        elif piece_status == STATUS_UNEVALUABLE and overall != STATUS_FAIL:
            overall = STATUS_UNEVALUABLE
    return _result(overall, expected=expected, actual=[item["actual"] for item in per_piece], per_piece=per_piece)


def _condition_identity(observations: Sequence[Mapping[str, Any]], keys: Sequence[str],
                        *, require_progressive: bool = False) -> dict[str, Any]:
    values: list[Any] = []
    per_piece: list[dict[str, Any]] = []
    unavailable = False
    for index, observation in enumerate(observations):
        stream, error = _one_stream(observation)
        if error:
            unavailable = True
            values.append(None)
            per_piece.append({"piece_index": index, "status": STATUS_UNEVALUABLE, "actual": None, "reason": error})
            continue
        value = {key: stream.get(key) for key in keys}
        if any(key not in stream or stream.get(key) in (None, "", "unknown", "N/A") for key in keys):
            unavailable = True
            values.append(value)
            per_piece.append({"piece_index": index, "status": STATUS_UNEVALUABLE, "actual": value,
                              "reason": "identity field is missing, empty, or unavailable"})
            continue
        values.append(value)
        per_piece.append({"piece_index": index, "status": STATUS_PASS, "actual": value})
    if unavailable:
        return _result(STATUS_UNEVALUABLE, expected=values[0] if values else None, actual=values, per_piece=per_piece)
    equal = all(value == values[0] for value in values[1:])
    progressive = not require_progressive or all(value.get("field_order") == "progressive" for value in values)
    status = STATUS_PASS if equal and progressive else STATUS_FAIL
    for item in per_piece:
        item["status"] = STATUS_PASS if item["actual"] == values[0] and (
            not require_progressive or item["actual"].get("field_order") == "progressive") else STATUS_FAIL
    return _result(status, expected=values[0] if values else None, actual=values, per_piece=per_piece)


def _condition_x1(observations: Sequence[Mapping[str, Any]], audio_policy: str = "none") -> dict[str, Any]:
    per_piece: list[dict[str, Any]] = []
    status = STATUS_PASS
    expected = ([{"index": 0, "codec_type": "video"}]
                if audio_policy == "none" else [
                    {"index": 0, "codec_type": "video"},
                    {"index": 1, "codec_type": "audio"},
                ])
    for index, observation in enumerate(observations):
        if not _probe_ok(observation):
            piece_status, streams, reason = STATUS_UNEVALUABLE, None, "ffprobe command failed or did not return JSON"
        else:
            streams = observation["json"].get("streams")
            if not isinstance(streams, list):
                piece_status, reason = STATUS_UNEVALUABLE, "stream list is missing"
            else:
                projected = [
                    {"index": stream.get("index"), "codec_type": stream.get("codec_type")}
                    for stream in streams if isinstance(stream, Mapping)
                ]
                all_streams_were_objects = len(projected) == len(streams)
                streams = projected if all_streams_were_objects else streams
                piece_status = STATUS_PASS if (
                    all_streams_were_objects
                    and projected == expected
                ) else STATUS_FAIL
                reason = None
        per_piece.append({"piece_index": index, "status": piece_status, "actual": streams,
                          **({"reason": reason} if reason else {})})
        if piece_status == STATUS_FAIL:
            status = STATUS_FAIL
        elif piece_status == STATUS_UNEVALUABLE and status != STATUS_FAIL:
            status = STATUS_UNEVALUABLE
    return _result(status, expected=expected,
                   actual=[item["actual"] for item in per_piece], per_piece=per_piece)


def _side_data_list(value: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    if value is None:
        return [], None
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        return None, "side_data_list is not a list of objects"
    return [dict(item) for item in value], None


def _condition_x7(observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values: list[Any] = []
    per_piece: list[dict[str, Any]] = []
    unavailable = False
    for index, observation in enumerate(observations):
        stream, error = _one_stream(observation)
        value, side_error = _side_data_list(None if stream is None else stream.get("side_data_list"))
        reason = error or side_error
        if reason:
            unavailable = True
            value = None
        values.append(value)
        per_piece.append({"piece_index": index, "status": STATUS_UNEVALUABLE if reason else STATUS_PASS,
                          "actual": value, **({"reason": reason} if reason else {})})
    if unavailable:
        status = STATUS_UNEVALUABLE
    else:
        status = STATUS_PASS if all(value == values[0] for value in values[1:]) else STATUS_FAIL
        for item in per_piece:
            item["status"] = STATUS_PASS if item["actual"] == values[0] else STATUS_FAIL
    return _result(status, expected=values[0] if values else None, actual=values, per_piece=per_piece)


def _normalise_x8(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Flatten the sampled frames into one canonical side-data collection.

    Which frame carries a side-data entry, and how many frames were sampled, are
    not compatibility properties: the condition asks whether the metadata is
    absent everywhere or the same everywhere.  Comparing per-frame slots would
    make two pieces differ merely because they were sampled to different depths.
    """
    frames = payload.get("frames")
    if not isinstance(frames, list):
        return None, "frame list is missing"
    kept: list[dict[str, Any]] = []
    for frame in frames:
        if not isinstance(frame, Mapping):
            return None, "frame list contains a non-object value"
        side_data, error = _side_data_list(frame.get("side_data_list"))
        if error:
            return None, error
        kept.extend(item for item in side_data if item.get("side_data_type") != EXCLUDED_X8_TYPE)
    return sorted(kept, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False)), None


def _condition_x8(observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values: list[Any] = []
    per_piece: list[dict[str, Any]] = []
    unavailable = False
    for index, observation in enumerate(observations):
        if not _probe_ok(observation):
            value, error = None, "ffprobe command failed or did not return JSON"
        else:
            value, error = _normalise_x8(observation["json"])
        values.append(value)
        per_piece.append({"piece_index": index, "status": STATUS_UNEVALUABLE if error else STATUS_PASS,
                          "actual": value, **({"reason": error} if error else {})})
        unavailable = unavailable or error is not None
    if unavailable:
        status = STATUS_UNEVALUABLE
    else:
        status = STATUS_PASS if all(value == values[0] for value in values[1:]) else STATUS_FAIL
        for item in per_piece:
            item["status"] = STATUS_PASS if item["actual"] == values[0] else STATUS_FAIL
    return _result(status, expected=values[0] if values else None, actual=values, per_piece=per_piece)


def _condition_r1(packet_observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    per_piece: list[dict[str, Any]] = []
    status = STATUS_PASS
    for index, observation in enumerate(packet_observations):
        packets, error = _packets(observation)
        flags = None if packets is None else packets[0].get("flags")
        if error or not isinstance(flags, str) or not flags:
            piece_status, reason = STATUS_UNEVALUABLE, error or "first packet flags are missing or empty"
        else:
            piece_status, reason = (STATUS_PASS if "K" in flags else STATUS_FAIL), None
        per_piece.append({"piece_index": index, "status": piece_status, "actual": flags,
                          **({"reason": reason} if reason else {})})
        if piece_status == STATUS_FAIL:
            status = STATUS_FAIL
        elif piece_status == STATUS_UNEVALUABLE and status != STATUS_FAIL:
            status = STATUS_UNEVALUABLE
    return _result(status, expected="first packet flags contain K", actual=[item["actual"] for item in per_piece], per_piece=per_piece)


def _condition_r2(packet_observations: Sequence[Mapping[str, Any]], policy: Mapping[str, Any]) -> dict[str, Any]:
    delta, delta_error = _expected_delta(policy)
    if delta_error:
        return _result(STATUS_UNEVALUABLE, expected=None, reason=delta_error)
    per_piece: list[dict[str, Any]] = []
    status = STATUS_PASS
    for index, observation in enumerate(packet_observations):
        packets, error = _packets(observation)
        dts_values: list[int] | None = None
        if not error:
            dts_values, error = _packet_dts(packets)
        if not error and len(dts_values) < 2:
            error = "at least two packets are required to evaluate decode order"
        if error:
            piece_status = STATUS_UNEVALUABLE
            deltas = None
        else:
            deltas = [right - left for left, right in zip(dts_values, dts_values[1:])]
            piece_status = STATUS_PASS if all(value > 0 and Fraction(value, 1) == delta for value in deltas) else STATUS_FAIL
        per_piece.append({"piece_index": index, "status": piece_status,
                          "actual": {"dts": dts_values, "deltas": deltas},
                          **({"reason": error} if error else {})})
        if piece_status == STATUS_FAIL:
            status = STATUS_FAIL
        elif piece_status == STATUS_UNEVALUABLE and status != STATUS_FAIL:
            status = STATUS_UNEVALUABLE
    expected = {"strictly_increasing": True, "constant_delta": {"num": delta.numerator, "den": delta.denominator}}
    return _result(status, expected=expected, actual=[item["actual"] for item in per_piece], per_piece=per_piece)


def _ffconcat_quote(path: Path) -> str:
    return "'" + str(path.resolve()).replace("'", "'\\''") + "'"


def _concat(pieces: Sequence[Path], output_path: Path, audio_policy: str) -> tuple[dict[str, Any], str]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".ffconcat", delete=False)
    try:
        descriptor.write("ffconcat version 1.0\n")
        for piece in pieces:
            descriptor.write(f"file {_ffconcat_quote(piece)}\n")
        descriptor.close()
        argv = [ffmpeg_path(), "-y", "-hide_banner", "-nostdin", "-f", "concat", "-safe", "0",
                "-i", descriptor.name]
        if audio_policy == "none":
            # This is the original silent concat argv, retained verbatim.
            argv += ["-map", "0:v:0", "-c", "copy", "-an", str(output_path)]
        else:
            argv += ["-map", "0:v:0", "-map", "0:a:0", "-c", "copy", str(output_path)]
        result = run_command(argv)
    finally:
        try:
            os.unlink(descriptor.name)
        except OSError:
            pass
    return result, descriptor.name


def _condition_r3(concat_packet_observation: Mapping[str, Any] | None,
                  packet_observations: Sequence[Mapping[str, Any]], policy: Mapping[str, Any],
                  concat_result: Mapping[str, Any] | None) -> dict[str, Any]:
    delta, delta_error = _expected_delta(policy)
    if delta_error:
        return _result(STATUS_UNEVALUABLE, reason=delta_error)
    if concat_result is None or concat_result.get("returncode") != 0:
        return _result(STATUS_UNEVALUABLE, reason="concat command failed")
    if concat_packet_observation is None:
        return _result(STATUS_UNEVALUABLE, reason="concat packet observation is missing")
    output_packets, output_error = _packets(concat_packet_observation)
    if output_error:
        return _result(STATUS_UNEVALUABLE, reason=output_error)
    output_dts, dts_error = _packet_dts(output_packets)
    if dts_error or len(output_dts) < 2:
        return _result(STATUS_UNEVALUABLE, reason=dts_error or "joined output has fewer than two packets")
    piece_counts: list[int] = []
    for observation in packet_observations:
        packets, error = _packets(observation)
        if error:
            return _result(STATUS_UNEVALUABLE, reason="piece packet count is unavailable")
        piece_counts.append(len(packets))
    tolerance = delta
    deltas = [right - left for left, right in zip(output_dts, output_dts[1:])]
    join_indices: list[int] = []
    cumulative_count = 0
    for count in piece_counts[:-1]:
        cumulative_count += count
        join_indices.append(cumulative_count)
    join_delta_indices = {index - 1 for index in join_indices}
    non_join_violations = [
        index for index, actual_delta in enumerate(deltas)
        if index not in join_delta_indices and Fraction(actual_delta, 1) != delta
    ]
    joins: list[dict[str, Any]] = []
    for join_index in join_indices:
        delta_index = join_index - 1
        actual_delta = deltas[delta_index] if 0 <= delta_index < len(deltas) else None
        deviation = None if actual_delta is None else abs(Fraction(actual_delta, 1) - delta)
        joins.append({
            "join_index": join_index,
            "expected_delta": _tick_evidence(delta),
            "actual_delta": actual_delta,
            "deviation_ticks": None if deviation is None else _tick_evidence(deviation),
            "tolerance_ticks": _tick_evidence(tolerance),
            "within_tolerance": deviation is not None and deviation < tolerance,
        })
    tolerance_violations = [
        join["join_index"] for join in joins if not join["within_tolerance"]
    ]
    output_count = len(output_packets)
    expected_count = sum(piece_counts)
    frame_count_matches = output_count == expected_count
    status = STATUS_PASS if (
        not non_join_violations
        and not tolerance_violations
        and frame_count_matches
    ) else STATUS_FAIL
    return _result(status,
                   expected={"constant_delta": {"num": delta.numerator, "den": delta.denominator},
                             "nb_frames": expected_count},
                   actual={
                       "dts": output_dts,
                       "dts_deltas": deltas,
                       "nb_frames": output_count,
                       "piece_packet_counts": piece_counts,
                       "tolerance_ticks": _tick_evidence(tolerance),
                       "tolerance_basis": "one_video_frame_interval",
                       "joins": joins,
                       "non_join_violation_indices": non_join_violations,
                       "tolerance_violation_join_indices": tolerance_violations,
                       "frame_count_matches": frame_count_matches,
                   })


def _tick_evidence(value: Fraction) -> int | dict[str, int]:
    """Keep whole tick values legible without making fractional values lossy."""
    if value.denominator == 1:
        return value.numerator
    return {"num": value.numerator, "den": value.denominator}


def _condition_r3_present(concat_packet_observation: Mapping[str, Any] | None,
                          packet_observations: Sequence[Mapping[str, Any]],
                          audio_observations: Sequence[Mapping[str, Any]],
                          policy: Mapping[str, Any],
                          concat_result: Mapping[str, Any] | None) -> dict[str, Any]:
    """Check audio-bearing concat DTS continuity at piece joins only.

    AAC packet cadence need not divide the video frame cadence. The concat
    demuxer can therefore move the video DTS at a join by less than one AAC
    frame while maintaining A/V synchronization. A non-join delta remains a
    strict CFR assertion.
    """
    delta, delta_error = _expected_delta(policy)
    if delta_error:
        return _result(STATUS_UNEVALUABLE, reason=delta_error)
    try:
        video_timebase = contract_fraction(policy.get("encoder_timebase"))
    except (RationalValueError, TypeError) as exc:
        return _result(STATUS_UNEVALUABLE, reason=str(exc))
    if concat_result is None or concat_result.get("returncode") != 0:
        return _result(STATUS_UNEVALUABLE, reason="concat command failed")
    if concat_packet_observation is None:
        return _result(STATUS_UNEVALUABLE, reason="concat packet observation is missing")
    output_packets, output_error = _packets(concat_packet_observation)
    if output_error:
        return _result(STATUS_UNEVALUABLE, reason=output_error)
    output_dts, dts_error = _packet_dts(output_packets)
    if dts_error or len(output_dts) < 2:
        return _result(STATUS_UNEVALUABLE, reason=dts_error or "joined output has fewer than two packets")

    piece_counts: list[int] = []
    for observation in packet_observations:
        packets, error = _packets(observation)
        if error:
            return _result(STATUS_UNEVALUABLE, reason="piece packet count is unavailable")
        piece_counts.append(len(packets))

    sample_rates: list[int] = []
    for index, observation in enumerate(audio_observations):
        stream, error = _one_stream(observation)
        raw_sample_rate = None if stream is None else stream.get("sample_rate")
        if error:
            return _result(STATUS_UNEVALUABLE,
                           reason=f"piece {index} audio sample rate is unavailable: {error}")
        if isinstance(raw_sample_rate, bool):
            return _result(STATUS_UNEVALUABLE,
                           reason=f"piece {index} audio sample rate is boolean")
        try:
            sample_rate = int(raw_sample_rate)
        except (TypeError, ValueError):
            return _result(STATUS_UNEVALUABLE,
                           reason=f"piece {index} audio sample rate is missing or non-integral: {raw_sample_rate!r}")
        if sample_rate <= 0:
            return _result(STATUS_UNEVALUABLE,
                           reason=f"piece {index} audio sample rate must be positive: {sample_rate}")
        sample_rates.append(sample_rate)
    if len(sample_rates) != len(piece_counts):
        return _result(STATUS_UNEVALUABLE, reason="audio sample rate count does not match piece count")
    if len(set(sample_rates)) != 1:
        return _result(STATUS_UNEVALUABLE, actual={"audio_sample_rates": sample_rates},
                       reason="audio sample rates differ across pieces")

    tolerance = Fraction(AAC_FRAME_SAMPLES, sample_rates[0]) / video_timebase
    deltas = [right - left for left, right in zip(output_dts, output_dts[1:])]
    join_indices: list[int] = []
    cumulative_count = 0
    for count in piece_counts[:-1]:
        cumulative_count += count
        join_indices.append(cumulative_count)
    join_delta_indices = {index - 1 for index in join_indices}
    non_join_violations = [
        index for index, actual_delta in enumerate(deltas)
        if index not in join_delta_indices and Fraction(actual_delta, 1) != delta
    ]
    joins: list[dict[str, Any]] = []
    for join_index in join_indices:
        delta_index = join_index - 1
        actual_delta = deltas[delta_index] if 0 <= delta_index < len(deltas) else None
        deviation = None if actual_delta is None else abs(Fraction(actual_delta, 1) - delta)
        joins.append({
            "join_index": join_index,
            "expected_delta": _tick_evidence(delta),
            "actual_delta": actual_delta,
            "deviation_ticks": None if deviation is None else _tick_evidence(deviation),
            "within_tolerance": deviation is not None and deviation < tolerance,
        })
    deviating_joins = [
        join for join in joins
        if join["actual_delta"] is None or Fraction(join["actual_delta"], 1) != delta
    ]
    output_count = len(output_packets)
    expected_count = sum(piece_counts)
    frame_count_matches = output_count == expected_count
    joins_within_tolerance = all(join["within_tolerance"] for join in joins)
    deviation_count_within_join_count = len(deviating_joins) <= len(joins)
    status = STATUS_PASS if (
        frame_count_matches
        and not non_join_violations
        and joins_within_tolerance
        and deviation_count_within_join_count
    ) else STATUS_FAIL
    return _result(status,
                   expected={"constant_delta": {"num": delta.numerator, "den": delta.denominator},
                             "nb_frames": expected_count},
                   actual={
                       "dts": output_dts,
                       "dts_deltas": deltas,
                       "nb_frames": output_count,
                       "piece_packet_counts": piece_counts,
                       "audio_sample_rates": sample_rates,
                       "tolerance_ticks": _tick_evidence(tolerance),
                       "tolerance_basis": {
                           "audio_frame_samples": AAC_FRAME_SAMPLES,
                           "audio_sample_rate": sample_rates[0],
                           "video_timebase": {"num": video_timebase.numerator,
                                              "den": video_timebase.denominator},
                           "formula": "audio_frame_samples / audio_sample_rate / video_timebase",
                       },
                       "joins": joins,
                       "non_join_violation_indices": non_join_violations,
                       "deviating_join_count": len(deviating_joins),
                       "join_count": len(joins),
                       "deviating_join_count_within_join_count": deviation_count_within_join_count,
                   })


def _vacuous(condition_id: str) -> dict[str, Any]:
    return _result(STATUS_PASS, expected="not applicable to a single-piece plan", actual="not compared",
                   reason=f"{condition_id} is vacuous for one piece under spec v2", vacuous=True)


def _failure_pairs(conditions: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"condition_id": condition_id, "expected_or_first": result.get("expected"),
         "actual_or_later": result.get("actual"), "reason": result.get("reason")}
        for condition_id, result in conditions.items() if result.get("status") == STATUS_FAIL
    ]


def check_concat(pieces: Sequence[str | Path], output_policy: Mapping[str, Any], *,
                 concat_output: str | Path | None = None) -> dict[str, Any]:
    """Run the v2 scope gate and all in-scope conditions with raw evidence."""
    paths = [Path(piece).resolve() for piece in pieces]
    evidence: dict[str, Any] = {
        "spec_id": SPEC_ID,
        "spec_version": SPEC_VERSION,
        "tool_versions": tool_versions(),
        "pieces": [],
        "scope_gate": None,
        "scope_commands": [],
        "conditions": {},
        "condition_commands": [],
        "concat": None,
        "failures": [],
    }
    if not paths:
        evidence["scope_gate"] = {"status": STATUS_UNEVALUABLE, "criterion": "at least one piece", "reasons": [
            {"reason": "piece list is empty"}
        ]}
        evidence["verdict"] = "unverified"
        return evidence
    for index, path in enumerate(paths):
        digest, error = _sha256(path)
        evidence["pieces"].append({"order": index, "path": str(path), "sha256": digest, "hash_error": error})
    scope_gate, scope_records = _scope_gate(paths, output_policy)
    evidence["scope_gate"] = scope_gate
    evidence["scope_commands"] = scope_records
    if scope_gate["status"] != STATUS_PASS or any(piece["hash_error"] for piece in evidence["pieces"]):
        evidence["verdict"] = "unverified"
        evidence["unverified_reason"] = "one or more pieces are unreadable or outside the MP4/H.264/video-only v2 scope"
        return evidence

    observations: dict[str, list[dict[str, Any]]] = {}
    for observation_id, arguments in OBSERVATIONS.items():
        # X7's historic no-selector argv is intentionally retained for the
        # video-only policy. In the present branch it must select v:0, because
        # X7 judges video side data, not the new audio stream.
        effective_arguments = (X7_VIDEO_OBSERVATION if observation_id == "X7"
                               and output_policy.get("audio_policy") == "present" else arguments)
        observations[observation_id] = []
        for index, path in enumerate(paths):
            result = probe_json(effective_arguments, path)
            observations[observation_id].append(result)
            evidence["condition_commands"].append({
                "condition_ids": observation_id.split("/"), "observation_id": observation_id,
                "piece_index": index, "piece": str(path), "probe": result,
            })

    audio_condition: dict[str, Any] | None = None
    audio_observations: list[dict[str, Any]] | None = None
    if output_policy.get("audio_policy") == "present":
        audio_observations = []
        for index, path in enumerate(paths):
            result = probe_json(AUDIO_OBSERVATION, path)
            audio_observations.append(result)
            evidence["condition_commands"].append({
                "condition_ids": ["audio_identity"], "observation_id": "audio_identity",
                "piece_index": index, "piece": str(path), "probe": result,
            })
        audio_condition = _condition_audio_identity(audio_observations)
        evidence["audio"] = {"condition": audio_condition}

    conditions: dict[str, dict[str, Any]] = {}
    conditions["P1"] = _condition_p1(observations["P1"], output_policy)
    conditions["P2"] = _compare_scalar_per_piece(observations["P2"], "pix_fmt", output_policy.get("pixel_format"))
    conditions["P3"] = _condition_rational_per_piece(observations["P3"], output_policy.get("sar"),
                                                      "sample_aspect_ratio", ":")
    conditions["P4"] = _condition_p4(observations["P4"], output_policy)
    conditions["P5"] = _condition_p5(observations["P5-rate"], observations["P5/R1/R2"], output_policy)
    conditions["P6"] = _condition_rational_per_piece(observations["P6"], output_policy.get("encoder_timebase"),
                                                      "time_base", "/")

    if len(paths) == 1:
        for condition_id in ("X1", "X2", "X3", "X4", "X5", "X6", "X7", "X8"):
            conditions[condition_id] = _vacuous(condition_id)
    else:
        conditions["X1"] = _condition_x1(observations["X1"], str(output_policy.get("audio_policy")))
        conditions["X2"] = _condition_identity(observations["X2"], ("codec_name", "codec_tag_string"))
        conditions["X3"] = _condition_identity(observations["X3"], ("extradata_size", "extradata_hash"))
        conditions["X4"] = _condition_identity(observations["X4"], ("profile", "level"))
        conditions["X5"] = _condition_identity(observations["X5"], ("field_order",), require_progressive=True)
        conditions["X6"] = _condition_identity(observations["X6"], ("chroma_location",))
        conditions["X7"] = _condition_x7(observations["X7"])
        conditions["X8"] = _condition_x8(observations["X8"])

    conditions["R1"] = _condition_r1(observations["P5/R1/R2"])
    conditions["R2"] = _condition_r2(observations["P5/R1/R2"], output_policy)
    if len(paths) == 1:
        conditions["R3"] = _vacuous("R3")
    else:
        if concat_output is None:
            file_handle, generated_name = tempfile.mkstemp(prefix="draftcut-concat-check-", suffix=".mp4")
            os.close(file_handle)
            Path(generated_name).unlink(missing_ok=True)
            concat_path = Path(generated_name)
        else:
            concat_path = Path(concat_output).resolve()
        concat_result, descriptor_path = _concat(paths, concat_path, str(output_policy.get("audio_policy")))
        concat_packet = probe_json(OBSERVATIONS["P5/R1/R2"], concat_path) if concat_result.get("returncode") == 0 else None
        concat_hash, concat_hash_error = _sha256(concat_path) if concat_path.exists() else (None, "concat output not created")
        evidence["concat"] = {
            "output_path": str(concat_path), "descriptor_path_used": descriptor_path,
            "command": concat_result, "packet_probe": concat_packet,
            "sha256": concat_hash, "hash_error": concat_hash_error,
        }
        if output_policy.get("audio_policy") == "present":
            conditions["R3"] = _condition_r3_present(
                concat_packet, observations["P5/R1/R2"], audio_observations or [], output_policy, concat_result)
        else:
            conditions["R3"] = _condition_r3(concat_packet, observations["P5/R1/R2"], output_policy, concat_result)

    evidence["conditions"] = conditions
    evidence["failures"] = _failure_pairs(conditions)
    if audio_condition is not None and audio_condition.get("status") == STATUS_FAIL:
        evidence["failures"].append({
            "condition_id": "audio_identity", "expected_or_first": audio_condition.get("expected"),
            "actual_or_later": audio_condition.get("actual"), "reason": audio_condition.get("reason"),
        })
    statuses = [conditions[condition_id]["status"] for condition_id in CONDITION_IDS]
    if audio_condition is not None:
        statuses.append(audio_condition["status"])
    if STATUS_FAIL in statuses:
        evidence["verdict"] = "incompatible"
    elif STATUS_UNEVALUABLE in statuses:
        evidence["verdict"] = "unverified"
    else:
        evidence["verdict"] = "compatible"
    return evidence


def _load_policy(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    policy = payload.get("output_policy", payload) if isinstance(payload, Mapping) else payload
    if not isinstance(policy, Mapping):
        raise ValueError("policy JSON must be an output_policy object or an EditPlan containing one")
    return policy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pieces", nargs="+", type=Path, help="ordered concat pieces")
    parser.add_argument("--policy", required=True, type=Path, help="output_policy or EditPlan JSON")
    parser.add_argument("--concat-output", type=Path, help="path for the measured concat output")
    parser.add_argument("--evidence", type=Path, help="write the evidence JSON to this path")
    arguments = parser.parse_args()
    result = check_concat(arguments.pieces, _load_policy(arguments.policy), concat_output=arguments.concat_output)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if arguments.evidence:
        arguments.evidence.parent.mkdir(parents=True, exist_ok=True)
        arguments.evidence.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
