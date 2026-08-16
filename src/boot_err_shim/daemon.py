"""The watch loop.

Split deliberately into two halves:

* :func:`decide_after_ping` and :func:`decide_after_recovery` are **pure**.
  They take facts and return a :class:`Decision`. No sockets, no clock, no
  filesystem. Tier 4 drives them as a table, and the table passing unchanged is
  the gate on any refactor here.
* :class:`Daemon` performs the I/O those decisions imply.

The reason for the split is the keypress. Sending a key to a firmware console
that is not showing the prompt is the one genuinely damaging thing this program
can do, so the rule governing it should be readable in one screen and testable
without a socket in sight. :func:`may_press_key` states it once, and
everything else defers to it.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .config import Config
from .errors import ProtocolError, ShimError
from .frame import Frame
from .history import InterventionHistory
from .log import event, get_logger
from .probe import ProbeResult

log = get_logger("daemon")


class Action(Enum):
    """What the loop should do next."""

    SLEEP = "sleep"
    ATTEMPT_RECOVERY = "attempt_recovery"
    PRESS_KEY = "press_key"


@dataclass(frozen=True)
class Decision:
    action: Action
    #: Seconds to sleep after carrying out the action. Zero for
    #: ATTEMPT_RECOVERY, which happens immediately.
    sleep_for: int
    #: Stable token, logged verbatim. Not prose -- alerting may match on it.
    reason: str
    #: Whether the consecutive-failure counter should return to zero.
    reset_failures: bool = False


def decide_after_ping(
    *,
    up: bool,
    failures: int,
    threshold: int,
    ping_interval: int,
    retry_interval: int,
) -> Decision:
    """Decide what follows a reachability probe.

    ``failures`` is the counter *after* accounting for this probe.
    """
    if up:
        return Decision(
            action=Action.SLEEP,
            sleep_for=ping_interval,
            reason="host.up",
            reset_failures=True,
        )

    if failures < threshold:
        return Decision(
            action=Action.SLEEP,
            sleep_for=retry_interval,
            reason="below.threshold",
        )

    # At or past the threshold. Note this stays true on every subsequent
    # cycle while the host is down, which is what keeps the daemon in
    # recovery rather than dropping back to routine polling.
    return Decision(
        action=Action.ATTEMPT_RECOVERY,
        sleep_for=0,
        reason="threshold.reached",
    )


def may_press_key(*, connected: bool, matched: bool, calibrated: bool, no_act: bool) -> bool:
    """The single rule governing whether a key may be sent.

    Every condition is necessary:

    * ``connected`` -- obviously; there is nothing to send to otherwise.
    * ``matched`` -- the prompt was actually found on screen. Never press
      because the host merely stopped answering pings; a host can be down for
      a hundred reasons that a keystroke would not fix and might worsen.
    * ``calibrated`` -- matching without a calibration is guesswork, and this
      program declines to guess about keystrokes.
    * ``not no_act`` -- the operator asked us to observe only.
    """
    return connected and matched and calibrated and not no_act


def decide_after_recovery(
    *,
    connected: bool,
    matched: bool,
    calibrated: bool,
    no_act: bool,
    recovery_interval: int,
    post_fix_sleep: int,
) -> Decision:
    """Decide what follows an attempt to look at the console."""
    if may_press_key(
        connected=connected, matched=matched, calibrated=calibrated, no_act=no_act
    ):
        return Decision(
            action=Action.PRESS_KEY,
            sleep_for=post_fix_sleep,
            reason="match.pressed",
            reset_failures=True,
        )

    if not connected:
        return Decision(
            action=Action.SLEEP, sleep_for=recovery_interval, reason="connect.failed"
        )

    if not matched:
        return Decision(
            action=Action.SLEEP, sleep_for=recovery_interval, reason="no.match"
        )

    if not calibrated:
        # Matched, but we have no business believing the match.
        return Decision(
            action=Action.SLEEP, sleep_for=recovery_interval, reason="not.calibrated"
        )

    # Matched, calibrated, connected, but --no-act. Nothing was fixed, so the
    # host is still down and the short interval is the honest one.
    return Decision(
        action=Action.SLEEP, sleep_for=recovery_interval, reason="match.no_act"
    )


class Clock(Protocol):
    """Wall clock, plus an interruptible sleep."""

    def now(self) -> float:
        """Seconds since the epoch, for history timestamps."""

    def sleep(self, seconds: float) -> bool:
        """Sleep. Returns True if interrupted by a stop request."""


class SystemClock:
    """Real time, with sleeps that a signal can cut short.

    A ten-minute post-fix sleep must not delay ``service stop`` by ten
    minutes, so the wait is on an Event rather than in time.sleep.
    """

    def __init__(self, stop: threading.Event | None = None) -> None:
        self.stop = stop or threading.Event()

    def now(self) -> float:
        import time

        return time.time()

    def sleep(self, seconds: float) -> bool:
        return self.stop.wait(seconds)

    def request_stop(self) -> None:
        self.stop.set()

    @property
    def stopping(self) -> bool:
        return self.stop.is_set()


@dataclass(frozen=True)
class DetectResult:
    matched: bool
    #: Stable token describing how the decision was reached.
    detail: str = ""
    #: What the screen appeared to say, when the detector can tell us.
    text: str | None = None


class Console(Protocol):
    """An open connection to the host's console."""

    def capture(self) -> Frame: ...

    def send_key(self, keysym: int) -> None: ...

    def close(self) -> None: ...


