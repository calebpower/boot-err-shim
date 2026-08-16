"""Tier 1: the intervention history.

Boundaries at the day window and at the retention cutoff, plus the rule that a
damaged history file must never stop the daemon working. Losing the warning is
a nuisance; refusing to boot a host because a JSON file got truncated is an
outage.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from boot_err_shim.history import (
    DAY_SECONDS,
    RETENTION_SECONDS,
    InterventionHistory,
)

NOW = 1_800_000_000.0


class HistoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "history.json"


class TestCounting(HistoryTest):
    def history(self, *timestamps: float) -> InterventionHistory:
        return InterventionHistory(path=self.path, timestamps=list(timestamps))

    def test_empty(self) -> None:
        self.assertEqual(self.history().count_within(NOW), 0)

    def test_counts_events_inside_the_window(self) -> None:
        history = self.history(NOW - 10, NOW - 3600, NOW - 7200)
        self.assertEqual(history.count_within(NOW), 3)

    def test_excludes_events_outside_the_window(self) -> None:
        history = self.history(NOW - DAY_SECONDS - 1)
        self.assertEqual(history.count_within(NOW), 0)

    def test_exactly_at_the_window_edge_is_excluded(self) -> None:
        # Strictly greater than the cutoff, so an event exactly a day old has
        # aged out. Either convention is defensible; this pins the one chosen.
        self.assertEqual(self.history(NOW - DAY_SECONDS).count_within(NOW), 0)

    def test_one_second_inside_the_edge_is_included(self) -> None:
        self.assertEqual(self.history(NOW - DAY_SECONDS + 1).count_within(NOW), 1)

    def test_a_custom_window(self) -> None:
        history = self.history(NOW - 100, NOW - 10_000)
        self.assertEqual(history.count_within(NOW, window=1000), 1)

    def test_future_timestamps_count(self) -> None:
        # A clock that jumped backwards should not make the count negative or
        # silently drop the record.
        self.assertEqual(self.history(NOW + 500).count_within(NOW), 1)


class TestRecording(HistoryTest):
    def test_record_persists(self) -> None:
        history = InterventionHistory.load(self.path)
        history.record(NOW)
        self.assertEqual(InterventionHistory.load(self.path).timestamps, [NOW])

    def test_records_accumulate(self) -> None:
        history = InterventionHistory.load(self.path)
        history.record(NOW - 100)
        history.record(NOW)
        self.assertEqual(InterventionHistory.load(self.path).timestamps, [NOW - 100, NOW])

    def test_timestamps_stay_sorted_even_out_of_order(self) -> None:
        history = InterventionHistory.load(self.path)
        history.record(NOW)
        history.record(NOW - 500)
        self.assertEqual(history.timestamps, [NOW - 500, NOW])

    def test_recording_prunes_old_entries(self) -> None:
        history = InterventionHistory(
            path=self.path, timestamps=[NOW - RETENTION_SECONDS - 1]
        )
        history.record(NOW)
        self.assertEqual(history.timestamps, [NOW])

    def test_entries_just_inside_retention_are_kept(self) -> None:
        keeper = NOW - RETENTION_SECONDS + 1
        history = InterventionHistory(path=self.path, timestamps=[keeper])
        history.record(NOW)
        self.assertEqual(history.timestamps, [keeper, NOW])

    def test_the_file_is_valid_json_with_a_named_key(self) -> None:
        history = InterventionHistory.load(self.path)
        history.record(NOW)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"interventions": [NOW]})

    def test_parent_directories_are_created(self) -> None:
        path = Path(self._dir.name) / "a" / "b" / "history.json"
        history = InterventionHistory.load(path)
        history.record(NOW)
        self.assertTrue(path.exists())


class TestDamagedFiles(HistoryTest):
    """Every one of these must yield an empty history, never an exception."""

    def load_after(self, content: str) -> InterventionHistory:
        self.path.write_text(content, encoding="utf-8")
        return InterventionHistory.load(self.path)

    def test_missing_file(self) -> None:
        self.assertEqual(InterventionHistory.load(self.path).timestamps, [])

    def test_empty_file(self) -> None:
        self.assertEqual(self.load_after("").timestamps, [])

    def test_truncated_json(self) -> None:
        # Exactly what a SIGKILL mid-write would leave behind if the write
        # were not atomic.
        self.assertEqual(self.load_after('{"interventions": [1234').timestamps, [])

    def test_wrong_shape(self) -> None:
        self.assertEqual(self.load_after('{"other": 1}').timestamps, [])

    def test_json_that_is_not_an_object(self) -> None:
        self.assertEqual(self.load_after("[1, 2, 3]").timestamps, [])

    def test_non_numeric_entries_are_dropped_not_fatal(self) -> None:
        history = self.load_after('{"interventions": [1.0, "x", null, 2.0]}')
        self.assertEqual(history.timestamps, [1.0, 2.0])

    def test_binary_garbage(self) -> None:
        self.path.write_bytes(b"\x00\xff\xfe")
        self.assertEqual(InterventionHistory.load(self.path).timestamps, [])

    def test_a_directory_where_the_file_should_be(self) -> None:
        self.path.mkdir()
        self.assertEqual(InterventionHistory.load(self.path).timestamps, [])

    def test_unsorted_input_is_sorted_on_load(self) -> None:
        history = self.load_after('{"interventions": [5.0, 1.0, 3.0]}')
        self.assertEqual(history.timestamps, [1.0, 3.0, 5.0])


if __name__ == "__main__":
    unittest.main()
