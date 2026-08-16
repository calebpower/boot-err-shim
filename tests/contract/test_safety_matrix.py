"""Tier 4: when may this program press a key?

Separated from the general decision matrix because it is not just another
rule. Sending a keystroke to a firmware console that is not showing the prompt
is the one genuinely damaging thing this program can do, and unlike every other
mistake here it cannot be undone by the next loop iteration.

So the matrix is exhaustive rather than representative: all sixteen
combinations of the four inputs are enumerated, and exactly one of them is
allowed to press. If somebody adds a fifth input, this file stops compiling
the full product and the count assertion fails.
"""

from __future__ import annotations

import itertools
import unittest

from boot_err_shim.daemon import Action, decide_after_recovery, may_press_key

INTERVAL = 60
POST_FIX = 600


def decide(connected: bool, matched: bool, calibrated: bool, no_act: bool):
    return decide_after_recovery(
        connected=connected,
        matched=matched,
        calibrated=calibrated,
        no_act=no_act,
        recovery_interval=INTERVAL,
        post_fix_sleep=POST_FIX,
    )


ALL_COMBINATIONS = list(itertools.product([False, True], repeat=4))

#: The only combination under which a key may be sent.
PERMITTED = (True, True, True, False)  # connected, matched, calibrated, not no_act


class TestExhaustiveSafetyMatrix(unittest.TestCase):
    def test_the_matrix_is_actually_exhaustive(self) -> None:
        # Guards the rest of the file from silently shrinking.
        self.assertEqual(len(ALL_COMBINATIONS), 16)

    def test_exactly_one_combination_permits_a_keypress(self) -> None:
        permitted = [
            combo
            for combo in ALL_COMBINATIONS
            if may_press_key(
                connected=combo[0],
                matched=combo[1],
                calibrated=combo[2],
                no_act=combo[3],
            )
        ]
        self.assertEqual(permitted, [PERMITTED])

    def test_every_other_combination_refuses(self) -> None:
        for combo in ALL_COMBINATIONS:
            if combo == PERMITTED:
                continue
            connected, matched, calibrated, no_act = combo
            with self.subTest(
                connected=connected, matched=matched, calibrated=calibrated, no_act=no_act
            ):
                decision = decide(*combo)
                self.assertIsNot(
                    decision.action,
                    Action.PRESS_KEY,
                    "this combination must never press a key",
                )

    def test_the_permitted_combination_does_press(self) -> None:
        # The inverse failure is just as bad in its own way: a program that
        # never presses is a program that does nothing.
        decision = decide(*PERMITTED)
        self.assertIs(decision.action, Action.PRESS_KEY)


class TestWhyEachRefusalHappens(unittest.TestCase):
    """Each necessary condition, isolated so the reason is unambiguous."""

    def test_never_press_merely_because_the_host_is_down(self) -> None:
        # The whole point. A host can be down for a hundred reasons a
        # keystroke would not fix and might make worse.
        decision = decide(connected=True, matched=False, calibrated=True, no_act=False)
        self.assertIs(decision.action, Action.SLEEP)
        self.assertEqual(decision.reason, "no.match")

    def test_never_press_without_a_calibration(self) -> None:
        # Matching without a calibration is guesswork, and this program
        # declines to guess about keystrokes.
        decision = decide(connected=True, matched=True, calibrated=False, no_act=False)
        self.assertIs(decision.action, Action.SLEEP)
        self.assertEqual(decision.reason, "not.calibrated")

    def test_never_press_when_asked_to_observe_only(self) -> None:
        decision = decide(connected=True, matched=True, calibrated=True, no_act=True)
        self.assertIs(decision.action, Action.SLEEP)
        self.assertEqual(decision.reason, "match.no_act")

    def test_never_press_when_not_connected(self) -> None:
        decision = decide(connected=False, matched=True, calibrated=True, no_act=False)
        self.assertIs(decision.action, Action.SLEEP)
        self.assertEqual(decision.reason, "connect.failed")

    def test_connect_failure_outranks_every_other_reason(self) -> None:
        # If we never connected, "matched" cannot be meaningful; the reason
        # reported should say so rather than blaming the screen contents.
        for matched, calibrated in [(True, True), (True, False), (False, True)]:
            with self.subTest(matched=matched, calibrated=calibrated):
                decision = decide(False, matched, calibrated, False)
                self.assertEqual(decision.reason, "connect.failed")


class TestSleepDurations(unittest.TestCase):
    def test_a_successful_press_earns_the_long_sleep(self) -> None:
        self.assertEqual(decide(*PERMITTED).sleep_for, POST_FIX)

    def test_a_successful_press_clears_the_failure_counter(self) -> None:
        self.assertTrue(decide(*PERMITTED).reset_failures)

    def test_every_refusal_uses_the_short_interval(self) -> None:
        # Nothing was fixed, so the host is presumably still down; waiting ten
        # minutes to look again would be wrong.
        for combo in ALL_COMBINATIONS:
            if combo == PERMITTED:
                continue
            with self.subTest(combo=combo):
                self.assertEqual(decide(*combo).sleep_for, INTERVAL)

    def test_no_refusal_clears_the_failure_counter(self) -> None:
        # Clearing it would drop the daemon out of recovery and back to
        # routine polling while the host is still stuck.
        for combo in ALL_COMBINATIONS:
            if combo == PERMITTED:
                continue
            with self.subTest(combo=combo):
                self.assertFalse(decide(*combo).reset_failures)

    def test_no_decision_ever_sleeps_for_zero_after_recovery(self) -> None:
        # A zero sleep here would be a busy loop against somebody's iDRAC.
        for combo in ALL_COMBINATIONS:
            with self.subTest(combo=combo):
                self.assertGreater(decide(*combo).sleep_for, 0)


class TestReasonTokens(unittest.TestCase):
    """Reasons are logged verbatim and may be alerted on; pin them."""

    def test_reasons_are_stable_tokens(self) -> None:
        expected = {
            "match.pressed",
            "connect.failed",
            "no.match",
            "not.calibrated",
            "match.no_act",
        }
        seen = {decide(*combo).reason for combo in ALL_COMBINATIONS}
        self.assertEqual(seen, expected)

    def test_reasons_contain_no_whitespace(self) -> None:
        for combo in ALL_COMBINATIONS:
            with self.subTest(combo=combo):
                self.assertNotIn(" ", decide(*combo).reason)


if __name__ == "__main__":
    unittest.main()
