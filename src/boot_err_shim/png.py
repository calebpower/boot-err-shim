"""A minimal PNG encoder and decoder, on zlib and struct alone.

We write PNGs because every examined frame goes to a ring buffer on disk -- a
false negative is otherwise undiagnosable. We read them because ``configure
--from`` and ``test-detect`` work on saved frames, which is what lets the
detector be iterated on without rebooting a server.

The encoder writes exactly one shape: 8-bit RGB, non-interlaced, no filtering.
Nothing needs more, and a small writer has fewer places to be subtly wrong.

The decoder is deliberately more forgiving, because it reads files other tools
wrote -- a screenshot from a phone, an export from a viewer. It handles the
five filter types, greyscale, palette, alpha, and 16-bit samples. It rejects
interlaced images with a clear message rather than silently producing a
scrambled frame.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

from .errors import ImageError
from .frame import Frame

MAGIC = b"\x89PNG\r\n\x1a\n"

#: PNG colour types.
_GREY = 0
_RGB = 2
_PALETTE = 3
_GREY_ALPHA = 4
_RGBA = 6

_CHANNELS = {_GREY: 1, _RGB: 3, _PALETTE: 1, _GREY_ALPHA: 2, _RGBA: 4}

#: Refuse anything larger than this many pixels. A hostile or corrupt header
#: claiming 65535x65535 would otherwise have us allocate 12GB before failing.
MAX_PIXELS = 64 * 1024 * 1024


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def encode(frame: Frame) -> bytes:
    """Encode a frame as an 8-bit RGB PNG."""
    header = struct.pack(
        ">IIBBBBB",
        frame.width,
        frame.height,
        8,  # bit depth
        _RGB,
        0,  # deflate
        0,  # adaptive filtering
        0,  # non-interlaced
    )

    stride = frame.width * 3
    raw = bytearray()
    for y in range(frame.height):
        raw.append(0)  # filter type None
        raw += frame.data[y * stride : (y + 1) * stride]

    return (
        MAGIC
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _chunk(b"IEND", b"")
    )


def write_frame(path: Path, frame: Frame) -> Path:
    """Write a frame to ``path``, creating parents as needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode(frame))
    return path


def _iter_chunks(data: bytes):
    if not data.startswith(MAGIC):
        raise ImageError("not a PNG file (bad signature)")

    offset = len(MAGIC)
    while offset < len(data):
        if offset + 8 > len(data):
            raise ImageError("truncated PNG: incomplete chunk header")
        (length,) = struct.unpack_from(">I", data, offset)
        kind = data[offset + 4 : offset + 8]
        start = offset + 8
        end = start + length
        if end + 4 > len(data):
            raise ImageError(f"truncated PNG: {kind!r} chunk runs past end of file")

        payload = data[start:end]
        (declared,) = struct.unpack_from(">I", data, end)
        actual = zlib.crc32(kind + payload) & 0xFFFFFFFF
        if declared != actual:
            raise ImageError(f"corrupt PNG: bad CRC in {kind!r} chunk")

        yield kind, payload
        offset = end + 4


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw: bytes, height: int, stride: int, bpp: int) -> bytearray:
    out = bytearray(height * stride)
    expected = height * (stride + 1)
    if len(raw) < expected:
        raise ImageError(
            f"truncated PNG image data: {len(raw)} bytes, expected {expected}"
        )

    previous = bytearray(stride)
    for y in range(height):
        base = y * (stride + 1)
        filter_type = raw[base]
        line = bytearray(raw[base + 1 : base + 1 + stride])

        if filter_type == 0:
            pass
        elif filter_type == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                upper_left = previous[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, previous[i], upper_left)) & 0xFF
        else:
            raise ImageError(f"unknown PNG filter type {filter_type} on row {y}")

        out[y * stride : (y + 1) * stride] = line
        previous = line

    return out


