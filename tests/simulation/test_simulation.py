"""Tier 9, part two: the simulation.

Only meaningful because test_oracle_selftest.py has already shown that every
invariant here can fail. Run that first if you are reading in order.

    BOOT_ERR_SHIM_SIM_SEED=99 python -m unittest tests.simulation...
    BOOT_ERR_SHIM_SIM_TIMELINES=200 python -m unittest tests.simulation...

A failure prints the seed, the shrunk timeline, and the violated invariants,
so it becomes a regression rather than an anecdote.
"""

from __future__ import annotations

import unittest

from tests.simulation import get_seed, steps, timelines
from tests.simulation.harness import shrink, simulate
from tests.simulation.world import Event, Host, Idrac, generate


class TestGeneratedTimelines(unittest.TestCase):
    """Thousands of simulated hours of a flaky host and a flaky iDRAC."""

    def report(self, seed: int, events: list[Event], outcome) -> str:
        step_count = outcome.steps
        minimal = shrink(events, steps=step_count)
        lines = [
            "",
            f"seed={seed} (BOOT_ERR_SHIM_SIM_SEED={seed} to reproduce)",
            f"{len(events)} events shrank to {len(minimal)}:",
        ]
        from tests.simulation.world import World

        lines.append(World(minimal).describe())
        lines.append("")
        lines.append("violated:")
        for violation in outcome.violations:
            lines.append(f"  {violation}")
        return "\n".join(lines)

    def test_every_generated_timeline_holds_the_invariants(self) -> None:
        base = get_seed()
        for index in range(timelines()):
            seed = base + index
            with self.subTest(seed=seed):
                events = generate(seed, hours=24.0, density=1.0)
                outcome = simulate(events, steps=steps())
                if outcome.violations:
                    self.fail(self.report(seed, events, outcome))

    def test_a_quiet_timeline_never_touches_the_console(self) -> None:
        # A healthy host for a simulated day. The daemon should be invisible.
        outcome = simulate([], steps=200)
        self.assertEqual(outcome.run.world.presses, [])
        self.assertEqual(outcome.violations, [])

    def test_a_busy_timeline_does_act(self) -> None:
        # The complement: if nothing ever pressed a key, every safety
        # invariant above would hold vacuously.
        base = get_seed()
        pressed = 0
        for index in range(timelines()):
            events = generate(base + index, hours=24.0, density=2.0)
            outcome = simulate(events, steps=steps())
            pressed += len(outcome.run.world.presses)
        self.assertGreater(
            pressed, 0, "no generated timeline ever led to a keystroke"
        )


