"""Tier 5: the RFB client against a fake server, over real sockets.

Split into the happy path and the fault matrix. The fault matrix is the
valuable half: every entry is a way a half-dead iDRAC can behave, and the
oracle for all of them is the same -- a typed error within the deadline, never
a hang, never an untyped exception.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))

from boot_err_shim.errors import (  # noqa: E402
    AuthError,
    ConnectionFailed,
    ProtocolError,
    ShimError,
    Timeout,
)
from boot_err_shim.frame import Frame  # noqa: E402
from boot_err_shim.rfb import RFBClient  # noqa: E402
from fake_vnc_server import FAULTS, FakeVNCServer  # noqa: E402

KEYSYM_Y = 0x59


class ServerTest(unittest.TestCase):
    """Starts a fake server per test and always tears it down."""

    def serve(self, **kwargs) -> FakeVNCServer:
        server = FakeVNCServer(**kwargs)
        server.start()
        self.addCleanup(server.stop)
        return server

    def client(self, server: FakeVNCServer, **kwargs) -> RFBClient:
        params = {
            "host": server.host,
            "port": server.port,
            "password": server.password,
            "connect_timeout": 5,
            "read_timeout": 5,
        }
        params.update(kwargs)
        client = RFBClient(**params)
        self.addCleanup(client.close)
        return client


class TestHappyPath(ServerTest):
    def test_handshake_reports_the_geometry(self) -> None:
        server = self.serve(width=32, height=16)
        info = self.client(server).connect()
        self.assertEqual((info.width, info.height), (32, 16))

    def test_desktop_name_is_read(self) -> None:
        server = self.serve()
        self.assertEqual(self.client(server).connect().name, "fake-vnc")

    def test_offered_security_types_are_recorded(self) -> None:
        # configure reports these, so an unexpected server is diagnosable
        # rather than mysterious.
        server = self.serve()
        info = self.client(server).connect()
        self.assertEqual(info.security_types, (1,))
        self.assertEqual(info.security_used, 1)

    def test_capture_returns_the_served_image(self) -> None:
        server = self.serve(width=16, height=8)
        client = self.client(server)
        client.connect()
        frame = client.capture()
        self.assertEqual(frame, Frame(16, 8, server.framebuffer()))

    def test_colour_channels_are_not_swapped(self) -> None:
        # The pixel format asks for red at shift 16 on a little-endian wire,
        # so pixels arrive BGRX. Getting this backwards is invisible on a
        # greyscale console and wrong everywhere else.
        pixels = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 10, 20, 30])
        server = self.serve(width=4, height=1, pixels=pixels)
        client = self.client(server)
        client.connect()
        frame = client.capture()
        self.assertEqual(frame.pixel(0, 0), (255, 0, 0))
        self.assertEqual(frame.pixel(1, 0), (0, 255, 0))
        self.assertEqual(frame.pixel(2, 0), (0, 0, 255))
        self.assertEqual(frame.pixel(3, 0), (10, 20, 30))

    def test_capture_twice_on_one_connection(self) -> None:
        server = self.serve(width=8, height=8)
        client = self.client(server)
        client.connect()
        self.assertEqual(client.capture(), client.capture())

    def test_a_single_pixel_framebuffer(self) -> None:
        server = self.serve(width=1, height=1, pixels=bytes([1, 2, 3]))
        client = self.client(server)
        client.connect()
        self.assertEqual(client.capture().pixel(0, 0), (1, 2, 3))

    def test_key_event_sends_down_then_up(self) -> None:
        server = self.serve()
        client = self.client(server)
        client.connect()
        client.send_key(KEYSYM_Y)
        deadline = time.monotonic() + 5
        while len(server.keys) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(server.keys, [(KEYSYM_Y, True), (KEYSYM_Y, False)])

    def test_context_manager_connects_and_closes(self) -> None:
        server = self.serve(width=8, height=8)
        with RFBClient(host=server.host, port=server.port, read_timeout=5) as client:
            self.assertEqual(client.info.width, 8)
        self.assertIsNone(client.sock)

    def test_close_is_idempotent(self) -> None:
        client = self.client(self.serve())
        client.connect()
        client.close()
        client.close()


class TestAuthentication(ServerTest):
    def test_correct_password_authenticates(self) -> None:
        server = self.serve(password="secret12")
        info = self.client(server).connect()
        self.assertEqual(info.security_used, 2)
        self.assertEqual(server.errors, [])

    def test_the_server_accepts_our_challenge_response(self) -> None:
        # The fake server verifies with the real des module, so this is a
        # genuine check of the bit-reversal quirk, not a self-consistency one.
        server = self.serve(password="hunter2")
        self.client(server).connect()
        self.assertNotIn("client failed the challenge", server.errors)

    def test_wrong_password_is_an_auth_error(self) -> None:
        server = self.serve(password="correct")
        client = self.client(server, password="wrong")
        with self.assertRaises(AuthError):
            client.connect()

    def test_password_longer_than_eight_still_works(self) -> None:
        # RFB truncates to eight bytes; both ends must truncate identically.
        server = self.serve(password="a-very-long-password")
        self.client(server).connect()
        self.assertEqual(server.errors, [])

    def test_server_requiring_auth_without_a_configured_password(self) -> None:
        server = self.serve(password="secret")
        client = self.client(server, password=None)
        with self.assertRaises(AuthError) as caught:
            client.connect()
        self.assertIn("no vnc.password", str(caught.exception))

    def test_password_offered_but_not_required_is_fine(self) -> None:
        server = self.serve(password=None)
        self.client(server, password="unnecessary").connect()


class TestFaultMatrix(ServerTest):
    """Every fault the fixture can inject, against one oracle.

    A typed ShimError, raised within the deadline. Not a hang, not a bare
    OSError, not a struct.error escaping from the parser.
    """

    #: Faults that are expected to fail during connect().
    CONNECT_FAULTS = {
        "refuse": ConnectionFailed,
        "hang-after-accept": Timeout,
        "bad-banner": ProtocolError,
        "old-version": ProtocolError,
        "truncated-banner": ProtocolError,
        "no-security-types": AuthError,
        "no-security-types-silent": AuthError,
        "unsupported-security": AuthError,
        "auth-failure": AuthError,
        "auth-failure-silent": AuthError,
        "truncated-challenge": ProtocolError,
        "hang-after-auth": Timeout,
        "zero-size": ProtocolError,
        "huge-name": ProtocolError,
    }

    #: Faults that only occur partway through VNC authentication, so the
    #: server must be offering it. Without a password the client picks
    #: security type None and never reaches them.
    NEEDS_PASSWORD = {"truncated-challenge", "auth-failure", "auth-failure-silent"}

    #: Faults that connect cleanly and then fail during capture().
    CAPTURE_FAULTS = {
        "no-rectangles": ProtocolError,
        "truncated-rectangle": ProtocolError,
        "absurd-rectangle-count": ProtocolError,
        "rectangle-outside-framebuffer": ProtocolError,
        "wrong-encoding": ProtocolError,
        "reset-mid-frame": ProtocolError,
        "unknown-message": ProtocolError,
    }

    def test_every_fault_is_covered_by_this_file(self) -> None:
        # The fixture and the matrix must not drift apart: a fault added to
        # the server with no test here would look covered and not be.
        tolerated = {"none", "slow-loris", "bell-then-frame", "cut-text-then-frame",
                     "colour-map-then-frame"}
        covered = set(self.CONNECT_FAULTS) | set(self.CAPTURE_FAULTS) | tolerated
        self.assertEqual(set(FAULTS), covered)

    def test_connect_faults(self) -> None:
        for fault, expected in self.CONNECT_FAULTS.items():
            with self.subTest(fault=fault):
                password = "secret" if fault in self.NEEDS_PASSWORD else None
                server = self.serve(fault=fault, password=password, width=16, height=8)
                client = self.client(server, read_timeout=1, connect_timeout=1)
                started = time.monotonic()
                with self.assertRaises(expected):
                    client.connect()
                self.assertLess(
                    time.monotonic() - started, 10, "took far longer than the deadline"
                )

    def test_capture_faults(self) -> None:
        for fault, expected in self.CAPTURE_FAULTS.items():
            with self.subTest(fault=fault):
                server = self.serve(fault=fault, password=None, width=16, height=8)
                client = self.client(server, read_timeout=2)
                client.connect()
                started = time.monotonic()
                with self.assertRaises(expected):
                    client.capture()
                self.assertLess(time.monotonic() - started, 10)

    def test_auth_faults_reach_the_right_error_with_a_password_set(self) -> None:
        for fault in ("auth-failure", "auth-failure-silent", "truncated-challenge"):
            with self.subTest(fault=fault):
                server = self.serve(fault=fault, password="secret", width=8, height=8)
                client = self.client(server, read_timeout=2)
                with self.assertRaises(ShimError):
                    client.connect()

    def test_no_fault_ever_raises_something_untyped(self) -> None:
        # The tier 7 oracle, applied here: nothing but ShimError escapes.
        for fault in sorted(set(self.CONNECT_FAULTS) | set(self.CAPTURE_FAULTS)):
            with self.subTest(fault=fault):
                password = "secret" if fault in self.NEEDS_PASSWORD else None
                server = self.serve(fault=fault, password=password, width=8, height=8)
                client = self.client(server, read_timeout=1, connect_timeout=1)
                try:
                    client.connect()
                    client.capture()
                except ShimError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"{fault} raised untyped {type(exc).__name__}: {exc}")


class TestSanityGuards(ServerTest):
    """The bounds that stop a corrupt header costing us memory or time.

    Each of these would otherwise still raise *something* -- the server hangs
    up and we report a lost connection -- so asserting only the exception type
    would leave the guard itself untested. The message is the evidence that we
    refused on our own terms rather than being rescued by the peer.
    """

    def test_absurd_rectangle_count_is_refused_by_us(self) -> None:
        server = self.serve(fault="absurd-rectangle-count", width=8, height=8)
        client = self.client(server, read_timeout=1)
        client.connect()
        with self.assertRaises(ProtocolError) as caught:
            client.capture()
        self.assertIn("implausible rectangle count", str(caught.exception))

    def test_implausible_desktop_name_is_refused_by_us(self) -> None:
        server = self.serve(fault="huge-name", width=8, height=8)
        client = self.client(server, read_timeout=1)
        with self.assertRaises(ProtocolError) as caught:
            client.connect()
        self.assertIn("implausible desktop name", str(caught.exception))

    def test_rectangle_outside_the_framebuffer_is_named_as_such(self) -> None:
        server = self.serve(fault="rectangle-outside-framebuffer", width=8, height=8)
        client = self.client(server, read_timeout=1)
        client.connect()
        with self.assertRaises(ProtocolError) as caught:
            client.capture()
        self.assertIn("outside", str(caught.exception))

    def test_unadvertised_encoding_is_named_as_such(self) -> None:
        server = self.serve(fault="wrong-encoding", width=8, height=8)
        client = self.client(server, read_timeout=1)
        client.connect()
        with self.assertRaises(ProtocolError) as caught:
            client.capture()
        self.assertIn("Raw only", str(caught.exception))

    def test_an_old_protocol_version_is_refused_by_us(self) -> None:
        # Without the version check the client would send its own 3.8 banner,
        # the server would hang up, and we would report a lost connection --
        # a passing test that proves nothing. Asserting the message is what
        # distinguishes "we refused" from "the peer rescued us".
        server = self.serve(fault="old-version", width=8, height=8)
        client = self.client(server, read_timeout=1)
        with self.assertRaises(ProtocolError) as caught:
            client.connect()
        message = str(caught.exception)
        self.assertIn("3.3", message)
        self.assertIn("requires 3.8", message)

    def test_a_non_rfb_banner_is_refused_by_us(self) -> None:
        server = self.serve(fault="bad-banner", width=8, height=8)
        client = self.client(server, read_timeout=1)
        with self.assertRaises(ProtocolError) as caught:
            client.connect()
        self.assertIn("not an RFB server", str(caught.exception))

    def test_an_empty_update_is_not_silently_a_black_screen(self) -> None:
        # Returning an all-zero frame here would be far worse than failing:
        # the detector would find no match and the daemon would conclude the
        # console does not show the prompt.
        server = self.serve(fault="no-rectangles", width=8, height=8)
        client = self.client(server, read_timeout=1)
        client.connect()
        with self.assertRaises(ProtocolError) as caught:
            client.capture()
        self.assertIn("no rectangles", str(caught.exception))


class TestSlowLoris(ServerTest):
    """The fault a socket timeout cannot catch.

    Every individual recv succeeds, so a per-call timeout resets forever. Only
    a deadline spanning the whole read ends this.
    """

    def test_a_dribbling_server_hits_the_deadline(self) -> None:
        server = self.serve(fault="slow-loris", width=16, height=16, slow_delay=0.02)
        client = self.client(server, read_timeout=1)
        client.connect()

        started = time.monotonic()
        with self.assertRaises(Timeout):
            client.capture()
        elapsed = time.monotonic() - started

        # 16x16x4 bytes at 20ms each would be twenty seconds.
        self.assertLess(elapsed, 5, "the deadline did not bound the read")
        self.assertGreaterEqual(elapsed, 0.9, "gave up before the deadline")

    def test_the_error_says_what_it_was_doing(self) -> None:
        server = self.serve(fault="slow-loris", width=16, height=16, slow_delay=0.02)
        client = self.client(server, read_timeout=1)
        client.connect()
        with self.assertRaises(Timeout) as caught:
            client.capture()
        self.assertIn("pixels", str(caught.exception))


class TestToleratedNoise(ServerTest):
    """Messages a server may legitimately interleave before the update."""

    def test_bell_before_the_frame(self) -> None:
        server = self.serve(fault="bell-then-frame", width=8, height=8)
        client = self.client(server)
        client.connect()
        self.assertEqual(client.capture(), Frame(8, 8, server.framebuffer()))

    def test_cut_text_before_the_frame(self) -> None:
        server = self.serve(fault="cut-text-then-frame", width=8, height=8)
        client = self.client(server)
        client.connect()
        self.assertEqual(client.capture(), Frame(8, 8, server.framebuffer()))

    def test_colour_map_before_the_frame(self) -> None:
        server = self.serve(fault="colour-map-then-frame", width=8, height=8)
        client = self.client(server)
        client.connect()
        self.assertEqual(client.capture(), Frame(8, 8, server.framebuffer()))


class TestNotConnected(unittest.TestCase):
    def test_capture_before_connect_is_a_protocol_error(self) -> None:
        client = RFBClient(host="127.0.0.1", port=1, read_timeout=1)
        with self.assertRaises(ProtocolError):
            client.capture()

    def test_send_key_before_connect_is_a_protocol_error(self) -> None:
        client = RFBClient(host="127.0.0.1", port=1, read_timeout=1)
        with self.assertRaises(ProtocolError):
            client.send_key(KEYSYM_Y)

    def test_connecting_to_a_dead_port_fails_quickly(self) -> None:
        import socket

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        client = RFBClient(host="127.0.0.1", port=port, connect_timeout=2)
        with self.assertRaises(ConnectionFailed):
            client.connect()


if __name__ == "__main__":
    unittest.main()
