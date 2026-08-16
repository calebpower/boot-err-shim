"""The simulated host and iDRAC.

Deliberately not a mock of the daemon's collaborators. The world renders real
framebuffers and the real detector runs on them, so the simulation exercises
the actual recognition path rather than a stand-in that would agree with
whatever the detector happens to do.

Frames are small (a 6x10 cell font, 380x80) and rendered once at construction,
so a timeline of hundreds of steps costs a second rather than a minute.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))

from boot_err_shim.errors import AuthError, ConnectionFailed, ProtocolError
from boot_err_shim.frame import Frame
from boot_err_shim.probe import ProbeResult
from render_frame import THE_MESSAGE, render

CELL_WIDTH = 6
CELL_HEIGHT = 10
SCREEN_WIDTH = 380
SCREEN_HEIGHT = 80

OTHER_ERROR = (
    "No boot device available.",
    "Press F1 to retry boot, F2 for setup.",
    "Press F5 for diagnostics.",
)


class Host(Enum):
    """What the machine is doing."""

    UP = "up"
    #: Stopped at the PERC prompt. A keystroke fixes this.
    STUCK_PROMPT = "stuck_prompt"
    #: Stopped at something else. A keystroke does not fix it and must not
    #: be sent.
    STUCK_OTHER = "stuck_other"
    #: Powered off or wedged with a blank console.
    DARK = "dark"


class Idrac(Enum):
    """What the management controller is doing."""

    OK = "ok"
    #: Refuses TCP entirely.
    REFUSING = "refusing"
    #: Accepts, then dies partway through the framebuffer.
    DROPPING = "dropping"
    #: Password no longer works.
    AUTH_FAILING = "auth_failing"
    #: Serves a frame captured mid-redraw.
    TEARING = "tearing"


@dataclass
class Event:
    at: float
    host: Host | None = None
    idrac: Idrac | None = None
    #: Free-text label for the shrinker's report.
    label: str = ""
    #: Nemesis actions that are not simple state changes.
    action: str = ""


@dataclass
class Press:
    """A keystroke that reached the console."""

    at: float
    keysym: int
    #: What the shadow model says was on screen at that instant.
    host_state: Host
    #: Which appearance of the prompt this belongs to, or None.
    epoch: int | None


@dataclass
class Appearance:
    """One continuous period with the prompt on screen."""

    epoch: int
    started: float
    ended: float | None = None
    #: Seconds during this appearance for which the iDRAC was reachable.
    reachable_seconds: float = 0.0
    presses: int = 0
    honoured: bool = False
    #: Keystrokes that arrived *after* one had already been accepted.
    #:
    #: This, not the raw press count, is the double-press defect. When the
    #: firmware swallows a keystroke the daemon is right to send another, and
    #: counting presses per appearance would call that correct retry a bug.
    presses_after_honoured: int = 0


class SimulatedConsole:
    """What console_factory hands the daemon."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.closed = False

    def capture(self) -> Frame:
        return self.world.capture()

    def send_key(self, keysym: int) -> None:
        self.world.receive_key(keysym)

    def close(self) -> None:
        self.closed = True


