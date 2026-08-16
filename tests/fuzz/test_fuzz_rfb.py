"""Tier 7: a hostile RFB byte stream.

The methodology notes that most defects found in practice were unguarded
dereferences surfacing as 500s. The direct analogue here is an unguarded
``struct.unpack`` on a short read, or a length taken from the wire and used
as a length.

So this fuzzer plays the server badly: it follows the shape of the protocol
just closely enough for the client to keep parsing, and fills every field with
whatever the seeded generator produces. The oracle is the same as everywhere
else in this tier -- a typed ShimError inside the deadline, nothing else,
ever, and the client still usable afterwards.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
import unittest

from boot_err_shim.errors import ShimError
from boot_err_shim.rfb import PROTOCOL_VERSION, RFBClient
from tests.fuzz import get_seed, iterations, rng

#: Small, so a fuzzed length field cannot make the client wait for megabytes
#: that will never arrive. The deadline would catch it, but slowly.
READ_TIMEOUT = 1


class FuzzServer:
    """Speaks a mangled approximation of RFB, driven by a seeded generator."""

    def __init__(self, random, *, script: list[bytes] | None = None) -> None:
        self.random = random
        self.script = script
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.sock.settimeout(5)
        self.port = self.sock.getsockname()[1]
        self.sent = bytearray()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _blob(self, low: int, high: int) -> bytes:
        return bytes(
            self.random.randrange(256)
            for _ in range(self.random.randrange(low, high))
        )

    #: Framebuffer the valid baseline advertises. Small, so a stream that
    #: survives every mutation still finishes quickly.
    WIDTH, HEIGHT = 8, 4

    def _valid(self) -> list[bytes]:
        """A complete, correct conversation, one chunk per protocol field."""
        name = b"fuzz"
        pixels = self.WIDTH * self.HEIGHT * 4
        return [
            PROTOCOL_VERSION,
            struct.pack("BB", 1, 2),                 # one security type: VNC auth
            bytes(range(16)),                        # challenge
            struct.pack(">I", 0),                    # auth OK
            struct.pack(">HH", self.WIDTH, self.HEIGHT),
            bytes(16),                               # pixel format
            struct.pack(">I", len(name)),
            name,
            struct.pack("B", 0),                     # FramebufferUpdate
            struct.pack(">xH", 1),                   # one rectangle
            struct.pack(">HHHHi", 0, 0, self.WIDTH, self.HEIGHT, 0),
            bytes(pixels),
        ]

    def _generate(self) -> list[bytes]:
        """A valid conversation with a few fields corrupted.

        Built by corrupting a correct baseline rather than by generating bytes
        freely. Free generation is what an earlier version of this fixture
        did, and the odds of random bytes happening to contain an acceptable
        security type, a zero auth result *and* a plausible name length are
        near zero -- so every stream died at the first read, the deeper
        parsing was never reached, and the fuzzer was green while testing
        almost nothing.
        """
        random = self.random
        chunks = self._valid()

        for _ in range(random.randrange(1, 4)):
            index = random.randrange(len(chunks))
            chunk = chunks[index]
            choice = random.randrange(6)

            if choice == 0 and chunk:                # flip a bit
                out = bytearray(chunk)
                out[random.randrange(len(out))] ^= 1 << random.randrange(8)
                chunks[index] = bytes(out)
            elif choice == 1 and chunk:              # truncate
                chunks[index] = chunk[: random.randrange(len(chunk))]
            elif choice == 2:                        # noise of a similar size
                chunks[index] = self._blob(0, max(2, len(chunk) + 4))
            elif choice == 3 and len(chunk) == 4:    # inflate a length field
                chunks[index] = struct.pack(">I", random.randrange(0, 1 << 31))
            elif choice == 4:                        # drop the field entirely
                chunks[index] = b""
            else:                                    # append junk after it
                chunks[index] = chunk + self._blob(1, 24)

        if random.random() < 0.15:                   # hang up early
            chunks = chunks[: random.randrange(len(chunks))]

        return chunks

    def _serve(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except (TimeoutError, OSError):
            return

        try:
            conn.settimeout(2)
            for chunk in self.script if self.script is not None else self._generate():
                self.sent += chunk
                conn.sendall(chunk)
                # Drain whatever the client says without caring what it is,
                # so its sends never block against a full receive buffer.
                conn.setblocking(False)
                try:
                    conn.recv(4096)
                except (BlockingIOError, OSError):
                    pass
                conn.setblocking(True)
                conn.settimeout(2)

            # Half-close, then drain until the client gives up.
            #
            # Closing outright here loses the fuzz entirely: with unread
            # incoming data the close is abortive, the peer gets RST, and
            # everything we just sent is discarded before the client parses
            # any of it. An earlier version of this fixture did exactly that,
            # and the result was a fuzzer where no stream ever got past the
            # first read -- green, and testing nothing.
            try:
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    if not conn.recv(4096):
                        break
                except TimeoutError:
                    continue
                except OSError:
                    break
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class TestFuzzRFB(unittest.TestCase):
    def test_no_stream_escapes_as_an_untyped_exception(self) -> None:
        random = rng("rfb")
        reached_capture = 0

        for index in range(iterations(120)):
            server = FuzzServer(random)
            client = RFBClient(
                host="127.0.0.1",
                port=server.port,
                password="secret12",
                connect_timeout=2,
                read_timeout=READ_TIMEOUT,
            )
            started = time.monotonic()
            try:
                client.connect()
                reached_capture += 1
                client.capture()
            except ShimError:
                pass
            except Exception as exc:  # noqa: BLE001
                self.fail(
                    f"untyped {type(exc).__name__}: {exc}\n"
                    f"  iteration {index}, seed={get_seed()}\n"
                    f"  server sent: {bytes(server.sent)[:300].hex()}"
                )
            finally:
                elapsed = time.monotonic() - started
                client.close()
                server.close()

            self.assertLess(
                elapsed,
                20,
                f"iteration {index} took {elapsed:.1f}s; a deadline did not hold",
            )

        # If the client never got past the handshake, this test is only
        # exercising the first few reads.
        self.assertGreater(
            reached_capture, 0, "no fuzzed stream ever completed a handshake"
        )

    def test_a_client_survives_to_be_used_again(self) -> None:
        # "The server is still alive afterwards", in client form: a mangled
        # conversation must not leave the process unable to do the next one.
        random = rng("rfb-recovery")
        for _ in range(iterations(30)):
            server = FuzzServer(random)
            client = RFBClient(
                host="127.0.0.1",
                port=server.port,
                connect_timeout=2,
                read_timeout=READ_TIMEOUT,
            )
            try:
                client.connect()
            except ShimError:
                pass
            finally:
                client.close()
                server.close()

        # Now a well-behaved server must still work.
        import sys
        from pathlib import Path

        sys.path.insert(
            0, str(Path(__file__).resolve().parent.parent.parent / "tools")
        )
        from fake_vnc_server import FakeVNCServer

        good = FakeVNCServer(width=16, height=8)
        good.start()
        self.addCleanup(good.stop)
        client = RFBClient(host="127.0.0.1", port=good.port, read_timeout=5)
        self.addCleanup(client.close)
        client.connect()
        self.assertEqual(client.capture().size, (16, 8))

    def test_lengths_from_the_wire_are_never_trusted(self) -> None:
        """A well-formed handshake that then claims an enormous name length.

        Hand-written rather than generated, because it is the specific shape
        the guard exists for and a random generator hits it rarely.
        """
        script = [
            b"RFB 003.008\n",
            struct.pack("BB", 1, 1),  # one security type: None
            struct.pack(">I", 0),  # auth OK
            struct.pack(">HH", 640, 400),
            b"\x00" * 16,  # pixel format
            struct.pack(">I", 0xFFFFFFF),  # desktop name length: absurd
            b"short",
        ]
        server = FuzzServer(rng("rfb-length"), script=script)
        self.addCleanup(server.close)

        client = RFBClient(
            host="127.0.0.1", port=server.port, connect_timeout=2, read_timeout=2
        )
        self.addCleanup(client.close)

        started = time.monotonic()
        with self.assertRaises(ShimError) as caught:
            client.connect()
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("implausible", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
