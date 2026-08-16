"""Running a daemon through a world, and shrinking a failure.

The daemon here is the real one, with the real decision code and the real
detector working on real framebuffers. Only time and the outside world are
simulated.
"""

from __future__ import annotations

import io
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from boot_err_shim.calibrate import analyse
from boot_err_shim.daemon import Daemon
from boot_err_shim.detect import CalibratedDetector
from boot_err_shim.errors import ShimError
from boot_err_shim.history import InterventionHistory
from boot_err_shim.log import setup_logging
from tests.fakes import make_config
from tests.simulation.checker import Run, check
from tests.simulation.world import Event, Host, World

CONFIG_OVERLAY = """
[ping]
interval       = 120
retry_interval = 60
threshold      = 3

[recovery]
interval       = 60
post_fix_sleep = 600
max_per_day    = 3
"""


class SimClock:
    """A clock the world advances with."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.stopped = False

    def now(self) -> float:
        return self.world.now

    def sleep(self, seconds: float) -> bool:
        self.world.advance(self.world.now + seconds)
        return self.stopped


@dataclass
class Outcome:
    run: Run
    violations: list
    steps: int


def simulate(
    events: list[Event],
    *,
    steps: int = 400,
    daemon_factory=None,
    keysym: int = 0x59,
) -> Outcome:
    """Run a daemon through a timeline and check the invariants."""
    world = World(events)
    config = make_config(CONFIG_OVERLAY)

    calibration = analyse(world.screens[Host.STUCK_PROMPT.value], config.detect.lines)
    detector = CalibratedDetector(calibration, config.detect.tolerance)

    clock = SimClock(world)
    decisions: list[tuple[float, int, str]] = []
    counts: list[int] = []

    with tempfile.TemporaryDirectory() as tmp:
        history = InterventionHistory.load(Path(tmp) / "history.json")

        setup_logging(stream=io.StringIO(), syslog="never", level="CRITICAL")

        build = daemon_factory or Daemon
        daemon = build(
            config,
            probe=world.probe,
            console_factory=world.console_factory,
            detector=detector,
            clock=clock,
            history=history,
            calibrated=True,
            no_act=False,
            frame_writer=world.write_frame,
        )

        for _ in range(steps):
            before = len(world.presses)
            at = world.now
            try:
                decision = daemon.step()
            except ShimError:
                # A typed failure is a survivable cycle; the daemon's own
                # run loop treats it the same way.
                clock.sleep(config.recovery.interval)
                continue
            decisions.append((at, decision.sleep_for, decision.reason))
            if len(world.presses) > before:
                counts.append(len(history.timestamps))

        recorded = len(history.timestamps)

    run = Run(
        world=world,
        decisions=decisions,
        intervention_counts=counts,
        recorded_interventions=recorded,
        ping_interval=config.ping.interval,
        retry_interval=config.ping.retry_interval,
        recovery_interval=config.recovery.interval,
        post_fix_sleep=config.recovery.post_fix_sleep,
        threshold=config.ping.threshold,
    )
    return Outcome(run=run, violations=check(run, keysym=keysym), steps=steps)


def shrink(
    events: list[Event],
    *,
    steps: int,
    daemon_factory=None,
    keysym: int = 0x59,
) -> list[Event]:
    """Reduce a failing timeline to something a person can read.

    Delta debugging, simplest form: try removing each event, keep the removal
    if the run still fails. A timeline of two hundred events usually reduces
    to two or three, and the difference between those is the difference
    between a bug report somebody acts on and one they file away.
    """

    def still_fails(candidate: list[Event]) -> bool:
        if not candidate:
            return False
        try:
            return bool(
                simulate(
                    candidate,
                    steps=steps,
                    daemon_factory=daemon_factory,
                    keysym=keysym,
                ).violations
            )
        except Exception:  # noqa: BLE001 - a crash is also a failure
            return True

    current = list(events)
    changed = True
    while changed:
        changed = False
        index = 0
        while index < len(current):
            attempt = current[:index] + current[index + 1 :]
            if still_fails(attempt):
                current = attempt
                changed = True
            else:
                index += 1
    return current
