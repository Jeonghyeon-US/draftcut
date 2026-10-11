#!/usr/bin/env python3
"""Locate sensitive strings on screen and report a box from the first matching pattern to the end of the recognized line.

Two things this does that a shape or colour search cannot.

⛔ **It asks Vision where the string is instead of guessing from appearance.**
A search by fill colour or width finds some occurrences and misses others that
sit on a same-coloured background. A partial success is worse than a failure: it
looks done.

⛔ **It reports the box from the match onward, not of the whole line.**
`VNRecognizedText.boundingBoxForRange:` gives the box for a character range, and
this tool asks for the range from the START of the first matching pattern to the
END of the recognised line (one match per line). Text before the match is left
out; text after it is included. Write the pattern so the match starts where the
part to cover starts.

Run this from a normal macOS shell, never from a sandboxed or restricted
environment: Vision returns `ok=False`, `err=None` and no observations there,
which is indistinguishable from "nothing on screen". This script raises instead.

    python3 skills/mask-plan/locate_sensitive.py \
      --frames-dir scratch/run/frames --width 1920 --height 1080 \
      --pattern 'PRIVATE' \
      --output scratch/run/sensitive.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

FRAME_PTS = re.compile(r"pts-(\d+)\.png$")


def recognise(png: Path, patterns: list[re.Pattern], width: int, height: int) -> list[dict]:
    from Foundation import NSURL, NSRange
    from Vision import VNImageRequestHandler, VNRecognizeTextRequest

    request = VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLanguages_(["ko-KR", "en-US"])
    handler = VNImageRequestHandler.alloc().initWithURL_options_(
        NSURL.fileURLWithPath_(str(png)), None)
    ok, err = handler.performRequests_error_([request], None)
    if not ok:
        raise RuntimeError(f"Vision request failed on {png} (ok=False, err={err!r})")

    hits = []
    for observation in (request.results() or []):
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        candidate = candidates[0]
        text = str(candidate.string())
        match = next((m for m in (p.search(text) for p in patterns) if m), None)
        if match is None:
            continue
        # The box of the matched range, not of the line.
        result = candidate.boundingBoxForRange_error_(
            NSRange(match.start(), len(text) - match.start()), None)
        rectangle = result[0] if isinstance(result, tuple) else result
        if rectangle is None:
            raise RuntimeError(f"no substring box for {text!r} in {png}")
        box = rectangle.boundingBox()
        x0 = box.origin.x * width
        x1 = (box.origin.x + box.size.width) * width
        # Vision's origin is bottom-left; the plan's is top-left.
        y1 = (1.0 - box.origin.y) * height
        y0 = (1.0 - box.origin.y - box.size.height) * height
        hits.append({"text": text, "match": text[match.start():],
                     "box": [round(x0), round(x1), round(y0), round(y1)]})
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--frames-dir", required=True, type=Path,
                        help="frames named sample-*-pts-<ticks>.png, at source resolution")
    parser.add_argument("--width", required=True, type=int)
    parser.add_argument("--height", required=True, type=int)
    parser.add_argument("--pattern", required=True, action="append",
                        help="Repeatable regular expression; report a box from the first matching pattern to the end of the recognized line.")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    patterns = [re.compile(p) for p in args.pattern]
    frames = sorted(args.frames_dir.glob("sample-*.png"))
    if not frames:
        raise SystemExit(f"no frames in {args.frames_dir}")

    found = []
    for png in frames:
        matched = FRAME_PTS.search(png.name)
        if not matched:
            raise SystemExit(f"frame name carries no pts: {png.name}")
        found.append({"pts": int(matched.group(1)),
                      "hits": recognise(png, patterns, args.width, args.height)})

    args.output.write_text(json.dumps(found, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"frames={len(found)} with_match={sum(1 for f in found if f['hits'])} "
          f"boxes={sum(len(f['hits']) for f in found)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
