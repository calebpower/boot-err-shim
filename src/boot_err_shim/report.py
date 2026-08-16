"""Rendering the human-facing `configure` report.

Kept apart from the analysis, and kept deterministic, for two reasons. Tier 2
compares this output against golden files, which only works if nothing varies
run to run -- so timestamps and paths arrive as arguments rather than being
read from the environment here. And the report is the operator's only window
into what calibration actually did, so it is worth writing on purpose rather
than accumulating print statements at the call site.

A failed analysis gets a report too. "Could not calibrate" on its own leaves
somebody with a stuck server and no next step; the same failure plus the
resolution, the detected grid, and a picture of what the analyser thought the
text rows looked like is something they can act on.
"""

from __future__ import annotations

from .bitmap import Bitmap, contrast_ratio
from .calibrate import Calibration, Findings
from .rfb import ServerInfo

#: Below this WCAG ratio, binarisation is unreliable and we say so.
CONTRAST_FLOOR = 3.0


def _hex(colour: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*colour)


def connection_report(host: str, port: int, info: ServerInfo) -> list[str]:
    return [
        f"connecting {host}:{port} ... ok",
        f"  RFB 3.8, security types offered: [{info.security_description}]"
        f", TLS: {'yes' if info.tls else 'no'}",
        f"  desktop: {info.name!r}",
        f"  framebuffer: {info.width}x{info.height}",
    ]


def findings_report(findings: Findings) -> list[str]:
    """What the analysis worked out, success or failure."""
    lines = [
        f"  {findings.width}x{findings.height}, ink {findings.ink_fraction:.1%}",
        f"  {_hex(findings.foreground)} on {_hex(findings.background)}, "
        f"contrast {findings.contrast:.1f}:1, threshold {findings.threshold}"
        f"{', inverted' if findings.inverted else ''}",
    ]

    if findings.contrast < CONTRAST_FLOOR:
        # Computed, not eyeballed: a low-contrast console makes the threshold
        # unreliable, and that is not visible to somebody looking at a PNG.
        lines.append(
            f"  WARNING: contrast {findings.contrast:.1f}:1 is below "
            f"{CONTRAST_FLOOR:.1f}:1; binarisation may be unreliable"
        )

    lines.append(f"  text rows found: {len(findings.bands)}")
    if findings.cell:
        lines.append(f"  grid: {findings.cell[0]}x{findings.cell[1]} cells")
    if findings.origin:
        lines.append(f"  origin: ({findings.origin[0]}, {findings.origin[1]})")
    return lines


def success_report(calibration: Calibration, lines: tuple[str, ...]) -> list[str]:
    # The grid and origin are not repeated here: findings_report has already
    # printed them, and it runs on both the success and failure paths.
    out = [
        f"  located the message at {calibration.region[0]},{calibration.region[1]} "
        f"({calibration.region[2]}x{calibration.region[3]})",
    ]
    total = len(lines)
    for index, line in enumerate(lines, 1):
        shown = line if len(line) <= 58 else line[:55] + "..."
        out.append(f"    {index}/{total}  {shown!r:62} {len(line):3d} ok")

    out.append(f"  learned {len(calibration.glyphs)} distinct glyphs")
    if calibration.exact:
        out.append("  verify: re-render matches the framebuffer exactly (0 px differ)")
    else:
        out.append(
            f"  verify: {calibration.verify_delta} px differ on re-render "
            f"({calibration.verify_delta / max(1, calibration.region_pixels):.2%} "
            "of the message area)"
        )
    return out


