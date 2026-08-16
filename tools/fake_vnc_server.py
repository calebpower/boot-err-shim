#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""A fake VNC server, for exercising the client against transport failures.

This is a test fixture with a command line attached, not a toy. It is the only
practical way to ask "what happens when the iDRAC accepts the connection and
then stops talking?", which is the most likely real failure this program will
meet -- far likelier than the clean refusals a live server produces on demand.

It imports the **real** ``boot_err_shim.des`` to verify challenge responses
rather than carrying its own copy. A second implementation would let both
drift into agreeing on something wrong.

Faults are named on the command line or passed to :class:`FakeVNCServer`:

    uv run tools/fake_vnc_server.py --fault hang-after-accept
    uv run tools/fake_vnc_server.py --port 5901 --password secret

Run with no fault it is a working, if extremely minimal, VNC server that
serves one image forever.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from boot_err_shim.des import vnc_response  # noqa: E402

PROTOCOL_VERSION = b"RFB 003.008\n"

#: Every fault this fixture can inject, with what it simulates in the field.
FAULTS = {
    "none": "behave correctly",
    "refuse": "refuse the TCP connection outright",
    "hang-after-accept": "accept, then never send the version banner",
    "bad-banner": "send something that is not an RFB version string",
    "old-version": "claim RFB 3.3, which we do not implement",
    "truncated-banner": "send half a version banner and stop",
    "no-security-types": "offer zero security types with a reason string",
    "no-security-types-silent": "offer zero types then hang up immediately",
    "unsupported-security": "offer only VeNCrypt",
    "auth-failure": "reject the password with a reason",
    "auth-failure-silent": "reject the password then hang up",
    "truncated-challenge": "send 8 bytes of a 16-byte challenge",
    "hang-after-auth": "authenticate, then never send ServerInit",
    "zero-size": "report a 0x0 framebuffer",
    "huge-name": "claim an implausible desktop name length",
    "no-rectangles": "send an update containing zero rectangles",
    "truncated-rectangle": "stop halfway through the pixel data",
    "absurd-rectangle-count": "claim 60000 rectangles",
    "rectangle-outside-framebuffer": "send a rectangle beyond the screen",
    "wrong-encoding": "use an encoding we never advertised",
    "slow-loris": "send the framebuffer one byte at a time",
    "reset-mid-frame": "abort the connection during pixel data",
    "bell-then-frame": "send Bell messages before the update",
    "cut-text-then-frame": "send ServerCutText before the update",
    "colour-map-then-frame": "send SetColourMapEntries before the update",
    "unknown-message": "send an undefined server message type",
}


