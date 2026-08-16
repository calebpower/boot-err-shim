"""Tier 1: platform defaults.

Every branch is exercised on whatever machine runs the suite. That is the
whole reason ``platform_defaults`` takes a ``system`` argument: we do not have
a FreeBSD box, and "the FreeBSD ping flags are right" must still be a claim the
suite can make.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from boot_err_shim.platform_ import (
    CONFIG_FILE_NAME,
    STATE_DIR_NAME,
    platform_defaults,
    render_ping_command,
)


class TestFreeBSDDefaults(unittest.TestCase):
    def setUp(self) -> None:
        self.defaults = platform_defaults("FreeBSD")

    def test_paths(self) -> None:
        self.assertEqual(
            self.defaults.config_path, Path("/usr/local/etc") / CONFIG_FILE_NAME
        )
        self.assertEqual(self.defaults.state_dir, Path("/var/db") / STATE_DIR_NAME)
        self.assertEqual(self.defaults.syslog_socket, Path("/var/run/log"))

    def test_derived_paths_live_under_state_dir(self) -> None:
        for path in (
            self.defaults.calibration_path,
            self.defaults.snapshot_dir,
            self.defaults.history_path,
            self.defaults.lock_path,
        ):
            self.assertEqual(path.parent, self.defaults.state_dir)


class TestLinuxDefaults(unittest.TestCase):
    def setUp(self) -> None:
        self.defaults = platform_defaults("Linux")

    def test_paths(self) -> None:
        self.assertEqual(self.defaults.config_path, Path("/etc") / CONFIG_FILE_NAME)
        self.assertEqual(self.defaults.state_dir, Path("/var/lib") / STATE_DIR_NAME)
        self.assertEqual(self.defaults.syslog_socket, Path("/dev/log"))


class TestPingFlagUnits(unittest.TestCase):
    """The divergence that silently breaks a config copied between the two.

    FreeBSD ``ping -W`` is milliseconds; iputils ``ping -W`` is seconds. Using
    the Linux value on FreeBSD means a 2ms timeout, every ping fails, and the
    daemon concludes a healthy host is down -- which is how this program ends
    up at a console it should have left alone. These assertions exist to make
    that swap impossible to make quietly.
    """

    def test_freebsd_wait_is_milliseconds(self) -> None:
        command = platform_defaults("FreeBSD").ping_command
        self.assertEqual(command[command.index("-W") + 1], "2000")

    def test_linux_wait_is_seconds(self) -> None:
        command = platform_defaults("Linux").ping_command
        self.assertEqual(command[command.index("-W") + 1], "2")

    def test_the_two_are_not_interchangeable(self) -> None:
        freebsd = platform_defaults("FreeBSD").ping_command
        linux = platform_defaults("Linux").ping_command
        self.assertNotEqual(freebsd, linux)

    def test_unknown_system_omits_the_flag_rather_than_guessing(self) -> None:
        # A wrong -W is worse than no -W: no flag falls back to ping's default
        # and our own subprocess timeout, which is merely suboptimal.
        command = platform_defaults("Plan9").ping_command
        self.assertNotIn("-W", command)
        self.assertIn("{host}", command)

    def test_every_platform_template_carries_a_host_placeholder(self) -> None:
        for system in ("FreeBSD", "Linux", "Darwin", "Windows", "Plan9"):
            with self.subTest(system=system):
                command = platform_defaults(system).ping_command
                self.assertTrue(any("{host}" in part for part in command))


class TestRenderPingCommand(unittest.TestCase):
    def test_substitutes_host(self) -> None:
        self.assertEqual(
            render_ping_command(("ping", "-c", "1", "{host}"), "10.0.0.50"),
            ["ping", "-c", "1", "10.0.0.50"],
        )

    def test_leaves_other_tokens_alone(self) -> None:
        self.assertEqual(
            render_ping_command(("ping", "-W", "2000", "{host}"), "h"),
            ["ping", "-W", "2000", "h"],
        )

    def test_host_containing_braces_is_not_evaluated(self) -> None:
        # str.replace, not str.format: a hostile hostname cannot reach into
        # the template or raise KeyError.
        result = render_ping_command(("ping", "{host}"), "{port}")
        self.assertEqual(result, ["ping", "{port}"])

    def test_repeated_placeholder_in_one_token(self) -> None:
        self.assertEqual(render_ping_command(("x{host}y{host}",), "A"), ["xAyA"])

    def test_accepts_a_list_as_well_as_a_tuple(self) -> None:
        self.assertEqual(render_ping_command(["ping", "{host}"], "h"), ["ping", "h"])


if __name__ == "__main__":
    unittest.main()
