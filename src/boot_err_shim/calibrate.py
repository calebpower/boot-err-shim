"""Working backwards from a framebuffer to a font.

The problem: VNC gives pixels, and we need to know whether a particular
sentence is on screen. The usual answers are to ship a font and hope the
console uses it, or to run OCR and hope it reads correctly. Both guess.

The observation that removes the guess: **the config already says what the
text is**. When the operator runs ``configure`` the message is on screen, and
we are told exactly what it says. That is enough to solve for everything else
-- cell geometry, where the text sits, and a bitmap for every character in it
-- from the operator's own hardware, in their own font, at whatever scaling
their iDRAC applies.

How it works
------------
1. Binarise. Console screens are bimodal, so this is lossless in practice.
2. Group rows of ink into bands. Each band is a line of text.
3. Find a run of bands matching the expected line count.
4. Estimate the cell height from the pitch between band tops.
5. Estimate the cell width from the ink widths of lines of *different*
   character counts: two lines drawn in the same font differ in ink width by
   exactly the difference in character count times the cell width, which
   cancels out the unknown bearings at each end.
6. Search a small neighbourhood around those estimates for the grid that
   makes every occurrence of a character produce an identical bitmap.
7. Verify by re-rendering the message from the learned glyphs and diffing
   against the frame.

Step 7 is what makes the rest trustworthy. Any error in the geometry shows up
as the same character yielding two different bitmaps, which shows up as a
nonzero pixel delta on re-render. A calibration that verifies at zero has
demonstrated it can reproduce the screen it came from.
"""

from __future__ import annotations

import base64
import binascii
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .bitmap import Band, Binarisation, Bitmap, binarise, find_bands
from .errors import (
    AnalysisError,
    CalibrationError,
    CalibrationNotFound,
    CalibrationStale,
)
from .frame import Frame
from .lock import atomic_write_text

#: Plausible character cell sizes, in pixels. Covers 8x8, 8x14, 8x16 and 9x16
#: VGA text modes and their integer multiples, plus typical UEFI fonts.
MIN_CELL_WIDTH, MAX_CELL_WIDTH = 4, 40
MIN_CELL_HEIGHT, MAX_CELL_HEIGHT = 6, 64

#: How far around the estimated grid origin to search, in pixels.
ORIGIN_SEARCH = 3

#: Give up on a candidate grid once this many characters have produced
#: conflicting bitmaps. A wrong grid goes wrong almost immediately, so there
#: is nothing to learn from grinding through the rest of the message -- and
#: without this, analysing a screen that does not contain the text at all
#: means a full extraction for every candidate.
CONFLICT_ABORT = 8

#: Calibration file format version, so a future change can be detected rather
#: than misread.
FORMAT_VERSION = 1


@dataclass(frozen=True)
class Findings:
    """What the analysis worked out, whether or not it succeeded.

    Carried on the exception when it fails, so a failed `configure` reports
    what it did determine instead of just refusing.
    """

    width: int
    height: int
    background: tuple[int, int, int]
    foreground: tuple[int, int, int]
    threshold: int
    inverted: bool
    ink_fraction: float
    contrast: float
    bands: tuple[Band, ...] = ()
    cell: tuple[int, int] | None = None
    origin: tuple[int, int] | None = None
    note: str = ""


