"""Tier 9: a simulated host, over a long timeline.

The question only this tier answers: what breaks only after history
accumulates? Every tier below examines one cycle, or one connection, or one
frame. A daemon can be correct in all of those and still be wrong about the
fourth outage in a row, or about a controller that starts flapping, or about
what happens when the iDRAC goes away halfway through the year.

Six parts, following the methodology:

* **Generator** -- a seeded PRNG builds a timeline of events. Looks random,
  reproduces exactly.
* **Actors** -- real Daemon instances, running the real decision code against
  real Frames through the real detector.
* **Shadow model** -- a deliberately partial account of the world: what is on
  screen and whether the host answers. Partial on purpose, because a shadow
  that mirrored the implementation would agree with its bugs.
* **Checker** -- invariants, evaluated against the shadow rather than against
  the daemon's own log.
* **Nemesis** -- adversarial events: the iDRAC dropping mid-frame, a password
  rotated underneath us, the clock jumping, the disk filling, the calibration
  deleted while running.
* **Shrinker** -- bisects a failing timeline down to something a person can
  read.

The cardinal invariant is the same one the safety matrix states at tier 4, now
asserted across thousands of simulated hours rather than sixteen rows: **never
press a key when the prompt is not on screen.**
"""

from __future__ import annotations

import os

#: Fixed so the suite is reproducible; override to explore.
DEFAULT_SEED = 20260816


def get_seed() -> int:
    raw = os.environ.get("BOOT_ERR_SHIM_SIM_SEED")
    return int(raw) if raw else DEFAULT_SEED


def timelines(default: int = 12) -> int:
    raw = os.environ.get("BOOT_ERR_SHIM_SIM_TIMELINES")
    return int(raw) if raw else default


def steps(default: int = 400) -> int:
    raw = os.environ.get("BOOT_ERR_SHIM_SIM_STEPS")
    return int(raw) if raw else default