def decode(data: bytes) -> Frame:
    """Decode a PNG into an 8-bit RGB frame."""
    header = None
    palette = b""
    idat = bytearray()

    for kind, payload in _iter_chunks(data):
        if kind == b"IHDR":
            if len(payload) != 13:
                raise ImageError("corrupt PNG: IHDR is not 13 bytes")
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"PLTE":
            palette = payload
        elif kind == b"IDAT":
            idat += payload
        elif kind == b"IEND":
            break

    if header is None:
        raise ImageError("corrupt PNG: no IHDR chunk")

    width, height, depth, colour, compression, filter_method, interlace = header

    if width == 0 or height == 0:
        raise ImageError(f"PNG has no area: {width}x{height}")
    if width * height > MAX_PIXELS:
        raise ImageError(f"PNG is implausibly large: {width}x{height}")
    if compression != 0:
        raise ImageError(f"unsupported PNG compression method {compression}")
    if filter_method != 0:
        raise ImageError(f"unsupported PNG filter method {filter_method}")
    if interlace != 0:
        # Better a clear refusal than a scrambled frame that then fails to
        # calibrate for reasons nobody can diagnose.
        raise ImageError(
            "interlaced PNGs are not supported; re-save without Adam7 interlacing"
        )
    if colour not in _CHANNELS:
        raise ImageError(f"unsupported PNG colour type {colour}")
    if depth not in (1, 2, 4, 8, 16):
        raise ImageError(f"unsupported PNG bit depth {depth}")
    if colour != _PALETTE and depth < 8:
        raise ImageError(f"unsupported PNG bit depth {depth} for colour type {colour}")
    if not idat:
        raise ImageError("corrupt PNG: no image data")

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise ImageError(f"corrupt PNG image data: {exc}") from exc

    channels = _CHANNELS[colour]
    bits_per_pixel = channels * depth
    stride = (width * bits_per_pixel + 7) // 8
    bpp = max(1, bits_per_pixel // 8)

    lines = _unfilter(raw, height, stride, bpp)
    return _to_rgb(lines, width, height, stride, depth, colour, palette)


def _sample_reader(line: memoryview, depth: int, index: int) -> int:
    """Read sample ``index`` from a scanline, normalised to 8 bits."""
    if depth == 8:
        return line[index]
    if depth == 16:
        return line[index * 2]  # high byte; low byte is below our precision
    # Sub-byte depths, palette images only.
    per_byte = 8 // depth
    byte = line[index // per_byte]
    shift = 8 - depth * (index % per_byte + 1)
    return (byte >> shift) & ((1 << depth) - 1)


def _to_rgb(
    lines: bytearray,
    width: int,
    height: int,
    stride: int,
    depth: int,
    colour: int,
    palette: bytes,
) -> Frame:
    out = bytearray(width * height * 3)
    channels = _CHANNELS[colour]

    for y in range(height):
        line = memoryview(lines)[y * stride : (y + 1) * stride]
        for x in range(width):
            target = (y * width + x) * 3

            if colour == _RGB:
                base = x * channels
                out[target] = _sample_reader(line, depth, base)
                out[target + 1] = _sample_reader(line, depth, base + 1)
                out[target + 2] = _sample_reader(line, depth, base + 2)
            elif colour == _RGBA:
                base = x * channels
                # Alpha is dropped rather than composited. Console captures
                # are opaque; anything else is outside what this reads.
                out[target] = _sample_reader(line, depth, base)
                out[target + 1] = _sample_reader(line, depth, base + 1)
                out[target + 2] = _sample_reader(line, depth, base + 2)
            elif colour in (_GREY, _GREY_ALPHA):
                value = _sample_reader(line, depth, x * channels)
                out[target] = out[target + 1] = out[target + 2] = value
            else:  # palette
                index = _sample_reader(line, depth, x)
                offset = index * 3
                if offset + 3 > len(palette):
                    raise ImageError(
                        f"corrupt PNG: palette index {index} outside PLTE"
                    )
                out[target : target + 3] = palette[offset : offset + 3]

    return Frame(width, height, bytes(out))


def read_frame(path: Path) -> Frame:
    """Read a PNG file into a frame."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ImageError(f"{path}: cannot read: {exc}") from exc
    return decode(data)