@dataclass
class Calibration:
    """Everything needed to recognise the message again."""

    #: Frame size this was learned from. A different size means the console
    #: changed mode and the calibration no longer applies.
    width: int
    height: int
    cell_width: int
    cell_height: int
    origin_x: int
    origin_y: int
    threshold: int
    inverted: bool
    background: tuple[int, int, int]
    foreground: tuple[int, int, int]
    #: The text this was calibrated against, normalised.
    text: tuple[str, ...]
    #: character -> glyph bitmap
    glyphs: dict[str, Bitmap]
    #: Where the message sits: (x, y, width, height) in pixels.
    region: tuple[int, int, int, int]
    #: The binarised ink of that region, for the fast exact matcher.
    region_mask: Bitmap
    #: Pixels that differed when the learned glyphs were re-rendered.
    verify_delta: int = 0
    format_version: int = FORMAT_VERSION
    #: What the analysis observed on the way here. Populated by `analyse`,
    #: never persisted -- it describes one run, not the calibration.
    findings: Findings | None = field(default=None, compare=False, repr=False)

    @property
    def exact(self) -> bool:
        return self.verify_delta == 0

    @property
    def region_pixels(self) -> int:
        return self.region[2] * self.region[3]

    def matches_text(self, lines: tuple[str, ...]) -> bool:
        return tuple(normalise(line) for line in lines) == self.text

    # -- persistence ----------------------------------------------------

    def to_toml(self) -> str:
        out: list[str] = [
            "# Written by `boot-err-shim configure`. Generated; do not hand-edit.",
            "#",
            "# The glyphs below were extracted from your own console, so they",
            "# are readable on purpose -- you can check the font by eye.",
            "",
            f"format_version = {self.format_version}",
            "",
            "[frame]",
            f"width = {self.width}",
            f"height = {self.height}",
            "",
            "[grid]",
            f"cell_width = {self.cell_width}",
            f"cell_height = {self.cell_height}",
            f"origin_x = {self.origin_x}",
            f"origin_y = {self.origin_y}",
            "",
            "[colour]",
            f"threshold = {self.threshold}",
            f"inverted = {str(self.inverted).lower()}",
            f"background = {list(self.background)}",
            f"foreground = {list(self.foreground)}",
            "",
            "[message]",
            f"region = {list(self.region)}",
            f"verify_delta = {self.verify_delta}",
            "text = [",
        ]
        for line in self.text:
            out.append(f"  {_toml_string(line)},")
        out.append("]")
        out.append("mask = [")
        for row in self.region_mask.to_rows():
            out.append(f'  "{row}",')
        out.append("]")
        out.append("")
        out.append("[glyphs]")
        for char in sorted(self.glyphs):
            rows = self.glyphs[char].to_rows()
            out.append(f"{_toml_string(char)} = [")
            for row in rows:
                out.append(f'  "{row}",')
            out.append("]")
        return "\n".join(out) + "\n"

    def save(self, path: Path) -> None:
        atomic_write_text(Path(path), self.to_toml())

    @classmethod
    def load(cls, path: Path) -> Calibration:
        path = Path(path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            # Distinguished from an unreadable or corrupt one, because the
            # advice differs: "you have not calibrated yet" versus "your
            # calibration is damaged". Both end in `configure`, but only one
            # of them means something went wrong.
            raise CalibrationNotFound(
                f"{path}: no calibration yet. Reboot the host, let it stop at "
                f"the error, then run: boot-err-shim configure"
            ) from exc
        except OSError as exc:
            raise CalibrationError(f"{path}: cannot read: {exc}") from exc
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise CalibrationError(f"{path}: not valid TOML: {exc}") from exc

        try:
            return cls.from_dict(data)
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise CalibrationError(f"{path}: malformed calibration: {exc}") from exc

    @classmethod
    def from_dict(cls, data: dict) -> Calibration:
        version = data.get("format_version")
        if version != FORMAT_VERSION:
            raise ValueError(
                f"format_version {version!r}, this build understands "
                f"{FORMAT_VERSION}; re-run configure"
            )

        frame, grid = data["frame"], data["grid"]
        colour, message = data["colour"], data["message"]

        glyphs = {
            char: Bitmap.from_rows(list(rows))
            for char, rows in data.get("glyphs", {}).items()
        }
        return cls(
            width=int(frame["width"]),
            height=int(frame["height"]),
            cell_width=int(grid["cell_width"]),
            cell_height=int(grid["cell_height"]),
            origin_x=int(grid["origin_x"]),
            origin_y=int(grid["origin_y"]),
            threshold=int(colour["threshold"]),
            inverted=bool(colour["inverted"]),
            background=tuple(colour["background"]),
            foreground=tuple(colour["foreground"]),
            text=tuple(message["text"]),
            glyphs=glyphs,
            region=tuple(int(v) for v in message["region"]),
            region_mask=Bitmap.from_rows(list(message["mask"])),
            verify_delta=int(message.get("verify_delta", 0)),
        )


def _toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def normalise(text: str) -> str:
    """Collapse whitespace and case, so wrapping and capitals do not matter."""
    return " ".join(text.split()).casefold()


# -- the analysis -------------------------------------------------------


@dataclass
class _Candidate:
    cell_width: int
    cell_height: int
    origin_x: int
    origin_y: int
    glyphs: dict[str, Bitmap] = field(default_factory=dict)
    conflicts: int = 0
    delta: int = 0
    columns: tuple[int, ...] = ()
    rows: tuple[int, ...] = ()


def analyse(
    frame: Frame,
    lines: tuple[str, ...],
    *,
    cell: tuple[int, int] | None = None,
    origin: tuple[int, int] | None = None,
    threshold: int | None = None,
    invert: bool | None = None,
) -> Calibration:
    """Learn a calibration from a frame known to be showing ``lines``.

    Raises :class:`AnalysisError` carrying :class:`Findings` when it cannot,
    so the caller can tell the operator what was determined before it gave up.
    """
    if not lines:
        raise AnalysisError("no text configured to look for")

    binarised = binarise(frame, threshold=threshold, invert=invert)
    findings = _findings(frame, binarised)

    bands = tuple(find_bands(binarised.mask))
    findings = _with(findings, bands=bands)

    if not bands:
        raise AnalysisError(
            "the screen has no text on it at all (no foreground pixels found)",
            findings,
        )
    if len(bands) < len(lines):
        raise AnalysisError(
            f"found {len(bands)} line(s) of text but the configured message "
            f"has {len(lines)}; is the console showing the error?",
            findings,
        )

    best: _Candidate | None = None
    best_run: tuple[Band, ...] = ()

    for run in _candidate_runs(bands, lines):
        for candidate in _candidates(run, lines, cell=cell, origin=origin):
            _extract(candidate, binarised.mask, run, lines, abort_after=CONFLICT_ABORT)
            if candidate.delta == 0:
                calibration = _build(frame, binarised, candidate, run, lines)
                calibration.findings = _with(
                    findings,
                    cell=(candidate.cell_width, candidate.cell_height),
                    origin=(candidate.origin_x, candidate.origin_y),
                )
                return calibration
            if best is None or candidate.conflicts < best.conflicts:
                best, best_run = candidate, run

    if best is None:  # pragma: no cover - _candidates always yields something
        raise AnalysisError("could not propose any character grid", findings)

    # The search aborted early on every candidate, so the counts carried by
    # the best one are partial. Redo that one in full, purely so the operator
    # is told how close it got rather than a truncated number.
    _extract(best, binarised.mask, best_run, lines, abort_after=None)
    best_findings = _with(
        findings,
        cell=(best.cell_width, best.cell_height),
        origin=(best.origin_x, best.origin_y),
    )

    raise AnalysisError(
        f"found a {best.cell_width}x{best.cell_height} grid but could not make "
        f"it consistent: {best.conflicts} character(s) produced conflicting "
        f"bitmaps, {best.delta} pixel(s) differ on re-render. The console may "
        f"be scaled or antialiased; try --cell WxH, or engine = \"ocr\"",
        best_findings,
    )


def _findings(frame: Frame, binarised: Binarisation) -> Findings:
    return Findings(
        width=frame.width,
        height=frame.height,
        background=binarised.background,
        foreground=binarised.foreground,
        threshold=binarised.threshold,
        inverted=binarised.inverted,
        ink_fraction=binarised.ink_fraction,
        contrast=binarised.contrast,
    )


def _with(findings: Findings, **changes) -> Findings:
    from dataclasses import replace

    return replace(findings, **changes)


def _candidate_runs(
    bands: tuple[Band, ...], lines: tuple[str, ...]
) -> list[tuple[Band, ...]]:
    """Every run of consecutive bands that could be the message, likeliest first.

    The message may sit on a screen that also shows other text, so every
    window of the right size is a candidate. They are ordered by how well the
    bands' relative widths agree with the lines' relative character counts: in
    a fixed-width font the implied cell width must come out the same for each
    line, so the spread of those estimates ranks the windows. The right one is
    usually first, which is what keeps the search cheap.
    """
    count = len(lines)
    runs = [tuple(bands[i : i + count]) for i in range(len(bands) - count + 1)]

    def spread(run: tuple[Band, ...]) -> float:
        estimates = [
            band.width / len(line)
            for band, line in zip(run, lines, strict=True)
            if line
        ]
        if not estimates:
            return float("inf")
        mean = sum(estimates) / len(estimates)
        return sum(abs(value - mean) for value in estimates) / len(estimates)

    return sorted(runs, key=spread)


def _candidates(
    run: tuple[Band, ...],
    lines: tuple[str, ...],
    *,
    cell: tuple[int, int] | None,
    origin: tuple[int, int] | None,
):
    """Propose grids for this run of bands."""
    heights = _cell_height_candidates(run) if cell is None else [cell[1]]
    widths = _cell_width_candidates(run, lines) if cell is None else [cell[0]]

    base_x = min(band.left for band in run)
    base_y = run[0].top

    for cell_height in heights:
        if not MIN_CELL_HEIGHT <= cell_height <= MAX_CELL_HEIGHT:
            continue
        for cell_width in widths:
            if not MIN_CELL_WIDTH <= cell_width <= MAX_CELL_WIDTH:
                continue
            if origin is not None:
                yield _Candidate(cell_width, cell_height, origin[0], origin[1])
                continue
            for dy in range(-ORIGIN_SEARCH, 1):
                for dx in range(-ORIGIN_SEARCH, 1):
                    yield _Candidate(
                        cell_width, cell_height, base_x + dx, base_y + dy
                    )


def _cell_height_candidates(run: tuple[Band, ...]) -> list[int]:
    """Cell height from the pitch between consecutive line tops."""
    if len(run) >= 2:
        pitches = [
            run[i + 1].top - run[i].top for i in range(len(run) - 1)
        ]
        ordered = sorted(pitches)
        median = ordered[len(ordered) // 2]
        candidates = [median, *pitches]
    else:
        candidates = []

    # A single-line message has no pitch to measure, so fall back to the ink
    # height plus a little leading.
    tallest = max(band.height for band in run)
    candidates.extend([tallest + 2, tallest + 3, tallest + 4, tallest])

    seen: list[int] = []
    for value in candidates:
        if value not in seen:
            seen.append(value)
    return seen


def _cell_width_candidates(
    run: tuple[Band, ...], lines: tuple[str, ...]
) -> list[int]:
    """Cell width, chiefly from lines of differing length.

    Two lines in the same fixed-width font differ in ink width by exactly the
    difference in character count times the cell width. The unknown bearing at
    each end cancels, which makes this far steadier than dividing one line's
    width by its length.
    """
    candidates: list[int] = []

    for i in range(len(run)):
        for j in range(len(run)):
            delta_chars = len(lines[i]) - len(lines[j])
            if delta_chars <= 0:
                continue
            delta_width = run[i].width - run[j].width
            if delta_width <= 0:
                continue
            estimate = round(delta_width / delta_chars)
            candidates.extend([estimate, estimate + 1, estimate - 1])

    for band, line in zip(run, lines, strict=True):
        if line:
            estimate = round(band.width / len(line))
            candidates.extend([estimate, estimate + 1])

    seen: list[int] = []
    for value in candidates:
        if value >= MIN_CELL_WIDTH and value not in seen:
            seen.append(value)
    return seen


def _extract(
    candidate: _Candidate,
    mask: Bitmap,
    run: tuple[Band, ...],
    lines: tuple[str, ...],
    *,
    abort_after: int | None = None,
) -> None:
    """Fill in a candidate's glyph table, conflict count, and pixel delta.

    ``abort_after`` stops once that many characters have disagreed with
    themselves. A wrong grid disagrees within the first few characters, so
    finishing tells us nothing and costs everything.
    """
    glyphs: dict[str, Bitmap] = {}
    conflicts = 0
    aborted = False

    columns = []
    for band in run:
        offset = band.left - candidate.origin_x
        columns.append(max(0, round(offset / candidate.cell_width)))

    candidate.columns = tuple(columns)
    candidate.rows = tuple(range(len(lines)))

    for line_index, line in enumerate(lines):
        for char_index, char in enumerate(line):
            cell = _cell_at(
                mask, candidate, columns[line_index] + char_index, line_index
            )
            existing = glyphs.get(char)
            if existing is None:
                glyphs[char] = cell
            elif existing.data != cell.data:
                conflicts += 1
                if abort_after is not None and conflicts > abort_after:
                    aborted = True
                    break
        if aborted:
            break

    candidate.glyphs = glyphs
    candidate.conflicts = conflicts
    if aborted:
        # Not a real pixel count; only used to rank against zero.
        candidate.delta = -1
    else:
        candidate.delta = _delta(mask, candidate, lines)


def _cell_at(mask: Bitmap, candidate: _Candidate, column: int, row: int) -> Bitmap:
    return mask.crop(
        candidate.origin_x + column * candidate.cell_width,
        candidate.origin_y + row * candidate.cell_height,
        candidate.cell_width,
        candidate.cell_height,
    )


def _delta(mask: Bitmap, candidate: _Candidate, lines: tuple[str, ...]) -> int:
    """Pixels that differ when the message is redrawn from learned glyphs.

    Zero means every occurrence of every character produced the same bitmap,
    which is only true when the grid is right.
    """
    total = 0
    for line_index, line in enumerate(lines):
        row = candidate.rows[line_index]
        for char_index, char in enumerate(line):
            actual = _cell_at(mask, candidate, candidate.columns[line_index] + char_index, row)
            total += candidate.glyphs[char].differences(actual)
    return total


def _build(
    frame: Frame,
    binarised: Binarisation,
    candidate: _Candidate,
    run: tuple[Band, ...],
    lines: tuple[str, ...],
) -> Calibration:
    left = min(
        candidate.origin_x + candidate.columns[i] * candidate.cell_width
        for i in range(len(lines))
    )
    right = max(
        candidate.origin_x
        + (candidate.columns[i] + len(lines[i])) * candidate.cell_width
        for i in range(len(lines))
    )
    top = candidate.origin_y + candidate.rows[0] * candidate.cell_height
    bottom = candidate.origin_y + (candidate.rows[-1] + 1) * candidate.cell_height

    left = max(0, left)
    top = max(0, top)
    width = min(right, frame.width) - left
    height = min(bottom, frame.height) - top

    region_mask = binarised.mask.crop(left, top, width, height)

    return Calibration(
        width=frame.width,
        height=frame.height,
        cell_width=candidate.cell_width,
        cell_height=candidate.cell_height,
        origin_x=candidate.origin_x,
        origin_y=candidate.origin_y,
        threshold=binarised.threshold,
        inverted=binarised.inverted,
        background=binarised.background,
        foreground=binarised.foreground,
        text=tuple(normalise(line) for line in lines),
        glyphs=candidate.glyphs,
        region=(left, top, width, height),
        region_mask=region_mask,
        verify_delta=candidate.delta,
    )


def check_calibration(calibration: Calibration, lines: tuple[str, ...]) -> None:
    """Raise if a loaded calibration does not describe the configured text."""
    if not calibration.matches_text(lines):
        raise CalibrationStale(
            "the calibration was made for different text than detect.text "
            "now configures; re-run configure"
        )
