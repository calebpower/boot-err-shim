"""Tier 7: images fed to the decoder.

`configure --from` and `test-detect` read PNGs somebody else produced, and the
ring buffer reads back files that may have been truncated by a full disk or a
kill. Every one of those paths must produce a typed ImageError or a valid
frame -- never a traceback out of struct, zlib, or an index.
"""

from __future__ import annotations

import sys
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))

from boot_err_shim.calibrate import analyse  # noqa: E402
from boot_err_shim.errors import AnalysisError, ImageError, ShimError  # noqa: E402
from boot_err_shim.frame import Frame  # noqa: E402
from boot_err_shim.png import decode, encode  # noqa: E402
from render_frame import THE_MESSAGE, render  # noqa: E402
from tests.fuzz import describe, iterations, rng  # noqa: E402


def seed_corpus() -> list[bytes]:
    """Valid PNGs of assorted shapes, as starting points for mutation."""
    return [
        encode(Frame(1, 1, b"\x00\x00\x00")),
        encode(Frame(4, 4, bytes(4 * 4 * 3))),
        encode(render(("Please press 'Y' to continue.",), width=120, height=64)),
        encode(render(THE_MESSAGE, width=320, height=200)),
    ]


def mutate(random, data: bytes) -> bytes:
    """Apply one seeded mutation, favouring the shapes that break parsers."""
    out = bytearray(data)
    choice = random.randrange(8)

    if choice == 0 and out:  # flip a bit
        index = random.randrange(len(out))
        out[index] ^= 1 << random.randrange(8)
    elif choice == 1 and out:  # truncate
        return bytes(out[: random.randrange(len(out))])
    elif choice == 2 and out:  # splice in random bytes
        index = random.randrange(len(out))
        count = random.randrange(1, 16)
        out[index : index + count] = bytes(
            random.randrange(256) for _ in range(count)
        )
    elif choice == 3:  # append junk
        out += bytes(random.randrange(256) for _ in range(random.randrange(1, 64)))
    elif choice == 4 and len(out) > 24:  # corrupt a declared length
        index = random.randrange(8, len(out) - 4)
        out[index : index + 4] = random.randrange(1 << 32).to_bytes(4, "big")
    elif choice == 5 and len(out) > 24:  # corrupt the IHDR body
        index = 16 + random.randrange(13)
        out[index] = random.randrange(256)
    elif choice == 6:  # entirely random
        return bytes(
            random.randrange(256) for _ in range(random.randrange(0, 512))
        )
    elif out:  # zero a run
        index = random.randrange(len(out))
        for offset in range(min(16, len(out) - index)):
            out[index + offset] = 0

    return bytes(out)


class TestFuzzDecoder(unittest.TestCase):
    def test_no_input_escapes_as_an_untyped_exception(self) -> None:
        random = rng("png-decode")
        corpus = seed_corpus()
        survived = 0
        decoded = 0

        for _ in range(iterations()):
            data = mutate(random, random.choice(corpus))
            try:
                frame = decode(data)
            except ImageError:
                survived += 1
            except Exception as exc:  # noqa: BLE001
                self.fail(
                    f"untyped {type(exc).__name__}: {exc}\n  {describe(data)}"
                )
            else:
                decoded += 1
                # A frame that decoded must be internally consistent, or the
                # length checks let something impossible through.
                self.assertEqual(
                    len(frame.data), frame.width * frame.height * 3, describe(data)
                )
                survived += 1

        self.assertEqual(survived, iterations())
        # If nothing ever decoded, the mutations are all destroying the
        # signature and this test is only exercising one early return.
        self.assertGreater(decoded, 0, "no mutated image ever decoded")

    def test_every_rejection_explains_itself(self) -> None:
        random = rng("png-messages")
        corpus = seed_corpus()
        for _ in range(iterations(150)):
            data = mutate(random, random.choice(corpus))
            try:
                decode(data)
            except ImageError as exc:
                message = str(exc)
                self.assertTrue(message.strip(), f"empty message: {describe(data)}")
                self.assertNotIn(
                    "Traceback", message, f"leaked a traceback: {describe(data)}"
                )

    def test_random_bytes_are_never_accepted_as_an_image(self) -> None:
        random = rng("png-random")
        for _ in range(iterations(200)):
            data = bytes(
                random.randrange(256) for _ in range(random.randrange(0, 256))
            )
            with self.assertRaises(ImageError):
                decode(data)

    def test_a_decoder_bomb_is_refused_promptly(self) -> None:
        # A header claiming enormous dimensions with a tiny body: the guard
        # must fire before anything tries to allocate for it.
        import struct

        from boot_err_shim.png import MAGIC

        def chunk(kind: bytes, payload: bytes) -> bytes:
            return (
                struct.pack(">I", len(payload))
                + kind
                + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
            )

        header = struct.pack(">IIBBBBB", 65535, 65535, 8, 2, 0, 0, 0)
        data = (
            MAGIC
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"\x00" * 16))
            + chunk(b"IEND", b"")
        )
        with self.assertRaises(ImageError) as caught:
            decode(data)
        self.assertIn("implausibly large", str(caught.exception))


class TestFuzzAnalysis(unittest.TestCase):
    """Frames fed to the calibration analysis.

    A frame that decoded successfully can still be nonsense -- noise, a solid
    colour, one pixel. The analyser must decline in a typed way rather than
    dividing by a zero band count or indexing past a profile.
    """

    def frames(self, random, count: int):
        for _ in range(count):
            width = random.randrange(1, 80)
            height = random.randrange(1, 60)
            style = random.randrange(4)
            if style == 0:  # uniform
                value = random.randrange(256)
                data = bytes([value]) * (width * height * 3)
            elif style == 1:  # pure noise
                data = bytes(
                    random.randrange(256) for _ in range(width * height * 3)
                )
            elif style == 2:  # sparse ink
                buffer = bytearray(width * height * 3)
                for _ in range(random.randrange(0, width * height // 2 + 1)):
                    index = random.randrange(width * height) * 3
                    buffer[index : index + 3] = b"\xff\xff\xff"
                data = bytes(buffer)
            else:  # horizontal stripes, which look band-like
                buffer = bytearray()
                for y in range(height):
                    lit = (y // max(1, random.randrange(1, 6))) % 2
                    buffer += (b"\xff\xff\xff" if lit else b"\x00\x00\x00") * width
                data = bytes(buffer)
            yield Frame(width, height, data)

    def test_no_frame_escapes_as_an_untyped_exception(self) -> None:
        random = rng("analyse")
        for frame in self.frames(random, iterations(120)):
            try:
                analyse(frame, THE_MESSAGE)
            except AnalysisError:
                pass
            except Exception as exc:  # noqa: BLE001
                self.fail(
                    f"untyped {type(exc).__name__}: {exc} on a "
                    f"{frame.width}x{frame.height} frame (seed in tests.fuzz)"
                )

    def test_a_frame_that_calibrates_by_accident_still_verifies(self) -> None:
        # Extremely unlikely, but if random noise ever did produce a
        # calibration it must still satisfy the invariant that makes one
        # trustworthy.
        random = rng("analyse-verify")
        for frame in self.frames(random, iterations(60)):
            try:
                calibration = analyse(frame, THE_MESSAGE)
            except ShimError:
                continue
            self.assertEqual(calibration.verify_delta, 0)


if __name__ == "__main__":
    unittest.main()
