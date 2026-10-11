#!/usr/bin/env python3
"""Add a word-timestamped local MLX Whisper transcript to an existing Index.

This is the machine-transcript path. It deliberately does not modify
``record_transcript.py``: that script records a human decision and refuses
generated text precisely because this tool must declare the STT backend.

The normal backend is the official ``mlx_whisper`` package. By default it
uses 600-second chunks with a short trailing overlap, so an end-of-chunk model
failure is discarded from that chunk and retranscribed in the next one. The
separate backend seam is intentionally small so the contract conversion can be
tested without downloading a model or invoking ffmpeg.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import json
import math
import statistics
import subprocess
import sys
import tempfile
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable


MILLISECOND_TIMEBASE = {"num": 1, "den": 1000}
DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_CHUNK_SECONDS = "600"
DEFAULT_CHUNK_OVERLAP_SECONDS = "30"
DEFAULT_LANGUAGE = "ko"
Backend = Callable[[Path, dict[str, Any], str, str], Any]


class TranscriptionError(RuntimeError):
    """An input or backend response cannot safely become an Index transcript."""


def require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TranscriptionError(f"{label}: non-empty string required")
    return value.strip()


def require_probability(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TranscriptionError(f"{label}: model probability in 0..1 required")
    probability = float(value)
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise TranscriptionError(f"{label}: model probability must be within 0..1")
    return probability


def milliseconds_time(value: int) -> dict[str, Any]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TranscriptionError(f"millisecond time: non-negative integer required (got {value!r})")
    return {"value": value, "timebase": dict(MILLISECOND_TIMEBASE)}


def milliseconds_range(start: int, end: int, label: str) -> dict[str, Any]:
    if end <= start:
        raise TranscriptionError(f"{label}: end must be after start")
    return {"start": milliseconds_time(start), "end": milliseconds_time(end)}


def time_fraction(value: Any, label: str) -> Fraction:
    if not isinstance(value, dict) or set(value) != {"value", "timebase"}:
        raise TranscriptionError(f"{label}: canonical time object required")
    ticks, base = value.get("value"), value.get("timebase")
    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 0:
        raise TranscriptionError(f"{label}.value: non-negative integer required")
    if not isinstance(base, dict) or set(base) != {"num", "den"}:
        raise TranscriptionError(f"{label}.timebase: num/den required")
    num, den = base.get("num"), base.get("den")
    if any(isinstance(part, bool) or not isinstance(part, int) or part <= 0 for part in (num, den)):
        raise TranscriptionError(f"{label}.timebase: positive integer num/den required")
    return Fraction(ticks * num, den)


def source_for(index: dict[str, Any], source_id: str) -> dict[str, Any]:
    if not isinstance(index, dict):
        raise TranscriptionError("index: JSON object required")
    for source in index.get("sources", []):
        if isinstance(source, dict) and source.get("source_id") == source_id:
            return source
    raise TranscriptionError(f"source_id {source_id!r} is not in the Index")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def chunk_seconds_to_ms(value: str | int | float, label: str) -> int | None:
    try:
        seconds = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TranscriptionError(f"{label}: non-negative decimal seconds required") from exc
    if not seconds.is_finite() or seconds < 0:
        raise TranscriptionError(f"{label}: non-negative decimal seconds required")
    if seconds == 0:
        return None
    milliseconds = int((seconds * 1000).to_integral_value(rounding=ROUND_HALF_UP))
    if milliseconds <= 0:
        raise TranscriptionError(f"{label}: must be at least one millisecond")
    return milliseconds


def overlap_seconds_to_ms(value: str | int | float, label: str) -> int:
    """Return a non-negative millisecond overlap (unlike chunk 0, keep zero)."""
    try:
        seconds = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TranscriptionError(f"{label}: non-negative decimal seconds required") from exc
    if not seconds.is_finite() or seconds < 0:
        raise TranscriptionError(f"{label}: non-negative decimal seconds required")
    milliseconds = int((seconds * 1000).to_integral_value(rounding=ROUND_HALF_UP))
    if seconds != 0 and milliseconds <= 0:
        raise TranscriptionError(f"{label}: must be at least one millisecond or zero")
    return milliseconds


def seconds_to_ms(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TranscriptionError(f"{label}: backend seconds number required")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise TranscriptionError(f"{label}: non-negative finite backend seconds required")
    # The backend exposes float seconds. The Index never does: this is the
    # one-way conversion to its required integer millisecond rational time.
    return int(round(seconds * 1000))


def duration_ms(source: dict[str, Any]) -> int:
    duration = time_fraction(source.get("duration"), "source.duration")
    milliseconds = duration.numerator * 1000 // duration.denominator
    if milliseconds <= 0:
        raise TranscriptionError("source.duration: at least one whole millisecond required")
    return milliseconds


def chunks_for(duration: int, chunk_ms: int | None) -> list[tuple[int, int]]:
    if chunk_ms is None:
        return [(0, duration)]
    return [(start, min(start + chunk_ms, duration)) for start in range(0, duration, chunk_ms)]


def response_segments(response: Any, label: str) -> list[dict[str, Any]]:
    segments = response.get("segments") if isinstance(response, dict) else response
    if not isinstance(segments, list):
        raise TranscriptionError(f"{label}: backend response needs a segments array")
    rows: list[dict[str, Any]] = []
    for position, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise TranscriptionError(f"{label}.segments[{position}]: object required")
        rows.append(segment)
    return rows


def validate_generated_utterance(utterance: dict[str, Any], source_duration_ms: int) -> None:
    """Validate the generated-only invariants the contract checker intentionally does not relate."""
    required = {
        "utterance_id", "source_id", "range", "text", "provenance_status",
        "utterance_confidence", "word_confidence", "speaker_confidence",
        "word_timestamps_available", "words",
    }
    if set(utterance) != required:
        raise TranscriptionError("generated utterance: exact transcript/words fields required")
    require_text(utterance.get("text"), "generated utterance.text")
    if utterance.get("provenance_status") != "generated":
        raise TranscriptionError("generated utterance.provenance_status: generated required")
    if utterance.get("word_timestamps_available") is not True:
        raise TranscriptionError("generated utterance: words present but word_timestamps_available is not true")
    for field in ("utterance_confidence", "word_confidence", "speaker_confidence"):
        require_probability(utterance.get(field), f"generated utterance.{field}")
    if utterance["speaker_confidence"] != 0.0:
        raise TranscriptionError("generated utterance.speaker_confidence: 0.0 required without diarization")
    utterance_start = time_fraction(utterance.get("range", {}).get("start"), "generated utterance.range.start")
    utterance_end = time_fraction(utterance.get("range", {}).get("end"), "generated utterance.range.end")
    if utterance_start < 0 or utterance_end <= utterance_start or utterance_end > Fraction(source_duration_ms, 1000):
        raise TranscriptionError("generated utterance.range: must be non-empty and within source")
    words = utterance.get("words")
    if not isinstance(words, list) or not words:
        raise TranscriptionError("generated utterance: word_timestamps_available true requires non-empty words")
    previous_start: Fraction | None = None
    for position, word in enumerate(words):
        label = f"generated utterance.words[{position}]"
        if not isinstance(word, dict) or set(word) != {"text", "start", "end", "confidence"}:
            raise TranscriptionError(f"{label}: exact word fields required")
        require_text(word.get("text"), f"{label}.text")
        start = time_fraction(word.get("start"), f"{label}.start")
        end = time_fraction(word.get("end"), f"{label}.end")
        require_probability(word.get("confidence"), f"{label}.confidence")
        if end < start:
            raise TranscriptionError(f"{label}: end must not be before start")
        if start < utterance_start or end > utterance_end:
            raise TranscriptionError(f"{label}: falls outside its utterance range")
        if previous_start is not None and start < previous_start:
            raise TranscriptionError(f"{label}: starts before the previous word started")
        previous_start = start


def is_empty_segment_to_skip(segment: dict[str, Any]) -> bool:
    """Return whether a structurally valid empty backend segment is ignorable.

    MLX Whisper can emit a segment with an empty word list, sometimes together
    with an empty text field. It is not a generated utterance, so do not
    invent one. A non-empty word list with empty text remains on the existing
    refusal path.
    """
    text_value = segment.get("text")
    words_value = segment.get("words")
    return isinstance(text_value, str) and isinstance(words_value, list) and not words_value


def segment_to_utterance(
    segment: dict[str, Any], *, source_id: str, chunk_start_ms: int, chunk_end_ms: int,
    source_duration_ms: int, utterance_id: str, label: str,
    adopt_before_ms: int | None = None,
) -> tuple[dict[str, Any] | None, int, int]:
    if is_empty_segment_to_skip(segment):
        return None, 0, 0
    text = require_text(segment.get("text"), f"{label}.text")
    start_ms = chunk_start_ms + seconds_to_ms(segment.get("start"), f"{label}.start")
    end_ms = chunk_start_ms + seconds_to_ms(segment.get("end"), f"{label}.end")
    if start_ms < chunk_start_ms or end_ms > chunk_end_ms:
        raise TranscriptionError(f"{label}: segment time falls outside its extracted chunk")
    words_value = segment.get("words")
    if not isinstance(words_value, list) or not words_value:
        raise TranscriptionError(f"{label}: generated transcript requires non-empty word timestamps")
    words: list[dict[str, Any]] = []
    probabilities: list[float] = []
    zero_duration_words = 0
    words_dropped_in_overlap = 0
    for position, raw_word in enumerate(words_value):
        word_label = f"{label}.words[{position}]"
        if not isinstance(raw_word, dict):
            raise TranscriptionError(f"{word_label}: object required")
        word_text = require_text(raw_word.get("word"), f"{word_label}.word")
        probability = require_probability(raw_word.get("probability"), f"{word_label}.probability")
        raw_start = raw_word.get("start")
        raw_end = raw_word.get("end")
        word_start_ms = chunk_start_ms + seconds_to_ms(raw_start, f"{word_label}.start")
        word_end_ms = chunk_start_ms + seconds_to_ms(raw_end, f"{word_label}.end")
        if raw_end < raw_start or word_end_ms < word_start_ms:
            raise TranscriptionError(f"{word_label}: end must not be before start")
        word = {
            "text": word_text,
            "start": milliseconds_time(word_start_ms),
            "end": milliseconds_time(word_end_ms),
            "confidence": probability,
        }
        # A trailing overlap is extraction-only. Its words belong to the
        # following nominal chunk, where there is no artificial audio ending.
        if adopt_before_ms is not None and word_start_ms >= adopt_before_ms:
            words_dropped_in_overlap += 1
            continue
        if word_end_ms == word_start_ms:
            zero_duration_words += 1
        words.append(word)
        probabilities.append(probability)
    if not words:
        return None, 0, words_dropped_in_overlap
    # This is selection by the timestamp rule above, not a textual attempt to
    # recognize hallucinations: transcript text must not retain rejected words.
    adopted_text = text if words_dropped_in_overlap == 0 else " ".join(word["text"] for word in words)
    word_start_ms = min(word["start"]["value"] for word in words)
    word_end_ms = max(word["end"]["value"] for word in words)
    utterance = {
        "utterance_id": utterance_id,
        "source_id": source_id,
        "range": milliseconds_range(word_start_ms, word_end_ms, label),
        "text": adopted_text,
        "provenance_status": "generated",
        "utterance_confidence": statistics.fmean(probabilities),
        "word_confidence": min(probabilities),
        "speaker_confidence": 0.0,
        "word_timestamps_available": True,
        "words": words,
    }
    validate_generated_utterance(utterance, source_duration_ms)
    return utterance, zero_duration_words, words_dropped_in_overlap


def overlapping(left: tuple[Fraction, Fraction], right: tuple[Fraction, Fraction]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def ensure_chunk_ranges_do_not_overlap(index: dict[str, Any], proposed: list[tuple[int, int]]) -> None:
    existing = index.get("transcription_chunks", [])
    if not isinstance(existing, list):
        raise TranscriptionError("index.transcription_chunks: array required when present")
    new_ranges = [(Fraction(start, 1000), Fraction(end, 1000)) for start, end in proposed]
    for position, chunk in enumerate(existing):
        if not isinstance(chunk, dict):
            raise TranscriptionError(f"index.transcription_chunks[{position}]: object required")
        original = chunk.get("original_range")
        if not isinstance(original, dict):
            raise TranscriptionError(f"index.transcription_chunks[{position}].original_range: range required")
        old_range = (time_fraction(original.get("start"), "existing chunk.start"),
                     time_fraction(original.get("end"), "existing chunk.end"))
        if old_range[1] <= old_range[0]:
            raise TranscriptionError(f"index.transcription_chunks[{position}]: non-empty range required")
        if any(overlapping(old_range, fresh) for fresh in new_ranges):
            raise TranscriptionError("existing transcription_chunks overlap this STT run; refusing ambiguous reassembly")


def unique_id(prefix: str, used: set[str]) -> str:
    number = 1
    while f"{prefix}-{number:04d}" in used:
        number += 1
    result = f"{prefix}-{number:04d}"
    used.add(result)
    return result


def extract_chunk(media: Path, start_ms: int, end_ms: int, destination: Path) -> None:
    command = [
        "ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start_ms / 1000:.3f}",
        "-i", str(media), "-t", f"{(end_ms - start_ms) / 1000:.3f}", "-vn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(destination),
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise TranscriptionError(f"ffmpeg chunk extraction failed to start: {exc}") from exc
    if completed.returncode != 0 or not destination.is_file() or destination.stat().st_size == 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise TranscriptionError(f"ffmpeg chunk extraction failed: {detail or 'no output produced'}")


def mlx_whisper_backend(chunk_media: Path, _chunk: dict[str, Any], model: str, language: str) -> Any:
    try:
        import mlx_whisper # type: ignore[import-not-found]
    except ImportError as exc:
        raise TranscriptionError("mlx-whisper is not installed locally") from exc
    # No batch size is exposed here. One extracted file is submitted per call,
    # avoiding batch_size>1 seek-offset defect.
    return mlx_whisper.transcribe(str(chunk_media), path_or_hf_repo=model,
                                  word_timestamps=True, language=language)


def installed_backend_version() -> str:
    try:
        return importlib.metadata.version("mlx-whisper")
    except importlib.metadata.PackageNotFoundError as exc:
        raise TranscriptionError("mlx-whisper package version is unavailable") from exc


def transcribe(
    index: dict[str, Any], source_id: str, media: Path, *, model: str = DEFAULT_MODEL,
    chunk_seconds: str | int | float = DEFAULT_CHUNK_SECONDS, language: str = DEFAULT_LANGUAGE,
    chunk_overlap_seconds: str | int | float = DEFAULT_CHUNK_OVERLAP_SECONDS,
    backend: Backend | None = None, backend_version: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a copied Index and report; an injected backend avoids model work in tests."""
    source = source_for(index, source_id)
    if not media.is_file():
        raise TranscriptionError(f"media: file does not exist: {media}")
    expected_sha = source.get("sha256")
    if not isinstance(expected_sha, str) or sha256_file(media) != expected_sha:
        raise TranscriptionError("media SHA-256 does not match Index source.sha256")
    require_text(model, "model")
    require_text(language, "language")
    source_duration_ms = duration_ms(source)
    chunk_ms = chunk_seconds_to_ms(chunk_seconds, "chunk-seconds")
    overlap_ms = overlap_seconds_to_ms(chunk_overlap_seconds, "chunk-overlap-seconds")
    ranges = chunks_for(source_duration_ms, chunk_ms)
    ensure_chunk_ranges_do_not_overlap(index, ranges)
    version = backend_version if backend_version is not None else installed_backend_version()
    require_text(version, "backend version")

    out = copy.deepcopy(index)
    transcript = out.setdefault("transcript", [])
    evidence = out.setdefault("evidence", [])
    chunks = out.setdefault("transcription_chunks", [])
    selections = out.setdefault("backend_selections", [])
    if not all(isinstance(value, list) for value in (transcript, evidence, chunks, selections)):
        raise TranscriptionError("index transcript/evidence/transcription_chunks/backend_selections must be arrays")
    utterance_ids = {row.get("utterance_id") for row in transcript if isinstance(row, dict) and isinstance(row.get("utterance_id"), str)}
    evidence_ids = {row.get("evidence_id") for row in evidence if isinstance(row, dict) and isinstance(row.get("evidence_id"), str)}
    chunk_ids = {row.get("chunk_id") for row in chunks if isinstance(row, dict) and isinstance(row.get("chunk_id"), str)}
    selected_backend: Backend = backend or mlx_whisper_backend
    started = time.monotonic()

    new_utterances: list[dict[str, Any]] = []
    segments_seen = 0
    empty_segments_skipped = 0
    zero_duration_words = 0
    words_dropped_in_overlap = 0
    previous_word_start: Fraction | None = None
    with tempfile.TemporaryDirectory(prefix="draftcut-stt-") as temporary_directory:
        temporary = Path(temporary_directory)
        for ordinal, (start_ms, end_ms) in enumerate(ranges, start=1):
            chunk_label = f"chunk {ordinal}"
            chunk_range = milliseconds_range(start_ms, end_ms, chunk_label)
            extracted_end_ms = min(end_ms + overlap_ms, source_duration_ms)
            extracted_range = milliseconds_range(start_ms, extracted_end_ms, f"{chunk_label} extraction")
            chunk_info = {"ordinal": ordinal, "original_range": chunk_range,
                          "extracted_range": extracted_range,
                          "actual_offset": milliseconds_time(start_ms)}
            if backend is None and chunk_ms is not None:
                chunk_media = temporary / f"chunk-{ordinal:04d}.wav"
                extract_chunk(media, start_ms, extracted_end_ms, chunk_media)
                response = selected_backend(chunk_media, chunk_info, model, language)
            else:
                response = selected_backend(media, chunk_info, model, language)
            for segment_position, segment in enumerate(response_segments(response, chunk_label), start=1):
                segments_seen += 1
                if is_empty_segment_to_skip(segment):
                    empty_segments_skipped += 1
                    continue
                utterance_id = unique_id(f"stt-{source_id}-utterance", utterance_ids)
                utterance, zero_words, dropped_words = segment_to_utterance(
                    segment, source_id=source_id, chunk_start_ms=start_ms, chunk_end_ms=extracted_end_ms,
                    source_duration_ms=source_duration_ms, utterance_id=utterance_id,
                    label=f"{chunk_label}.segments[{segment_position - 1}]",
                    adopt_before_ms=end_ms if extracted_end_ms > end_ms else None,
                )
                words_dropped_in_overlap += dropped_words
                if utterance is None:
                    continue
                zero_duration_words += zero_words
                # A valid word list must progress over the complete transcript,
                # not merely inside each model segment.
                for word in utterance["words"]:
                    word_start = time_fraction(word["start"], "generated word.start")
                    if previous_word_start is not None and word_start < previous_word_start:
                        raise TranscriptionError("generated word timestamps are not monotonically increasing")
                    previous_word_start = word_start
                transcript.append(utterance)
                new_utterances.append(utterance)
            evidence_id = unique_id(f"stt-{source_id}-evidence", evidence_ids)
            chunk_id = unique_id(f"stt-{source_id}-chunk", chunk_ids)
            evidence.append({
                "evidence_id": evidence_id, "kind": "signal", "source_id": source_id,
                "range": chunk_range,
                "provenance": {
                    "tool": f"mlx-whisper/{model}", "version": version,
                    "rationale": f"word-timestamp STT chunk {ordinal} reassembly evidence",
                },
            })
            chunks.append({
                "chunk_id": chunk_id, "original_range": chunk_range,
                "actual_offset": milliseconds_time(start_ms),
                "reassembly_evidence_ids": [evidence_id],
            })

    if segments_seen and empty_segments_skipped == segments_seen:
        raise TranscriptionError("all backend segments were empty; refusing zero-utterance transcript")

    elapsed = time.monotonic() - started
    all_words = [word for utterance in new_utterances
                 for word in utterance.get("words", []) if isinstance(word, dict)]
    word_starts = [time_fraction(word["start"], "report word.start") for word in all_words]
    word_ends = [time_fraction(word["end"], "report word.end") for word in all_words]
    gaps = [float(next_start - previous_end) for previous_end, next_start in zip(word_ends, word_starts[1:])]
    near_grid = sum(1 for start in word_starts if abs(float(start / 30) - round(float(start / 30))) <= (0.25 / 30))
    report = {
        "schema_version": "draftcut-transcript-stt-report/v1",
        "index_id": out.get("index_id"), "source_id": source_id,
        "backend": "mlx-whisper", "model": model, "backend_version": version,
        "elapsed_seconds": elapsed,
        "utterance_count": len(new_utterances),
        "word_count": len(all_words),
        "empty_segments_skipped": empty_segments_skipped,
        "chunk_overlap_seconds": overlap_ms / 1000,
        "words_dropped_in_overlap": words_dropped_in_overlap,
        "last_chunk_tail_limit": (
            "the last chunk reaches the source end and has no trailing overlap; "
            "an end-boundary hallucination can remain"
        ),
        "zero_duration_words": zero_duration_words,
        "zero_duration_policy": "zero-duration words are preserved; text preserved",
        "word_gap_median_seconds": statistics.median(gaps) if gaps else None,
        "words_near_30_second_grid_ratio": near_grid / len(word_starts) if word_starts else 0.0,
        "word_grid_definition": "word start within 30-second multiple plus or minus 0.25 seconds",
        "speaker_diarization": "not performed",
        "accuracy_limit": "accuracy is not judged",
    }
    selections.append({"purpose": "stt", "backend_id": f"mlx-whisper/{model}",
                       "backend_version": version, "cost_tier": "무료-로컬"})
    return out, report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--media", required=True, type=Path)
    parser.add_argument("--out-index", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Local model path or Hugging Face model repository (default: %(default)s).")
    parser.add_argument("--chunk-seconds", default=DEFAULT_CHUNK_SECONDS,
                        help="Nominal chunk length in seconds (default: %(default)s); use 0 to transcribe the original source in one pass.")
    parser.add_argument("--chunk-overlap-seconds", default=DEFAULT_CHUNK_OVERLAP_SECONDS,
                        help="trailing audio overlap per chunk (default: 30; 0 disables overlap)")
    parser.add_argument("--language", default=DEFAULT_LANGUAGE, help="Transcription language code (default: %(default)s, Korean); use en for English.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        index = json.loads(args.index.read_text(encoding="utf-8"))
        out, report = transcribe(index, args.source_id, args.media, model=args.model,
                                 chunk_seconds=args.chunk_seconds,
                                 chunk_overlap_seconds=args.chunk_overlap_seconds,
                                 language=args.language)
    except (TranscriptionError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        print(f"STT TRANSCRIPTION ERROR: {exc}", file=sys.stderr)
        return 2
    args.out_index.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.out_index.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"status": "transcribed", "index": str(args.out_index), "report": str(args.report),
                      "utterance_count": report["utterance_count"], "word_count": report["word_count"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
