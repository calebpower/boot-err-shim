"""Invariants over a finished timeline.

Evaluated against the world's own record, never against the daemon's log. A
checker that read the daemon's log would be asking the daemon whether it
behaved, which is the one witness with a motive.

Each invariant returns a list of violations. An empty list is silence, and
silence is only meaningful because test_oracle_selftest.py proves every one of
these can fire.
"""

from __future__ import annotations

from dataclasses import dataclass

from tests.simulation.world import Host, World


@dataclass(frozen=True)
class Violation:
    invariant: str
    detail: str

    def __str__(self) -> str:
        return f"{self.invariant}: {self.detail}"


@dataclass
class Run:
    """Everything a checker is allowed to look at."""

    world: World
    #: (time, sleep_seconds, reason) for each cycle the daemon completed.
    decisions: list[tuple[float, int, str]]
    #: Intervention counts observed after each press, in order.
    intervention_counts: list[int]
    #: How many interventions the history holds when the run ends.
    recorded_interventions: int
    #: Configured bounds, so sleeps can be checked against them.
    ping_interval: int
    retry_interval: int
    recovery_interval: int
    post_fix_sleep: int
    threshold: int


def never_press_without_the_prompt(run: Run) -> list[Violation]:
    """The cardinal one.

    A keystroke at a console showing something else is the only thing this
    program does that the next iteration cannot undo.
    """
    violations = []
    for press in run.world.presses:
        if press.host_state is not Host.STUCK_PROMPT:
            violations.append(
                Violation(
                    "never-press-without-the-prompt",
                    f"pressed 0x{press.keysym:02x} at t+{press.at - run.world.start:.0f}s "
                    f"while the host was {press.host_state.value}",
                )
            )
    return violations


def never_press_twice_for_one_appearance(run: Run) -> list[Violation]:
    """Once a keystroke has been accepted, do not send another.

    Counted from the accepted press onward, not from the start of the
    appearance. The simulation found the difference: when the firmware
    swallows a keystroke, the daemon is right to send a second one, and an
    invariant phrased as "at most one press per appearance" calls that
    correct retry a defect. What is actually wrong is pressing again at a
    console that has already taken the key and is on its way up.
    """
    violations = []
    for appearance in run.world.appearances:
        if appearance.presses_after_honoured:
            violations.append(
                Violation(
                    "never-press-twice-for-one-appearance",
                    f"appearance {appearance.epoch} received "
                    f"{appearance.presses_after_honoured} further keystroke(s) "
                    f"after one had already been accepted",
                )
            )
    return violations


def eventually_press_when_it_can(run: Run) -> list[Violation]:
    """The inverse failure: a daemon that never acts is also broken.

    Only appearances that stayed on screen, with a reachable iDRAC, for
    comfortably longer than the configured worst case are required to have
    been acted on.
    """
    # threshold failed pings at retry_interval, then a recovery attempt, with
    # generous slack for the cycle the outage began in.
    needed = run.threshold * run.retry_interval + run.recovery_interval
    budget = needed * 3 + run.ping_interval

    violations = []
    for appearance in run.world.appearances:
        if appearance.presses:
            continue
        if appearance.reachable_seconds < budget:
            continue
        ended = appearance.ended if appearance.ended is not None else run.world.now
        violations.append(
            Violation(
                "eventually-press-when-it-can",
                f"appearance {appearance.epoch} was on screen and reachable for "
                f"{appearance.reachable_seconds:.0f}s (budget {budget}s, "
                f"lasted {ended - appearance.started:.0f}s) and was never acted on",
            )
        )
    return violations


def sleeps_stay_within_configured_bounds(run: Run) -> list[Violation]:
    """No busy loop, and no nap longer than anything configured."""
    allowed = {
        run.ping_interval,
        run.retry_interval,
        run.recovery_interval,
        run.post_fix_sleep,
    }
    violations = []
    for at, seconds, reason in run.decisions:
        if seconds <= 0:
            violations.append(
                Violation(
                    "sleeps-stay-within-configured-bounds",
                    f"slept {seconds}s at t+{at - run.world.start:.0f}s "
                    f"({reason}) -- a busy loop against the iDRAC",
                )
            )
        elif seconds not in allowed:
            violations.append(
                Violation(
                    "sleeps-stay-within-configured-bounds",
                    f"slept {seconds}s at t+{at - run.world.start:.0f}s "
                    f"({reason}), which is not a configured interval "
                    f"{sorted(allowed)}",
                )
            )
    return violations


def intervention_count_is_monotone(run: Run) -> list[Violation]:
    """The history only ever grows within a run."""
    violations = []
    previous = 0
    for count in run.intervention_counts:
        if count < previous:
            violations.append(
                Violation(
                    "intervention-count-is-monotone",
                    f"count went from {previous} to {count}",
                )
            )
        previous = count
    return violations


def every_press_is_recorded(run: Run) -> list[Violation]:
    """A keystroke that leaves no trace cannot be escalated later.

    The whole justification for this program being acceptable is that it is
    loud about how often it fires.
    """
    # Against the persisted history, not against what the harness observed at
    # the time: an earlier version compared the harness's own bookkeeping to
    # the press count, which agreed with itself no matter what the daemon
    # wrote to disk.
    if run.recorded_interventions != len(run.world.presses):
        return [
            Violation(
                "every-press-is-recorded",
                f"{len(run.world.presses)} keystroke(s) reached the console but "
                f"the history holds {run.recorded_interventions}",
            )
        ]
    return []


def only_the_configured_key_is_sent(run: Run, keysym: int) -> list[Violation]:
    violations = []
    for press in run.world.presses:
        if press.keysym != keysym:
            violations.append(
                Violation(
                    "only-the-configured-key-is-sent",
                    f"sent 0x{press.keysym:02x}, expected 0x{keysym:02x}",
                )
            )
    return violations


#: Every invariant, so a caller cannot quietly check a subset.
INVARIANTS = (
    never_press_without_the_prompt,
    never_press_twice_for_one_appearance,
    eventually_press_when_it_can,
    sleeps_stay_within_configured_bounds,
    intervention_count_is_monotone,
    every_press_is_recorded,
)


def check(run: Run, *, keysym: int = 0x59) -> list[Violation]:
    violations: list[Violation] = []
    for invariant in INVARIANTS:
        violations.extend(invariant(run))
    violations.extend(only_the_configured_key_is_sent(run, keysym))
    return violations
