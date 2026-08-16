"""A captured console framebuffer.

Deliberately dumb: width, height, and packed 8-bit RGB. The RFB client forces
this pixel format on the server via SetPixelFormat, so nothing downstream has
to branch on bit depth, endianness, or colour maps.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ImageError

#: Bytes per pixel. Fixed by the pixel format we demand from the server.
BPP = 3


@dataclass(frozen=True)
class Frame:
    width: int
    height: int
    #: ``width * height * 3`` bytes, row-major, no padding.
    data: bytes

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ImageError(f"frame has no area: {self.width}x{self.height}")
        expected = self.width * self.height * BPP
        if len(self.data) != expected:
            raise ImageError(
                f"frame is {len(self.data)} bytes, expected {expected} "
                f"for {self.width}x{self.height}"
            )

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def outlined(
        self,
        region: tuple[int, int, int, int],
        colour: tuple[int, int, int] = (255, 0, 0),
    ) -> Frame:
        """A copy with a rectangle drawn around ``region``.

        For the evidence a person actually looks at. "The detector matched"
        is a claim; a snapshot with a box drawn round precisely the pixels it
        compared is something an operator can check at a glance -- and if the
        box is around the wrong part of the screen, that is obvious here and
        invisible in any number.
        """
        x, y, width, height = region
        data = bytearray(self.data)

        def put(px: int, py: int) -> None:
            if 0 <= px < self.width and 0 <= py < self.height:
                offset = (py * self.width + px) * BPP
                data[offset : offset + BPP] = bytes(colour)

        for column in range(x - 1, x + width + 1):
            put(column, y - 1)
            put(column, y + height)
        for row in range(y - 1, y + height + 1):
            put(x - 1, row)
            put(x + width, row)

        return Frame(self.width, self.height, bytes(data))

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ImageError(f"pixel ({x}, {y}) outside {self.width}x{self.height}")
        offset = (y * self.width + x) * BPP
        return (
            self.data[offset],
            self.data[offset + 1],
            self.data[offset + 2],
        )
