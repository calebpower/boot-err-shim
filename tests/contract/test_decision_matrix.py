"""Tier 4: the ping-side decision matrix.

A table of (host state, failure count) -> (action, sleep, reason). Written as
data rather than as prose assertions so that a refactor of daemon.py is graded
against the same rows it started with.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from boot_err_shim.daemon import Action, decide_after_ping

THRESHOLD = 3
PING_INTERVAL = 120
RETRY_INTERVAL = 90


@dataclass(frozen=True)
class Row:
    label: str
    up: bool
    failures: int
    action: Action
    sleep_for: int
    reason: str
    reset_failures: bool


MATRIX: tuple[Row, ...] = (
    Row(
        label="healthy host polls on the long interval",
        up=True,
        failures=0,
        action=Action.SLEEP,
        sleep_for=PING_INTERVAL,
        reason="host.up",
        reset_failures=True,
    ),
    Row(
        label="host that just came back also resets",
        up=True,
        failures=7,
        action=Action.SLEEP,
        sleep_for=PING_INTERVAL,
        reason="host.up",
        reset_failures=True,
    ),
    Row(
        label="first failure waits, does not act",
        up=False,
        failures=1,
        action=Action.SLEEP,
        sleep_for=RETRY_INTERVAL,
        reason="below.threshold",
        reset_failures=False,
    ),
    Row(
        label="second failure still waits",
        up=False,
        failures=2,
        action=Action.SLEEP,
        sleep_for=RETRY_INTERVAL,
        reason="below.threshold",
        reset_failures=False,
    ),
    Row(
        label="third failure reaches the threshold and looks at the console",
        up=False,
        failures=3,
        action=Action.ATTEMPT_RECOVERY,
        sleep_for=0,
        reason="threshold.reached",
        reset_failures=False,
    ),
    Row(
        label="past the threshold it stays in recovery",
        up=False,
        failures=4,
        action=Action.ATTEMPT_RECOVERY,
        sleep_for=0,
        reason="threshold.reached",
        reset_failures=False,
    ),
    Row(
        label="still in recovery much later",
        up=False,
        failures=97,
        action=Action.ATTEMPT_RECOVERY,
        sleep_for=0,
        reason="threshold.reached",
        reset_failures=False,
    ),
)


def decide(up: bool, failures: int, threshold: int = THRESHOLD):
    return decide_after_ping(
        up=up,
        failures=failures,
        threshold=threshold,
        ping_interval=PING_INTERVAL,
        retry_interval=RETRY_INTERVAL,
    )


class TestDecisionMatrix(unittest.TestCase):
    def test_every_row(self) -> None:
        for row in MATRIX:
            with self.subTest(row.label):
                decision = decide(row.up, row.failures)
                self.assertIs(decision.action, row.action)
                self.assertEqual(decision.sleep_for, row.sleep_for)
                self.assertEqual(decision.reason, row.reason)
                self.assertEqual(decision.reset_failures, row.reset_failures)

    def test_the_matrix_covers_both_sides_of_the_threshold(self) -> None:
        # A matrix that drifted to all-up or all-down rows would still pass
        # every assertion above while proving nothing.
        actions = {row.action for row in MATRIX}
        self.assertEqual(actions, {Action.SLEEP, Action.ATTEMPT_RECOVERY})


class TestThresholdBoundary(unittest.TestCase):
    """Off-by-one here means acting a whole cycle early, or never acting."""

    def test_exactly_at_the_threshold_acts(self) -> None:
        self.assertIs(decide(False, THRESHOLD).action, Action.ATTEMPT_RECOVERY)

    def test_one_below_the_threshold_waits(self) -> None:
        self.assertIs(decide(False, THRESHOLD - 1).action, Action.SLEEP)

    def test_one_above_the_threshold_acts(self) -> None:
        self.assertIs(decide(False, THRESHOLD + 1).action, Action.ATTEMPT_RECOVERY)

    def test_threshold_of_one_acts_on_the_first_failure(self) -> None:
        self.assertIs(decide(False, 1, threshold=1).action, Action.ATTEMPT_RECOVERY)

    def test_threshold_of_one_still_does_nothing_at_zero_failures(self) -> None:
        # Reached only via an up host, but assert it rather than assume it.
        self.assertIs(decide(True, 0, threshold=1).action, Action.SLEEP)

    def test_large_threshold_waits_the_whole_way(self) -> None:
        for failures in range(1, 10):
            with self.subTest(failures=failures):
                self.assertIs(decide(False, failures, threshold=10).action, Action.SLEEP)
        self.assertIs(decide(False, 10, threshold=10).action, Action.ATTEMPT_RECOVERY)


class TestIntervalSelection(unittest.TestCase):
    def test_up_uses_the_healthy_interval_not_the_retry_one(self) -> None:
        # Swapping these is an easy edit to make and produces a daemon that
        # hammers a healthy host and dawdles over a failing one.
        self.assertEqual(decide(True, 0).sleep_for, PING_INTERVAL)

    def test_down_below_threshold_uses_the_retry_interval(self) -> None:
        self.assertEqual(decide(False, 1).sleep_for, RETRY_INTERVAL)

    def test_recovery_does_not_sleep_first(self) -> None:
        # The console is looked at immediately; the sleep comes after.
        self.assertEqual(decide(False, THRESHOLD).sleep_for, 0)


class TestCounterSemantics(unittest.TestCase):
    def test_only_an_up_host_resets_the_counter(self) -> None:
        for failures in range(0, 6):
            with self.subTest(failures=failures):
                self.assertFalse(decide(False, failures).reset_failures)
        self.assertTrue(decide(True, 0).reset_failures)

    def test_reason_tokens_are_stable(self) -> None:
        self.assertEqual(
            {decide(row.up, row.failures).reason for row in MATRIX},
            {"host.up", "below.threshold", "threshold.reached"},
        )


if __name__ == "__main__":
    unittest.main()
