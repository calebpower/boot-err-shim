"""Deciding whether the prompt is on screen, given a calibration.

Two matchers, used in that order:

**Region match** is the primary. Compare the binarised ink over the exact
rectangle the message occupied when we calibrated. On an unscaled console this
is bit-for-bit identical and costs a single pass; a tolerance absorbs the
occasional resampled pixel if the iDRAC is not perfectly stable.

**Glyph decode** is the fallback, and runs only when the region match fails.
It reads the whole screen through the learned glyph table and looks for the
text anywhere on it, which survives the message appearing a line higher or
lower than it did during calibration. It also gives the log something
human-readable about what the console actually said, which is the difference
between "no match" and "no match, the screen said POST error 1801".

Both are conservative by construction. Anything they cannot account for is a
non-match, and a non-match means no keypress.
"""

from __future__ import annotations

from dataclasses import dataclass

from .bitmap import Bitmap, binarise
from .calibrate import Calibration, normalise
from .frame import Frame


@dataclass(frozen=True)
class DetectResult:
    matched: bool
    #: Stable token naming which matcher decided, for the log.
    detail: str = ""
    #: What the screen appeared to say, when glyph decode ran.
    text: str | None = None
    #: Fraction of region pixels that differed, when the region matcher ran.
    difference: float | None = None


class CalibratedDetector:
    """Matches a frame against a calibration."""

    def __init__(self, calibration: Calibration, tolerance: float = 0.02) -> None:
        self.calibration = calibration
        self.tolerance = tolerance
        # bitmap bytes -> character, for reading cells back into text.
        self._reverse: dict[bytes, str] = {}
        for char, glyph in calibration.glyphs.items():
            # A blank cell is a space. If some other character also renders
            # blank the font is unusable for decoding, and first-wins here
            # would silently pick one; prefer the space explicitly.
            if glyph.count() == 0 and char != " ":
                continue
            self._reverse.setdefault(glyph.data, char)

    def __call__(self, frame: Frame) -> DetectResult:
        return self.detect(frame)

    def detect(self, frame: Frame) -> DetectResult:
        """Decide whether the message is on screen.

        An **exact** region match is accepted on its own -- every pixel of the
        message area is identical to calibration time, and nothing more needs
        saying.

        A region match that is merely *within tolerance* is not accepted on
        its own, and this is the important rule. The tolerance is a fraction
        of a region several hundred pixels on a side, while a single wrong
        character is a few dozen pixels: "Please press 'N'" differs from
        "Please press 'Y'" by about 0.2% of the area, so any tolerance loose
        enough to absorb a speck of noise is also loose enough to accept the
        wrong message. So a near match must be corroborated by actually
        reading the text back through the learned glyphs.

        The tolerance therefore buys what it should -- immunity to stray
        pixels elsewhere in the region -- and cannot buy what it must not,
        which is immunity to the words being different.
        """
        binarised = binarise(
            frame,
            threshold=self.calibration.threshold,
            invert=self.calibration.inverted,
        )

        region = self._region_match(binarised.mask, frame)
        if region.matched and region.difference == 0.0:
            return region

        decoded = self._glyph_match(binarised.mask, frame)
        if decoded.matched:
            detail = "region+glyph" if region.matched else "glyph"
            return DetectResult(
                matched=True,
                detail=detail,
                text=decoded.text,
                difference=region.difference,
            )

        # Report the region difference alongside the decoded text: together
        # they say both "how close was it" and "what was actually there".
        return DetectResult(
            matched=False,
            detail=region.detail,
            text=decoded.text,
            difference=region.difference,
        )

    # -- primary ---------------------------------------------------------

    def _region_match(self, mask: Bitmap, frame: Frame) -> DetectResult:
        calibration = self.calibration

        if (frame.width, frame.height) != (calibration.width, calibration.height):
            # The console changed video mode. The stored rectangle no longer
            # refers to the same part of the screen, so this matcher has
            # nothing to say and must not guess.
            return DetectResult(
                matched=False,
                detail=(
                    f"frame-size-changed:{frame.width}x{frame.height}"
                    f"!={calibration.width}x{calibration.height}"
                ),
            )

        x, y, width, height = calibration.region
        actual = mask.crop(x, y, width, height)
        differing = actual.differences(calibration.region_mask)
        total = width * height
        fraction = differing / total if total else 1.0

        if fraction <= self.tolerance:
            return DetectResult(
                matched=True, detail="region", difference=fraction
            )
        return DetectResult(
            matched=False, detail="region-mismatch", difference=fraction
        )

    # -- fallback --------------------------------------------------------

    def _glyph_match(self, mask: Bitmap, frame: Frame) -> DetectResult:
        text = self.read_screen(mask)
        haystack = normalise(text)
        wanted = self.calibration.text

        if all(line in haystack for line in wanted):
            return DetectResult(matched=True, detail="glyph", text=text)
        return DetectResult(matched=False, detail="glyph-mismatch", text=text)

    def read_screen(self, mask: Bitmap) -> str:
        """Decode the whole screen through the learned glyphs."""
        calibration = self.calibration
        cell_width = calibration.cell_width
        cell_height = calibration.cell_height

        # Start from the calibrated origin, but walk back to the top-left in
        # whole cells so text above the message is read too.
        start_x = calibration.origin_x % cell_width
        start_y = calibration.origin_y % cell_height

        lines: list[str] = []
        y = start_y
        while y + cell_height <= mask.height:
            row_chars: list[str] = []
            x = start_x
            while x + cell_width <= mask.width:
                cell = mask.crop(x, y, cell_width, cell_height)
                if cell.count() == 0:
                    row_chars.append(" ")
                else:
                    row_chars.append(self._reverse.get(cell.data, "�"))
                x += cell_width
            line = "".join(row_chars).rstrip()
            if line.strip():
                lines.append(line)
            y += cell_height

        return "\n".join(lines)


def build_detector(
    calibration: Calibration, tolerance: float
) -> CalibratedDetector:
    return CalibratedDetector(calibration, tolerance)
