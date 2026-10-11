#!/usr/bin/env python3
"""Report whether Draftcut can run here.  Standard library only.

    python3 check_env.py           # human-readable table
    python3 check_env.py --json    # machine-readable, for an agent

Anything missing is printed together with the command that installs it.
The exit code reflects required items only: 0 means the required tools were found.
It does not check package versions, codec/filter support, media, fonts or plan validity.

This script installs NOTHING.  It only reports what is missing and how to get it.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys

MIN_PYTHON = (3, 10)


def have_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def tool_version(exe: str) -> str | None:
    path = os.environ.get("DRAFTCUT_" + exe.upper()) or shutil.which(exe)
    if not path:
        return None
    try:
        out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return out.splitlines()[0] if out else path


def brew_or_note() -> str:
    if platform.system() == "Darwin":
        return "brew install ffmpeg"
    return "Linux: apt install ffmpeg (or your distribution's package manager)"


def collect() -> list[dict]:
    py_ok = sys.version_info >= MIN_PYTHON
    mac = platform.system() == "Darwin"
    rows: list[dict] = [
        {
            "id": "python", "required": True, "ok": py_ok,
            "found": platform.python_version(),
            "need": f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer",
            "why": "uses X | None type syntax",
            "install": "install a newer Python from python.org or pyenv",
        },
        {
            "id": "numpy", "required": True, "ok": have_module("numpy"),
            "found": None, "need": "numpy>=1.24",
            "why": "audio and signal analysis",
            "install": f"{sys.executable} -m pip install -r requirements.txt",
        },
        {
            "id": "Pillow", "required": True, "ok": have_module("PIL"),
            "found": None, "need": "Pillow>=10.0",
            "why": "draws caption and overlay PNGs",
            "install": f"{sys.executable} -m pip install -r requirements.txt",
        },
    ]
    for exe, why in (("ffmpeg", "does the actual encoding"), ("ffprobe", "reads the footage and inspects results")):
        v = tool_version(exe)
        rows.append({
            "id": exe, "required": True, "ok": v is not None, "found": v,
            "need": "on PATH, or set DRAFTCUT_" + exe.upper(),
            "why": why, "install": brew_or_note(),
        })
    rows.append({
        "id": "pyobjc(Vision)", "required": False, "ok": have_module("Vision") and have_module("Foundation"),
        "found": None, "need": "macOS only",
        "why": "On-screen text recognition (OCR) requires macOS and PyObjC Vision; used by ocr_vision and locate_sensitive.",
        "install": (f"{sys.executable} -m pip install pyobjc-framework-Vision" if mac
                    else "OCR requires macOS."),
    })
    rows.append({
        "id": "mlx-whisper", "required": False, "ok": have_module("mlx_whisper"),
        "found": None, "need": "Apple Silicon Mac only",
        "why": "Local MLX speech transcription requires an Apple Silicon Mac; supply a reviewed transcript on unsupported platforms.",
        "install": (f"Apple Silicon Mac only: {sys.executable} -m pip install mlx-whisper" if mac
                    else "Requires an Apple Silicon Mac; supply a reviewed transcript on unsupported platforms."),
    })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Check the Draftcut runtime environment")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args()

    rows = collect()
    missing_required = [r for r in rows if r["required"] and not r["ok"]]
    missing_optional = [r for r in rows if not r["required"] and not r["ok"]]

    if a.json:
        print(json.dumps({
            "ready": not missing_required,
            "platform": platform.platform(),
            "checks": rows,
            "install_commands": sorted({r["install"] for r in missing_required}),
            "optional_install_commands": sorted({r["install"] for r in missing_optional}),
        }, ensure_ascii=False, indent=2))
        return 0 if not missing_required else 1

    width = max(len(r["id"]) for r in rows)
    print("Required")
    for r in rows:
        if not r["required"]:
            continue
        mark = "  ✓" if r["ok"] else "  ✗"
        found = f"  ({r['found']})" if r["found"] else ""
        print(f"{mark} {r['id']:<{width}}{found}")
    print("Optional")
    for r in rows:
        if r["required"]:
            continue
        print(f"{'  ✓' if r['ok'] else '  –'} {r['id']:<{width}}  {r['why']}")

    if not missing_required:
        print("\nRequired tools found.")
    else:
        print(f"\nMissing {len(missing_required)} required item(s). Install with:")
        for c in sorted({r["install"] for r in missing_required}):
            print(f"    {c}")
    if missing_optional:
        print("\nOptional features and platform requirements:")
        for r in missing_optional:
            print(f"    {r['install']}\n      → {r['why']}")
    return 0 if not missing_required else 1


if __name__ == "__main__":
    raise SystemExit(main())
