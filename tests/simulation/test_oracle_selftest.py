"""Tier 9, part one: does the checker actually work?

Written and run before the simulator, because an invariant that never fires is
indistinguishable from a passing suite. A checker that silently agrees with
everything turns thousands of simulated hours into an expensive way of
printing OK.

So each invariant is fed a daemon deliberately broken in the specific way it
claims to detect, and is required to complain. If any of these ever stops
failing, the corresponding invariant has become decorative.
"""

from __future__ import annotations

import unittest

from boot_err_shim.daemon import Action, Daemon
from tests.simulation.checker import Run, check
from tests.simulation.harness import simulate
from tests.simulation.world import Event, Host, Idrac, World


def a_timeline_with_a_stuck_host() -> list[Event]:
    start = 1_800_000_000.0
    return [
        Event(at=start + 100, host=Host.STUCK_PROMPT, label="crash"),
        Event(at=start + 4000, host=Host.UP, label="somebody fixed it"),
    ]


def a_timeline_stuck_at_another_error() -> list[Event]:
    start = 1_800_000_000.0
    return [Event(at=start + 100, host=Host.STUCK_OTHER, label="different error")]


# -- deliberately broken daemons ---------------------------------------


class PressesAlways(Daemon):
    """Sends the key whenever it can reach the console, match or no match."""

    def _recover(self):
        decision = super()._recover()
        try:
            console = self.console_factory()
        except Exception:  # noqa: BLE001
            return decision
        console.send_key(self.config.detect.keysym)
        return decision


class PressesTwice(Daemon):
    """Sends the key twice per successful recovery."""

    def _press(self, console) -> None:
        super()._press(console)
        console.send_key(self.config.detect.keysym)


class NeverPresses(Daemon):
    """Detects correctly and then does nothing."""

    def _press(self, console) -> None:
        return


class SleepsForever(Daemon):
    """Uses a sleep nobody configured."""

    def step(self):
        decision = super().step()
        object.__setattr__(decision, "sleep_for", 99999)
        return decision


class BusyLoops(Daemon):
    """Never sleeps at all."""

    def step(self):
        decision = super().step()
        object.__setattr__(decision, "sleep_for", 0)
        return decision


class PressesTheWrongKey(Daemon):
    def _press(self, console) -> None:
        console.send_key(0x4E)  # 'N'
        self._record_intervention()


class PressesWithoutRecording(Daemon):
    """Sends the key but never writes it to the history."""

    def _record_intervention(self) -> None:
        return


# -- the self-test -----------------------------------------------------


class TestTheCheckerFires(unittest.TestCase):
    def names(self, violations) -> set[str]:
        return {violation.invariant for violation in violations}

    def test_a_correct_daemon_produces_no_violations(self) -> None:
        # The control. Without this, every assertion below could be satisfied
        # by a checker that complains about everything.
        outcome = simulate(a_timeline_with_a_stuck_host(), steps=120)
        self.assertEqual(
            [str(v) for v in outcome.violations],
            [],
            "the unmodified daemon should satisfy every invariant",
        )

    def test_pressing_without_the_prompt_is_caught(self) -> None:
        outcome = simulate(
            a_timeline_stuck_at_another_error(),
            steps=120,
            daemon_factory=PressesAlways,
        )
        self.assertIn("never-press-without-the-prompt", self.names(outcome.violations))

    def test_pressing_at_a_dark_console_is_caught(self) -> None:
        start = 1_800_000_000.0
        outcome = simulate(
            [Event(at=start + 100, host=Host.DARK)],
            steps=120,
            daemon_factory=PressesAlways,
        )
        self.assertIn("never-press-without-the-prompt", self.names(outcome.violations))

    def test_pressing_twice_is_caught(self) -> None:
        outcome = simulate(
            a_timeline_with_a_stuck_host(), steps=120, daemon_factory=PressesTwice
        )
        self.assertIn(
            "never-press-twice-for-one-appearance", self.names(outcome.violations)
        )

    def test_never_pressing_is_caught(self) -> None:
        # The inverse failure. A daemon that does nothing satisfies every
        # safety property perfectly.
        outcome = simulate(
            a_timeline_with_a_stuck_host(), steps=200, daemon_factory=NeverPresses
        )
        self.assertIn("eventually-press-when-it-can", self.names(outcome.violations))

    def test_an_unconfigured_sleep_is_caught(self) -> None:
        outcome = simulate(
            a_timeline_with_a_stuck_host(), steps=40, daemon_factory=SleepsForever
        )
        self.assertIn(
            "sleeps-stay-within-configured-bounds", self.names(outcome.violations)
        )

    def test_a_busy_loop_is_caught(self) -> None:
        outcome = simulate(
            a_timeline_with_a_stuck_host(), steps=40, daemon_factory=BusyLoops
        )
        self.assertIn(
            "sleeps-stay-within-configured-bounds", self.names(outcome.violations)
        )

    def test_the_wrong_key_is_caught(self) -> None:
        outcome = simulate(
            a_timeline_with_a_stuck_host(),
            steps=120,
            daemon_factory=PressesTheWrongKey,
        )
        self.assertIn(
            "only-the-configured-key-is-sent", self.names(outcome.violations)
        )

    def test_an_unrecorded_press_is_caught(self) -> None:
        outcome = simulate(
            a_timeline_with_a_stuck_host(),
            steps=120,
            daemon_factory=PressesWithoutRecording,
        )
        self.assertIn("every-press-is-recorded", self.names(outcome.violations))

    def test_a_non_monotone_history_is_caught(self) -> None:
        # Constructed directly: no plausible daemon produces this, but the
        # invariant is only worth having if it can fire.
        world = World([])
        run = Run(
            world=world,
            decisions=[],
            intervention_counts=[1, 2, 1],
            recorded_interventions=0,
            ping_interval=120,
            retry_interval=60,
            recovery_interval=60,
            post_fix_sleep=600,
            threshold=3,
        )
        self.assertIn(
            "intervention-count-is-monotone", self.names(check(run))
        )

    def test_every_invariant_has_a_self_test(self) -> None:
        """The list of invariants and the list of self-tests must agree.

        Adding an invariant without a self-test is how a checker becomes
        decorative one function at a time.
        """
        from tests.simulation import checker

        covered = {
            "never-press-without-the-prompt",
            "never-press-twice-for-one-appearance",
            "eventually-press-when-it-can",
            "sleeps-stay-within-configured-bounds",
            "intervention-count-is-monotone",
            "every-press-is-recorded",
            "only-the-configured-key-is-sent",
        }
        declared = {
            invariant.__name__.replace("_", "-") for invariant in checker.INVARIANTS
        }
        declared.add("only-the-configured-key-is-sent")
        self.assertEqual(declared, covered)


