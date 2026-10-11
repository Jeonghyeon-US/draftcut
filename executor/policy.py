#!/usr/bin/env python3
"""Exact mappings between EditPlan output policy values and ffprobe values."""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Mapping


POLICY_TO_PROBE = {
    "width": "width",
    "height": "height",
    "pixel_format": "pix_fmt",
    "sar": "sample_aspect_ratio",
    "frame_rate": ("r_frame_rate", "avg_frame_rate"),
    "encoder_timebase": "time_base",
    "color_primaries": "color_primaries",
    "color_transfer": "color_transfer",
    "color_matrix": "color_space",
    "color_range": "color_range",
}


class RationalValueError(ValueError):
    """Raised when a contract or probe rational is unavailable or malformed."""


def contract_fraction(value: Mapping[str, Any]) -> Fraction:
    """Parse a contract {num, den} rational without a floating-point conversion."""
    if not isinstance(value, Mapping) or set(value) != {"num", "den"}:
        raise RationalValueError("contract rational must contain exactly num and den")
    numerator = value["num"]
    denominator = value["den"]
    if isinstance(numerator, bool) or not isinstance(numerator, int):
        raise RationalValueError("rational numerator must be an integer")
    if isinstance(denominator, bool) or not isinstance(denominator, int) or denominator <= 0:
        raise RationalValueError("rational denominator must be a positive integer")
    return Fraction(numerator, denominator)


def probe_fraction(value: Any, separator: str = "/") -> Fraction:
    """Parse an ffprobe rational string exactly, rejecting unavailable SAR values."""
    if not isinstance(value, str) or value in ("", "unknown", "N/A"):
        raise RationalValueError(f"probe rational is unavailable: {value!r}")
    if separator not in value:
        raise RationalValueError(f"probe rational lacks {separator!r}: {value!r}")
    parts = value.split(separator)
    if len(parts) != 2:
        raise RationalValueError(f"probe rational is malformed: {value!r}")
    try:
        numerator, denominator = (int(part) for part in parts)
    except ValueError as exc:
        raise RationalValueError(f"probe rational is not integral: {value!r}") from exc
    if denominator <= 0 or (separator == ":" and numerator == 0):
        raise RationalValueError(f"probe rational denotes no value: {value!r}")
    return Fraction(numerator, denominator)


def ffmpeg_rational(value: Mapping[str, Any], separator: str = "/") -> str:
    """Format the exact contract spelling for an ffmpeg rational argument."""
    fraction = contract_fraction(value)
    return f"{fraction.numerator}{separator}{fraction.denominator}"


def compare_rational(
    policy_value: Mapping[str, Any], probe_value: Any, *, separator: str
) -> dict[str, Any]:
    """Compare contract and probe rationals while retaining exact operands."""
    try:
        expected = contract_fraction(policy_value)
        actual = probe_fraction(probe_value, separator)
    except RationalValueError as exc:
        return {
            "status": "unevaluable",
            "expected": dict(policy_value) if isinstance(policy_value, Mapping) else policy_value,
            "actual": probe_value,
            "reason": str(exc),
        }
    return {
        "status": "pass" if actual == expected else "fail",
        "expected": {"num": expected.numerator, "den": expected.denominator},
        "actual": {"num": actual.numerator, "den": actual.denominator},
    }