#: Opens a console, or raises ProtocolError.
ConsoleFactory = Callable[[], Console]

#: Examines a frame for the configured message.
Detector = Callable[[Frame], DetectResult]

#: Persists a frame for later diagnosis. Returns where it went, or None.
FrameWriter = Callable[[Frame, str], object]


class Daemon:
    """Carries out what the decision functions decide.

    Everything with a side effect arrives through the constructor so the loop
    can be driven end to end in tests with no sockets, no files, and no real
    sleeping.
    """

    def __init__(
        self,
        config: Config,
        *,
        probe: Callable[[str], ProbeResult],
        console_factory: ConsoleFactory,
        detector: Detector,
        clock: Clock,
        history: InterventionHistory,
        calibrated: bool,
        no_act: bool = False,
        frame_writer: FrameWriter | None = None,
        notifier: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.config = config
        self.probe = probe
        self.console_factory = console_factory
        self.detector = detector
        self.clock = clock
        self.history = history
        self.calibrated = calibrated
        self.no_act = no_act
        self.frame_writer = frame_writer
        self.notifier = notifier or _run_notify_command

        self.failures = 0
        #: Set when a stop was requested during a sleep.
        self.stopped = False

    # -- one turn of the loop -------------------------------------------

    def step(self) -> Decision:
        """Run exactly one cycle and return the decision that governed it.

        Returned rather than merely acted on so tests and the simulator can
        assert the timeline without reading the log.
        """
        result = self.probe(self.config.target.host)
        if result.up:
            self.failures = 0
        else:
            self.failures += 1

        decision = decide_after_ping(
            up=result.up,
            failures=self.failures,
            threshold=self.config.ping.threshold,
            ping_interval=self.config.ping.interval,
            retry_interval=self.config.ping.retry_interval,
        )

        event(
            log,
            logging.INFO if result.up else logging.WARNING,
            "ping.up" if result.up else "ping.down",
            host=self.config.target.host,
            reason=result.reason,
            failures=self.failures,
            threshold=self.config.ping.threshold,
        )

        if decision.action is Action.ATTEMPT_RECOVERY:
            decision = self._recover()

        if decision.reset_failures:
            self.failures = 0

        if decision.sleep_for:
            self.stopped = self.clock.sleep(decision.sleep_for)

        return decision

    def run(self) -> None:
        """Loop until stopped."""
        event(
            log,
            logging.INFO,
            "daemon.start",
            host=self.config.target.host,
            vnc=f"{self.config.vnc.host}:{self.config.vnc.port}",
            threshold=self.config.ping.threshold,
            calibrated=self.calibrated,
            no_act=self.no_act,
        )
        if not self.calibrated:
            event(
                log,
                logging.WARNING,
                "daemon.uncalibrated",
                detail="no calibration; keys will NOT be pressed. "
                "Run: boot-err-shim configure",
            )

        while not self.stopped:
            try:
                self.step()
            except ShimError as exc:
                # A typed failure is a bad cycle, not a dead daemon. Sleep and
                # try again rather than exiting and letting the supervisor
                # restart-loop us.
                event(
                    log,
                    logging.ERROR,
                    "cycle.failed",
                    error=type(exc).__name__,
                    detail=str(exc),
                )
                self.stopped = self.clock.sleep(self.config.recovery.interval)

        event(log, logging.INFO, "daemon.stop")

    # -- recovery -------------------------------------------------------

    def _recover(self) -> Decision:
        connected = False
        matched = False
        console: Console | None = None
        detail = ""

        try:
            console = self.console_factory()
            connected = True
        except ProtocolError as exc:
            event(
                log,
                logging.WARNING,
                "vnc.connect_failed",
                host=self.config.vnc.host,
                port=self.config.vnc.port,
                error=type(exc).__name__,
                detail=str(exc),
            )

        frame: Frame | None = None
        if console is not None:
            try:
                frame = console.capture()
                event(
                    log,
                    logging.INFO,
                    "vnc.captured",
                    width=frame.width,
                    height=frame.height,
                )
                result = self.detector(frame)
                matched = result.matched
                detail = result.detail
                event(
                    log,
                    logging.INFO,
                    "detect.match" if matched else "detect.no_match",
                    detail=detail,
                )
            except ProtocolError as exc:
                # Connected, then the transport died mid-capture. Treat as a
                # failed connection: there is nothing on screen we can trust.
                connected = False
                event(
                    log,
                    logging.WARNING,
                    "vnc.capture_failed",
                    error=type(exc).__name__,
                    detail=str(exc),
                )

        if frame is not None and self.frame_writer is not None:
            # Written whether or not it matched. A false negative is
            # undiagnosable without the frame that produced it.
            try:
                where = self.frame_writer(frame, "match" if matched else "no-match")
                event(log, logging.DEBUG, "frame.saved", path=where)
            except OSError as exc:
                event(log, logging.WARNING, "frame.save_failed", detail=str(exc))

        decision = decide_after_recovery(
            connected=connected,
            matched=matched,
            calibrated=self.calibrated,
            no_act=self.no_act,
            recovery_interval=self.config.recovery.interval,
            post_fix_sleep=self.config.recovery.post_fix_sleep,
        )

        if decision.action is Action.PRESS_KEY and console is not None:
            self._press(console)
        elif matched and self.no_act:
            event(
                log,
                logging.WARNING,
                "key.suppressed",
                key=self.config.detect.key,
                detail="--no-act",
            )
        elif matched and not self.calibrated:
            event(
                log,
                logging.ERROR,
                "key.refused",
                key=self.config.detect.key,
                detail="no calibration",
            )

        if console is not None:
            try:
                console.close()
            except OSError:
                pass

        return decision

    def _press(self, console: Console) -> None:
        console.send_key(self.config.detect.keysym)
        event(
            log,
            logging.WARNING,
            "key.pressed",
            key=self.config.detect.key,
            keysym=self.config.detect.keysym,
            host=self.config.target.host,
        )
        self._record_intervention()

    def _record_intervention(self) -> None:
        now = self.clock.now()
        self.history.record(now)
        recent = self.history.count_within(now)

        limit = self.config.recovery.max_per_day
        if limit and recent > limit:
            event(
                log,
                logging.WARNING,
                "intervention.frequent",
                count=recent,
                limit=limit,
                detail="controller is failing repeatedly; replace it",
            )
            if self.config.recovery.notify_command:
                try:
                    self.notifier(list(self.config.recovery.notify_command))
                except OSError as exc:
                    event(log, logging.WARNING, "notify.failed", detail=str(exc))
        else:
            event(log, logging.INFO, "intervention.recorded", count=recent, limit=limit)


def _run_notify_command(command: list[str]) -> None:
    subprocess.run(command, check=False, timeout=30)  # noqa: S603
