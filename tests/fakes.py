"""Test doubles shared across tiers.

Everything with a side effect in the daemon arrives through its constructor,
which is what lets the whole loop run here with no sockets, no files, and no
real sleeping. A ten-minute post-fix sleep costs nothing and its exact
duration is assertable.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from boot_err_shim.config import Config, parse_config
from boot_err_shim.daemon import DetectResult
from boot_err_shim.errors import ProtocolError
from boot_err_shim.frame import Frame
from boot_err_shim.platform_ import platform_defaults
from boot_err_shim.probe import ProbeResult

BASE_CONFIG = """
[target]
host = "10.0.0.50"

[ping]
interval       = 120
retry_interval = 90
threshold      = 3

[vnc]
host = "10.0.0.51"

[detect]
text = "Please press 'Y' to continue."

[recovery]
interval       = 60
post_fix_sleep = 600
max_per_day    = 3
"""


def make_config(overlay: str = "", *, system: str = "Linux") -> Config:
    """Parse BASE_CONFIG with an optional overlay merged one table deep."""
    data = tomllib.loads(BASE_CONFIG)
    for table, values in tomllib.loads(overlay).items():
        if isinstance(values, dict) and isinstance(data.get(table), dict):
            data[table] = {**data[table], **values}
        else:
            data[table] = values
    return parse_config(data, defaults=platform_defaults(system))


class FakeClock:
    """Records sleeps instead of taking them.

    ``stop_after`` makes ``sleep`` report an interruption once that many
    sleeps have happened, which is how loop tests terminate.
    """

    def __init__(self, start: float = 1_000_000.0, stop_after: int | None = None):
        self.time = start
        self.sleeps: list[float] = []
        self.stop_after = stop_after
        self.stopped = False

    def now(self) -> float:
        return self.time

    def sleep(self, seconds: float) -> bool:
        self.sleeps.append(seconds)
        self.time += seconds
        if self.stop_after is not None and len(self.sleeps) >= self.stop_after:
            self.stopped = True
        return self.stopped


def blank_frame(width: int = 8, height: int = 4, value: int = 0) -> Frame:
    return Frame(width, height, bytes([value]) * (width * height * 3))


@dataclass
class FakeConsole:
    """A console that behaves however the test needs it to."""

    frame: Frame = field(default_factory=blank_frame)
    #: Raise this from capture() instead of returning a frame.
    capture_error: Exception | None = None
    keys_sent: list[int] = field(default_factory=list)
    closed: bool = False
    close_error: Exception | None = None

    def capture(self) -> Frame:
        if self.capture_error is not None:
            raise self.capture_error
        return self.frame

    def send_key(self, keysym: int) -> None:
        self.keys_sent.append(keysym)

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


@dataclass
class FakeConsoleFactory:
    """Hands out consoles, or refuses to.

    ``fail_with`` set means every attempt raises. ``consoles`` is consumed in
    order so a test can make the first attempt fail and the second succeed.
    """

    console: FakeConsole | None = None
    fail_with: Exception | None = None
    attempts: int = 0
    handed_out: list[FakeConsole] = field(default_factory=list)

    def __call__(self) -> FakeConsole:
        self.attempts += 1
        if self.fail_with is not None:
            raise self.fail_with
        console = self.console or FakeConsole()
        self.handed_out.append(console)
        return console


def refusing_factory(message: str = "connection refused"):
    return FakeConsoleFactory(fail_with=ProtocolError(message))


@dataclass
class ScriptedProbe:
    """Returns a canned sequence of up/down, repeating the last value."""

    results: list[bool]
    calls: list[str] = field(default_factory=list)

    def __call__(self, host: str) -> ProbeResult:
        self.calls.append(host)
        index = min(len(self.calls) - 1, len(self.results) - 1)
        up = self.results[index]
        return ProbeResult(up=up, reason="ok" if up else "unreachable")


def always_up(host: str) -> ProbeResult:
    return ProbeResult(up=True, reason="ok")


def always_down(host: str) -> ProbeResult:
    return ProbeResult(up=False, reason="unreachable")


def matching_detector(frame: Frame) -> DetectResult:
    return DetectResult(matched=True, detail="region+glyph")


def non_matching_detector(frame: Frame) -> DetectResult:
    return DetectResult(matched=False, detail="region-mismatch")


@dataclass
class RecordingFrameWriter:
    """Captures what the daemon tried to persist."""

    written: list[tuple[Frame, str]] = field(default_factory=list)
    fail_with: Exception | None = None

    def __call__(self, frame: Frame, label: str) -> Path:
        if self.fail_with is not None:
            raise self.fail_with
        self.written.append((frame, label))
        return Path(f"/snapshots/{len(self.written)}-{label}.png")


@dataclass
class RecordingNotifier:
    calls: list[list[str]] = field(default_factory=list)
    fail_with: Exception | None = None

    def __call__(self, command: list[str]) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append(list(command))
