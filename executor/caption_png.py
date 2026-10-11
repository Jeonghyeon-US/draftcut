#!/usr/bin/env python3
"""Render a deterministic RGBA caption PNG for ffmpeg's overlay filter."""
import argparse
import hashlib
import json
import os
import sys
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


STYLE_FIELDS = {
    "style_id", "font_family", "font_asset_id", "font_size_px", "line_gap_px",
    "alignment", "fill_rgba", "background_rgba", "padding_px",
    "corner_radius_px", "max_width_ratio",
}
ASSET_FIELDS = {
    "asset_id", "sha256", "rights_status", "usage_scope", "source_class",
    "approval_status",
}
PROBE_FONT_CANDIDATES = (
    # macOS
    Path("/System/Library/Fonts/AppleSDGothicNeo.ttc"),
    # Linux
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJKkr-Regular.otf"),
    Path("/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf"),
    Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
    # Windows
    Path("C:/Windows/Fonts/malgun.ttf"),
    Path("C:/Windows/Fonts/gulim.ttc"),
)


def parse_rgba(value):
    """Accept #RRGGBB, #RRGGBBAA, or comma-separated R,G,B[,A]."""
    raw = value.strip()
    if raw.startswith("#"):
        hex_value = raw[1:]
        if len(hex_value) == 6:
            hex_value += "ff"
        if len(hex_value) != 8:
            raise argparse.ArgumentTypeError("colour must be #RRGGBB or #RRGGBBAA")
        try:
            return tuple(int(hex_value[index:index + 2], 16) for index in range(0, 8, 2))
        except ValueError as exc:
            raise argparse.ArgumentTypeError("colour contains non-hexadecimal digits") from exc
    try:
        values = tuple(int(item.strip()) for item in raw.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("colour must contain integer channels") from exc
    if len(values) == 3:
        values += (255,)
    if len(values) != 4 or any(channel < 0 or channel > 255 for channel in values):
        raise argparse.ArgumentTypeError("colour must be R,G,B[,A] with channels from 0 to 255")
    return values


def parse_ratio(value):
    """Parse a CLI ratio exactly, without passing through binary64."""
    try:
        return Fraction(value)
    except (ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError("max-ratio must be an exact rational or decimal") from exc


def load_json(path, label):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label} JSON: {path}: {exc}") from exc


def exact_object(value, fields, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    missing = sorted(fields - value.keys())
    extra = sorted(value.keys() - fields)
    if missing or extra:
        raise ValueError(f"{label} fields mismatch: missing={missing}, extra={extra}")
    return value


def string_value(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def integer_value(value, label, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        requirement = "a positive integer" if positive else "a non-negative integer"
        raise ValueError(f"{label} must be {requirement}")
    return value


def rgba_object(value, label):
    value = exact_object(value, {"r", "g", "b", "a"}, label)
    channels = tuple(value[channel] for channel in ("r", "g", "b", "a"))
    if any(type(channel) is not int or channel < 0 or channel > 255 for channel in channels):
        raise ValueError(f"{label} channels must be integers from 0 to 255")
    return channels


def rational_object(value, label):
    value = exact_object(value, {"num", "den"}, label)
    numerator = value["num"]
    denominator = value["den"]
    if type(numerator) is not int or type(denominator) is not int or denominator <= 0:
        raise ValueError(f"{label} must contain integer num and positive integer den")
    return Fraction(numerator, denominator)


def sha256_file(path):
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeError(f"cannot read resolved font asset: {path}: {exc}") from exc
    return digest.hexdigest()


def resolve_probe_font(cli_font):
    """Resolve a usable font for executability probes and direct CLI calls."""
    if cli_font:
        return Path(cli_font).expanduser()

    environment_font = os.environ.get("DRAFTCUT_FONT_PATH")
    if environment_font:
        return Path(environment_font).expanduser()

    for candidate in PROBE_FONT_CANDIDATES:
        if candidate.is_file():
            return candidate

    searched = "\n".join(f"  - {candidate}" for candidate in PROBE_FONT_CANDIDATES)
    raise ValueError(
        "probe/direct-call mode could not find a font; searched candidates in order:\n"
        f"{searched}"
    )


def resolve_contract_style(style_path, assets_path, asset_map_path):
    """Validate one caption style and bind its font asset to a verified local file."""
    style = exact_object(load_json(style_path, "caption style"), STYLE_FIELDS, "caption style")
    style_id = string_value(style["style_id"], "caption style.style_id")
    font_family = string_value(style["font_family"], "caption style.font_family")
    font_asset_id = string_value(style["font_asset_id"], "caption style.font_asset_id")
    size = integer_value(style["font_size_px"], "caption style.font_size_px", positive=True)
    line_gap = integer_value(style["line_gap_px"], "caption style.line_gap_px")
    if style["alignment"] != "center":
        raise ValueError("caption style.alignment must be 'center'")
    fg = rgba_object(style["fill_rgba"], "caption style.fill_rgba")
    bg = rgba_object(style["background_rgba"], "caption style.background_rgba")
    padding = exact_object(style["padding_px"], {"x", "y"}, "caption style.padding_px")
    pad_x = integer_value(padding["x"], "caption style.padding_px.x")
    pad_y = integer_value(padding["y"], "caption style.padding_px.y")
    radius = integer_value(style["corner_radius_px"], "caption style.corner_radius_px")
    max_ratio = rational_object(style["max_width_ratio"], "caption style.max_width_ratio")

    assets = load_json(assets_path, "assets")
    if not isinstance(assets, list):
        raise ValueError("assets must be a JSON array")
    matches = [asset for asset in assets
               if isinstance(asset, dict) and asset.get("asset_id") == font_asset_id]
    if len(matches) != 1:
        raise ValueError(f"font_asset_id {font_asset_id!r} must resolve to exactly one assets[] record")
    asset = exact_object(matches[0], ASSET_FIELDS, f"asset {font_asset_id!r}")
    for field in ("asset_id", "rights_status", "usage_scope"):
        string_value(asset[field], f"asset {font_asset_id!r}.{field}")
    if asset["source_class"] not in {"project_provided", "external_candidate"}:
        raise ValueError(f"asset {font_asset_id!r}.source_class is not permitted")
    if asset["approval_status"] != "approved":
        raise ValueError(f"font asset {font_asset_id!r} is not approved")
    expected_sha256 = asset["sha256"]
    if (not isinstance(expected_sha256, str) or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)):
        raise ValueError(f"asset {font_asset_id!r}.sha256 must be 64 lowercase hexadecimal characters")

    asset_map = load_json(asset_map_path, "asset map")
    if not isinstance(asset_map, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                              for key, value in asset_map.items()):
        raise ValueError("asset map must be a JSON object of asset_id to file path strings")
    if font_asset_id not in asset_map:
        raise ValueError(f"asset map has no path for font_asset_id {font_asset_id!r}")
    font_path = Path(asset_map[font_asset_id]).expanduser()
    if not font_path.is_absolute():
        font_path = asset_map_path.parent / font_path
    font_path = font_path.resolve()
    if not font_path.is_file():
        raise ValueError(f"resolved font asset is not a file: {font_path}")
    actual_sha256 = sha256_file(font_path)
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"font asset {font_asset_id!r} SHA-256 mismatch: "
            f"expected {expected_sha256}, got {actual_sha256}"
        )

    return {
        "style_id": style_id,
        "font_family": font_family,
        "font_asset_id": font_asset_id,
        "font_path": font_path,
        "size": size,
        "line_gap": line_gap,
        "max_ratio": max_ratio,
        "pad_x": pad_x,
        "pad_y": pad_y,
        "radius": radius,
        "bg": bg,
        "fg": fg,
        "font_sha256": actual_sha256,
    }


def bitmap(font, character):
    mask = font.getmask(character)
    return mask.size, bytes(mask)


def missing_glyphs(font, text):
    """Return non-whitespace characters rendered as this font's PUA tofu glyph.

    A missing character must be compared with an unsupported private-use glyph;
    a missing glyph has a non-empty tofu mask, so getbbox() is not a valid test.
    """
    tofu = bitmap(font, "\ue000")
    return "".join(sorted({character for character in text if not character.isspace()
                           and bitmap(font, character) == tofu}))


def wrap_words(draw, text, font, max_pixels):
    """Wrap at words only; an overlong individual word remains uncut."""
    lines = []
    for paragraph in text.splitlines() or [text]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if not current or draw.textlength(candidate, font=font) <= max_pixels:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines or [""]


def render_caption(*, text, out_path, width, size, line_gap, font_path, max_ratio,
                   pad_x, pad_y, radius, bg, fg, allow_missing_glyphs,
                   contract_metadata=None):
    if width <= 0 or size <= 0 or line_gap < 0 or pad_x < 0 or pad_y < 0 or radius < 0:
        raise ValueError(
            "width and size must be positive; line gap, padding, and radius cannot be negative"
        )
    if not 0 < max_ratio <= 1:
        raise ValueError("max-ratio must be greater than 0 and no more than 1")
    try:
        font = ImageFont.truetype(font_path, size)
    except OSError as exc:
        raise RuntimeError(f"cannot load font: {font_path}: {exc}") from exc

    missing = missing_glyphs(font, text)
    if missing and not allow_missing_glyphs:
        print(f"missing glyphs: {missing}", file=sys.stderr)
        return None, 3
    if missing:
        print(f"warning: missing glyphs allowed: {missing}", file=sys.stderr)

    max_text_width_exact = Fraction(width, 1) * max_ratio - (2 * pad_x)
    max_text_width = int(max_text_width_exact)
    if max_text_width <= 0:
        raise ValueError("max-ratio and pad-x leave no text width")
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines = wrap_words(probe, text, font, max_text_width)
    line_widths = [probe.textlength(line, font=font) for line in lines]
    for line, line_width in zip(lines, line_widths):
        if line_width > max_text_width:
            word = next(
                (candidate for candidate in line.split()
                 if probe.textlength(candidate, font=font) > max_text_width),
                line,
            )
            word_width = probe.textlength(word, font=font)
            print(
                "contract width rejection: "
                f"word={word!r}; measured_width_px={word_width:g}; "
                f"max_text_width_px={max_text_width}",
                file=sys.stderr,
            )
            return None, 2
    ascent, descent = font.getmetrics()
    line_height = ascent + descent
    text_height = line_height * len(lines) + line_gap * (len(lines) - 1)
    #: PNG bounds equal the background box, so overlay_y is its visible top.
    image_height = text_height + (2 * pad_y)

    widest = max(line_widths)
    box_left = (width - widest) / 2 - pad_x
    box_top = 0
    box_right = width - box_left
    box_bottom = box_top + text_height + (2 * pad_y)

    # Measure font ink before rendering it into the finite PNG. A raster bbox
    # cannot reveal ink that clipping has already removed.
    overflow = []
    for index, (line, line_width) in enumerate(zip(lines, line_widths)):
        text_x = (width - line_width) / 2
        text_y = pad_y + index * (line_height + line_gap)
        ink_bbox = probe.textbbox((text_x, text_y), line, font=font)
        if ink_bbox is None:
            continue
        ink_left, ink_top, ink_right, ink_bottom = ink_bbox
        sides = []
        if ink_left < box_left:
            sides.append(f"left={box_left - ink_left:g}px")
        if ink_top < box_top:
            sides.append(f"top={box_top - ink_top:g}px")
        if ink_right > box_right:
            sides.append(f"right={ink_right - box_right:g}px")
        if ink_bottom > box_bottom:
            sides.append(f"bottom={ink_bottom - box_bottom:g}px")
        if sides:
            overflow.append(f"line={index + 1}: " + ", ".join(sides))
    if overflow:
        print(
            "contract ink rejection: font ink outside background box: "
            + "; ".join(overflow),
            file=sys.stderr,
        )
        return None, 2

    image = Image.new("RGBA", (width, image_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((box_left, box_top, box_right, box_bottom), radius=radius, fill=bg)

    y = box_top + pad_y
    for line in lines:
        line_width = draw.textlength(line, font=font)
        draw.text(((width - line_width) / 2, y), line, font=font, fill=fg)
        y += line_height + line_gap

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Explicit format/options prevent metadata or adaptive choices from varying output.
    image.save(out_path, format="PNG", optimize=False, compress_level=9)
    result = {
        "width": width,
        "height": image_height,
        "lines": len(lines),
        "font": str(font_path),
        "size": size,
        "line_gap_px": line_gap,
        "max_text_width_px": max_text_width,
        "max_width_ratio": {"num": max_ratio.numerator, "den": max_ratio.denominator},
        "padding_px": {"x": pad_x, "y": pad_y},
        "corner_radius_px": radius,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    if contract_metadata:
        result.update(contract_metadata)
    return result, 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--size", type=int)
    parser.add_argument("--line-gap", type=int)
    parser.add_argument("--font")
    parser.add_argument("--max-ratio", type=parse_ratio)
    parser.add_argument("--pad-x", type=int)
    parser.add_argument("--pad-y", type=int)
    parser.add_argument("--radius", type=int)
    parser.add_argument("--bg", type=parse_rgba)
    parser.add_argument("--fg", type=parse_rgba)
    parser.add_argument("--caption-style-json", type=Path, help="JSON object containing the contract caption style; requires --assets-json and an asset map.")
    parser.add_argument("--assets-json", type=Path, help="JSON array of asset records referenced by the style; requires --caption-style-json and an asset map.")
    parser.add_argument("--asset-map", type=Path, help="JSON object mapping asset IDs to file paths for contract inputs; defaults to DRAFTCUT_ASSET_MAP when omitted.")
    parser.add_argument("--allow-missing-glyphs", action="store_true", help="Allow rendering when the font lacks glyphs for some characters; inspect the output.")
    args = parser.parse_args()
    try:
        contract_mode = args.caption_style_json is not None or args.assets_json is not None
        contract_metadata = None
        if contract_mode:
            #: contract rendering is fail-closed. Never use probe font discovery here;
            # the declared asset must resolve through the map and pass approval and SHA checks.
            asset_map_value = args.asset_map or os.environ.get("DRAFTCUT_ASSET_MAP")
            if args.caption_style_json is None or args.assets_json is None or not asset_map_value:
                raise ValueError(
                    "contract mode requires --caption-style-json, --assets-json, and "
                    "--asset-map (or DRAFTCUT_ASSET_MAP)"
                )
            legacy_style_options = {
                "--size": args.size, "--line-gap": args.line_gap, "--font": args.font,
                "--max-ratio": args.max_ratio, "--pad-x": args.pad_x, "--pad-y": args.pad_y,
                "--radius": args.radius, "--bg": args.bg, "--fg": args.fg,
            }
            supplied = [name for name, value in legacy_style_options.items() if value is not None]
            if supplied:
                raise ValueError(
                    "contract mode does not accept legacy style overrides: " + ", ".join(supplied)
                )
            style = resolve_contract_style(
                args.caption_style_json, args.assets_json, Path(asset_map_value)
            )
            font_path = style["font_path"]
            size = style["size"]
            line_gap = style["line_gap"]
            max_ratio = style["max_ratio"]
            pad_x, pad_y = style["pad_x"], style["pad_y"]
            radius, bg, fg = style["radius"], style["bg"], style["fg"]
            contract_metadata = {
                "style_id": style["style_id"],
                "font_family": style["font_family"],
                "font_asset_id": style["font_asset_id"],
                "font_sha256": style["font_sha256"],
            }
        else:
            #: probes/direct callers only measure executability and determinism, so they
            # may discover the first installed cross-platform candidate when no font is injected.
            if args.asset_map is not None:
                raise ValueError("--asset-map is only valid with contract inputs")
            font_path = resolve_probe_font(args.font)
            size = 40 if args.size is None else args.size
            line_gap = 8 if args.line_gap is None else args.line_gap
            max_ratio = Fraction(23, 25) if args.max_ratio is None else args.max_ratio
            pad_x = 28 if args.pad_x is None else args.pad_x
            pad_y = 20 if args.pad_y is None else args.pad_y
            radius = 14 if args.radius is None else args.radius
            bg = (0, 0, 0, 175) if args.bg is None else args.bg
            fg = (255, 255, 255, 255) if args.fg is None else args.fg
        result, code = render_caption(
            text=args.text, out_path=Path(args.out), width=args.width, size=size,
            line_gap=line_gap, font_path=font_path, max_ratio=max_ratio, pad_x=pad_x,
            pad_y=pad_y, radius=radius, bg=bg, fg=fg,
            allow_missing_glyphs=args.allow_missing_glyphs,
            contract_metadata=contract_metadata,
        )
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if code:
        return code
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
