"""Tier 2: conformance. Did rendered output drift?

The `configure` report is the operator's only window into what calibration
actually did, and the log line format is an interface anything alerting on
these lines depends on. Both are compared as whole strings against committed
golden files -- not "contains", not a regex. A reworded line is a change to an
interface and should have to be acknowledged.

Regenerate deliberately, never reflexively:

    BOOT_ERR_SHIM_REGENERATE_GOLDEN=1 python -m unittest tests.conformance...

and read the diff before committing it.
"""

from __future__ import annotations

import os
from pathlib import Path

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "golden"

REGENERATE = os.environ.get("BOOT_ERR_SHIM_REGENERATE_GOLDEN") == "1"


def compare(test, name: str, actual: str) -> None:
    """Assert ``actual`` equals the golden file ``name``."""
    path = GOLDEN_DIR / name
    if REGENERATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
        test.skipTest(f"regenerated {name}")

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
        test.fail(
            f"{name} did not exist; it has been written. Read it, check it is "
            "what you meant, and commit it."
        )

    expected = path.read_text(encoding="utf-8")
    test.assertEqual(
        actual,
        expected,
        f"\n{name} drifted. If the change is intended, regenerate with "
        "BOOT_ERR_SHIM_REGENERATE_GOLDEN=1 and read the diff.",
    )
