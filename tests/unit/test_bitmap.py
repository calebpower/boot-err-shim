"""Tier 1: binarisation, bands, and the one-bit image type.

Everything the detector concludes rests on this module getting two things
right: which pixels count as ink, and where the lines of text are. Both were
covered only indirectly, through calibration -- which means a subtle error
here would surface as "calibration failed" with no indication why.

The choices worth pinning are the ones a reasonable person would get wrong:
the background is the *most common* colour rather than the darkest, the
foreground is the most common colour far enough away in brightness rather than
simply the second most common, and neither assumes light text on dark.
"""

from __future__ import annotations

import unittest

from boot_err_shim.bitmap import (
    Bitmap,
    binarise,
    contrast_ratio,
    dominant_colours,
    find_bands,
    luma,
    relative_luminance,
)
from boot_err_shim.frame import Frame


def solid(width: int, height: int, colour: tuple[int, int, int]) -> Frame:
    return Frame(width, height, bytes(colour) * (width * height))


def from_rows(rows: list[str], ink=(255, 255, 255), bg=(0, 0, 0)) -> Frame:
    """Build a frame from '#' and '.' rows."""
    width, height = len(rows[0]), len(rows)
    data = bytearray()
    for row in rows:
        for char in row:
            data += bytes(ink if char == "#" else bg)
    return Frame(width, height, bytes(data))


class TestLuma(unittest.TestCase):
    def test_black_and_white(self) -> None:
        self.assertEqual(luma((0, 0, 0)), 0)
        self.assertGreaterEqual(luma((255, 255, 255)), 250)

    def test_green_is_brighter_than_blue(self) -> None:
        # Perceptual weighting, not a plain average -- an amber console and a
        # blue one are not equally bright at the same channel value.
        self.assertGreater(luma((0, 255, 0)), luma((0, 0, 255)))

    def test_red_sits_between_them(self) -> None:
        self.assertGreater(luma((255, 0, 0)), luma((0, 0, 255)))
        self.assertLess(luma((255, 0, 0)), luma((0, 255, 0)))

    def test_it_is_monotone_in_brightness(self) -> None:
        values = [luma((v, v, v)) for v in range(0, 256, 15)]
        self.assertEqual(values, sorted(values))


class TestContrast(unittest.TestCase):
    def test_the_extremes(self) -> None:
        self.assertAlmostEqual(contrast_ratio((255, 255, 255), (0, 0, 0)), 21.0, 1)
        self.assertAlmostEqual(contrast_ratio((0, 0, 0), (0, 0, 0)), 1.0, 3)

    def test_it_is_symmetric(self) -> None:
        # Which colour is called foreground must not change the answer.
        a, b = (200, 200, 200), (30, 30, 30)
        self.assertAlmostEqual(contrast_ratio(a, b), contrast_ratio(b, a), 6)

    def test_relative_luminance_bounds(self) -> None:
        self.assertAlmostEqual(relative_luminance((0, 0, 0)), 0.0, 6)
        self.assertAlmostEqual(relative_luminance((255, 255, 255)), 1.0, 6)

    def test_it_is_not_a_channel_difference(self) -> None:
        # Mid grey on black and white on mid grey differ by the same raw
        # amount per channel and are nowhere near equally legible.
        self.assertNotAlmostEqual(
            contrast_ratio((128, 128, 128), (0, 0, 0)),
            contrast_ratio((255, 255, 255), (128, 128, 128)),
            places=1,
        )


class TestDominantColours(unittest.TestCase):
    def test_most_common_first(self) -> None:
        frame = from_rows(["#..", "...", "..."])
        colours = dominant_colours(frame)
        self.assertEqual(colours[0][0], (0, 0, 0))
        self.assertEqual(colours[0][1], 8)
        self.assertEqual(colours[1][0], (255, 255, 255))

    def test_a_single_colour_frame(self) -> None:
        colours = dominant_colours(solid(4, 4, (7, 8, 9)))
        self.assertEqual(len(colours), 1)
        self.assertEqual(colours[0], ((7, 8, 9), 16))


