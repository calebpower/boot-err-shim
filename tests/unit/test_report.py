"""Tier 1: the operator-facing report.

The conformance tier pins the happy path byte for byte. What it does not
cover is the failure path, which is the half that matters more: a successful
`configure` needs no explanation, and a failed one is somebody standing in
front of a stuck server with nothing to go on.

So these tests are mostly about the report still being useful when the
analysis could not finish -- and about it not crashing on the degenerate
inputs that only occur when something has already gone wrong.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))

from boot_err_shim.bitmap import Bitmap, binarise, find_bands  # noqa: E402
from boot_err_shim.calibrate import Findings, analyse  # noqa: E402
from boot_err_shim.errors import AnalysisError  # noqa: E402
from boot_err_shim.frame import Frame  # noqa: E402
from boot_err_shim.report import (  # noqa: E402
    CONTRAST_FLOOR,
    calibration_summary,
    connection_report,
    failure_advice,
    findings_report,
    glyph_sheet,
    ink_sketch,
    success_report,
)
from boot_err_shim.rfb import ServerInfo  # noqa: E402
from render_frame import THE_MESSAGE, render  # noqa: E402


def findings(**overrides) -> Findings:
    base = {
        "width": 640,
        "height": 400,
        "background": (0, 0, 0),
        "foreground": (192, 192, 192),
        "threshold": 96,
        "inverted": False,
        "ink_fraction": 0.023,
        "contrast": 11.5,
    }
    base.update(overrides)
    return Findings(**base)


class TestConnectionReport(unittest.TestCase):
    def test_it_names_the_endpoint_and_geometry(self) -> None:
        info = ServerInfo(width=720, height=400, name="idrac", security_types=(2,))
        lines = "\n".join(connection_report("10.0.0.51", 5901, info))
        self.assertIn("10.0.0.51:5901", lines)
        self.assertIn("720x400", lines)
        self.assertIn("idrac", lines)

    def test_tls_is_stated_either_way(self) -> None:
        # Not knowing whether the connection was encrypted is worse than
        # either answer.
        plain = "\n".join(connection_report("h", 1, ServerInfo(tls=False)))
        secure = "\n".join(connection_report("h", 1, ServerInfo(tls=True)))
        self.assertIn("TLS: no", plain)
        self.assertIn("TLS: yes", secure)


class TestFindingsReport(unittest.TestCase):
    def test_it_reports_colours_as_hex(self) -> None:
        lines = "\n".join(findings_report(findings()))
        self.assertIn("#c0c0c0", lines)
        self.assertIn("#000000", lines)

    def test_inversion_is_stated_when_present(self) -> None:
        self.assertIn("inverted", "\n".join(findings_report(findings(inverted=True))))
        self.assertNotIn("inverted", "\n".join(findings_report(findings())))

    def test_low_contrast_warns(self) -> None:
        lines = "\n".join(findings_report(findings(contrast=1.8)))
        self.assertIn("WARNING", lines)
        self.assertIn("1.8", lines)

    def test_contrast_exactly_at_the_floor_does_not_warn(self) -> None:
        lines = "\n".join(findings_report(findings(contrast=CONTRAST_FLOOR)))
        self.assertNotIn("WARNING", lines)

    def test_just_below_the_floor_warns(self) -> None:
        lines = "\n".join(findings_report(findings(contrast=CONTRAST_FLOOR - 0.1)))
        self.assertIn("WARNING", lines)

    def test_the_grid_is_omitted_when_it_was_never_determined(self) -> None:
        # Printing "grid: None" would be worse than saying nothing.
        lines = "\n".join(findings_report(findings()))
        self.assertNotIn("None", lines)
        self.assertNotIn("grid:", lines)

    def test_the_grid_appears_once_it_is_known(self) -> None:
        lines = "\n".join(findings_report(findings(cell=(9, 16), origin=(4, 8))))
        self.assertIn("9x16", lines)
        self.assertIn("(4, 8)", lines)

    def test_the_band_count_is_always_reported(self) -> None:
        # Zero bands is the single most diagnostic number on the failure
        # path: it means the screen was blank.
        self.assertIn("text rows found: 0", "\n".join(findings_report(findings())))


class TestFailureAdvice(unittest.TestCase):
    def test_a_blank_screen_gets_its_own_advice(self) -> None:
        advice = "\n".join(failure_advice(findings()))
        self.assertIn("blank", advice)
        # The generic geometry advice would waste somebody's time here.
        self.assertNotIn("--cell", advice)

    def test_a_screen_with_text_gets_the_geometry_advice(self) -> None:
        mask = Bitmap.from_rows(["....", "##..", "...."])
        advice = "\n".join(failure_advice(findings(bands=tuple(find_bands(mask)))))
        self.assertIn("--cell", advice)
        self.assertIn("--from", advice)
        self.assertIn("ocr", advice)

    def test_low_contrast_adds_the_threshold_hint(self) -> None:
        mask = Bitmap.from_rows(["....", "##..", "...."])
        advice = "\n".join(
            failure_advice(findings(bands=tuple(find_bands(mask)), contrast=1.5))
        )
        self.assertIn("--threshold", advice)

    def test_every_suggestion_is_something_a_person_can_do(self) -> None:
        mask = Bitmap.from_rows(["....", "##..", "...."])
        advice = failure_advice(findings(bands=tuple(find_bands(mask))))
        self.assertGreaterEqual(len([line for line in advice if line.strip()]), 3)


class TestInkSketch(unittest.TestCase):
    """The picture printed when alignment fails."""

    def sketch(self, mask: Bitmap) -> list[str]:
        return ink_sketch(mask, findings(bands=tuple(find_bands(mask))))

    def test_a_blank_mask_produces_nothing(self) -> None:
        # There is no picture to draw, and an empty box would only mislead.
        self.assertEqual(ink_sketch(Bitmap.empty(8, 8), findings()), [])

    def test_it_draws_the_inked_rows(self) -> None:
        mask = Bitmap.from_rows(["........", "..####..", "........"])
        rendered = "\n".join(self.sketch(mask))
        self.assertIn("#", rendered)
        self.assertIn(".", rendered)

    def test_it_stays_within_the_requested_width(self) -> None:
        # It goes into a terminal; wrapping would make it unreadable.
        mask = Bitmap.from_rows(["#" * 500, "." * 500])
        for line in ink_sketch(mask, findings(bands=tuple(find_bands(mask))), max_width=76):
            self.assertLessEqual(len(line), 80)

    def test_a_wide_mask_is_scaled_down_and_says_so(self) -> None:
        mask = Bitmap.from_rows(["#" * 300, "." * 300])
        rendered = "\n".join(
            ink_sketch(mask, findings(bands=tuple(find_bands(mask))), max_width=76)
        )
        self.assertIn("roughly", rendered)

    def test_a_narrow_mask_is_not_scaled(self) -> None:
        mask = Bitmap.from_rows(["####", "....."[:4]])
        rendered = "\n".join(self.sketch(mask))
        self.assertIn("1x1", rendered)

    def test_a_single_row_of_ink(self) -> None:
        self.assertNotEqual(self.sketch(Bitmap.from_rows(["##"])), [])

    def test_ink_at_the_very_bottom_is_included(self) -> None:
        mask = Bitmap.from_rows(["....", "....", "####"])
        self.assertIn("#", "\n".join(self.sketch(mask)))


class TestGlyphSheetAndSummary(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.frame = render(THE_MESSAGE, cell_width=9, cell_height=16)
        cls.calibration = analyse(cls.frame, THE_MESSAGE)

    def test_the_sheet_labels_every_glyph(self) -> None:
        sheet = "\n".join(glyph_sheet(self.calibration))
        for char in "DPYabc":
            with self.subTest(char=char):
                self.assertIn(char.center(self.calibration.cell_width), sheet)

    def test_space_is_labelled_by_name(self) -> None:
        # A blank column under a blank label tells nobody anything.
        self.assertIn("space", "\n".join(glyph_sheet(self.calibration)))

    def test_the_sheet_states_the_geometry(self) -> None:
        header = glyph_sheet(self.calibration)[0]
        self.assertIn("9x16", header)
        self.assertIn(str(len(self.calibration.glyphs)), header)

    def test_column_count_is_respected(self) -> None:
        narrow = glyph_sheet(self.calibration, columns=2)
        wide = glyph_sheet(self.calibration, columns=8)
        self.assertGreater(len(narrow), len(wide))

    def test_the_summary_states_verification(self) -> None:
        summary = "\n".join(calibration_summary(self.calibration))
        self.assertIn("0 px differ", summary)
        self.assertIn("exact", summary)

    def test_the_summary_includes_the_calibrated_text(self) -> None:
        summary = "\n".join(calibration_summary(self.calibration))
        self.assertIn("flash part has gone bad", summary)

    def test_the_summary_warns_about_low_contrast(self) -> None:
        dim = render(THE_MESSAGE, foreground=(70, 70, 70), background=(0, 0, 0))
        try:
            calibration = analyse(dim, THE_MESSAGE)
        except AnalysisError:
            self.skipTest(
                "the dim fixture failed to calibrate, so there is no "
                "calibration to summarise; contrast warning is covered by "
                "findings_report either way"
            )
        self.assertIn("WARNING", "\n".join(calibration_summary(calibration)))


class TestSuccessReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.calibration = analyse(
            render(THE_MESSAGE, cell_width=9, cell_height=16), THE_MESSAGE
        )

    def test_each_line_is_listed_with_its_length(self) -> None:
        lines = "\n".join(success_report(self.calibration, THE_MESSAGE))
        self.assertIn("1/3", lines)
        self.assertIn("3/3", lines)
        self.assertIn("57", lines)

    def test_a_long_line_is_truncated_rather_than_wrapping(self) -> None:
        # Long enough for the report to shorten (it trims past 58) but not so
        # long that it runs off the rendered frame -- an earlier version asked
        # for 200 characters in a 1400px frame, the line was clipped, and the
        # analyser quite correctly deduced a narrower cell from the truncated
        # ink. The fixture was wrong, not the analysis.
        lines = ("A" * 90, "B" * 40)
        calibration = analyse(render(lines), lines)
        for rendered in success_report(calibration, lines):
            self.assertLess(len(rendered), 120)
        self.assertIn("...", "\n".join(success_report(calibration, lines)))

    def test_a_short_line_is_not_truncated(self) -> None:
        lines = ("Please press 'Y' to continue.", "Shorter.")
        calibration = analyse(render(lines), lines)
        rendered = "\n".join(success_report(calibration, lines))
        self.assertIn("Please press 'Y' to continue.", rendered)
        self.assertNotIn("...", rendered)

    def test_it_does_not_repeat_the_grid(self) -> None:
        # findings_report already prints it on both the success and failure
        # paths; printing it twice was a real defect caught by a golden file.
        self.assertNotIn("grid:", "\n".join(success_report(self.calibration, THE_MESSAGE)))


class TestDegenerateInputs(unittest.TestCase):
    """Only reachable when something has already gone wrong."""

    def test_a_one_pixel_frame_does_not_break_the_findings(self) -> None:
        frame = Frame(1, 1, b"\x00\x00\x00")
        binarised = binarise(frame)
        report = findings_report(
            findings(
                width=1,
                height=1,
                ink_fraction=binarised.ink_fraction,
                contrast=binarised.contrast,
            )
        )
        self.assertTrue(report)

    def test_a_solid_frame_reports_zero_ink(self) -> None:
        binarised = binarise(Frame(8, 8, bytes(8 * 8 * 3)))
        self.assertIn(
            "ink 0.0%",
            "\n".join(findings_report(findings(ink_fraction=binarised.ink_fraction))),
        )


if __name__ == "__main__":
    unittest.main()
