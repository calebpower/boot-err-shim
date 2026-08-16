"""Tier 1: the PNG codec.

The encoder is checked against zlib directly rather than only against our own
decoder -- a round trip would pass just as happily if both halves shared a
mistake. The decoder is checked against fixtures built by hand from the spec,
including every filter type, because that is the part that reads files other
tools wrote.
"""

from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from boot_err_shim.errors import ImageError
from boot_err_shim.frame import Frame
from boot_err_shim.png import MAGIC, decode, encode, read_frame, write_frame


def gradient(width: int, height: int) -> Frame:
    data = bytearray()
    for y in range(height):
        for x in range(width):
            data += bytes([(x * 7) & 0xFF, (y * 11) & 0xFF, (x + y) & 0xFF])
    return Frame(width, height, bytes(data))


def build_png(
    width: int,
    height: int,
    depth: int,
    colour: int,
    rows: list[bytes],
    *,
    palette: bytes = b"",
    interlace: int = 0,
    compression: int = 0,
) -> bytes:
    """Assemble a PNG by hand, so the decoder is tested against the spec."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(
        ">IIBBBBB", width, height, depth, colour, compression, 0, interlace
    )
    out = MAGIC + chunk(b"IHDR", ihdr)
    if palette:
        out += chunk(b"PLTE", palette)
    out += chunk(b"IDAT", zlib.compress(b"".join(rows)))
    return out + chunk(b"IEND", b"")


class TestEncoder(unittest.TestCase):
    def test_starts_with_the_signature(self) -> None:
        self.assertTrue(encode(gradient(3, 2)).startswith(MAGIC))

    def test_header_records_the_right_geometry(self) -> None:
        data = encode(gradient(7, 5))
        # IHDR payload begins 8 (magic) + 4 (length) + 4 (type) in.
        width, height, depth, colour = struct.unpack_from(">IIBB", data, 16)
        self.assertEqual((width, height, depth, colour), (7, 5, 8, 2))

    def test_pixels_survive_a_manual_inflate(self) -> None:
        # Verified against zlib, not against our own decoder.
        frame = gradient(4, 3)
        data = encode(frame)
        start = data.index(b"IDAT") + 4
        (length,) = struct.unpack_from(">I", data, start - 8)
        raw = zlib.decompress(data[start : start + length])

        stride = frame.width * 3
        for y in range(frame.height):
            self.assertEqual(raw[y * (stride + 1)], 0, "filter byte should be None")
            row = raw[y * (stride + 1) + 1 : (y + 1) * (stride + 1)]
            self.assertEqual(row, frame.data[y * stride : (y + 1) * stride])

    def test_ends_with_iend(self) -> None:
        self.assertTrue(encode(gradient(2, 2)).endswith(b"IEND\xae\x42\x60\x82"))

    def test_single_pixel(self) -> None:
        frame = Frame(1, 1, b"\x10\x20\x30")
        self.assertEqual(decode(encode(frame)), frame)

    def test_encoding_is_deterministic(self) -> None:
        # Snapshots are compared byte for byte in the conformance tier.
        frame = gradient(6, 4)
        self.assertEqual(encode(frame), encode(frame))


class TestRoundTrip(unittest.TestCase):
    def test_various_sizes(self) -> None:
        for width, height in [(1, 1), (1, 9), (9, 1), (17, 13), (64, 48)]:
            with self.subTest(size=(width, height)):
                frame = gradient(width, height)
                self.assertEqual(decode(encode(frame)), frame)

    def test_a_realistic_console_size(self) -> None:
        frame = gradient(320, 200)
        self.assertEqual(decode(encode(frame)), frame)


class TestDecoderColourTypes(unittest.TestCase):
    def test_greyscale_expands_to_rgb(self) -> None:
        data = build_png(2, 1, 8, 0, [b"\x00" + bytes([10, 200])])
        frame = decode(data)
        self.assertEqual(frame.pixel(0, 0), (10, 10, 10))
        self.assertEqual(frame.pixel(1, 0), (200, 200, 200))

    def test_rgb(self) -> None:
        data = build_png(1, 1, 8, 2, [b"\x00" + bytes([1, 2, 3])])
        self.assertEqual(decode(data).pixel(0, 0), (1, 2, 3))

    def test_palette(self) -> None:
        palette = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])
        data = build_png(3, 1, 8, 3, [b"\x00" + bytes([2, 0, 1])], palette=palette)
        frame = decode(data)
        self.assertEqual(frame.pixel(0, 0), (0, 0, 255))
        self.assertEqual(frame.pixel(1, 0), (255, 0, 0))
        self.assertEqual(frame.pixel(2, 0), (0, 255, 0))

    def test_palette_at_one_bit_depth(self) -> None:
        # A two-colour console screenshot is a very plausible input.
        palette = bytes([0, 0, 0, 255, 255, 255])
        # 0b10100000 -> pixels 1,0,1,0,0,0,0,0
        data = build_png(4, 1, 1, 3, [b"\x00" + bytes([0b10100000])], palette=palette)
        frame = decode(data)
        self.assertEqual(
            [frame.pixel(x, 0) for x in range(4)],
            [(255, 255, 255), (0, 0, 0), (255, 255, 255), (0, 0, 0)],
        )

    def test_palette_at_four_bit_depth(self) -> None:
        palette = bytes([0, 0, 0] * 15 + [9, 9, 9])
        data = build_png(2, 1, 4, 3, [b"\x00" + bytes([0x0F])], palette=palette)
        frame = decode(data)
        self.assertEqual(frame.pixel(0, 0), (0, 0, 0))
        self.assertEqual(frame.pixel(1, 0), (9, 9, 9))

    def test_rgba_drops_alpha(self) -> None:
        data = build_png(1, 1, 8, 6, [b"\x00" + bytes([7, 8, 9, 128])])
        self.assertEqual(decode(data).pixel(0, 0), (7, 8, 9))

    def test_grey_alpha(self) -> None:
        data = build_png(1, 1, 8, 4, [b"\x00" + bytes([77, 255])])
        self.assertEqual(decode(data).pixel(0, 0), (77, 77, 77))

    def test_sixteen_bit_takes_the_high_byte(self) -> None:
        row = b"\x00" + struct.pack(">HHH", 0x1234, 0x5678, 0x9ABC)
        self.assertEqual(decode(build_png(1, 1, 16, 2, [row])).pixel(0, 0), (0x12, 0x56, 0x9A))

    def test_palette_index_past_the_end_is_an_error(self) -> None:
        with self.assertRaises(ImageError) as caught:
            decode(build_png(1, 1, 8, 3, [b"\x00" + bytes([9])], palette=bytes(3)))
        self.assertIn("palette index", str(caught.exception))


class TestDecoderFilters(unittest.TestCase):
    """All five filter types, hand-built from the spec."""

    def test_none(self) -> None:
        data = build_png(2, 1, 8, 2, [b"\x00" + bytes([10, 20, 30, 40, 50, 60])])
        self.assertEqual(decode(data).pixel(1, 0), (40, 50, 60))

    def test_sub(self) -> None:
        # Second pixel stored as a delta from the first.
        row = b"\x01" + bytes([10, 20, 30, 5, 5, 5])
        self.assertEqual(decode(build_png(2, 1, 8, 2, [row])).pixel(1, 0), (15, 25, 35))

    def test_up(self) -> None:
        rows = [b"\x00" + bytes([10, 20, 30]), b"\x02" + bytes([1, 2, 3])]
        self.assertEqual(decode(build_png(1, 2, 8, 2, rows)).pixel(0, 1), (11, 22, 33))

    def test_average(self) -> None:
        # row0 = 10,20,30 ; row1 filter 3: x + (left + up) // 2, left = 0
        rows = [b"\x00" + bytes([10, 20, 30]), b"\x03" + bytes([1, 1, 1])]
        self.assertEqual(decode(build_png(1, 2, 8, 2, rows)).pixel(0, 1), (6, 11, 16))

    def test_paeth(self) -> None:
        rows = [b"\x00" + bytes([10, 20, 30]), b"\x04" + bytes([1, 1, 1])]
        # left=0, up=10, upper-left=0 -> paeth predicts 10
        self.assertEqual(decode(build_png(1, 2, 8, 2, rows)).pixel(0, 1), (11, 21, 31))

    def test_unknown_filter_type_is_rejected(self) -> None:
        with self.assertRaises(ImageError) as caught:
            decode(build_png(1, 1, 8, 2, [b"\x09" + bytes([1, 2, 3])]))
        self.assertIn("filter type 9", str(caught.exception))

    def test_filters_can_differ_per_row(self) -> None:
        rows = [
            b"\x00" + bytes([10, 20, 30]),
            b"\x02" + bytes([1, 1, 1]),
            b"\x00" + bytes([9, 9, 9]),
        ]
        frame = decode(build_png(1, 3, 8, 2, rows))
        self.assertEqual(frame.pixel(0, 1), (11, 21, 31))
        self.assertEqual(frame.pixel(0, 2), (9, 9, 9))


class TestDecoderRejections(unittest.TestCase):
    def test_not_a_png(self) -> None:
        with self.assertRaises(ImageError) as caught:
            decode(b"GIF89a and some other bytes")
        self.assertIn("signature", str(caught.exception))

    def test_empty_input(self) -> None:
        with self.assertRaises(ImageError):
            decode(b"")

    def test_signature_only(self) -> None:
        with self.assertRaises(ImageError):
            decode(MAGIC)

    def test_truncated_mid_chunk(self) -> None:
        data = encode(gradient(8, 8))
        with self.assertRaises(ImageError) as caught:
            decode(data[: len(data) // 2])
        self.assertIn("truncated", str(caught.exception))

    def test_bad_crc(self) -> None:
        data = bytearray(encode(gradient(4, 4)))
        data[20] ^= 0xFF  # corrupt inside IHDR
        with self.assertRaises(ImageError) as caught:
            decode(bytes(data))
        self.assertIn("CRC", str(caught.exception))

    def test_interlaced_is_refused_clearly(self) -> None:
        data = build_png(1, 1, 8, 2, [b"\x00" + bytes([1, 2, 3])], interlace=1)
        with self.assertRaises(ImageError) as caught:
            decode(data)
        self.assertIn("interlac", str(caught.exception).lower())

    def test_unknown_colour_type(self) -> None:
        with self.assertRaises(ImageError):
            decode(build_png(1, 1, 8, 5, [b"\x00" + bytes([1])]))

    def test_unknown_compression_method(self) -> None:
        with self.assertRaises(ImageError):
            decode(build_png(1, 1, 8, 2, [b"\x00" + bytes([1, 2, 3])], compression=1))

    def test_zero_dimension(self) -> None:
        with self.assertRaises(ImageError):
            decode(build_png(0, 1, 8, 2, [b""]))

    def test_implausible_dimensions_are_refused_before_allocating(self) -> None:
        # A corrupt header claiming 60000x60000 must not have us try to
        # allocate ten gigabytes first.
        data = build_png(1, 1, 8, 2, [b"\x00" + bytes([1, 2, 3])])
        patched = bytearray(data)
        struct.pack_into(">II", patched, 16, 60000, 60000)
        # Repair the IHDR CRC so we reach the dimension check, not the CRC one.
        ihdr = bytes(patched[12:29])
        struct.pack_into(">I", patched, 29, zlib.crc32(ihdr) & 0xFFFFFFFF)
        with self.assertRaises(ImageError) as caught:
            decode(bytes(patched))
        self.assertIn("implausibly large", str(caught.exception))

    def test_truncated_image_data(self) -> None:
        with self.assertRaises(ImageError) as caught:
            decode(build_png(4, 4, 8, 2, [b"\x00" + bytes([1, 2, 3])]))
        self.assertIn("truncated", str(caught.exception))

    def test_corrupt_deflate_stream(self) -> None:
        data = bytearray(encode(gradient(4, 4)))
        index = data.index(b"IDAT") + 6
        data[index] ^= 0xFF
        # Fix the chunk CRC so the failure is in inflate, not the CRC check.
        (length,) = struct.unpack_from(">I", data, data.index(b"IDAT") - 4)
        start = data.index(b"IDAT")
        payload = bytes(data[start : start + 4 + length])
        struct.pack_into(">I", data, start + 4 + length, zlib.crc32(payload) & 0xFFFFFFFF)
        with self.assertRaises(ImageError) as caught:
            decode(bytes(data))
        self.assertIn("corrupt", str(caught.exception))

    def test_no_image_data(self) -> None:
        with self.assertRaises(ImageError):
            decode(build_png(1, 1, 8, 2, [b""]))


class TestFiles(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

    def test_write_then_read(self) -> None:
        frame = gradient(9, 7)
        path = write_frame(self.dir / "sub" / "f.png", frame)
        self.assertEqual(read_frame(path), frame)

    def test_write_creates_parents(self) -> None:
        write_frame(self.dir / "a" / "b" / "c.png", gradient(2, 2))
        self.assertTrue((self.dir / "a" / "b" / "c.png").exists())

    def test_reading_a_missing_file_is_an_image_error(self) -> None:
        with self.assertRaises(ImageError) as caught:
            read_frame(self.dir / "absent.png")
        self.assertIn("absent.png", str(caught.exception))

    def test_reading_a_non_png_is_an_image_error(self) -> None:
        path = self.dir / "notes.txt"
        path.write_text("just some text", encoding="utf-8")
        with self.assertRaises(ImageError):
            read_frame(path)


if __name__ == "__main__":
    unittest.main()