class TestTheWorldItself(unittest.TestCase):
    """The simulation has to be right before its verdicts mean anything."""

    def test_the_prompt_screen_is_not_the_healthy_screen(self) -> None:
        world = World([])
        self.assertNotEqual(
            world.screens[Host.STUCK_PROMPT.value],
            world.screens[Host.UP.value],
        )

    def test_appearances_are_tracked(self) -> None:
        start = 1_800_000_000.0
        world = World(
            [
                Event(at=start + 10, host=Host.STUCK_PROMPT),
                Event(at=start + 20, host=Host.UP),
                Event(at=start + 30, host=Host.STUCK_PROMPT),
            ]
        )
        world.advance(start + 40)
        self.assertEqual(len(world.appearances), 2)
        self.assertEqual(world.appearances[0].ended, start + 20)

    def test_reachable_time_only_counts_while_the_idrac_is_up(self) -> None:
        start = 1_800_000_000.0
        world = World(
            [
                Event(at=start + 10, host=Host.STUCK_PROMPT),
                Event(at=start + 20, idrac=Idrac.REFUSING),
            ]
        )
        world.advance(start + 120)
        self.assertAlmostEqual(world.appearances[0].reachable_seconds, 10.0, places=3)

    def test_a_keystroke_at_the_prompt_boots_the_host(self) -> None:
        start = 1_800_000_000.0
        world = World([Event(at=start + 10, host=Host.STUCK_PROMPT)])
        world.advance(start + 20)
        world.receive_key(0x59)

        # Honoured at once, but the host is still at the prompt until the
        # boot finishes -- that window is where a second keystroke would go.
        self.assertTrue(world.appearances[0].honoured)
        self.assertIs(world.host, Host.STUCK_PROMPT)

        world.advance(start + 20 + World.BOOT_DELAY + 1)
        self.assertIs(world.host, Host.UP)

    def test_a_second_keystroke_during_the_boot_window_lands_in_the_same_appearance(
        self,
    ) -> None:
        start = 1_800_000_000.0
        world = World([Event(at=start + 10, host=Host.STUCK_PROMPT)])
        world.advance(start + 20)
        world.receive_key(0x59)
        world.receive_key(0x59)
        self.assertEqual(world.appearances[0].presses, 2)
        self.assertEqual(len({p.epoch for p in world.presses}), 1)

    def test_an_ignored_keystroke_leaves_the_host_stuck(self) -> None:
        start = 1_800_000_000.0
        world = World(
            [
                Event(at=start + 10, host=Host.STUCK_PROMPT),
                Event(at=start + 15, action="ignore_press"),
            ]
        )
        world.advance(start + 20)
        world.receive_key(0x59)
        self.assertIs(world.host, Host.STUCK_PROMPT)
        self.assertFalse(world.appearances[0].honoured)

    def test_the_real_detector_matches_the_prompt_screen(self) -> None:
        # If it did not, every simulation would be exercising a daemon that
        # can never act, and all the safety invariants would hold vacuously.
        from boot_err_shim.calibrate import analyse
        from boot_err_shim.detect import CalibratedDetector
        from tests.fakes import make_config

        world = World([])
        config = make_config()
        calibration = analyse(
            world.screens[Host.STUCK_PROMPT.value], config.detect.lines
        )
        detector = CalibratedDetector(calibration, 0.02)

        self.assertTrue(
            detector.detect(world.screens[Host.STUCK_PROMPT.value]).matched
        )
        for other in (Host.UP, Host.STUCK_OTHER, Host.DARK):
            with self.subTest(screen=other.value):
                self.assertFalse(detector.detect(world.screens[other.value]).matched)
        self.assertFalse(detector.detect(world.screens["torn"]).matched)


if __name__ == "__main__":
    unittest.main()
