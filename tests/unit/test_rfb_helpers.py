"""Tier 1: the RFB pieces that are not the protocol itself.

The fake-server tier drives the protocol over real sockets. What it does not
examine closely is the plumbing around it, and one piece of that plumbing is
security relevant.

`client_from_config` copies eight fields by hand. Drop one and nothing breaks
loudly: forget `tls_verify` and an operator who deliberately turned
certificate checking on silently gets a client that does not check, with the
config still saying it does. So the mapping is asserted by introspection --
add a field to VncConfig without wiring it up and this fails, rather than
waiting for somebody to notice.
"""

from __future__ import annotations

import dataclasses
import time
import unittest
from pathlib import Path

from boot_err_shim.config import VncConfig
from boot_err_shim.errors import Timeout
from boot_err_shim.rfb import (
    SECURITY_NAMES,
    RFBClient,
    ServerInfo,
    _Deadline,
    client_from_config,
)
from tests.fakes import make_config


class TestDeadline(unittest.TestCase):
    """The budget that stops a dribbling server holding the daemon forever."""

    def test_remaining_shrinks(self) -> None:
        deadline = _Deadline(10)
        first = deadline.remaining()
        time.sleep(0.05)
        self.assertLess(deadline.remaining(), first)

    def test_check_returns_the_time_left(self) -> None:
        deadline = _Deadline(5)
        left = deadline.check("reading")
        self.assertGreater(left, 0)
        self.assertLessEqual(left, 5)

    def test_an_expired_deadline_raises(self) -> None:
        deadline = _Deadline(0.01)
        time.sleep(0.05)
        with self.assertRaises(Timeout):
            deadline.check("reading pixels")

    def test_the_error_says_what_it_was_doing_and_for_how_long(self) -> None:
        # An operator reading a log needs both: which read stalled, and what
        # budget it blew.
        deadline = _Deadline(0.01)
        time.sleep(0.05)
        with self.assertRaises(Timeout) as caught:
            deadline.check("reading pixels")
        message = str(caught.exception)
        self.assertIn("reading pixels", message)
        self.assertIn("0.01", message)

    def test_a_zero_budget_is_immediately_expired(self) -> None:
        with self.assertRaises(Timeout):
            _Deadline(0).check("anything")

    def test_the_budget_is_shared_not_renewed(self) -> None:
        # The whole point: several reads against one deadline consume the
        # same allowance rather than each getting a fresh one.
        deadline = _Deadline(0.3)
        deadline.check("first")
        time.sleep(0.2)
        remaining = deadline.check("second")
        self.assertLess(remaining, 0.2)


class TestServerInfo(unittest.TestCase):
    def test_known_security_types_are_named(self) -> None:
        info = ServerInfo(security_types=(1, 2))
        description = info.security_description
        self.assertIn("1 None", description)
        self.assertIn("2 VNC authentication", description)

    def test_an_unknown_type_is_reported_rather_than_hidden(self) -> None:
        # `configure` prints this so an unexpected server is diagnosable; a
        # silently dropped code would be the opposite of helpful.
        info = ServerInfo(security_types=(99,))
        self.assertIn("99", info.security_description)
        self.assertIn("unknown", info.security_description)

    def test_venc_crypt_is_named_because_we_will_meet_it(self) -> None:
        # TigerVNC offers it and this client does not implement it, so the
        # message an operator sees needs to name it.
        self.assertEqual(SECURITY_NAMES[19], "VeNCrypt")
        self.assertIn("VeNCrypt", ServerInfo(security_types=(19,)).security_description)

    def test_no_types_renders_as_empty_rather_than_crashing(self) -> None:
        self.assertEqual(ServerInfo().security_description, "")

    def test_defaults_are_inert(self) -> None:
        info = ServerInfo()
        self.assertEqual((info.width, info.height), (0, 0))
        self.assertFalse(info.tls)


class TestClientFromConfig(unittest.TestCase):
    """Every VNC setting must actually reach the client."""

    def build(self, overlay: str = "") -> tuple[RFBClient, object]:
        config = make_config(overlay)
        return client_from_config(config), config

    def test_the_obvious_fields(self) -> None:
        client, config = self.build(
            '[vnc]\nhost = "10.9.9.9"\nport = 5999\npassword = "hunter2"\n'
        )
        self.assertEqual(client.host, "10.9.9.9")
        self.assertEqual(client.port, 5999)
        self.assertEqual(client.password, "hunter2")

    def test_timeouts_are_carried_over(self) -> None:
        client, _ = self.build("[vnc]\nconnect_timeout = 7\nread_timeout = 11\n")
        self.assertEqual(client.connect_timeout, 7)
        self.assertEqual(client.read_timeout, 11)

    def test_tls_verification_is_carried_over(self) -> None:
        # The security-relevant one. Dropping this silently disables
        # certificate checking for somebody who deliberately enabled it.
        client, _ = self.build(
            '[vnc]\ntls = true\ntls_verify = true\ntls_ca = "/etc/ca.pem"\n'
        )
        self.assertTrue(client.tls)
        self.assertTrue(client.tls_verify)
        self.assertEqual(client.tls_ca, Path("/etc/ca.pem"))

    def test_verification_off_by_default(self) -> None:
        client, _ = self.build()
        self.assertFalse(client.tls)
        self.assertFalse(client.tls_verify)
        self.assertIsNone(client.tls_ca)

    def test_an_absent_password_stays_absent(self) -> None:
        # None and "" mean different things to the handshake.
        client, _ = self.build()
        self.assertIsNone(client.password)

    def test_every_vnc_setting_reaches_the_client(self) -> None:
        """Introspective, so a new field cannot be forgotten.

        Adding one to VncConfig and not to client_from_config is a change
        nothing else notices: the config validates, the client constructs,
        and the setting simply has no effect.
        """
        config = make_config(
            '[vnc]\nhost = "10.1.2.3"\nport = 5911\npassword = "pw"\n'
            'tls = true\ntls_verify = true\ntls_ca = "/etc/ca.pem"\n'
            "connect_timeout = 9\nread_timeout = 13\n"
        )
        client = client_from_config(config)

        unmapped = []
        for field in dataclasses.fields(VncConfig):
            expected = getattr(config.vnc, field.name)
            if not hasattr(client, field.name):
                unmapped.append(f"{field.name} (client has no such attribute)")
                continue
            actual = getattr(client, field.name)
            if actual != expected:
                unmapped.append(f"{field.name}: client has {actual!r}, config {expected!r}")

        self.assertEqual(
            unmapped,
            [],
            "settings that do not reach the client. Wire them up in "
            "rfb.client_from_config.",
        )

    def test_the_introspection_covers_a_realistic_number_of_fields(self) -> None:
        # Guards against the check above passing because the field list
        # somehow came back empty.
        self.assertGreaterEqual(len(dataclasses.fields(VncConfig)), 8)


class TestSendKeyShape(unittest.TestCase):
    """Argument handling only; the wire format is covered against a server."""

    def test_sending_without_a_connection_is_a_protocol_error(self) -> None:
        from boot_err_shim.errors import ProtocolError

        client = RFBClient(host="127.0.0.1", port=1)
        for pressed in (None, True, False):
            with self.subTest(pressed=pressed), self.assertRaises(ProtocolError):
                client.send_key(0x59, pressed=pressed)


if __name__ == "__main__":
    unittest.main()
