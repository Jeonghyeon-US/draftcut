#!/usr/bin/env python3
"""Align an edited programme to its source using 0.1-second audio fingerprints."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np


FORMAT = "draftcut.pair-align.alignment.v1"
SAMPLE_RATE = 8000
SAMPLE_WIDTH_BITS = 16
CHANNELS = 1
WINDOW_SAMPLES = 4096
HOP_SAMPLES = 800
WINDOW_FUNCTION = "hann"
SPECTRUM = "power"
FREQ_LOW_HZ = 300
FREQ_HIGH_HZ = 3000
BAND_COUNT = 33
FINGERPRINT_BITS = 32
SUBFINGERPRINT_BITS = 16
COMMON_SUBFINGERPRINT_DIVISOR = 500
WINDOW_FRAMES = 100
MODE_MIN_RATIO = 0.15
BER_THRESHOLD = 0.25
JUNCTION_SEARCH_FRAMES = 200  # +-20 s around a window-grid junction
CONFIDENCE_MIN = 0.5
SUMMARY_CONFIDENCE_MIN = 0.6
SPEED_CHANGE_OFFSET_DELTA_S = 0.3
FFT_BATCH_FRAMES = 1024


def run_json(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ffprobe_payload(path: Path) -> dict[str, Any]:
    return run_json([
        "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path),
    ])


def duration_from_probe(payload: dict[str, Any]) -> float:
    try:
        return float(payload["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("ffprobe did not return format.duration") from exc


def decode_wav(input_path: Path, output_path: Path, limit_seconds: float | None) -> None:
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(input_path),
        "-map", "0:a:0", "-vn", "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
        "-sample_fmt", "s16",
    ]
    if limit_seconds is not None:
        command.extend(["-t", str(limit_seconds)])
    command.append(str(output_path))
    subprocess.run(command, check=True)


def pcm_samples(wav_path: Path) -> np.ndarray:
    """Return signed little-endian 16-bit WAV data after validating its header."""
    import wave

    with wave.open(str(wav_path), "rb") as handle:
        if (handle.getnchannels(), handle.getsampwidth(), handle.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError(f"unexpected decoded WAV format: {wav_path}")
        return np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").astype(np.float32)


def band_matrix() -> np.ndarray:
    """Make a frequency-bin to logarithmic-band summation matrix once per process."""
    frequencies = np.fft.rfftfreq(WINDOW_SAMPLES, d=1.0 / SAMPLE_RATE)
    edges = np.geomspace(FREQ_LOW_HZ, FREQ_HIGH_HZ, BAND_COUNT + 1)
    membership = (frequencies[None, :] >= edges[:-1, None]) & (frequencies[None, :] < edges[1:, None])
    membership[-1] |= frequencies == FREQ_HIGH_HZ
    if np.any(membership.sum(axis=1) == 0):
        raise ValueError("a logarithmic fingerprint band has no FFT bins")
    return membership.astype(np.float32)


def fingerprints(samples: np.ndarray) -> np.ndarray:
    """Vectorized Haitsma--Kalker-style 32-bit fingerprints, one per 0.1 s frame."""
    if samples.size < WINDOW_SAMPLES:
        return np.empty(0, dtype=np.uint32)
    frame_count = 1 + (samples.size - WINDOW_SAMPLES) // HOP_SAMPLES
    frame_view = np.lib.stride_tricks.sliding_window_view(samples, WINDOW_SAMPLES)[::HOP_SAMPLES]
    if frame_view.shape[0] != frame_count:
        raise AssertionError("unexpected strided frame count")
    hann = np.hanning(WINDOW_SAMPLES).astype(np.float32)
    bands = band_matrix()
    contrasts = np.empty((frame_count, FINGERPRINT_BITS), dtype=np.float32)
    # Batches cap FFT working memory.  Frames within each batch are fully vectorized.
    for start in range(0, frame_count, FFT_BATCH_FRAMES):
        stop = min(start + FFT_BATCH_FRAMES, frame_count)
        spectrum = np.fft.rfft(frame_view[start:stop] * hann, axis=1)
        power = spectrum.real * spectrum.real + spectrum.imag * spectrum.imag
        energies = power.astype(np.float32, copy=False) @ bands.T
        contrasts[start:stop] = energies[:, :-1] - energies[:, 1:]
    bits = np.zeros((frame_count, FINGERPRINT_BITS), dtype=np.uint32)
    bits[1:] = (contrasts[1:] - contrasts[:-1] > 0).astype(np.uint32)
    return np.bitwise_or.reduce(bits << np.arange(FINGERPRINT_BITS, dtype=np.uint32), axis=1)


def make_subfingerprint_index(subfingerprints: np.ndarray) -> dict[int, np.ndarray]:
    """Index non-common 16-bit subfingerprints to source frame-index arrays."""
    threshold = subfingerprints.size / COMMON_SUBFINGERPRINT_DIVISOR
    index: dict[int, np.ndarray] = {}
    values, counts = np.unique(subfingerprints, return_counts=True)
    for value, count in zip(values, counts, strict=True):
        if int(count) <= threshold:
            index[int(value)] = np.flatnonzero(subfingerprints == value).astype(np.int32)
    return index


def source_lookup_tables(index: dict[int, np.ndarray]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Compact the required dict index into arrays for vectorized lookup in a window."""
    keys = np.array(sorted(index), dtype=np.uint32)
    counts = np.array([index[int(key)].size for key in keys], dtype=np.int32)
    starts = np.concatenate((np.array([0], dtype=np.int64), np.cumsum(counts[:-1], dtype=np.int64)))
    positions = np.concatenate([index[int(key)] for key in keys]).astype(np.int32, copy=False) if keys.size else np.empty(0, dtype=np.int32)
    return keys, starts, counts, positions