class TestBinarise(unittest.TestCase):
    def test_light_text_on_dark(self) -> None:
        result = binarise(from_rows(["#..", "..#"]))
        self.assertEqual(result.background, (0, 0, 0))
        self.assertEqual(result.foreground, (255, 255, 255))
        self.assertFalse(result.inverted)
        self.assertEqual(result.mask.at(0, 0), 1)
        self.assertEqual(result.mask.at(1, 0), 0)
        self.assertEqual(result.mask.at(2, 1), 1)

    def test_dark_text_on_light(self) -> None:
        # The console may be either way round, and nothing here may assume.
        result = binarise(from_rows(["#..", "..#"], ink=(0, 0, 0), bg=(255, 255, 255)))
        self.assertEqual(result.background, (255, 255, 255))
        self.assertTrue(result.inverted)
        self.assertEqual(result.mask.at(0, 0), 1, "the dark text should be ink")
        self.assertEqual(result.mask.at(1, 0), 0)

    def test_amber_on_black(self) -> None:
        result = binarise(from_rows(["#..", "..."], ink=(255, 176, 0)))
        self.assertEqual(result.foreground, (255, 176, 0))
        self.assertEqual(result.mask.count(), 1)

    def test_background_is_the_most_common_not_the_darkest(self) -> None:
        # A mostly-white screen with a little black text is dark text on
        # light, however unusual that looks.
        rows = ["." * 10 for _ in range(10)]
        rows[0] = "#" + "." * 9
        result = binarise(from_rows(rows, ink=(0, 0, 0), bg=(255, 255, 255)))
        self.assertEqual(result.background, (255, 255, 255))

    def test_a_near_background_shade_is_not_taken_as_text(self) -> None:
        # Anti-aliasing or a subtly different black must not be promoted to
        # foreground just because it is the second most common colour.
        rows = []
        for y in range(8):
            rows.append("".join("#" if (x == 0 and y == 0) else "." for x in range(8)))
        frame = from_rows(rows)
        data = bytearray(frame.data)
        # Paint several pixels a hair off the background.
        for index in range(1, 6):
            offset = index * 3
            data[offset : offset + 3] = bytes((4, 4, 4))
        result = binarise(Frame(frame.width, frame.height, bytes(data)))
        self.assertEqual(result.foreground, (255, 255, 255))

    def test_a_blank_screen_yields_no_ink(self) -> None:
        result = binarise(solid(8, 8, (0, 0, 0)))
        self.assertEqual(result.mask.count(), 0)
        self.assertEqual(result.ink_fraction, 0.0)

    def test_a_blank_light_screen_also_yields_no_ink(self) -> None:
        result = binarise(solid(8, 8, (255, 255, 255)))
        self.assertEqual(result.mask.count(), 0)

    def test_ink_fraction(self) -> None:
        result = binarise(from_rows(["##..", "....", "....", "...."]))
        self.assertAlmostEqual(result.ink_fraction, 2 / 16)

    def test_an_explicit_threshold_is_honoured(self) -> None:
        # The background has to be unambiguous. On a two-pixel frame both
        # colours appear once, the grey one wins the tie by being first, and
        # the frame is then read as dark text on grey -- which is a correct
        # reading of a meaningless picture, not a threshold bug.
        frame = from_rows(["#...", "....", "...."], ink=(100, 100, 100))
        self.assertEqual(binarise(frame, threshold=50).mask.at(0, 0), 1)
        self.assertEqual(binarise(frame, threshold=200).mask.at(0, 0), 0)

    def test_the_threshold_boundary(self) -> None:
        # Ink is strictly above the threshold, so a pixel sitting exactly on
        # it is background.
        frame = from_rows(["#...", "....", "...."], ink=(100, 100, 100))
        value = luma((100, 100, 100))
        self.assertEqual(binarise(frame, threshold=value).mask.at(0, 0), 0)
        self.assertEqual(binarise(frame, threshold=value - 1).mask.at(0, 0), 1)

    def test_a_colour_tie_resolves_the_same_way_every_time(self) -> None:
        # When two colours appear equally often the choice of background is
        # arbitrary, but it must not vary between runs -- a calibration that
        # depended on it would work once and fail the next time.
        frame = from_rows(["#."], ink=(100, 100, 100))
        first = binarise(frame)
        for _ in range(5):
            again = binarise(frame)
            self.assertEqual(again.background, first.background)
            self.assertEqual(again.foreground, first.foreground)
            self.assertEqual(again.mask.data, first.mask.data)

    def test_an_explicit_invert_is_honoured(self) -> None:
        frame = from_rows(["#."])
        forced = binarise(frame, invert=True)
        self.assertTrue(forced.inverted)
        # With inversion forced, the dark pixel becomes the ink.
        self.assertEqual(forced.mask.at(1, 0), 1)
        self.assertEqual(forced.mask.at(0, 0), 0)

    def test_contrast_is_reported(self) -> None:
        self.assertAlmostEqual(binarise(from_rows(["#."])).contrast, 21.0, places=1)


