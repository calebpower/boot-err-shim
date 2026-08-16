"""A record of every time the shim intervened.

This is the part that keeps the program honest about being a workaround. A
controller that needs rescuing once is a workaround doing its job; one that
needs rescuing three times a week is a controller somebody should be
replacing, and nobody will notice that from a log line buried among a
fortnight of routine pings.

Persisted so a restart does not reset the count -- otherwise a crash loop would
hide exactly the pattern worth seeing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .lock import atomic_write_text

#: Entries older than this are dropped when the file is rewritten, so it stays
#: small forever without needing separate maintenance.
RETENTION_SECONDS = 30 * 86400

DAY_SECONDS = 86400


@dataclass
class InterventionHistory:
    """Timestamps of past interventions, newest last."""

    path: Path
    timestamps: list[float]

    @classmethod
    def load(cls, path: Path) -> InterventionHistory:
        """Read the history, treating any unreadable file as empty.

        A corrupt history must not stop the daemon from doing its job. The
        worst case of ignoring it is a missed warning; the worst case of
        raising here is a host that stays down because a JSON file got
        truncated.
        """
        try:
            raw = path.read_bytes()
        except OSError:
            return cls(path=path, timestamps=[])

        try:
            # Decoded inside the guard, not by read_text: a file containing a
            # stray byte raises UnicodeDecodeError, and that must be as
            # survivable as any other damage.
            data = json.loads(raw.decode("utf-8"))
            entries = data["interventions"]
            timestamps = sorted(
                float(entry) for entry in entries if isinstance(entry, (int, float))
            )
        except (ValueError, TypeError, KeyError):
            return cls(path=path, timestamps=[])

        return cls(path=path, timestamps=timestamps)

    def record(self, when: float) -> None:
        """Add an intervention and persist atomically."""
        self.timestamps.append(when)
        self.timestamps.sort()
        self.prune(when)
        self.save()

    def prune(self, now: float) -> None:
        cutoff = now - RETENTION_SECONDS
        self.timestamps = [t for t in self.timestamps if t >= cutoff]

    def count_within(self, now: float, window: float = DAY_SECONDS) -> int:
        """How many interventions fall in the ``window`` ending at ``now``."""
        cutoff = now - window
        return sum(1 for t in self.timestamps if t > cutoff)

    def save(self) -> None:
        payload = json.dumps(
            {"interventions": self.timestamps}, indent=2, sort_keys=True
        )
        atomic_write_text(self.path, payload + "\n")