def offsets_for_subfingerprints(
    edited_subs: np.ndarray, edit_indices: np.ndarray, keys: np.ndarray, starts: np.ndarray, counts_by_key: np.ndarray, positions: np.ndarray,
) -> np.ndarray:
    """Expand subfingerprint matches for a frame range without a per-frame Python loop."""
    if keys.size == 0 or edited_subs.size == 0:
        return np.empty(0, dtype=np.int32)
    locations = np.searchsorted(keys, edited_subs)
    valid = locations < keys.size
    valid[valid] &= keys[locations[valid]] == edited_subs[valid]
    locations = locations[valid]
    if locations.size == 0:
        return np.empty(0, dtype=np.int32)
    counts = counts_by_key[locations].astype(np.int64, copy=False)
    repeated_edits = np.repeat(edit_indices[valid], counts)
    repeated_starts = np.repeat(starts[locations], counts)
    relative = np.arange(counts.sum(), dtype=np.int64) - np.repeat(np.cumsum(counts) - counts, counts)
    return positions[repeated_starts + relative].astype(np.int32) - repeated_edits.astype(np.int32)


def window_modes(source_fingerprints: np.ndarray, edited_fingerprints: np.ndarray) -> list[dict[str, int | None]]:
    high_index = make_subfingerprint_index(source_fingerprints >> SUBFINGERPRINT_BITS)
    low_index = make_subfingerprint_index(source_fingerprints & 0xFFFF)
    high_keys, high_starts, high_counts, high_positions = source_lookup_tables(high_index)
    low_keys, low_starts, low_counts, low_positions = source_lookup_tables(low_index)
    modes: list[dict[str, int | None]] = []
    for start in range(0, edited_fingerprints.size, WINDOW_FRAMES):
        stop = min(start + WINDOW_FRAMES, edited_fingerprints.size)
        edit_indices = np.arange(start, stop, dtype=np.int32)
        words = edited_fingerprints[start:stop]
        offsets = np.concatenate((
            offsets_for_subfingerprints(words >> SUBFINGERPRINT_BITS, edit_indices, high_keys, high_starts, high_counts, high_positions),
            offsets_for_subfingerprints(words & 0xFFFF, edit_indices, low_keys, low_starts, low_counts, low_positions),
        ))
        offset: int | None = None
        if offsets.size:
            candidates, votes = np.unique(offsets, return_counts=True)
            winner = int(np.argmax(votes))
            if int(votes[winner]) >= (stop - start) * MODE_MIN_RATIO:
                offset = int(candidates[winner])
        modes.append({"start": start, "stop": stop, "offset": offset})
    return modes