@dataclass
class FakeVNCServer:
    """Serves one connection at a time on an ephemeral port by default."""

    width: int = 64
    height: int = 32
    password: str | None = None
    fault: str = "none"
    #: RGB bytes to serve. Defaults to a recognisable pattern.
    pixels: bytes | None = None
    host: str = "127.0.0.1"
    port: int = 0
    #: Seconds a slow-loris byte is delayed by.
    slow_delay: float = 0.05

    listener: socket.socket | None = field(default=None, init=False)
    thread: threading.Thread | None = field(default=None, init=False)
    #: Key events the client sent, as (keysym, pressed).
    keys: list[tuple[int, bool]] = field(default_factory=list, init=False)
    #: Set once a client has completed the handshake.
    handshakes: int = field(default=0, init=False)
    errors: list[str] = field(default_factory=list, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)

    # -- lifecycle ------------------------------------------------------

    def start(self) -> int:
        """Begin listening. Returns the bound port."""
        if self.fault == "refuse":
            # Bind and immediately close, so the port is reliably closed
            # without depending on nothing else grabbing it.
            probe = socket.socket()
            probe.bind((self.host, 0))
            self.port = probe.getsockname()[1]
            probe.close()
            return self.port

        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((self.host, self.port))
        self.listener.listen(4)
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]

        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        return self.port

    def stop(self) -> None:
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        if self.listener is not None:
            self.listener.close()
            self.listener = None

    def __enter__(self) -> FakeVNCServer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- serving --------------------------------------------------------

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.listener.accept()  # type: ignore[union-attr]
            except TimeoutError:
                continue
            except OSError:
                return

            try:
                self._session(conn)
            except (OSError, struct.error) as exc:
                self.errors.append(f"{type(exc).__name__}: {exc}")
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _wait_for_stop(self, conn: socket.socket) -> None:
        """Hold the connection open doing nothing, until the test finishes."""
        while not self._stop.wait(0.05):
            pass

    def _session(self, conn: socket.socket) -> None:
        conn.settimeout(10)

        if self.fault == "hang-after-accept":
            return self._wait_for_stop(conn)
        if self.fault == "bad-banner":
            conn.sendall(b"HTTP/1.1 400 \r\n\r\n")
            return
        if self.fault == "truncated-banner":
            conn.sendall(b"RFB 003")
            return
        if self.fault == "old-version":
            conn.sendall(b"RFB 003.003\n")
            return

        conn.sendall(PROTOCOL_VERSION)
        client_version = _recv_exactly(conn, 12)
        if not client_version.startswith(b"RFB "):
            self.errors.append(f"client sent bad version {client_version!r}")
            return

        if not self._security(conn):
            return
        if self.fault == "hang-after-auth":
            return self._wait_for_stop(conn)
        if not self._init(conn):
            return

        self.handshakes += 1
        self._message_loop(conn)

    def _security(self, conn: socket.socket) -> bool:
        if self.fault == "no-security-types":
            conn.sendall(struct.pack("B", 0))
            reason = b"too many connections"
            conn.sendall(struct.pack(">I", len(reason)) + reason)
            return False
        if self.fault == "no-security-types-silent":
            conn.sendall(struct.pack("B", 0))
            return False
        if self.fault == "unsupported-security":
            conn.sendall(struct.pack("BB", 1, 19))  # VeNCrypt only
            return False

        types = [2] if self.password is not None else [1]
        conn.sendall(struct.pack("B", len(types)) + bytes(types))
        (chosen,) = struct.unpack("B", _recv_exactly(conn, 1))

        if chosen == 2:
            if self.fault == "truncated-challenge":
                conn.sendall(b"\x00" * 8)
                return False

            challenge = bytes(range(16))
            conn.sendall(challenge)
            answer = _recv_exactly(conn, 16)
            expected = vnc_response(
                challenge, (self.password or "").encode("utf-8")
            )
            if answer != expected or self.fault.startswith("auth-failure"):
                conn.sendall(struct.pack(">I", 1))
                if self.fault != "auth-failure-silent":
                    reason = b"authentication failed"
                    conn.sendall(struct.pack(">I", len(reason)) + reason)
                if answer != expected:
                    self.errors.append("client failed the challenge")
                return False
        elif self.fault.startswith("auth-failure"):
            conn.sendall(struct.pack(">I", 1))
            reason = b"authentication failed"
            conn.sendall(struct.pack(">I", len(reason)) + reason)
            return False

        conn.sendall(struct.pack(">I", 0))
        return True

    def _init(self, conn: socket.socket) -> bool:
        _recv_exactly(conn, 1)  # shared flag

        width = 0 if self.fault == "zero-size" else self.width
        height = 0 if self.fault == "zero-size" else self.height
        name = b"fake-vnc"
        name_length = 1 << 20 if self.fault == "huge-name" else len(name)

        pixel_format = struct.pack(
            ">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0
        )
        conn.sendall(
            struct.pack(">HH", width, height)
            + pixel_format
            + struct.pack(">I", name_length)
            + name
        )
        return self.fault not in ("zero-size", "huge-name")

    def _message_loop(self, conn: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                header = conn.recv(1)
            except TimeoutError:
                continue
            except OSError:
                return
            if not header:
                return

            kind = header[0]
            if kind == 0:  # SetPixelFormat
                _recv_exactly(conn, 19)
            elif kind == 2:  # SetEncodings
                rest = _recv_exactly(conn, 3)
                (count,) = struct.unpack(">H", rest[1:3])
                _recv_exactly(conn, count * 4)
            elif kind == 3:  # FramebufferUpdateRequest
                _recv_exactly(conn, 9)
                self._send_update(conn)
            elif kind == 4:  # KeyEvent
                body = _recv_exactly(conn, 7)
                down = body[0]
                (keysym,) = struct.unpack(">I", body[3:7])
                self.keys.append((keysym, bool(down)))
            elif kind == 5:  # PointerEvent
                _recv_exactly(conn, 5)
            elif kind == 6:  # ClientCutText
                rest = _recv_exactly(conn, 7)
                (length,) = struct.unpack(">I", rest[3:7])
                _recv_exactly(conn, length)
            else:
                self.errors.append(f"unknown client message {kind}")
                return

    # -- framebuffer ----------------------------------------------------

    def framebuffer(self) -> bytes:
        """The RGB image this server serves."""
        if self.pixels is not None:
            return self.pixels
        out = bytearray()
        for y in range(self.height):
            for x in range(self.width):
                out += bytes([(x * 3) & 0xFF, (y * 5) & 0xFF, ((x ^ y) * 7) & 0xFF])
        return bytes(out)

    def _raw_pixels(self) -> bytes:
        """RGB converted to the BGRX little-endian format we serve."""
        rgb = self.framebuffer()
        out = bytearray(self.width * self.height * 4)
        for index in range(self.width * self.height):
            out[index * 4] = rgb[index * 3 + 2]
            out[index * 4 + 1] = rgb[index * 3 + 1]
            out[index * 4 + 2] = rgb[index * 3]
        return bytes(out)

    def _send_update(self, conn: socket.socket) -> None:
        if self.fault == "unknown-message":
            conn.sendall(struct.pack("B", 200))
            return

        for _ in range(2 if self.fault == "bell-then-frame" else 0):
            conn.sendall(struct.pack("B", 2))  # Bell
        if self.fault == "cut-text-then-frame":
            text = b"clipboard"
            conn.sendall(struct.pack(">BxxxI", 3, len(text)) + text)
        if self.fault == "colour-map-then-frame":
            conn.sendall(struct.pack(">BxHH", 1, 0, 2) + b"\x00" * 12)

        if self.fault == "no-rectangles":
            conn.sendall(struct.pack(">BxH", 0, 0))
            return
        if self.fault == "absurd-rectangle-count":
            conn.sendall(struct.pack(">BxH", 0, 60000))
            return

        if self.fault == "rectangle-outside-framebuffer":
            conn.sendall(struct.pack(">BxH", 0, 1))
            conn.sendall(
                struct.pack(">HHHHi", self.width - 1, 0, 8, 8, 0) + b"\x00" * 256
            )
            return

        encoding = 16 if self.fault == "wrong-encoding" else 0
        conn.sendall(struct.pack(">BxH", 0, 1))
        conn.sendall(struct.pack(">HHHHi", 0, 0, self.width, self.height, encoding))
        if self.fault == "wrong-encoding":
            return

        data = self._raw_pixels()

        if self.fault == "truncated-rectangle":
            conn.sendall(data[: len(data) // 2])
            return
        if self.fault == "reset-mid-frame":
            conn.sendall(data[: len(data) // 2])
            conn.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
            )
            conn.close()
            return
        if self.fault == "slow-loris":
            # A per-recv timeout resets on every byte; only a deadline over
            # the whole read catches this.
            for byte in data:
                if self._stop.is_set():
                    return
                conn.sendall(bytes([byte]))
                time.sleep(self.slow_delay)
            return

        conn.sendall(data)


def _recv_exactly(conn: socket.socket, count: int) -> bytes:
    chunks = []
    received = 0
    while received < count:
        chunk = conn.recv(count - received)
        if not chunk:
            raise OSError(f"client closed after {received} of {count} bytes")
        chunks.append(chunk)
        received += len(chunk)
    return b"".join(chunks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5901)
    parser.add_argument("--password")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=400)
    parser.add_argument("--fault", default="none", choices=sorted(FAULTS))
    parser.add_argument("--list-faults", action="store_true")
    args = parser.parse_args()

    if args.list_faults:
        width = max(len(name) for name in FAULTS)
        for name, description in sorted(FAULTS.items()):
            print(f"{name:{width}}  {description}")
        return 0

    server = FakeVNCServer(
        host=args.host,
        port=args.port,
        width=args.width,
        height=args.height,
        password=args.password,
        fault=args.fault,
    )
    port = server.start()
    print(f"fake VNC server on {args.host}:{port} (fault: {args.fault})")
    print("Ctrl-C to stop.")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
