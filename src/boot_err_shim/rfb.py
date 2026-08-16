"""An RFB 3.8 client, kept to the smallest thing that does the job.

Design choices that matter downstream:

**Raw encoding only.** We advertise nothing else, so the server never sends us
JPEG. Lossy compression would put artefacts on glyph edges and quietly destroy
the exact pixel matching the whole detection design rests on. At one frame per
minute the bandwidth cost is irrelevant.

**SetPixelFormat is forced.** We demand 32bpp true colour with fixed shifts, so
nothing downstream branches on bit depth, endianness, or colour maps. Servers
are required to honour it.

**Every read has a deadline, not just a timeout.** A socket timeout bounds one
recv; it does nothing about a server that sends one byte per second forever. A
half-dead iDRAC is a very plausible failure here and it must not wedge the
daemon, so the deadline covers the whole operation.
"""

from __future__ import annotations

import logging
import socket
import ssl
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from .errors import AuthError, ConnectionFailed, ProtocolError, Timeout
from .frame import Frame
from .log import event, get_logger

log = get_logger("rfb")

PROTOCOL_VERSION = b"RFB 003.008\n"

# Security types.
SEC_INVALID = 0
SEC_NONE = 1
SEC_VNC_AUTH = 2

SECURITY_NAMES = {
    0: "Invalid",
    1: "None",
    2: "VNC authentication",
    5: "RA2",
    16: "Tight",
    18: "TLS",
    19: "VeNCrypt",
    30: "Apple ARD",
}

# Client-to-server message types.
MSG_SET_PIXEL_FORMAT = 0
MSG_SET_ENCODINGS = 2
MSG_FB_UPDATE_REQUEST = 3
MSG_KEY_EVENT = 4

# Server-to-client message types.
MSG_FB_UPDATE = 0
MSG_SET_COLOUR_MAP = 1
MSG_BELL = 2
MSG_CUT_TEXT = 3

ENCODING_RAW = 0

#: Sanity bound on a single FramebufferUpdate. A corrupt or hostile rectangle
#: header claiming 65535x65535 would otherwise have us wait for 17GB.
MAX_FRAME_BYTES = 256 * 1024 * 1024

#: Refuse absurd rectangle counts rather than looping on them.
MAX_RECTANGLES = 4096