class TestBitmapOperations(unittest.TestCase):
    def setUp(self) -> None:
        self.bitmap = Bitmap.from_rows(["#..#", ".##.", "#..#"])

    def test_from_rows_and_back(self) -> None:
        rows = ["#..#", ".##.", "#..#"]
        self.assertEqual(Bitmap.from_rows(rows).to_rows(), rows)

    def test_ragged_rows_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Bitmap.from_rows(["##", "###"])

    def test_empty(self) -> None:
        empty = Bitmap.from_rows([])
        self.assertEqual((empty.width, empty.height), (0, 0))

    def test_count(self) -> None:
        self.assertEqual(self.bitmap.count(), 6)

    def test_at_outside_the_bounds_is_background(self) -> None:
        # Returning 0 rather than raising: a glyph cell at the edge of the
        # screen may legitimately extend past it.
        for x, y in [(-1, 0), (0, -1), (99, 0), (0, 99)]:
            with self.subTest(x=x, y=y):
                self.assertEqual(self.bitmap.at(x, y), 0)

    def test_row_profile(self) -> None:
        self.assertEqual(self.bitmap.row_profile(), [2, 2, 2])

    def test_column_profile(self) -> None:
        self.assertEqual(self.bitmap.column_profile(), [2, 1, 1, 2])

    def test_profiles_on_an_empty_bitmap(self) -> None:
        blank = Bitmap.empty(3, 2)
        self.assertEqual(blank.row_profile(), [0, 0])
        self.assertEqual(blank.column_profile(), [0, 0, 0])

    def test_crop(self) -> None:
        self.assertEqual(self.bitmap.crop(1, 0, 2, 2).to_rows(), ["..", "##"])

    def test_crop_past_the_edge_pads_with_background(self) -> None:
        cropped = self.bitmap.crop(3, 2, 3, 3)
        self.assertEqual(cropped.to_rows(), ["#..", "...", "..."])

    def test_crop_entirely_outside(self) -> None:
        self.assertEqual(self.bitmap.crop(50, 50, 2, 2).count(), 0)

    def test_crop_with_a_negative_origin(self) -> None:
        cropped = self.bitmap.crop(-1, -1, 3, 3)
        self.assertEqual(cropped.to_rows(), ["...", ".#.", "..#"])

    def test_differences(self) -> None:
        other = Bitmap.from_rows(["#..#", ".#..", "#..#"])
        self.assertEqual(self.bitmap.differences(other), 1)
        self.assertEqual(self.bitmap.differences(self.bitmap), 0)

    def test_differences_between_mismatched_sizes(self) -> None:
        # Wholly different rather than zero: a size mismatch must never look
        # like a perfect match.
        other = Bitmap.from_rows(["##", "##"])
        self.assertEqual(self.bitmap.differences(other), 12)
        self.assertEqual(other.differences(self.bitmap), 12)

    def test_custom_render_characters(self) -> None:
        self.assertEqual(
            Bitmap.from_rows(["#."]).to_rows(ink="X", blank=" "), ["X "]
        )

    def test_round_trip_with_custom_ink(self) -> None:
        rows = ["X  X", " XX "]
        self.assertEqual(
            Bitmap.from_rows(rows, ink="X").to_rows(ink="X", blank=" "), rows
        )


