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

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise ImageError(f"pixel ({x}, {y}) outside {self.width}x{self.height}")
        offset = (y * self.width + x) * BPP
        return (
            self.data[offset],
            self.data[offset + 1],
            self.data[offset + 2],
        )