class _Deadline:
    """A wall-clock budget shared across every read in one operation."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.end = time.monotonic() + seconds

    def remaining(self) -> float:
        return self.end - time.monotonic()

    def check(self, what: str) -> float:
        left = self.remaining()
        if left <= 0:
            raise Timeout(f"timed out after {self.seconds:g}s while {what}")
        return left


@dataclass
class ServerInfo:
    """What the handshake told us. Reported by `configure`."""

    width: int = 0
    height: int = 0
    name: str = ""
    security_types: tuple[int, ...] = ()
    security_used: int = 0
    tls: bool = False

    @property
    def security_description(self) -> str:
        return ", ".join(
            f"{code} {SECURITY_NAMES.get(code, 'unknown')}"
            for code in self.security_types
        )


@dataclass
class RFBClient:
    """One connection to one VNC server."""

    host: str
    port: int
    password: str | None = None
    tls: bool = False
    tls_verify: bool = False
    tls_ca: Path | None = None
    connect_timeout: int = 10
    read_timeout: int = 30

    sock: socket.socket | None = field(default=None, init=False, repr=False)
    info: ServerInfo = field(default_factory=ServerInfo, init=False)

    # -- transport ------------------------------------------------------

    def connect(self) -> ServerInfo:
        """Open the connection and complete the handshake."""
        self._open_socket()
        deadline = _Deadline(self.read_timeout)
        self._handshake_version(deadline)
        self._handshake_security(deadline)
        self._client_init(deadline)
        self._set_pixel_format()
        self._set_encodings()
        return self.info

    def _open_socket(self) -> None:
        try:
            raw = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout
            )
        except OSError as exc:
            raise ConnectionFailed(
                f"cannot connect to {self.host}:{self.port}: {exc}"
            ) from exc

        if not self.tls:
            self.sock = raw
            return

        context = ssl.create_default_context()
        if self.tls_ca is not None:
            try:
                context.load_verify_locations(str(self.tls_ca))
            except OSError as exc:
                raw.close()
                raise ConnectionFailed(f"cannot load {self.tls_ca}: {exc}") from exc
        if not self.tls_verify:
            # iDRAC ships a self-signed certificate, so verification fails
            # unless the operator has installed their own. Defaulting to off
            # keeps tls=true usable; it still defeats passive capture of the
            # VNC password, which is the realistic threat on a management LAN.
            # Announced loudly rather than done quietly.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            event(
                log,
                logging.WARNING,
                "tls.unverified",
                host=self.host,
                detail="certificate not verified; set vnc.tls_verify and "
                "vnc.tls_ca to check it",
            )

        try:
            self.sock = context.wrap_socket(raw, server_hostname=self.host)
        except (OSError, ssl.SSLError) as exc:
            raw.close()
            raise ConnectionFailed(f"TLS handshake with {self.host} failed: {exc}") from exc

    def close(self) -> None:
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> RFBClient:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- primitives -----------------------------------------------------

    def _recv(self, count: int, deadline: _Deadline, what: str) -> bytes:
        """Read exactly ``count`` bytes or raise.

        The deadline spans the whole read. A server dribbling a byte at a time
        keeps resetting a per-recv timeout but cannot outlast this.
        """
        if self.sock is None:
            raise ProtocolError("not connected")

        chunks: list[bytes] = []
        received = 0
        while received < count:
            self.sock.settimeout(deadline.check(what))
            try:
                chunk = self.sock.recv(min(count - received, 1 << 16))
            except TimeoutError as exc:
                raise Timeout(f"timed out while {what}") from exc
            except OSError as exc:
                raise ProtocolError(f"connection lost while {what}: {exc}") from exc

            if not chunk:
                raise ProtocolError(
                    f"server closed the connection while {what} "
                    f"({received} of {count} bytes)"
                )
            chunks.append(chunk)
            received += len(chunk)

        return b"".join(chunks)

    def _send(self, data: bytes, what: str) -> None:
        if self.sock is None:
            raise ProtocolError("not connected")
        try:
            self.sock.settimeout(self.read_timeout)
            self.sock.sendall(data)
        except OSError as exc:
            raise ProtocolError(f"cannot send {what}: {exc}") from exc

    def _read_reason(self, deadline: _Deadline) -> str:
        """Read a length-prefixed failure string, tolerating a missing one."""
        try:
            (length,) = struct.unpack(">I", self._recv(4, deadline, "reading reason"))
            if length > 8192:
                return "(reason implausibly long)"
            return self._recv(length, deadline, "reading reason").decode(
                "utf-8", "replace"
            )
        except ProtocolError:
            # Some servers just hang up rather than explaining themselves.
            return "(no reason given)"

    # -- handshake ------------------------------------------------------

    def _handshake_version(self, deadline: _Deadline) -> None:
        banner = self._recv(12, deadline, "reading protocol version")
        if not banner.startswith(b"RFB "):
            raise ProtocolError(
                f"not an RFB server: banner was {banner!r}"
            )
        try:
            major = int(banner[4:7])
            minor = int(banner[8:11])
        except ValueError:
            raise ProtocolError(f"malformed RFB version banner {banner!r}") from None

        if (major, minor) < (3, 8):
            # 3.3 and 3.7 differ in the security handshake. Rather than
            # implement three variants for hardware that does not need them,
            # say so plainly.
            raise ProtocolError(
                f"server speaks RFB {major}.{minor}; this client requires 3.8"
            )

        self._send(PROTOCOL_VERSION, "protocol version")

    def _handshake_security(self, deadline: _Deadline) -> None:
        (count,) = struct.unpack("B", self._recv(1, deadline, "reading security types"))
        if count == 0:
            raise AuthError(f"server refused the connection: {self._read_reason(deadline)}")

        offered = tuple(self._recv(count, deadline, "reading security types"))
        self.info.security_types = offered
        self.info.tls = self.tls

        if SEC_VNC_AUTH in offered and self.password is not None:
            chosen = SEC_VNC_AUTH
        elif SEC_NONE in offered:
            chosen = SEC_NONE
        elif SEC_VNC_AUTH in offered:
            raise AuthError(
                "server requires VNC authentication but no vnc.password is set"
            )
        else:
            raise AuthError(
                "no supported security type; server offered: "
                + (self.info.security_description or "nothing")
            )

        self.info.security_used = chosen
        self._send(struct.pack("B", chosen), "security type")

        if chosen == SEC_VNC_AUTH:
            self._vnc_auth(deadline)

        (result,) = struct.unpack(">I", self._recv(4, deadline, "reading auth result"))
        if result != 0:
            raise AuthError(f"authentication failed: {self._read_reason(deadline)}")

    def _vnc_auth(self, deadline: _Deadline) -> None:
        from .des import vnc_response

        challenge = self._recv(16, deadline, "reading auth challenge")
        password = (self.password or "").encode("utf-8", "replace")
        self._send(vnc_response(challenge, password), "auth response")

    def _client_init(self, deadline: _Deadline) -> None:
        # shared-flag 1: do not disconnect other viewers. Somebody may be
        # watching the console we are about to press a key at.
        self._send(struct.pack("B", 1), "client init")

        header = self._recv(24, deadline, "reading server init")
        width, height = struct.unpack(">HH", header[:4])
        (name_length,) = struct.unpack(">I", header[20:24])
        if name_length > 8192:
            raise ProtocolError(f"implausible desktop name length {name_length}")
        name = self._recv(name_length, deadline, "reading desktop name")

        if width == 0 or height == 0:
            raise ProtocolError(f"server reported an empty framebuffer: {width}x{height}")

        self.info.width = width
        self.info.height = height
        self.info.name = name.decode("utf-8", "replace")

    def _set_pixel_format(self) -> None:
        pixel_format = struct.pack(
            ">BBBBHHHBBBxxx",
            32,  # bits per pixel
            24,  # depth
            0,  # big-endian flag: little
            1,  # true colour
            255,
            255,
            255,  # max values
            16,
            8,
            0,  # red, green, blue shift
        )
        self._send(
            struct.pack(">Bxxx", MSG_SET_PIXEL_FORMAT) + pixel_format, "pixel format"
        )

    def _set_encodings(self) -> None:
        self._send(
            struct.pack(">BxHi", MSG_SET_ENCODINGS, 1, ENCODING_RAW), "encodings"
        )

    # -- operations -----------------------------------------------------

    def capture(self) -> Frame:
        """Request the whole framebuffer and return it."""
        deadline = _Deadline(self.read_timeout)
        width, height = self.info.width, self.info.height

        self._send(
            struct.pack(">BBHHHH", MSG_FB_UPDATE_REQUEST, 0, 0, 0, width, height),
            "framebuffer update request",
        )

        pixels = bytearray(width * height * 3)
        painted = self._await_update(deadline, pixels, width, height)

        if not painted:
            raise ProtocolError("server sent an update containing no rectangles")

        return Frame(width, height, bytes(pixels))

    def _await_update(
        self, deadline: _Deadline, pixels: bytearray, width: int, height: int
    ) -> bool:
        """Consume messages until a FramebufferUpdate has been applied."""
        while True:
            (message,) = struct.unpack(
                "B", self._recv(1, deadline, "waiting for a framebuffer update")
            )

            if message == MSG_FB_UPDATE:
                return self._read_update(deadline, pixels, width, height)
            if message == MSG_BELL:
                continue
            if message == MSG_SET_COLOUR_MAP:
                header = self._recv(5, deadline, "reading colour map header")
                (count,) = struct.unpack(">H", header[3:5])
                self._recv(count * 6, deadline, "reading colour map")
                continue
            if message == MSG_CUT_TEXT:
                header = self._recv(7, deadline, "reading cut text header")
                (length,) = struct.unpack(">I", header[3:7])
                if length > 1 << 20:
                    raise ProtocolError(f"implausible cut-text length {length}")
                self._recv(length, deadline, "reading cut text")
                continue

            raise ProtocolError(f"unexpected server message type {message}")

    def _read_update(
        self, deadline: _Deadline, pixels: bytearray, width: int, height: int
    ) -> bool:
        header = self._recv(3, deadline, "reading update header")
        (count,) = struct.unpack(">H", header[1:3])
        if count > MAX_RECTANGLES:
            raise ProtocolError(f"implausible rectangle count {count}")

        painted = False
        for index in range(count):
            rect = self._recv(12, deadline, f"reading rectangle {index + 1}/{count}")
            x, y, w, h, encoding = struct.unpack(">HHHHi", rect)

            if encoding != ENCODING_RAW:
                # We advertised Raw only; anything else is a server bug and we
                # cannot know how many bytes follow, so the stream is lost.
                raise ProtocolError(
                    f"server used encoding {encoding} after we advertised Raw only"
                )

            if w == 0 or h == 0:
                continue
            if x + w > width or y + h > height:
                raise ProtocolError(
                    f"rectangle {x},{y} {w}x{h} falls outside the "
                    f"{width}x{height} framebuffer"
                )

            byte_count = w * h * 4
            if byte_count > MAX_FRAME_BYTES:
                raise ProtocolError(f"implausible rectangle size {w}x{h}")

            data = self._recv(byte_count, deadline, f"reading {w}x{h} pixels")
            _blit(pixels, width, data, x, y, w, h)
            painted = True

        return painted

    def send_key(self, keysym: int, *, pressed: bool | None = None) -> None:
        """Send a key press followed by a release.

        ``pressed`` sends only the down or only the up event; the default
        sends both, which is what a firmware prompt expects.
        """
        events = (True, False) if pressed is None else (pressed,)
        for down in events:
            self._send(
                struct.pack(">BBxxI", MSG_KEY_EVENT, 1 if down else 0, keysym),
                "key event",
            )


def client_from_config(config) -> RFBClient:
    """Build a client from a :class:`~boot_err_shim.config.Config`."""
    return RFBClient(
        host=config.vnc.host,
        port=config.vnc.port,
        password=config.vnc.password,
        tls=config.vnc.tls,
        tls_verify=config.vnc.tls_verify,
        tls_ca=config.vnc.tls_ca,
        connect_timeout=config.vnc.connect_timeout,
        read_timeout=config.vnc.read_timeout,
    )


def _blit(
    pixels: bytearray,
    fb_width: int,
    data: bytes,
    x: int,
    y: int,
    w: int,
    h: int,
) -> None:
    """Copy a raw 32bpp rectangle into the RGB framebuffer.

    Our SetPixelFormat asks for little-endian with red at shift 16, so each
    pixel arrives as BGRX in memory order.
    """
    for row in range(h):
        source = row * w * 4
        target = ((y + row) * fb_width + x) * 3
        for column in range(w):
            s = source + column * 4
            t = target + column * 3
            pixels[t] = data[s + 2]  # red
            pixels[t + 1] = data[s + 1]  # green
            pixels[t + 2] = data[s]  # blue