class TestFindBands(unittest.TestCase):
    def test_a_single_band(self) -> None:
        bands = find_bands(Bitmap.from_rows(["....", ".##.", "....."[:4], "...."]))
        self.assertEqual(len(bands), 1)
        self.assertEqual((bands[0].top, bands[0].bottom), (1, 1))

    def test_two_bands_separated_by_blank_rows(self) -> None:
        bands = find_bands(
            Bitmap.from_rows(["....", "###.", "....", "....", ".###", "...."])
        )
        self.assertEqual(len(bands), 2)
        self.assertEqual((bands[0].top, bands[0].bottom), (1, 1))
        self.assertEqual((bands[1].top, bands[1].bottom), (4, 4))

    def test_a_band_spanning_several_rows(self) -> None:
        bands = find_bands(Bitmap.from_rows(["....", "##..", ".##.", "..##", "...."]))
        self.assertEqual(len(bands), 1)
        self.assertEqual((bands[0].top, bands[0].bottom), (1, 3))

    def test_band_extents(self) -> None:
        bands = find_bands(Bitmap.from_rows(["......", ".#..#.", "......"]))
        self.assertEqual((bands[0].left, bands[0].right), (1, 4))
        self.assertEqual(bands[0].width, 4)
        self.assertEqual(bands[0].height, 1)

    def test_a_band_touching_the_last_row_is_closed(self) -> None:
        # The loop appends a sentinel zero for exactly this case; without it
        # a message at the bottom of the screen would be lost.
        bands = find_bands(Bitmap.from_rows(["....", "##.."]))
        self.assertEqual(len(bands), 1)
        self.assertEqual(bands[0].bottom, 1)

    def test_a_band_touching_the_first_row(self) -> None:
        bands = find_bands(Bitmap.from_rows(["##..", "...."]))
        self.assertEqual(bands[0].top, 0)

    def test_a_blank_bitmap_has_no_bands(self) -> None:
        self.assertEqual(find_bands(Bitmap.empty(4, 4)), [])

    def test_a_fully_inked_bitmap_is_one_band(self) -> None:
        bands = find_bands(Bitmap.from_rows(["####", "####"]))
        self.assertEqual(len(bands), 1)
        self.assertEqual((bands[0].top, bands[0].bottom), (0, 1))

    def test_min_ink_ignores_specks(self) -> None:
        # A single stray pixel row should not read as a line of text when a
        # threshold is set.
        mask = Bitmap.from_rows(["....", "#...", "....", "###.", "...."])
        self.assertEqual(len(find_bands(mask)), 2)
        self.assertEqual(len(find_bands(mask, min_ink=2)), 1)

    def test_three_lines_of_text_give_a_consistent_pitch(self) -> None:
        # The property calibration relies on to estimate the cell height.
        rows = []
        for line in range(3):
            rows.append("." * 8)
            rows.append("#" * 8)
            rows.append("." * 8)
        bands = find_bands(Bitmap.from_rows(rows))
        self.assertEqual(len(bands), 3)
        pitches = [bands[i + 1].top - bands[i].top for i in range(2)]
        self.assertEqual(pitches, [3, 3])


if __name__ == "__main__":
    unittest.main()