def failure_advice(findings: Findings) -> list[str]:
    """What to try next, chosen from what the analysis managed to determine."""
    advice = ["", "What to try:"]

    if not findings.bands:
        advice += [
            "  The screen appears blank. Is the host actually stopped at the",
            "  error, and is the iDRAC showing the host console rather than a",
            "  blanked screen? Press a key on the console and capture again.",
        ]
        return advice

    if findings.contrast < CONTRAST_FLOOR:
        advice.append(
            "  Contrast is low. Try --threshold N to place the cut manually."
        )

    advice += [
        "  - Check the saved snapshot: does it show the expected message?",
        "  - If detect.text does not match the screen word for word, fix it;",
        "    matching ignores case and spacing but not wording.",
        "  - Force the geometry with --cell WxH and --origin X,Y.",
        "  - Re-run against the snapshot with --from, no reboot needed.",
        '  - If the console is scaled or antialiased, set engine = "ocr".',
    ]
    return advice


def glyph_sheet(calibration: Calibration, *, columns: int = 6) -> list[str]:
    """The learned font, laid out for a person to look at.

    Tier 11 produces evidence rather than a verdict, and this is the evidence
    for the claim underneath everything else: that calibration recovered the
    console's actual font. A pixel-delta of zero says the glyphs are
    self-consistent; only looking at them says they are the letters they
    claim to be. Anyone can tell a 'D' from a smear.
    """
    out = [
        f"{len(calibration.glyphs)} glyphs, "
        f"{calibration.cell_width}x{calibration.cell_height} cells, "
        f"{_hex(calibration.foreground)} on {_hex(calibration.background)}",
        "",
    ]

    chars = sorted(calibration.glyphs)
    for start in range(0, len(chars), columns):
        batch = chars[start : start + columns]
        rendered = [calibration.glyphs[char].to_rows() for char in batch]

        labels = []
        for char in batch:
            name = "space" if char == " " else char
            labels.append(name.center(calibration.cell_width))
        out.append("  ".join(labels))

        for row in range(calibration.cell_height):
            out.append("  ".join(glyph[row] for glyph in rendered))
        out.append("")

    return out


def calibration_summary(calibration: Calibration) -> list[str]:
    """A short human-facing description of a stored calibration."""
    contrast = contrast_ratio(calibration.foreground, calibration.background)
    lines = [
        f"frame       {calibration.width}x{calibration.height}",
        f"grid        {calibration.cell_width}x{calibration.cell_height} cells "
        f"at ({calibration.origin_x}, {calibration.origin_y})",
        f"region      {calibration.region[0]},{calibration.region[1]} "
        f"{calibration.region[2]}x{calibration.region[3]} "
        f"({calibration.region_pixels} px)",
        f"colours     {_hex(calibration.foreground)} on "
        f"{_hex(calibration.background)}, contrast {contrast:.1f}:1"
        f"{', inverted' if calibration.inverted else ''}",
        f"threshold   {calibration.threshold}",
        f"glyphs      {len(calibration.glyphs)}",
        f"verify      {calibration.verify_delta} px differ on re-render"
        f"{' (exact)' if calibration.exact else ''}",
        "",
        "text:",
    ]
    lines.extend(f"  {line}" for line in calibration.text)

    if contrast < CONTRAST_FLOOR:
        lines.append("")
        lines.append(
            f"WARNING: contrast {contrast:.1f}:1 is below {CONTRAST_FLOOR:.1f}:1"
        )
    return lines


def ink_sketch(mask: Bitmap, findings: Findings, *, max_width: int = 76) -> list[str]:
    """A coarse picture of where the analyser saw ink.

    When alignment fails this is usually the fastest way to see why -- a
    message split across two bands, or a screen that is not the one you
    expected, is obvious here and invisible in a number.
    """
    if not findings.bands:
        return []

    scale = max(1, -(-mask.width // max_width))
    out = ["", "  ink map (each character is roughly "
           f"{scale}x{scale} pixels):"]

    top = max(0, min(band.top for band in findings.bands) - 2)
    bottom = min(mask.height, max(band.bottom for band in findings.bands) + 3)

    for y in range(top, bottom, scale):
        row = []
        for x in range(0, mask.width, scale):
            block = 0
            for dy in range(scale):
                for dx in range(scale):
                    block += mask.at(x + dx, y + dy)
            row.append("#" if block > (scale * scale) // 4 else ".")
        out.append("  " + "".join(row))
    return out
