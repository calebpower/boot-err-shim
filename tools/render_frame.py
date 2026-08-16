#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Render text into a framebuffer with a known font.

Exists so the calibration analysis can be tested by round trip: render a
message with a font we chose, hand the result to ``configure``, and require it
to recover that exact font. If it cannot rediscover a font it was just shown,
it will not manage one it has never seen.

The font is generated rather than shipped. Two reasons. The obvious CP437
bitmaps come from GPL or CC-BY-SA sources, which do not belong in an MIT
project. And a fixture font that merely *looks* like a console font would
prove less than one whose properties we control deliberately: distinct glyphs,
a blank last column so characters do not touch, and blank top and bottom rows
so consecutive lines form separate bands the way real text does.

    uv run tools/render_frame.py --text "Hello" -o frame.png
    uv run tools/render_frame.py --cell 9x16 --scale 2 -o big.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from boot_err_shim.frame import Frame  # noqa: E402
from boot_err_shim.png import write_frame  # noqa: E402

#: Blank rows above and below each glyph, so lines of text form separate
#: horizontal bands rather than one continuous smear.
TOP_MARGIN = 2
BOTTOM_MARGIN = 2

#: Blank columns to the right of each glyph, so adjacent characters do not
#: merge into a single run of ink.
RIGHT_MARGIN = 1

THE_MESSAGE = (
    "Disabling writes to flash as the flash part has gone bad.",
    "Please contact technical support to resolve this issue.",
    "Please press 'Y' to continue.",
)


def glyph(char: str, cell_width: int, cell_height: int) -> list[list[int]]:
    """A deterministic, distinct bitmap for one character.

    Derived from the code point with a small LCG, so it is stable across runs
    and platforms without needing a font file. Space is blank; everything else
    is guaranteed at least one lit pixel, otherwise it would be
    indistinguishable from space and the round-trip test would be lying.
    """
    rows = [[0] * cell_width for _ in range(cell_height)]
    if char == " ":
        return rows

    inner_width = max(1, cell_width - RIGHT_MARGIN)
    inner_height = max(1, cell_height - TOP_MARGIN - BOTTOM_MARGIN)

    state = (ord(char) * 2654435761) & 0xFFFFFFFF
    lit = 0
    for y in range(inner_height):
        for x in range(inner_width):
            state = (state * 1103515245 + 12345) & 0x7FFFFFFF
            if (state >> 16) & 1:
                rows[TOP_MARGIN + y][x] = 1
                lit += 1

    if lit == 0:
        rows[TOP_MARGIN][0] = 1
    return rows


def render(
    lines: tuple[str, ...],
    *,
    cell_width: int = 9,
    cell_height: int = 16,
    origin_x: int = 72,
    origin_y: int = 208,
    width: int | None = None,
    height: int | None = None,
    foreground: tuple[int, int, int] = (192, 192, 192),
    background: tuple[int, int, int] = (0, 0, 0),
    scale: int = 1,
    noise: int = 0,
) -> Frame:
    """Draw ``lines`` on a blank screen and return the frame.

    ``scale`` replicates every pixel, which is what an iDRAC doing integer
    upscaling looks like. ``noise`` lights that many scattered pixels, to
    check the analysis is not thrown by a speck of dirt on the console.
    """
    longest = max((len(line) for line in lines), default=0)
    width = width or max(640, origin_x + longest * cell_width + 16)
    height = height or max(400, origin_y + len(lines) * cell_height + 16)

    pixels = bytearray()
    for _ in range(width * height):
        pixels += bytes(background)

    def put(x: int, y: int) -> None:
        if 0 <= x < width and 0 <= y < height:
            offset = (y * width + x) * 3
            pixels[offset : offset + 3] = bytes(foreground)

    for line_index, line in enumerate(lines):
        for char_index, char in enumerate(line):
            bitmap = glyph(char, cell_width, cell_height)
            base_x = origin_x + char_index * cell_width
            base_y = origin_y + line_index * cell_height
            for y, row in enumerate(bitmap):
                for x, on in enumerate(row):
                    if on:
                        put(base_x + x, base_y + y)

    if noise:
        state = 0x12345678
        for _ in range(noise):
            state = (state * 1103515245 + 12345) & 0x7FFFFFFF
            x = (state >> 8) % width
            state = (state * 1103515245 + 12345) & 0x7FFFFFFF
            y = (state >> 8) % height
            put(x, y)

    frame = Frame(width, height, bytes(pixels))
    return upscale(frame, scale) if scale > 1 else frame


def upscale(frame: Frame, factor: int) -> Frame:
    """Nearest-neighbour integer upscale, as an iDRAC might apply."""
    width, height = frame.width * factor, frame.height * factor
    out = bytearray(width * height * 3)
    for y in range(height):
        source_row = (y // factor) * frame.width
        for x in range(width):
            source = (source_row + x // factor) * 3
            target = (y * width + x) * 3
            out[target : target + 3] = frame.data[source : source + 3]
    return Frame(width, height, bytes(out))


def blur(frame: Frame) -> Frame:
    """A cheap 3x1 box blur, standing in for non-integer rescaling.

    Used to check the analysis *fails* rather than silently producing a
    calibration that cannot be trusted.
    """
    out = bytearray(frame.data)
    for y in range(frame.height):
        for x in range(1, frame.width - 1):
            for channel in range(3):
                left = frame.data[((y * frame.width) + x - 1) * 3 + channel]
                here = frame.data[((y * frame.width) + x) * 3 + channel]
                right = frame.data[((y * frame.width) + x + 1) * 3 + channel]
                out[((y * frame.width) + x) * 3 + channel] = (left + here + right) // 3
    return Frame(frame.width, frame.height, bytes(out))


def _parse_cell(value: str) -> tuple[int, int]:
    try:
        width, height = value.lower().split("x")
        return int(width), int(height)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected WxH, got {value!r}") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--text",
        action="append",
        help="a line of text; repeat for more (default: the PERC message)",
    )
    parser.add_argument("--cell", type=_parse_cell, default=(9, 16), metavar="WxH")
    parser.add_argument("--origin", type=_parse_cell, default=(72, 208), metavar="X,Y")
    parser.add_argument("--scale", type=int, default=1)
    parser.add_argument("--noise", type=int, default=0)
    parser.add_argument("--invert", action="store_true", help="dark text on light")
    parser.add_argument("--blur", action="store_true", help="simulate rescaling")
    parser.add_argument("-o", "--output", type=Path, default=Path("frame.png"))
    args = parser.parse_args()

    lines = tuple(args.text) if args.text else THE_MESSAGE
    frame = render(
        lines,
        cell_width=args.cell[0],
        cell_height=args.cell[1],
        origin_x=args.origin[0],
        origin_y=args.origin[1],
        scale=args.scale,
        noise=args.noise,
        foreground=(0, 0, 0) if args.invert else (192, 192, 192),
        background=(255, 255, 255) if args.invert else (0, 0, 0),
    )
    if args.blur:
        frame = blur(frame)

    write_frame(args.output, frame)
    print(f"wrote {args.output} ({frame.width}x{frame.height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
