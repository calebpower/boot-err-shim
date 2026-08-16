"""Tier 1: the framebuffer container.

Small, but the validation matters: a short buffer from a truncated
FramebufferUpdate must be rejected at construction rather than becoming an
IndexError somewhere in the middle of glyph matching, where the cause would be
unrecoverable from the traceback.
"""

from __future__ import annotations

import unittest

from boot_err_shim.errors import ImageError
from boot_err_shim.frame import BPP, Frame


def solid(width: int, height: int, colour=(0, 0, 0)) -> Frame:
    return Frame(width, height, bytes(colour) * (width * height))


class TestConstruction(unittest.TestCase):
    def test_valid_frame(self) -> None:
        frame = solid(4, 3)
        self.assertEqual(frame.size, (4, 3))
        self.assertEqual(len(frame.data), 4 * 3 * BPP)

    def test_smallest_possible_frame(self) -> None:
        self.assertEqual(solid(1, 1).size, (1, 1))

    def test_zero_width_is_rejected(self) -> None:
        with self.assertRaises(ImageError):
            Frame(0, 4, b"")

    def test_zero_height_is_rejected(self) -> None:
        with self.assertRaises(ImageError):
            Frame(4, 0, b"")

    def test_negative_dimensions_are_rejected(self) -> None:
        with self.assertRaises(ImageError):
            Frame(-1, 4, b"")

    def test_short_buffer_is_rejected(self) -> None:
        # The truncated-FramebufferUpdate case.
        with self.assertRaises(ImageError) as caught:
            Frame(4, 3, b"\x00" * (4 * 3 * BPP - 1))
        self.assertIn("expected", str(caught.exception))

    def test_long_buffer_is_rejected(self) -> None:
        with self.assertRaises(ImageError):
            Frame(4, 3, b"\x00" * (4 * 3 * BPP + 1))

    def test_error_reports_both_sizes(self) -> None:
        with self.assertRaises(ImageError) as caught:
            Frame(2, 2, b"\x00")
        message = str(caught.exception)
        self.assertIn("1 bytes", message)
        self.assertIn("12", message)


class TestPixelAccess(unittest.TestCase):
    def setUp(self) -> None:
        # 2x2, distinguishable per pixel, so a transposed index is visible.
        self.frame = Frame(
            2,
            2,
            bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]),
        )

    def test_row_major_order(self) -> None:
        self.assertEqual(self.frame.pixel(0, 0), (1, 2, 3))
        self.assertEqual(self.frame.pixel(1, 0), (4, 5, 6))
        self.assertEqual(self.frame.pixel(0, 1), (7, 8, 9))
        self.assertEqual(self.frame.pixel(1, 1), (10, 11, 12))

    def test_x_and_y_are_not_transposed(self) -> None:
        # On a square frame a transposition is invisible; assert on a
        # rectangular one where it is not.
        frame = Frame(3, 1, bytes(range(9)))
        self.assertEqual(frame.pixel(2, 0), (6, 7, 8))
        with self.assertRaises(ImageError):
            frame.pixel(0, 2)

    def test_out_of_range_is_an_image_error(self) -> None:
        for x, y in [(-1, 0), (0, -1), (2, 0), (0, 2), (99, 99)]:
            with self.subTest(x=x, y=y), self.assertRaises(ImageError):
                self.frame.pixel(x, y)

    def test_error_names_the_coordinates(self) -> None:
        with self.assertRaises(ImageError) as caught:
            self.frame.pixel(9, 9)
        self.assertIn("(9, 9)", str(caught.exception))


class TestImmutability(unittest.TestCase):
    def test_frames_are_frozen(self) -> None:
        frame = solid(2, 2)
        with self.assertRaises(Exception):
            frame.width = 5  # type: ignore[misc]

    def test_equal_frames_compare_equal(self) -> None:
        self.assertEqual(solid(2, 2, (7, 8, 9)), solid(2, 2, (7, 8, 9)))

    def test_different_content_compares_unequal(self) -> None:
        self.assertNotEqual(solid(2, 2, (0, 0, 0)), solid(2, 2, (1, 1, 1)))


if __name__ == "__main__":
    unittest.main()
