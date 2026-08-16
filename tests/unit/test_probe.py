"""Tier 1: reachability probing.

The distinction under test throughout: a host that is down is a *result*, not
an error. Only a probe that could not be performed is an error. Getting this
backwards would either stall the state machine on every outage or hide a
broken ping binary as a permanent outage.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from boot_err_shim.errors import ProbeError
from boot_err_shim.probe import Prober, ProbeTimeout, _default_runner

COMMAND = ("ping", "-c", "1", "-W", "2", "{host}")


class TestProber(unittest.TestCase):
    def probe_with(self, runner):
        return Prober(COMMAND, timeout=15, runner=runner).probe("10.0.0.50")

    def test_exit_zero_is_up(self) -> None:
        result = self.probe_with(lambda cmd, t: (0, "1 packets received"))
        self.assertTrue(result.up)
        self.assertEqual(result.reason, "ok")

    def test_nonzero_exit_is_down_not_an_error(self) -> None:
        result = self.probe_with(lambda cmd, t: (1, "100% packet loss"))
        self.assertFalse(result.up)
        self.assertEqual(result.reason, "unreachable")

    def test_exit_two_is_also_down(self) -> None:
        # iputils uses 2 for "other error" (e.g. name resolution). Still down
        # from the daemon's point of view.
        self.assertFalse(self.probe_with(lambda cmd, t: (2, "unknown host")).up)

    def test_timeout_is_down_with_its_own_reason(self) -> None:
        # A ping that outlives its own -W and our backstop is indistinguishable
        # from down. Raising here would stall the loop instead of advancing it.
        def runner(cmd, t):
            raise ProbeTimeout

        result = self.probe_with(runner)
        self.assertFalse(result.up)
        self.assertEqual(result.reason, "timeout")

    def test_missing_binary_is_an_error(self) -> None:
        def runner(cmd, t):
            raise ProbeError("ping command not found")

        with self.assertRaises(ProbeError):
            self.probe_with(runner)

    def test_host_is_substituted_into_the_command(self) -> None:
        seen: list[list[str]] = []

        def runner(cmd, t):
            seen.append(list(cmd))
            return 0, ""

        self.probe_with(runner)
        self.assertEqual(seen, [["ping", "-c", "1", "-W", "2", "10.0.0.50"]])

    def test_timeout_is_passed_to_the_runner(self) -> None:
        seen: list[int] = []

        def runner(cmd, t):
            seen.append(t)
            return 0, ""

        Prober(COMMAND, timeout=7, runner=runner).probe("h")
        self.assertEqual(seen, [7])

    def test_output_is_captured_and_stripped(self) -> None:
        result = self.probe_with(lambda cmd, t: (0, "  round-trip 0.5ms  \n"))
        self.assertEqual(result.output, "round-trip 0.5ms")

    def test_output_is_captured_on_failure_too(self) -> None:
        # Without this the log says "down" and nothing about why.
        result = self.probe_with(lambda cmd, t: (1, "Destination Host Unreachable"))
        self.assertIn("Unreachable", result.output)


class TestDefaultRunner(unittest.TestCase):
    """The real subprocess wrapper, with subprocess itself mocked."""

    def test_translates_missing_binary(self) -> None:
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(ProbeError) as caught:
                _default_runner(["nosuchping", "h"], 5)
        self.assertIn("nosuchping", str(caught.exception))

    def test_translates_permission_denied(self) -> None:
        with mock.patch("subprocess.run", side_effect=PermissionError("nope")):
            with self.assertRaises(ProbeError):
                _default_runner(["ping", "h"], 5)

    def test_translates_generic_os_error(self) -> None:
        with mock.patch("subprocess.run", side_effect=OSError("exec format")):
            with self.assertRaises(ProbeError):
                _default_runner(["ping", "h"], 5)

    def test_translates_expiry_to_probe_timeout(self) -> None:
        expired = subprocess.TimeoutExpired(cmd="ping", timeout=5)
        with mock.patch("subprocess.run", side_effect=expired):
            with self.assertRaises(ProbeTimeout):
                _default_runner(["ping", "h"], 5)

    def test_decodes_undecodable_output_without_raising(self) -> None:
        # ping output is whatever the C locale hands us; a stray byte must not
        # take down the daemon.
        completed = subprocess.CompletedProcess(
            args=["ping"], returncode=0, stdout=b"ok \xff\xfe"
        )
        with mock.patch("subprocess.run", return_value=completed):
            status, output = _default_runner(["ping", "h"], 5)
        self.assertEqual(status, 0)
        self.assertIn("ok", output)


if __name__ == "__main__":
    unittest.main()