class World:
    """A host, an iDRAC, and a clock somebody else advances."""

    def __init__(self, events: list[Event], *, start: float = 1_800_000_000.0):
        self.events = sorted(events, key=lambda e: e.at)
        self.start = start
        self.now = start
        self.host = Host.UP
        self.idrac = Idrac.OK

        self._pending = list(self.events)
        self._epoch = 0
        self.appearances: list[Appearance] = []
        self.presses: list[Press] = []
        #: Nemesis flags the harness reads.
        self.disk_full = False
        self.calibration_deleted = False
        #: Set when a press is deliberately ignored, so "one press per
        #: appearance" is not asserted against a world that swallowed it.
        self.ignore_next_press = False

        self.screens = _render_screens()
        self._last_advance = start

    # -- time ----------------------------------------------------------

    def advance(self, to: float) -> None:
        """Move the clock, applying any events that fall in between."""
        while self._pending and self._pending[0].at <= to:
            event = self._pending.pop(0)
            self._accumulate(event.at)
            self._apply(event)
        self._accumulate(to)
        self.now = to

    def _accumulate(self, until: float) -> None:
        """Credit reachable time to the open appearance, then move the mark."""
        if until <= self._last_advance:
            return
        current = self._open_appearance()
        if current is not None and self.idrac is Idrac.OK:
            current.reachable_seconds += until - self._last_advance
        self._last_advance = until

    def _apply(self, event: Event) -> None:
        self.now = event.at
        if event.idrac is not None:
            self.idrac = event.idrac
        if event.host is not None:
            self._set_host(event.host)
        if event.action == "disk_full":
            self.disk_full = True
        elif event.action == "disk_ok":
            self.disk_full = False
        elif event.action == "delete_calibration":
            self.calibration_deleted = True
        elif event.action == "ignore_press":
            self.ignore_next_press = True

    def _set_host(self, state: Host) -> None:
        if state is self.host:
            return
        if self.host is Host.STUCK_PROMPT:
            appearance = self._open_appearance()
            if appearance is not None:
                appearance.ended = self.now
        self.host = state
        if state is Host.STUCK_PROMPT:
            self._epoch += 1
            self.appearances.append(
                Appearance(epoch=self._epoch, started=self.now)
            )

    def _open_appearance(self) -> Appearance | None:
        if not self.appearances:
            return None
        last = self.appearances[-1]
        return last if last.ended is None else None

    # -- what the daemon sees ------------------------------------------

    def probe(self, host: str) -> ProbeResult:
        if self.host is Host.UP:
            return ProbeResult(up=True, reason="ok")
        return ProbeResult(up=False, reason="unreachable")

    def console_factory(self) -> SimulatedConsole:
        if self.idrac is Idrac.REFUSING:
            raise ConnectionFailed("simulated: connection refused")
        if self.idrac is Idrac.AUTH_FAILING:
            raise AuthError("simulated: authentication failed")
        return SimulatedConsole(self)

    def capture(self) -> Frame:
        if self.idrac is Idrac.DROPPING:
            raise ProtocolError("simulated: connection lost mid-frame")
        if self.idrac is Idrac.TEARING:
            return self.screens["torn"]
        return self.screens[self.host.value]

    #: How long the host takes to finish booting after the keystroke.
    #:
    #: Not zero, and that matters. A world where the prompt vanishes the
    #: instant a key arrives makes "never press twice for one appearance"
    #: unreachable -- any second press lands after the host is already up and
    #: is caught by a different invariant instead. Real hardware takes a while
    #: to get from the prompt to a login, and it is exactly that window in
    #: which a careless daemon presses again.
    BOOT_DELAY = 45.0

    def receive_key(self, keysym: int) -> None:
        epoch = None
        appearance = self._open_appearance()
        if appearance is not None:
            epoch = appearance.epoch
            appearance.presses += 1
            if appearance.honoured:
                appearance.presses_after_honoured += 1

        self.presses.append(
            Press(at=self.now, keysym=keysym, host_state=self.host, epoch=epoch)
        )

        if self.host is Host.STUCK_PROMPT:
            if self.ignore_next_press:
                self.ignore_next_press = False
                return
            if appearance is not None:
                appearance.honoured = True
            # The boot finishes shortly afterwards, not instantly.
            self._schedule(Event(at=self.now + self.BOOT_DELAY, host=Host.UP,
                                 label="booted after keystroke"))

    def _schedule(self, event: Event) -> None:
        self._pending.append(event)
        self._pending.sort(key=lambda e: e.at)

    def write_frame(self, frame: Frame, label: str):
        if self.disk_full:
            raise OSError(28, "No space left on device")
        return f"/snapshots/{label}.png"

    # -- reporting -----------------------------------------------------

    def describe(self) -> str:
        lines = [f"start={self.start:.0f} events={len(self.events)}"]
        for event in self.events:
            parts = [f"  +{event.at - self.start:8.0f}s"]
            if event.host:
                parts.append(f"host={event.host.value}")
            if event.idrac:
                parts.append(f"idrac={event.idrac.value}")
            if event.action:
                parts.append(f"action={event.action}")
            if event.label:
                parts.append(f"({event.label})")
            lines.append(" ".join(parts))
        return "\n".join(lines)


def _render_screens() -> dict[str, Frame]:
    common = {
        "cell_width": CELL_WIDTH,
        "cell_height": CELL_HEIGHT,
        "origin_x": 4,
        "origin_y": 4,
        "width": SCREEN_WIDTH,
        "height": SCREEN_HEIGHT,
    }
    blank = Frame(
        SCREEN_WIDTH, SCREEN_HEIGHT, bytes(SCREEN_WIDTH * SCREEN_HEIGHT * 3)
    )
    return {
        Host.UP.value: render(("Ubuntu 26.04 LTS login:",), **common),
        Host.STUCK_PROMPT.value: render(THE_MESSAGE, **common),
        Host.STUCK_OTHER.value: render(OTHER_ERROR, **common),
        Host.DARK.value: blank,
        # A frame captured mid-redraw: the last line has not been drawn yet.
        "torn": render(THE_MESSAGE[:2], **common),
    }


# -- the generator -----------------------------------------------------

#: Adversarial actions, per the methodology's nemesis. Each is something that
#: has a plausible real cause, not merely a way to make the daemon unhappy.
NEMESIS = [
    ("idrac drops mid-frame", lambda at: Event(at=at, idrac=Idrac.DROPPING,
                                               label="nemesis")),
    ("idrac refuses", lambda at: Event(at=at, idrac=Idrac.REFUSING,
                                       label="nemesis")),
    ("password rotated", lambda at: Event(at=at, idrac=Idrac.AUTH_FAILING,
                                          label="nemesis")),
    ("frame captured mid-redraw", lambda at: Event(at=at, idrac=Idrac.TEARING,
                                                   label="nemesis")),
    ("disk full", lambda at: Event(at=at, action="disk_full", label="nemesis")),
    ("disk recovered", lambda at: Event(at=at, action="disk_ok", label="nemesis")),
    ("keystroke ignored", lambda at: Event(at=at, action="ignore_press",
                                           label="nemesis")),
    ("stuck at a different error", lambda at: Event(at=at, host=Host.STUCK_OTHER,
                                                    label="nemesis")),
    ("console goes dark", lambda at: Event(at=at, host=Host.DARK,
                                           label="nemesis")),
]


def generate(seed: int, *, hours: float = 24.0, density: float = 1.0) -> list[Event]:
    """Build a timeline. Quasi-nondeterministic: random-looking, reproducible."""
    rng = random.Random(f"world:{seed}")
    start = 1_800_000_000.0
    span = hours * 3600
    events: list[Event] = []

    at = start
    while at < start + span:
        at += rng.expovariate(density / 1800.0)
        if at >= start + span:
            break

        roll = rng.random()
        if roll < 0.35:
            events.append(Event(at=at, host=Host.STUCK_PROMPT, label="crash to prompt"))
        elif roll < 0.5:
            events.append(Event(at=at, host=Host.UP, label="recovered"))
        elif roll < 0.6:
            events.append(Event(at=at, idrac=Idrac.OK, label="idrac healthy"))
        else:
            _name, build = rng.choice(NEMESIS)
            events.append(build(at))

    return events