def merge_runs(modes: list[dict[str, int | None]]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for mode in modes:
        offset = mode["offset"]
        if offset is None:
            continue
        if runs and mode["start"] == runs[-1]["end"] and abs(int(offset) - int(runs[-1]["last_offset"])) <= 1:
            runs[-1]["end"] = int(mode["stop"])
            runs[-1]["offsets"].append(int(offset))
            runs[-1]["last_offset"] = int(offset)
        else:
            runs.append({
                "start": int(mode["start"]), "end": int(mode["stop"]), "offsets": [int(offset)], "last_offset": int(offset),
            })
    for run in runs:
        values, counts = np.unique(np.asarray(run["offsets"], dtype=np.int32), return_counts=True)
        run["offset"] = int(values[np.argmax(counts)])
    return runs


def ber(source_fingerprints: np.ndarray, edited_fingerprints: np.ndarray, edit_indices: np.ndarray, offset: int) -> np.ndarray:
    source_indices = edit_indices + offset
    valid = (edit_indices >= 0) & (edit_indices < edited_fingerprints.size) & (source_indices >= 0) & (source_indices < source_fingerprints.size)
    result = np.ones(edit_indices.size, dtype=np.float32)
    xor = source_fingerprints[source_indices[valid]] ^ edited_fingerprints[edit_indices[valid]]
    # numpy 1.26 lacks np.bit_count; unpacking the four uint8 bytes is vectorized.
    result[valid] = np.unpackbits(xor.view(np.uint8)).reshape(-1, 4, 8).sum(axis=(1, 2), dtype=np.uint16) / FINGERPRINT_BITS
    return result


def refine_runs(runs: list[dict[str, Any]], source_fingerprints: np.ndarray, edited_fingerprints: np.ndarray) -> list[dict[str, Any]]:
    refined: list[dict[str, Any]] = []
    for index, run in enumerate(runs):
        start, end, offset = int(run["start"]), int(run["end"]), int(run["offset"])
        # These scans are frame-vectorized; locating the first failing frame does not change the prescribed rule.
        lower_bound = int(runs[index - 1]["end"]) if index else 0
        upper_bound = int(runs[index + 1]["start"]) if index + 1 < len(runs) else edited_fingerprints.size
        left_candidates = np.arange(start - 1, lower_bound - 1, -1, dtype=np.int32)
        if left_candidates.size:
            left_good = ber(source_fingerprints, edited_fingerprints, left_candidates, offset) < BER_THRESHOLD
            first_bad = np.flatnonzero(~left_good)
            start -= left_candidates.size if first_bad.size == 0 else int(first_bad[0])
        right_candidates = np.arange(end, upper_bound, dtype=np.int32)
        if right_candidates.size:
            right_good = ber(source_fingerprints, edited_fingerprints, right_candidates, offset) < BER_THRESHOLD
            first_bad = np.flatnonzero(~right_good)
            end += right_candidates.size if first_bad.size == 0 else int(first_bad[0])
        if end <= start:
            continue
        edit_indices = np.arange(start, end, dtype=np.int32)
        confidence = float(np.mean(ber(source_fingerprints, edited_fingerprints, edit_indices, offset) < BER_THRESHOLD))
        if confidence >= CONFIDENCE_MIN:
            refined.append({**run, "start": start, "end": end, "confidence": confidence})
    return refined


def refine_junctions(runs: list[dict[str, Any]], source_fingerprints: np.ndarray, edited_fingerprints: np.ndarray) -> list[dict[str, Any]]:
    """Settle the boundary between two touching runs.

    ``refine_runs`` bounds each scan by the neighbour's unrefined window edge, so two runs that
    touch (which is what every cut looks like) leave each other no candidates at all and the
    junction stays pinned to the window grid. Here the shared boundary is chosen explicitly: the
    split that maximises how many frames match their own side's offset.
    """
    for index in range(len(runs) - 1):
        left, right = runs[index], runs[index + 1]
        if int(left["end"]) != int(right["start"]):
            continue  # a real gap — both edges were already refined against it
        boundary = int(left["end"])
        low = max(int(left["start"]) + 1, boundary - JUNCTION_SEARCH_FRAMES)
        high = min(int(right["end"]) - 1, boundary + JUNCTION_SEARCH_FRAMES)
        if high <= low:
            continue
        candidates = np.arange(low, high + 1, dtype=np.int32)
        left_good = ber(source_fingerprints, edited_fingerprints, candidates, int(left["offset"])) < BER_THRESHOLD
        right_good = ber(source_fingerprints, edited_fingerprints, candidates, int(right["offset"])) < BER_THRESHOLD
        # score(k) = matches on the left side of k under the left offset + matches on the right under the right offset
        score = np.cumsum(left_good) - np.cumsum(left_good)[-1] + np.cumsum(right_good[::-1])[::-1]
        best = int(candidates[int(np.argmax(score))])
        left["end"] = best
        right["start"] = best
    settled = [run for run in runs if int(run["end"]) > int(run["start"])]
    for run in settled:
        edit_indices = np.arange(int(run["start"]), int(run["end"]), dtype=np.int32)
        run["confidence"] = float(np.mean(ber(source_fingerprints, edited_fingerprints, edit_indices, int(run["offset"])) < BER_THRESHOLD))
    return [run for run in settled if run["confidence"] >= CONFIDENCE_MIN]


def spans_from_segments(segments: list[dict[str, Any]], edited_duration: float) -> list[dict[str, float]]:
    spans: list[dict[str, float]] = []
    cursor = 0.0
    for segment in segments:
        if segment["edit_start_s"] > cursor:
            spans.append({"start_s": cursor, "end_s": segment["edit_start_s"]})
        cursor = max(cursor, segment["edit_end_s"])
    if cursor < edited_duration:
        spans.append({"start_s": cursor, "end_s": edited_duration})
    return spans


def partition_filtered_edit_spans(
    trusted_segments: list[dict[str, Any]],
    filtered_segments: list[dict[str, Any]],
    edited_duration: float,
) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
    """Split the legacy trusted-summary gaps into unmapped and filtered coverage.

    The reporting threshold intentionally excludes ``filtered_segments`` from the
    trusted metrics.  Their edit-time coverage must nevertheless not be described
    as unmapped.  Restricting the filtered spans to legacy gaps preserves the
    established trusted-summary coverage if candidates ever overlap.
    """
    trusted_gaps = spans_from_segments(trusted_segments, edited_duration)
    filtered_spans: list[dict[str, float]] = []
    for gap in trusted_gaps:
        for segment in filtered_segments:
            start_s = max(gap["start_s"], segment["edit_start_s"])
            end_s = min(gap["end_s"], segment["edit_end_s"])
            if start_s < end_s:
                filtered_spans.append({"start_s": start_s, "end_s": end_s})

    filtered_spans.sort(key=lambda span: (span["start_s"], span["end_s"]))
    coalesced_filtered: list[dict[str, float]] = []
    for span in filtered_spans:
        if coalesced_filtered and span["start_s"] <= coalesced_filtered[-1]["end_s"]:
            coalesced_filtered[-1]["end_s"] = max(coalesced_filtered[-1]["end_s"], span["end_s"])
        else:
            coalesced_filtered.append(span)

    unmapped_spans: list[dict[str, float]] = []
    for gap in trusted_gaps:
        cursor = gap["start_s"]
        for span in coalesced_filtered:
            if span["end_s"] <= cursor or span["start_s"] >= gap["end_s"]:
                continue
            if cursor < span["start_s"]:
                unmapped_spans.append({"start_s": cursor, "end_s": span["start_s"]})
            cursor = max(cursor, span["end_s"])
        if cursor < gap["end_s"]:
            unmapped_spans.append({"start_s": cursor, "end_s": gap["end_s"]})
    return unmapped_spans, coalesced_filtered


def with_summary_trust(
    segments: list[dict[str, Any]], summary_confidence_min: float = SUMMARY_CONFIDENCE_MIN,
) -> list[dict[str, Any]]:
    """Copy segments and annotate the reporting threshold without changing their matches."""
    return [
        {**segment, "trusted": float(segment["confidence"]) >= summary_confidence_min}
        for segment in segments
    ]


def summarize_segments(
    segments: list[dict[str, Any]],
    source_duration: float,
    edited_duration: float,
    summary_confidence_min: float = SUMMARY_CONFIDENCE_MIN,
    speed_change_suspected: bool = False,
) -> dict[str, Any]:
    """Summarize trusted metrics while separately reporting filtered candidates.

    Alignment candidates at the matching threshold remain in ``segments`` for auditability,
    while the higher reporting threshold excludes weak candidates from trusted derived metrics.
    Source-retention metrics coalesce trusted source-time spans, so duplicated source usage
    is reported separately from the source material retained at least once.
    """
    trusted_segments = [
        segment for segment in segments
        if float(segment["confidence"]) >= summary_confidence_min
    ]
    filtered_source_segments = [
        segment for segment in segments
        if float(segment["confidence"]) < summary_confidence_min
    ]
    filtered_segments = [
        {
            key: segment[key]
            for key in ("edit_start_s", "edit_end_s", "src_start_s", "src_end_s", "offset_s", "confidence")
        }
        for segment in filtered_source_segments
    ]
    mapped_duration = sum(item["edit_end_s"] - item["edit_start_s"] for item in trusted_segments)
    source_span_sum_s = sum(item["src_end_s"] - item["src_start_s"] for item in trusted_segments)
    source_spans = sorted(
        (item["src_start_s"], item["src_end_s"])
        for item in trusted_segments
    )
    source_union_seconds = 0.0
    if source_spans:
        merged_start, merged_end = source_spans[0]
        for start, end in source_spans[1:]:
            if start <= merged_end:
                merged_end = max(merged_end, end)
            else:
                source_union_seconds += merged_end - merged_start
                merged_start, merged_end = start, end
        source_union_seconds += merged_end - merged_start
    monotonic = all(
        trusted_segments[index]["src_start_s"] <= trusted_segments[index + 1]["src_start_s"]
        for index in range(len(trusted_segments) - 1)
    )
    monotonic_all_segments = all(
        segments[index]["src_start_s"] <= segments[index + 1]["src_start_s"]
        for index in range(len(segments) - 1)
    )
    reordering_candidates = [
        {
            "preceding_segment_index": index,
            "following_segment_index": index + 1,
            "preceding_segment": {
                key: segments[index][key]
                for key in ("edit_start_s", "edit_end_s", "src_start_s", "src_end_s")
            },
            "following_segment": {
                key: segments[index + 1][key]
                for key in ("edit_start_s", "edit_end_s", "src_start_s", "src_end_s")
            },
        }
        for index in range(len(segments) - 1)
        if segments[index]["src_start_s"] > segments[index + 1]["src_start_s"]
    ]
    unmapped_edit_spans, filtered_edit_spans = partition_filtered_edit_spans(
        trusted_segments, filtered_source_segments, edited_duration,
    )
    return {
        "edited_duration_s": edited_duration, "mapped_duration_s": mapped_duration,
        "mapped_ratio": mapped_duration / edited_duration if edited_duration else 0.0,
        "segment_count": len(trusted_segments), "cut_count": max(0, len(trusted_segments) - 1),
        "unmapped_edit_spans": unmapped_edit_spans,
        "filtered_edit_spans": filtered_edit_spans,
        "filtered_segments": filtered_segments,
        "source_span_sum_s": source_span_sum_s,
        "source_union_seconds": source_union_seconds,
        "duplicated_source_seconds": source_span_sum_s - source_union_seconds,
        "kept_ratio_of_source": source_union_seconds / source_duration if source_duration else 0.0,
        "monotonic": monotonic, "monotonic_basis": "trusted_segments",
        "monotonic_all_segments": monotonic_all_segments,
        "reordering_candidates": reordering_candidates,
        "speed_change_suspected": speed_change_suspected,
        "head_trim_s": min(item["src_start_s"] for item in trusted_segments) if trusted_segments else source_duration,
        "tail_trim_s": max(0.0, source_duration - max(item["src_end_s"] for item in trusted_segments)) if trusted_segments else source_duration,
    }


def params_payload(summary_confidence_min: float = SUMMARY_CONFIDENCE_MIN) -> dict[str, Any]:
    return {
        "sample_rate_hz": SAMPLE_RATE, "channels": CHANNELS, "sample_width_bits": SAMPLE_WIDTH_BITS,
        "window_samples": WINDOW_SAMPLES, "hop_samples": HOP_SAMPLES, "frame_rate_fps": SAMPLE_RATE / HOP_SAMPLES,
        "window_function": WINDOW_FUNCTION, "spectrum": SPECTRUM,
        "frequency_low_hz": FREQ_LOW_HZ, "frequency_high_hz": FREQ_HIGH_HZ, "log_band_count": BAND_COUNT,
        "band_edges": "numpy.geomspace(300, 3000, 34); FFT-bin centers in half-open bands, final upper edge included",
        "fingerprint_bits": FINGERPRINT_BITS, "subfingerprint_bits": SUBFINGERPRINT_BITS,
        "common_subfingerprint_max_count_exclusive": f"source_frame_count/{COMMON_SUBFINGERPRINT_DIVISOR}",
        "common_subfingerprint_divisor": COMMON_SUBFINGERPRINT_DIVISOR,
        "segmentation_window_frames": WINDOW_FRAMES, "segmentation_window_seconds": WINDOW_FRAMES * HOP_SAMPLES / SAMPLE_RATE,
        "mode_min_ratio": MODE_MIN_RATIO, "run_offset_merge_tolerance_frames": 1,
        "ber_threshold_exclusive": BER_THRESHOLD,
        "junction_search_frames": JUNCTION_SEARCH_FRAMES,
        "confidence_min": CONFIDENCE_MIN, "summary_confidence_min": summary_confidence_min,
        "speed_change_offset_delta_seconds_exclusive": SPEED_CHANGE_OFFSET_DELTA_S,
        "fft_batch_frames": FFT_BATCH_FRAMES,
    }


def parse_limit(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be a positive finite number") from exc
    if not np.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("limit must be a positive finite number")
    return parsed


def parse_summary_confidence_min(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("summary confidence minimum must be a finite number greater than 0 and at most 1") from exc
    if not np.isfinite(parsed) or not 0.0 < parsed <= 1.0:
        raise argparse.ArgumentTypeError("summary confidence minimum must be a finite number greater than 0 and at most 1")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="read-only source media")
    parser.add_argument("--edited", required=True, type=Path, help="read-only edited media")
    parser.add_argument("--out", required=True, type=Path, help="alignment JSON output")
    parser.add_argument("--limit-seconds", type=parse_limit, help="process only the first N seconds of each input")
    parser.add_argument(
        "--summary-confidence-min", type=parse_summary_confidence_min, default=SUMMARY_CONFIDENCE_MIN,
        help="exclude lower-confidence segments from summary metrics (default: %(default)s)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    for path in (args.source, args.edited):
        if not path.is_file():
            raise FileNotFoundError(path)
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise RuntimeError("ffmpeg and ffprobe are required")
    source_probe, edited_probe = ffprobe_payload(args.source), ffprobe_payload(args.edited)
    source_duration, edited_duration = duration_from_probe(source_probe), duration_from_probe(edited_probe)
    if args.limit_seconds is not None:
        source_duration = min(source_duration, args.limit_seconds)
        edited_duration = min(edited_duration, args.limit_seconds)
    started = perf_counter()
    with tempfile.TemporaryDirectory(prefix="pair-align-") as directory:
        temporary = Path(directory)
        source_wav, edited_wav = temporary / "source.wav", temporary / "edited.wav"
        decode_wav(args.source, source_wav, args.limit_seconds)
        decode_wav(args.edited, edited_wav, args.limit_seconds)
        source_fingerprints = fingerprints(pcm_samples(source_wav))
        edited_fingerprints = fingerprints(pcm_samples(edited_wav))
    if source_fingerprints.size == 0 or edited_fingerprints.size == 0:
        raise ValueError("input is too short for one 4096-sample fingerprint frame")
    runs = refine_runs(merge_runs(window_modes(source_fingerprints, edited_fingerprints)), source_fingerprints, edited_fingerprints)
    runs = refine_junctions(runs, source_fingerprints, edited_fingerprints)
    seconds_per_frame = HOP_SAMPLES / SAMPLE_RATE
    segments: list[dict[str, Any]] = []
    for run in runs:
        start, end, offset = int(run["start"]), int(run["end"]), int(run["offset"])
        segments.append({
            "edit_start_s": start * seconds_per_frame, "edit_end_s": end * seconds_per_frame,
            "src_start_s": (start + offset) * seconds_per_frame, "src_end_s": (end + offset) * seconds_per_frame,
            "offset_s": offset * seconds_per_frame, "confidence": run["confidence"], "frame_count": end - start,
        })
    segments = with_summary_trust(segments, args.summary_confidence_min)
    speed_change_suspected = any(
        abs(int(run["offsets"][-1]) - int(run["offsets"][0])) * seconds_per_frame > SPEED_CHANGE_OFFSET_DELTA_S
        for run in runs
    )
    summary = summarize_segments(
        segments, source_duration, edited_duration, args.summary_confidence_min, speed_change_suspected,
    )
    write_json(args.out, {
        "format": FORMAT,
        "source": {"path": str(args.source), "sha256": sha256_file(args.source), "duration_s": source_duration, "ffprobe": source_probe},
        "edited": {"path": str(args.edited), "sha256": sha256_file(args.edited), "duration_s": edited_duration, "ffprobe": edited_probe},
        "params": params_payload(args.summary_confidence_min), "segments": segments, "summary": summary,
        "processing_seconds": perf_counter() - started,
    })
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"align_audio.py: {exc}", file=sys.stderr)
        raise SystemExit(1)
