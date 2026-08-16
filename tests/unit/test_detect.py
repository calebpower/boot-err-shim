"""Tier 1: the runtime detector.

Half of this file is frames that must *not* match. That asymmetry is
deliberate: a false negative costs one more cycle of waiting, while a false
positive sends a keystroke to a console showing something else entirely. The
near-miss cases -- a different message, the same message with one word
changed, a blank screen, a screen at a different resolution -- are the ones
worth the most attention.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))

from boot_err_shim.calibrate import analyse  # noqa: E402
from boot_err_shim.detect import CalibratedDetector  # noqa: E402
from boot_err_shim.frame import Frame  # noqa: E402
from render_frame import THE_MESSAGE, render  # noqa: E402

OTHER_SCREENS = {
    "no boot device": (
        "No boot device available.",
        "Press F1 to retry boot, F2 for setup utility.",
        "Press F5 to run onboard diagnostics.",
    ),
    "a different controller error": (
        "PERC H730P Mini: firmware is in recovery mode.",
        "Please contact technical support to resolve this issue.",
        "Press any key to continue.",
    ),
    "an ordinary POST screen": (
        "BIOS Version 2.19.0",
        "Memory: 262144 MB installed",
        "Initializing storage controllers...",
    ),
}


class DetectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = render(THE_MESSAGE)
        self.calibration = analyse(self.frame, THE_MESSAGE)
        self.detector = CalibratedDetector(self.calibration, tolerance=0.02)


class TestMatches(DetectorTest):
    def test_the_frame_it_was_calibrated_on(self) -> None:
        result = self.detector.detect(self.frame)
        self.assertTrue(result.matched)
        self.assertEqual(result.detail, "region")
        self.assertEqual(result.difference, 0.0)

    def test_an_identical_frame_rendered_again(self) -> None:
        self.assertTrue(self.detector.detect(render(THE_MESSAGE)).matched)

    def test_the_message_moved_down_a_line(self) -> None:
        # The region matcher fails and the glyph decoder rescues it. Firmware
        # does not always draw at the same row.
        moved = render(THE_MESSAGE, origin_y=208 + 16)
        result = self.detector.detect(moved)
        self.assertTrue(result.matched)
        self.assertEqual(result.detail, "glyph")

    def test_the_message_with_other_text_around_it(self) -> None:
        busy = render(
            ("BIOS Version 2.19.0", "Memory: 262144 MB", *THE_MESSAGE),
            origin_y=208,
        )
        self.assertTrue(self.detector.detect(busy).matched)

    def test_the_message_at_a_different_resolution(self) -> None:
        # The console changed video mode but still shows the prompt. The
        # region matcher cannot help -- its rectangle no longer refers to the
        # same part of the screen -- so the glyph decoder has to carry it, and
        # pressing the key here is right.
        other = render(THE_MESSAGE, width=800, height=600)
        result = self.detector.detect(other)
        self.assertTrue(result.matched)
        self.assertEqual(result.detail, "glyph")

    def test_a_few_stray_pixels_are_within_tolerance(self) -> None:
        speckled = render(THE_MESSAGE, noise=3)
        self.assertTrue(self.detector.detect(speckled).matched)


class TestMustNotMatch(DetectorTest):
    def test_a_blank_screen(self) -> None:
        blank = Frame(640, 400, bytes(640 * 400 * 3))
        self.assertFalse(self.detector.detect(blank).matched)

    def test_other_screens(self) -> None:
        for label, lines in OTHER_SCREENS.items():
            with self.subTest(screen=label):
                self.assertFalse(self.detector.detect(render(lines)).matched)

    def test_a_screen_sharing_one_line_with_ours(self) -> None:
        # The middle line is word-for-word identical to the real message.
        # Matching on any one line would fire here.
        lines = (
            "PERC H730P Mini: firmware is in recovery mode.",
            "Please contact technical support to resolve this issue.",
            "Press any key to continue.",
        )
        self.assertFalse(self.detector.detect(render(lines)).matched)

    def test_the_message_with_one_word_changed(self) -> None:
        changed = list(THE_MESSAGE)
        changed[2] = "Please press 'N' to continue."
        self.assertFalse(self.detector.detect(render(tuple(changed))).matched)

    def test_a_different_resolution_without_the_message(self) -> None:
        other = render(OTHER_SCREENS["no boot device"], width=800, height=600)
        self.assertFalse(self.detector.detect(other).matched)

    def test_a_frame_of_solid_ink(self) -> None:
        solid = Frame(640, 400, bytes([255, 255, 255]) * (640 * 400))
        self.assertFalse(self.detector.detect(solid).matched)

    def test_the_message_scrolled_half_off_the_top(self) -> None:
        partial = render(THE_MESSAGE[1:], origin_y=0)
        self.assertFalse(self.detector.detect(partial).matched)


class TestReporting(DetectorTest):
    def test_a_non_match_reports_how_far_off_it_was(self) -> None:
        result = self.detector.detect(render(OTHER_SCREENS["no boot device"]))
        self.assertIsNotNone(result.difference)
        self.assertGreater(result.difference, 0.0)

    def test_a_non_match_reports_what_the_screen_said(self) -> None:
        # "No match" alone leaves an operator with nothing. "No match, the
        # screen said no boot device available" is a diagnosis.
        #
        # Only characters the calibration has seen can be read back -- 'N' is
        # not in our message, so it comes out as a replacement character. That
        # is a real limit of learning a font from one sentence, and the point
        # here is that the reader degrades into partial text rather than
        # giving up.
        result = self.detector.detect(render(OTHER_SCREENS["no boot device"]))
        self.assertIsNotNone(result.text)
        self.assertIn("boot device available.", result.text)

    def test_a_resolution_change_says_so_specifically(self) -> None:
        # Asserted against the region matcher directly: at whole-detector
        # level the glyph fallback answers first when the message is present.
        from boot_err_shim.bitmap import binarise

        frame = render(OTHER_SCREENS["no boot device"], width=800, height=600)
        mask = binarise(
            frame,
            threshold=self.calibration.threshold,
            invert=self.calibration.inverted,
        ).mask
        result = self.detector._region_match(mask, frame)
        self.assertFalse(result.matched)
        self.assertIn("frame-size-changed", result.detail)
        self.assertIn("800x600", result.detail)

    def test_detail_tokens_have_no_whitespace(self) -> None:
        for frame in (self.frame, render(OTHER_SCREENS["no boot device"])):
            self.assertNotIn(" ", self.detector.detect(frame).detail)


class TestKnownLimits(unittest.TestCase):
    """Things this detector genuinely cannot do, pinned so they stay known.

    Per the methodology: name what a test does not prove. A limitation
    somebody has written down and justified is a design decision; the same
    limitation undiscovered is a surprise at three in the morning.
    """

    def setUp(self) -> None:
        self.calibration = analyse(render(THE_MESSAGE), THE_MESSAGE)
        self.detector = CalibratedDetector(self.calibration, tolerance=0.02)

    def test_the_message_off_the_character_grid_is_not_found(self) -> None:
        """A message at an arbitrary pixel offset is missed.

        The glyph decoder walks the character grid whose phase came from
        calibration, so it reads text drawn on that grid and not text drawn
        five pixels below it. Real firmware redraws on a fixed character
        grid -- the phase is a property of the video mode, so a message that
        moves does so by whole cells, which is covered above.

        Searching every phase would cost cell_width x cell_height full-screen
        decodes per frame, which is not worth paying for a case text-mode
        firmware does not produce. If a console ever turns out to do this,
        engine = "ocr" is the documented way out.
        """
        off_grid = render(THE_MESSAGE, origin_y=208 + 5, origin_x=72 + 3)
        self.assertFalse(self.detector.detect(off_grid).matched)

    def test_only_characters_from_the_message_can_be_read_back(self) -> None:
        # The font is learned from one sentence, so the reader knows only the
        # characters in it. Everything else decodes to a replacement
        # character. That is fine for matching -- we only ever look for that
        # sentence -- but it limits how useful the logged screen text is.
        result = self.detector.detect(render(("ZZZZ QQQQ",)))
        self.assertFalse(result.matched)
        self.assertIn("�", result.text or "")


class TestScreenReading(DetectorTest):
    def test_reads_the_message_back(self) -> None:
        from boot_err_shim.bitmap import binarise

        mask = binarise(
            self.frame,
            threshold=self.calibration.threshold,
            invert=self.calibration.inverted,
        ).mask
        text = self.detector.read_screen(mask)
        for line in THE_MESSAGE:
            self.assertIn(line, text)

    def test_unknown_glyphs_do_not_crash_the_reader(self) -> None:
        # A screen containing characters the calibration never saw.
        other = render(("ZZZ QQQ XXX 12345", "@@@ %%% &&& 67890"))
        result = self.detector.detect(other)
        self.assertFalse(result.matched)
        self.assertIsNotNone(result.text)


class TestTolerance(DetectorTest):
    def test_zero_tolerance_still_matches_an_identical_frame(self) -> None:
        strict = CalibratedDetector(self.calibration, tolerance=0.0)
        self.assertTrue(strict.detect(self.frame).matched)

    def test_zero_tolerance_rejects_a_single_changed_pixel(self) -> None:
        from boot_err_shim.bitmap import binarise

        strict = CalibratedDetector(self.calibration, tolerance=0.0)
        data = bytearray(self.frame.data)
        x, y, _, _ = self.calibration.region
        offset = ((y + 8) * self.frame.width + (x + 8)) * 3
        data[offset : offset + 3] = b"\xff\xff\xff"
        nudged = Frame(self.frame.width, self.frame.height, bytes(data))
        mask = binarise(
            nudged,
            threshold=self.calibration.threshold,
            invert=self.calibration.inverted,
        ).mask
        self.assertFalse(strict._region_match(mask, nudged).matched)

    def test_a_loose_tolerance_cannot_authorise_the_wrong_message(self) -> None:
        # The rule that makes tolerance safe: a region match that is not
        # exact must be corroborated by reading the text. Without that, any
        # tolerance big enough to absorb a speck of dust is also big enough
        # to accept a different word -- one character is around 0.2% of the
        # region.
        loose = CalibratedDetector(self.calibration, tolerance=0.5)
        changed = list(THE_MESSAGE)
        changed[2] = "Please press 'N' to continue."
        self.assertFalse(loose.detect(render(tuple(changed))).matched)

    def test_a_loose_tolerance_cannot_authorise_a_blank_screen(self) -> None:
        loose = CalibratedDetector(self.calibration, tolerance=1.0)
        blank = Frame(640, 400, bytes(640 * 400 * 3))
        self.assertFalse(loose.detect(blank).matched)


if __name__ == "__main__":
    unittest.main()
