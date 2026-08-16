"""Tier 1: the calibration analysis.

The load-bearing test here is the round trip: render a message with a font we
chose, hand the frame to the analyser, and require it to recover that exact
font. An analyser that cannot rediscover a font it was just shown will not
manage one it has never seen.

Everything else checks the edges around that -- other cell sizes, inverted
colours, integer scaling, the message somewhere unexpected on a busy screen,
and above all the cases where it must **fail** rather than invent an
alignment. A calibration that verifies at zero has proved it can reproduce the
screen it came from; one that quietly settles for approximate has proved
nothing, and would then authorise keystrokes.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))

from boot_err_shim.bitmap import Bitmap  # noqa: E402
from boot_err_shim.calibrate import (  # noqa: E402
    FORMAT_VERSION,
    Calibration,
    analyse,
    check_calibration,
    normalise,
)
from boot_err_shim.errors import (  # noqa: E402
    AnalysisError,
    CalibrationError,
    CalibrationStale,
)
from boot_err_shim.frame import Frame  # noqa: E402
from render_frame import THE_MESSAGE, blur, glyph, render, upscale  # noqa: E402


class TestRoundTrip(unittest.TestCase):
    """Render with a known font; require the analyser to recover it."""

    def check(self, **kwargs) -> Calibration:
        cell_width = kwargs.get("cell_width", 9)
        cell_height = kwargs.get("cell_height", 16)
        frame = render(THE_MESSAGE, **kwargs)
        calibration = analyse(frame, THE_MESSAGE)

        self.assertEqual(calibration.cell_width, cell_width)
        self.assertEqual(calibration.cell_height, cell_height)
        self.assertEqual(
            calibration.verify_delta, 0, "re-render did not reproduce the frame"
        )
        return calibration

    def test_the_default_geometry(self) -> None:
        self.check()

    def test_eight_by_sixteen(self) -> None:
        self.check(cell_width=8, cell_height=16)

    def test_eight_by_fourteen(self) -> None:
        self.check(cell_width=8, cell_height=14)

    def test_eight_by_eight(self) -> None:
        self.check(cell_width=8, cell_height=8)

    def test_nine_pixel_cells(self) -> None:
        # VGA text mode 3 uses 9-wide cells with the ninth column replicated;
        # assuming 8 is the classic way to be one pixel wrong per character.
        self.check(cell_width=9, cell_height=16)

    def test_a_large_uefi_sized_font(self) -> None:
        self.check(cell_width=16, cell_height=32)

    def test_an_unusual_origin(self) -> None:
        self.check(origin_x=13, origin_y=7)

    def test_the_message_at_the_very_top_left(self) -> None:
        self.check(origin_x=0, origin_y=0)

    def test_inverted_colours(self) -> None:
        # Black on white. The analyser must not assume light-on-dark.
        calibration = self.check(
            foreground=(0, 0, 0), background=(255, 255, 255)
        )
        self.assertTrue(calibration.inverted)
        self.assertEqual(calibration.background, (255, 255, 255))

    def test_amber_on_black(self) -> None:
        self.check(foreground=(255, 176, 0))

    def test_integer_upscaling(self) -> None:
        # An iDRAC doubling the console. Every glyph is still exact, just
        # bigger, so the learned cell is bigger too.
        frame = upscale(render(THE_MESSAGE, cell_width=8, cell_height=16), 2)
        calibration = analyse(frame, THE_MESSAGE)
        self.assertEqual(calibration.cell_width, 16)
        self.assertEqual(calibration.cell_height, 32)
        self.assertEqual(calibration.verify_delta, 0)

    def test_a_speck_of_noise_elsewhere_on_screen(self) -> None:
        frame = render(THE_MESSAGE, noise=6)
        calibration = analyse(frame, THE_MESSAGE)
        self.assertEqual(calibration.verify_delta, 0)

    def test_a_single_line_message(self) -> None:
        lines = ("Please press 'Y' to continue.",)
        frame = render(lines)
        calibration = analyse(frame, lines)
        self.assertEqual(calibration.verify_delta, 0)


class TestGlyphsRecovered(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = render(THE_MESSAGE, cell_width=9, cell_height=16)
        self.calibration = analyse(self.frame, THE_MESSAGE)

    def test_every_character_in_the_message_was_learned(self) -> None:
        expected = set("".join(THE_MESSAGE))
        self.assertEqual(set(self.calibration.glyphs), expected)

    def test_glyphs_are_the_right_size(self) -> None:
        for char, bitmap in self.calibration.glyphs.items():
            with self.subTest(char=char):
                self.assertEqual(bitmap.width, self.calibration.cell_width)
                self.assertEqual(bitmap.height, self.calibration.cell_height)

    def test_the_space_glyph_is_blank(self) -> None:
        self.assertEqual(self.calibration.glyphs[" "].count(), 0)

    def test_letters_are_not_blank(self) -> None:
        # A font where everything came out empty would "verify" perfectly and
        # match every screen ever shown.
        for char in "Disabling":
            with self.subTest(char=char):
                self.assertGreater(self.calibration.glyphs[char].count(), 0)

    def test_distinct_characters_got_distinct_bitmaps(self) -> None:
        seen: dict[bytes, str] = {}
        for char, bitmap in self.calibration.glyphs.items():
            if bitmap.count() == 0:
                continue
            self.assertNotIn(
                bitmap.data,
                seen,
                f"{char!r} and {seen.get(bitmap.data)!r} share a bitmap",
            )
            seen[bitmap.data] = char

    def test_the_learned_glyphs_match_the_ones_rendered(self) -> None:
        """Not merely self-consistent: equal to the font actually rendered.

        Allowing for the grid offset. The analyser is free to settle on a grid
        a pixel or two left of and above the renderer's -- any alignment that
        fully contains each glyph is equally valid, and it picks the leftmost
        one that does. What must hold is that the learned cell contains the
        rendered glyph at exactly that offset.
        """
        cell_width = self.calibration.cell_width
        cell_height = self.calibration.cell_height
        dx = 72 - self.calibration.origin_x
        dy = 208 - self.calibration.origin_y
        self.assertGreaterEqual(dx, 0)
        self.assertGreaterEqual(dy, 0)

        for char in "DPY'. bnt":
            with self.subTest(char=char):
                rows = [[0] * cell_width for _ in range(cell_height)]
                for y, row in enumerate(glyph(char, 9, 16)):
                    for x, on in enumerate(row):
                        if on:
                            self.assertLess(
                                x + dx, cell_width, "glyph pushed out of its cell"
                            )
                            self.assertLess(y + dy, cell_height)
                            rows[y + dy][x + dx] = 1
                expected = bytes(value for row in rows for value in row)
                self.assertEqual(self.calibration.glyphs[char].data, expected)


class TestRegion(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = render(THE_MESSAGE, origin_x=72, origin_y=208)
        self.calibration = analyse(self.frame, THE_MESSAGE)

    def test_region_covers_the_message(self) -> None:
        x, y, width, height = self.calibration.region
        self.assertLessEqual(x, 72)
        self.assertLessEqual(y, 208)
        self.assertGreaterEqual(width, 57 * 9)
        self.assertGreaterEqual(height, 3 * 16)

    def test_region_stays_inside_the_frame(self) -> None:
        x, y, width, height = self.calibration.region
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + width, self.frame.width)
        self.assertLessEqual(y + height, self.frame.height)

    def test_region_mask_has_ink_in_it(self) -> None:
        self.assertGreater(self.calibration.region_mask.count(), 100)

    def test_region_mask_matches_the_region_size(self) -> None:
        _, _, width, height = self.calibration.region
        self.assertEqual(self.calibration.region_mask.width, width)
        self.assertEqual(self.calibration.region_mask.height, height)


class TestMustFail(unittest.TestCase):
    """Cases where inventing an answer would be worse than refusing."""

    def test_a_blank_screen(self) -> None:
        frame = Frame(320, 200, bytes(320 * 200 * 3))
        with self.assertRaises(AnalysisError) as caught:
            analyse(frame, THE_MESSAGE)
        self.assertIn("no text", str(caught.exception))

    def test_a_screen_with_the_wrong_text(self) -> None:
        frame = render(
            (
                "No boot device available.",
                "Press F1 to retry boot.",
                "Press F2 for setup utility.",
            )
        )
        with self.assertRaises(AnalysisError):
            analyse(frame, THE_MESSAGE)

    def test_a_screen_with_too_few_lines(self) -> None:
        frame = render(("Only one line here.",))
        with self.assertRaises(AnalysisError) as caught:
            analyse(frame, THE_MESSAGE)
        self.assertIn("has 3", str(caught.exception))

    def test_a_blurred_screen_is_refused_not_approximated(self) -> None:
        # Non-integer rescaling by the iDRAC. Settling for a close-enough
        # calibration here is how false positives get authorised later.
        frame = blur(render(THE_MESSAGE))
        with self.assertRaises(AnalysisError):
            analyse(frame, THE_MESSAGE)

    def test_no_configured_text(self) -> None:
        with self.assertRaises(AnalysisError):
            analyse(render(THE_MESSAGE), ())

    def test_a_nearly_perfect_screen_is_still_refused(self) -> None:
        """Almost consistent is not consistent.

        The blurred case above fails loudly -- dozens of characters disagree
        and the search abandons each grid early. This is the quiet one: the
        message is correct except for a handful of pixels inside a single
        glyph, so one occurrence of a repeated letter no longer matches the
        others. The pixel delta is tiny, and accepting a tiny delta is exactly
        how a calibration that cannot reproduce its own source screen ends up
        authorising keystrokes.
        """
        frame = render(THE_MESSAGE, origin_x=72, origin_y=208)
        data = bytearray(frame.data)

        # Invert a few pixels inside the cell of one 'l' in "Disabling",
        # a letter that recurs throughout the message.
        column = THE_MESSAGE[0].index("l")
        for dx in range(2, 6):
            x = 72 + column * 9 + dx
            y = 208 + 6
            offset = (y * frame.width + x) * 3
            lit = data[offset] > 128
            data[offset : offset + 3] = (
                b"\x00\x00\x00" if lit else b"\xc0\xc0\xc0"
            )

        nudged = Frame(frame.width, frame.height, bytes(data))
        self.assertNotEqual(nudged.data, frame.data, "the fixture changed nothing")

        with self.assertRaises(AnalysisError) as caught:
            analyse(nudged, THE_MESSAGE)
        self.assertIn("conflicting", str(caught.exception))

    def test_failure_still_reports_what_it_found(self) -> None:
        frame = render(("No boot device available.", "Press F1.", "Press F2."))
        with self.assertRaises(AnalysisError) as caught:
            analyse(frame, THE_MESSAGE)
        findings = caught.exception.findings
        self.assertIsNotNone(findings)
        self.assertEqual((findings.width, findings.height), frame.size)
        self.assertGreater(len(findings.bands), 0)
        self.assertGreater(findings.contrast, 1.0)

    def test_failure_on_a_blank_screen_reports_zero_bands(self) -> None:
        frame = Frame(64, 32, bytes(64 * 32 * 3))
        with self.assertRaises(AnalysisError) as caught:
            analyse(frame, THE_MESSAGE)
        self.assertEqual(len(caught.exception.findings.bands), 0)


class TestBusyScreen(unittest.TestCase):
    def test_finds_the_message_among_other_text(self) -> None:
        lines = (
            "BIOS Version 2.19.0",
            "Memory: 262144 MB",
            *THE_MESSAGE,
            "Press F2 for setup",
        )
        frame = render(lines, origin_y=64)
        calibration = analyse(frame, THE_MESSAGE)
        self.assertEqual(calibration.verify_delta, 0)

    def test_the_located_region_is_the_message_not_the_whole_screen(self) -> None:
        lines = ("BIOS Version 2.19.0", *THE_MESSAGE, "Press F2 for setup")
        frame = render(lines, origin_y=64)
        calibration = analyse(frame, THE_MESSAGE)
        # Three lines tall, not five.
        self.assertLessEqual(calibration.region[3], 3 * calibration.cell_height)


class TestHints(unittest.TestCase):
    """Operator overrides for when the automatic search cannot cope."""

    def test_forcing_the_cell_size(self) -> None:
        frame = render(THE_MESSAGE, cell_width=9, cell_height=16)
        calibration = analyse(frame, THE_MESSAGE, cell=(9, 16))
        self.assertEqual(calibration.verify_delta, 0)

    def test_forcing_a_wrong_cell_size_fails_rather_than_drifting(self) -> None:
        frame = render(THE_MESSAGE, cell_width=9, cell_height=16)
        with self.assertRaises(AnalysisError):
            analyse(frame, THE_MESSAGE, cell=(7, 16))

    def test_forcing_the_threshold(self) -> None:
        frame = render(THE_MESSAGE)
        calibration = analyse(frame, THE_MESSAGE, threshold=96)
        self.assertEqual(calibration.threshold, 96)

    def test_forcing_invert_on_a_light_console(self) -> None:
        frame = render(
            THE_MESSAGE, foreground=(0, 0, 0), background=(255, 255, 255)
        )
        calibration = analyse(frame, THE_MESSAGE, invert=True)
        self.assertEqual(calibration.verify_delta, 0)


class TestNormalise(unittest.TestCase):
    def test_collapses_whitespace(self) -> None:
        self.assertEqual(normalise("  a   b \t c "), "a b c")

    def test_folds_case(self) -> None:
        self.assertEqual(normalise("Please PRESS 'Y'"), "please press 'y'")

    def test_empty(self) -> None:
        self.assertEqual(normalise("   "), "")

    def test_apostrophes_survive(self) -> None:
        self.assertIn("'y'", normalise("Please press 'Y' to continue."))


class TestPersistence(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)
        self.calibration = analyse(render(THE_MESSAGE), THE_MESSAGE)
        self.path = self.dir / "calibration.toml"

    def test_save_then_load(self) -> None:
        self.calibration.save(self.path)
        loaded = Calibration.load(self.path)
        self.assertEqual(loaded.cell_width, self.calibration.cell_width)
        self.assertEqual(loaded.cell_height, self.calibration.cell_height)
        self.assertEqual(loaded.origin_x, self.calibration.origin_x)
        self.assertEqual(loaded.origin_y, self.calibration.origin_y)
        self.assertEqual(loaded.region, self.calibration.region)
        self.assertEqual(loaded.text, self.calibration.text)

    def test_glyphs_survive_the_round_trip(self) -> None:
        self.calibration.save(self.path)
        loaded = Calibration.load(self.path)
        self.assertEqual(set(loaded.glyphs), set(self.calibration.glyphs))
        for char, bitmap in self.calibration.glyphs.items():
            with self.subTest(char=char):
                self.assertEqual(loaded.glyphs[char].data, bitmap.data)

    def test_region_mask_survives_the_round_trip(self) -> None:
        self.calibration.save(self.path)
        loaded = Calibration.load(self.path)
        self.assertEqual(loaded.region_mask.data, self.calibration.region_mask.data)

    def test_the_file_is_human_readable(self) -> None:
        # The glyphs are stored as rows of # and . on purpose: a font you can
        # eyeball in the file is worth a few kilobytes.
        self.calibration.save(self.path)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("#", text)
        self.assertIn("[glyphs]", text)
        self.assertIn("[grid]", text)

    def test_writing_is_atomic(self) -> None:
        self.calibration.save(self.path)
        leftovers = [p for p in self.dir.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_quotes_and_backslashes_in_text_survive(self) -> None:
        lines = ('He said "hi" \\ bye.', "Please press 'Y' to continue.")
        calibration = analyse(render(lines), lines)
        calibration.save(self.path)
        self.assertEqual(Calibration.load(self.path).text, calibration.text)

    def test_missing_file(self) -> None:
        with self.assertRaises(CalibrationError):
            Calibration.load(self.dir / "absent.toml")

    def test_not_toml(self) -> None:
        self.path.write_text("this is not toml {{{", encoding="utf-8")
        with self.assertRaises(CalibrationError):
            Calibration.load(self.path)

    def test_truncated_file(self) -> None:
        self.calibration.save(self.path)
        content = self.path.read_text(encoding="utf-8")
        self.path.write_text(content[: len(content) // 2], encoding="utf-8")
        with self.assertRaises(CalibrationError):
            Calibration.load(self.path)

    def test_missing_section(self) -> None:
        self.path.write_text(f"format_version = {FORMAT_VERSION}\n", encoding="utf-8")
        with self.assertRaises(CalibrationError):
            Calibration.load(self.path)

    def test_a_future_format_version_is_refused(self) -> None:
        self.calibration.save(self.path)
        content = self.path.read_text(encoding="utf-8").replace(
            f"format_version = {FORMAT_VERSION}", "format_version = 99"
        )
        self.path.write_text(content, encoding="utf-8")
        with self.assertRaises(CalibrationError) as caught:
            Calibration.load(self.path)
        self.assertIn("re-run configure", str(caught.exception))

    def test_ragged_glyph_rows_are_refused(self) -> None:
        self.calibration.save(self.path)
        lines = self.path.read_text(encoding="utf-8").splitlines()

        # Lengthen exactly one bitmap row. Locating it by pattern rather than
        # by a literal prefix: an earlier version of this test searched for a
        # row starting with '#', no row does, and so it corrupted nothing and
        # passed for no reason.
        import re

        pattern = re.compile(r'^(\s+")([.#]+)(",)$')
        for index, line in enumerate(lines):
            match = pattern.match(line)
            if match:
                lines[index] = f"{match.group(1)}{match.group(2)}.{match.group(3)}"
                break
        else:
            self.fail("no bitmap row found in the calibration file")

        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(CalibrationError):
            Calibration.load(self.path)


class TestStaleness(unittest.TestCase):
    def setUp(self) -> None:
        self.calibration = analyse(render(THE_MESSAGE), THE_MESSAGE)

    def test_matching_text_is_accepted(self) -> None:
        check_calibration(self.calibration, THE_MESSAGE)

    def test_whitespace_and_case_differences_are_tolerated(self) -> None:
        relaxed = tuple(line.upper() + "   " for line in THE_MESSAGE)
        check_calibration(self.calibration, relaxed)

    def test_different_text_is_rejected(self) -> None:
        with self.assertRaises(CalibrationStale):
            check_calibration(self.calibration, ("Something else entirely.",))

    def test_a_changed_word_is_rejected(self) -> None:
        # Nearly right is still wrong: the region mask was built from the old
        # wording and would no longer describe the screen.
        changed = list(THE_MESSAGE)
        changed[2] = "Please press 'N' to continue."
        with self.assertRaises(CalibrationStale):
            check_calibration(self.calibration, tuple(changed))


if __name__ == "__main__":
    unittest.main()
