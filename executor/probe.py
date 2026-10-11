#!/usr/bin/env python3
"""Small, lossless subprocess wrappers for Draftcut ffmpeg evidence probes."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence


# Resolved from PATH so the tools run off a non-Homebrew install too; the
# Homebrew location stays as the last resort for a non-login shell.
DEFAULT_FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
DEFAULT_FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
UNAVAILABLE_LITERALS = frozenset(("unknown", "N/A"))


def ffprobe_path() -> str:
    """Return the configured ffprobe binary without inspecting the environment value."""
    return os.environ.get("DRAFTCUT_FFPROBE", DEFAULT_FFPROBE)


def ffmpeg_path() -> str:
    """Return the configured ffmpeg binary without inspecting the environment value."""
    return os.environ.get("DRAFTCUT_FFMPEG", DEFAULT_FFMPEG)


def command_string(arguments: Sequence[object]) -> str:
    """Render an argv vector as a reproducible, shell-quoted command string."""
    return shlex.join([str(argument) for argument in arguments])


def run_command(arguments: Sequence[object]) -> dict[str, Any]:
    """Run argv without a shell and retain complete stdout and stderr."""
    argv = [str(argument) for argument in arguments]
    try:
        completed = subprocess.run(
            argv,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as exc:
        return {
            "argv": argv,
            "command": command_string(argv),
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
            "execution_error": type(exc).__name__,
        }
    return {
        "argv": argv,
        "command": command_string(argv),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "execution_error": None,
    }


def probe_format_name(input_path: str | Path) -> dict[str, Any]:
    """Observe the demuxer's format names without changing any render command."""
    return run_command([
        ffprobe_path(), "-v", "error", "-show_entries", "format=format_name",
        "-of", "default=nw=1:nk=1", str(input_path),
    ])


def probe_json(arguments: Sequence[object], input_path: str | Path) -> dict[str, Any]:
    """Run one ffprobe JSON observation and preserve raw output and parse state."""
    argv = [ffprobe_path(), *[str(argument) for argument in arguments], str(input_path)]
    result = run_command(argv)
    result["json"] = None
    result["json_error"] = None
    if result["returncode"] == 0:
        try:
            result["json"] = json.loads(result["stdout"])
        except (json.JSONDecodeError, TypeError) as exc:
            result["json_error"] = str(exc)
    return result


def field_state(record: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Distinguish a value, an explicit unavailable value, and an absent key.

    Empty strings and null are retained as explicit unavailable values.  Callers
    decide whether a condition treats that state as failure or unevaluable.
    """
    if key not in record:
        return {"state": "missing", "value": None}
    value = record[key]
    if value is None or value == "" or (isinstance(value, str) and value in UNAVAILABLE_LITERALS):
        return {"state": "unavailable", "value": value}
    return {"state": "value", "value": value}


def tool_version(binary: str) -> dict[str, Any]:
    """Return the version command, first line, and its complete raw output."""
    result = run_command([binary, "-version"])
    first_line = result["stdout"].splitlines()[0] if result["stdout"].splitlines() else None
    return {**result, "version_string": first_line}


def tool_versions() -> dict[str, dict[str, Any]]:
    """Return independently reproducible ffmpeg and ffprobe version evidence."""
    return {
        "ffmpeg": tool_version(ffmpeg_path()),
        "ffprobe": tool_version(ffprobe_path()),
    }