class TestSpecificScenarios(unittest.TestCase):
    """Hand-built timelines for situations worth naming."""

    START = 1_800_000_000.0

    def run_events(self, events, steps_count=300):
        outcome = simulate(events, steps=steps_count)
        self.assertEqual([str(v) for v in outcome.violations], [])
        return outcome

    def test_the_ordinary_case(self) -> None:
        outcome = self.run_events(
            [Event(at=self.START + 100, host=Host.STUCK_PROMPT)]
        )
        self.assertEqual(len(outcome.run.world.presses), 1)

    def test_a_host_stuck_at_a_different_error_is_left_alone(self) -> None:
        outcome = self.run_events(
            [Event(at=self.START + 100, host=Host.STUCK_OTHER)]
        )
        self.assertEqual(outcome.run.world.presses, [])

    def test_a_dark_console_is_left_alone(self) -> None:
        outcome = self.run_events([Event(at=self.START + 100, host=Host.DARK)])
        self.assertEqual(outcome.run.world.presses, [])

    def test_an_unreachable_idrac_during_the_outage(self) -> None:
        outcome = self.run_events(
            [
                Event(at=self.START + 100, host=Host.STUCK_PROMPT),
                Event(at=self.START + 110, idrac=Idrac.REFUSING),
                Event(at=self.START + 4000, idrac=Idrac.OK),
            ]
        )
        self.assertEqual(len(outcome.run.world.presses), 1)

    def test_an_idrac_that_dies_mid_frame_then_recovers(self) -> None:
        outcome = self.run_events(
            [
                Event(at=self.START + 100, host=Host.STUCK_PROMPT),
                Event(at=self.START + 110, idrac=Idrac.DROPPING),
                Event(at=self.START + 2000, idrac=Idrac.OK),
            ]
        )
        self.assertEqual(len(outcome.run.world.presses), 1)

    def test_a_torn_frame_is_not_acted_on(self) -> None:
        # The screen is mid-redraw and shows only two of the three lines.
        # Acting on a partial message is acting on a guess.
        outcome = self.run_events(
            [
                Event(at=self.START + 100, host=Host.STUCK_PROMPT),
                Event(at=self.START + 105, idrac=Idrac.TEARING),
            ],
            steps_count=120,
        )
        self.assertEqual(outcome.run.world.presses, [])

    def test_a_rotated_password_stops_us_acting_and_says_so(self) -> None:
        outcome = self.run_events(
            [
                Event(at=self.START + 100, host=Host.STUCK_PROMPT),
                Event(at=self.START + 105, idrac=Idrac.AUTH_FAILING),
            ],
            steps_count=120,
        )
        self.assertEqual(outcome.run.world.presses, [])

    def test_a_full_disk_does_not_stop_the_fix(self) -> None:
        # Snapshots are diagnostics. Losing them must not cost the rescue.
        outcome = self.run_events(
            [
                Event(at=self.START + 50, action="disk_full"),
                Event(at=self.START + 100, host=Host.STUCK_PROMPT),
            ]
        )
        self.assertEqual(len(outcome.run.world.presses), 1)

    def test_an_ignored_keystroke_is_retried(self) -> None:
        # The firmware swallowed it. Pressing again is correct here, and the
        # "never twice" invariant must not object.
        outcome = self.run_events(
            [
                Event(at=self.START + 100, host=Host.STUCK_PROMPT),
                Event(at=self.START + 105, action="ignore_press"),
            ]
        )
        self.assertGreaterEqual(len(outcome.run.world.presses), 2)

    def test_a_flapping_controller_over_a_day(self) -> None:
        events = []
        for hour in range(12):
            events.append(
                Event(at=self.START + hour * 3600 + 60, host=Host.STUCK_PROMPT)
            )
        outcome = self.run_events(events, steps_count=600)
        self.assertGreaterEqual(len(outcome.run.world.presses), 6)

    def test_the_host_recovering_on_its_own_mid_outage(self) -> None:
        # Somebody walked over and pressed the key. We must not then press at
        # a console showing a login prompt.
        outcome = self.run_events(
            [
                Event(at=self.START + 100, host=Host.STUCK_PROMPT),
                Event(at=self.START + 200, host=Host.UP, label="a human got there first"),
            ]
        )
        for press in outcome.run.world.presses:
            self.assertIs(press.host_state, Host.STUCK_PROMPT)


class TestShrinker(unittest.TestCase):
    """The shrinker has to work, or a failure is unreadable."""

    def test_it_reduces_a_padded_timeline(self) -> None:
        from tests.simulation.test_oracle_selftest import PressesAlways

        start = 1_800_000_000.0
        # One event that causes the failure, buried in noise.
        events = [
            Event(at=start + index * 90, idrac=Idrac.OK, label="noise")
            for index in range(1, 25)
        ]
        events.insert(12, Event(at=start + 100, host=Host.STUCK_OTHER, label="cause"))

        outcome = simulate(events, steps=120, daemon_factory=PressesAlways)
        self.assertTrue(outcome.violations, "the fixture must fail to be shrinkable")

        minimal = shrink(events, steps=120, daemon_factory=PressesAlways)
        self.assertLess(len(minimal), len(events))
        self.assertTrue(
            any(event.host is Host.STUCK_OTHER for event in minimal),
            "the shrinker discarded the actual cause",
        )

    def test_it_leaves_a_passing_timeline_alone(self) -> None:
        start = 1_800_000_000.0
        events = [Event(at=start + 100, host=Host.STUCK_PROMPT)]
        self.assertEqual(shrink(events, steps=60), events)


if __name__ == "__main__":
    unittest.main()
