"""Tier 7: seeded fuzzing.

The question only this tier answers: do ordinary untried inputs crash it?

The oracle, translated from the methodology's "no 5xx, ever":

1. **No unhandled exception, ever.** Every failure is a typed ShimError.
2. **Every failure carries a usable message**, not an empty string.
3. **The process is still alive afterwards** and the next input still works.

The empirical note the methodology makes -- that most defects found in
practice were unguarded dereferences surfacing as 500s -- maps here almost
exactly. Our version is an unguarded ``struct.unpack`` on a short read, or an
index into a buffer whose length came from the wire.

Determinism is the whole game. The seed is fixed by default so CI is
reproducible, printed on every run, and overridable:

    BOOT_ERR_SHIM_FUZZ_SEED=12345 python -m unittest tests.fuzz...
    BOOT_ERR_SHIM_FUZZ_ITERATIONS=5000 python -m unittest tests.fuzz...

A failing case prints the seed and the offending input as hex, so it can be
pinned as a regression test rather than merely rediscovered.
"""

from __future__ import annotations

import os
import random

#: Fixed so the suite is reproducible; override to explore further.
DEFAULT_SEED = 20260816
DEFAULT_ITERATIONS = 300


def get_seed() -> int:
    raw = os.environ.get("BOOT_ERR_SHIM_FUZZ_SEED")
    return int(raw) if raw else DEFAULT_SEED


def iterations(default: int = DEFAULT_ITERATIONS) -> int:
    raw = os.environ.get("BOOT_ERR_SHIM_FUZZ_ITERATIONS")
    return int(raw) if raw else default


def rng(label: str) -> random.Random:
    """A generator seeded from the run seed and a per-target label.

    Per-target so that adding a case to one fuzzer does not shift every other
    fuzzer's inputs, which would make a regression look like a new finding.
    """
    return random.Random(f"{get_seed()}:{label}")


def describe(data: bytes, limit: int = 200) -> str:
    """Render an input for a failure message, truncated but replayable."""
    shown = data[:limit]
    suffix = f" ... (+{len(data) - limit} bytes)" if len(data) > limit else ""
    return f"seed={get_seed()} len={len(data)} hex={shown.hex()}{suffix}"
