"""Binarisation and the one-bit image type everything downstream works on.

Console screens are strongly bimodal -- a background colour and a foreground
colour, with almost nothing between -- so reducing to one bit per pixel loses
nothing that matters and makes exact comparison cheap and obvious.

Colours are chosen by frequency rather than by a fixed threshold, because a
console may be white on black, amber on black, or black on white, and the
program has no business assuming which.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .frame import Frame

#: Integer approximation of the Rec. 709 luma coefficients, x256.
_LUMA = (54, 183, 19)


def luma(colour: tuple[int, int, int]) -> int:
    """Perceptual brightness, 0-255."""
    r, g, b = colour
    return (_LUMA[0] * r + _LUMA[1] * g + _LUMA[2] * b) >> 8


def _linearise(channel: int) -> float:
    value = channel / 255.0
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(colour: tuple[int, int, int]) -> float:
    """WCAG relative luminance, 0.0-1.0."""
    r, g, b = colour
    return (
        0.2126 * _linearise(r) + 0.7152 * _linearise(g) + 0.0722 * _linearise(b)
    )


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """WCAG contrast ratio between two colours, 1.0-21.0.

    Computed rather than eyeballed. A low-contrast console makes the
    binarisation threshold unreliable, and that is not something an operator
    can judge by looking at a PNG.
    """
    first, second = relative_luminance(a), relative_luminance(b)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


@dataclass(frozen=True)
class Bitmap:
    """A one-bit image. ``data[i]`` is 1 where there is ink."""

    width: int
    height: int
    data: bytes

    @classmethod
    def empty(cls, width: int, height: int) -> Bitmap:
        return cls(width, height, bytes(width * height))

    def at(self, x: int, y: int) -> int:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return 0
        return self.data[y * self.width + x]

    def crop(self, x: int, y: int, width: int, height: int) -> Bitmap:
        """Crop, padding with background where the box runs off the edge.

        Padding rather than raising: a glyph cell at the right-hand edge of
        the screen may legitimately extend past it, and that is not an error.
        """
        out = bytearray(width * height)
        for row in range(height):
            source_y = y + row
            if not 0 <= source_y < self.height:
                continue
            for column in range(width):
                source_x = x + column
                if 0 <= source_x < self.width:
                    out[row * width + column] = self.data[source_y * self.width + source_x]
        return Bitmap(width, height, bytes(out))

    def count(self) -> int:
        return sum(self.data)

    def row_profile(self) -> list[int]:
        """Ink pixels per row."""
        return [
            sum(self.data[y * self.width : (y + 1) * self.width])
            for y in range(self.height)
        ]

    def column_profile(self) -> list[int]:
        """Ink pixels per column."""
        out = [0] * self.width
        for y in range(self.height):
            base = y * self.width
            for x in range(self.width):
                out[x] += self.data[base + x]
        return out

    def differences(self, other: Bitmap) -> int:
        """Count differing pixels. Mismatched sizes count as wholly different."""
        if (self.width, self.height) != (other.width, other.height):
            return max(self.width * self.height, other.width * other.height)
        return sum(a ^ b for a, b in zip(self.data, other.data, strict=True))

    def to_rows(self, ink: str = "#", blank: str = ".") -> list[str]:
        """Render as text, one string per row.

        Calibrations store glyphs this way. It costs a few kilobytes and buys
        a font somebody can read, diff, and eyeball in the file itself.
        """
        return [
            "".join(
                ink if self.data[y * self.width + x] else blank
                for x in range(self.width)
            )
            for y in range(self.height)
        ]

    @classmethod
    def from_rows(cls, rows: list[str], ink: str = "#") -> Bitmap:
        if not rows:
            return cls.empty(0, 0)
        width = len(rows[0])
        if any(len(row) != width for row in rows):
            raise ValueError("bitmap rows are not all the same length")
        data = bytearray()
        for row in rows:
            data += bytes(1 if char == ink else 0 for char in row)
        return cls(width, len(rows), bytes(data))


@dataclass(frozen=True)
class Binarisation:
    """The result of reducing a frame to ink and background."""

    mask: Bitmap
    background: tuple[int, int, int]
    foreground: tuple[int, int, int]
    threshold: int
    #: True when ink is darker than the background (black on white).
    inverted: bool

    @property
    def ink_fraction(self) -> float:
        total = self.mask.width * self.mask.height
        return self.mask.count() / total if total else 0.0

    @property
    def contrast(self) -> float:
        return contrast_ratio(self.foreground, self.background)


def dominant_colours(frame: Frame) -> list[tuple[tuple[int, int, int], int]]:
    """Colours present, most common first."""
    counter: Counter[tuple[int, int, int]] = Counter()
    data = frame.data
    for offset in range(0, len(data), 3):
        counter[(data[offset], data[offset + 1], data[offset + 2])] += 1
    return counter.most_common()


def binarise(
    frame: Frame,
    *,
    threshold: int | None = None,
    invert: bool | None = None,
) -> Binarisation:
    """Split a frame into ink and background.

    The background is the most common colour. The foreground is the most
    common *remaining* colour that is far enough from it in brightness to be
    plausibly text -- picking the second most common outright would choose an
    antialiasing shade on a console that has any.
    """
    colours = dominant_colours(frame)
    background = colours[0][0]
    background_luma = luma(background)

    foreground = None
    for colour, _count in colours[1:]:
        if abs(luma(colour) - background_luma) >= 32:
            foreground = colour
            break

    if foreground is None:
        # A blank or near-blank screen. Everything is background; downstream
        # will report that it found no text rather than inventing some.
        foreground = (255, 255, 255) if background_luma < 128 else (0, 0, 0)

    foreground_luma = luma(foreground)
    if invert is None:
        invert = foreground_luma < background_luma
    if threshold is None:
        threshold = (foreground_luma + background_luma) // 2

    data = frame.data
    out = bytearray(frame.width * frame.height)
    for index in range(frame.width * frame.height):
        offset = index * 3
        value = luma((data[offset], data[offset + 1], data[offset + 2]))
        out[index] = 1 if (value < threshold if invert else value > threshold) else 0

    return Binarisation(
        mask=Bitmap(frame.width, frame.height, bytes(out)),
        background=background,
        foreground=foreground,
        threshold=threshold,
        inverted=invert,
    )


@dataclass(frozen=True)
class Band:
    """A run of consecutive rows containing ink -- usually one line of text."""

    top: int
    bottom: int  # inclusive
    left: int
    right: int  # inclusive

    @property
    def height(self) -> int:
        return self.bottom - self.top + 1

    @property
    def width(self) -> int:
        return self.right - self.left + 1


def find_bands(mask: Bitmap, *, min_ink: int = 1) -> list[Band]:
    """Group rows containing ink into horizontal bands."""
    profile = mask.row_profile()
    bands: list[Band] = []
    top: int | None = None

    for y, ink in enumerate(profile + [0]):
        if ink >= min_ink and top is None:
            top = y
        elif ink < min_ink and top is not None:
            bands.append(_measure_band(mask, top, y - 1))
            top = None

    return bands


def _measure_band(mask: Bitmap, top: int, bottom: int) -> Band:
    left, right = mask.width, -1
    for y in range(top, bottom + 1):
        base = y * mask.width
        for x in range(mask.width):
            if mask.data[base + x]:
                left = min(left, x)
                right = max(right, x)
    if right < 0:  # pragma: no cover - a band always has ink by construction
        left, right = 0, -1
    return Band(top=top, bottom=bottom, left=left, right=right)
