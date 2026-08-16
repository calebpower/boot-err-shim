"""Tier 1: atomic writes and the single-instance lock.

These are tier 8 invariants tested at tier 1 where possible -- the cheapest
oracle that can answer the question. Tier 8 covers the parts that genuinely
need concurrency.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from boot_err_shim.errors import LockError
from boot_err_shim.lock import SingleInstanceLock, atomic_write_bytes, atomic_write_text


class TempDirTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

    def leftovers(self) -> list[str]:
        """Any file that is not one we deliberately created."""
        return [p.name for p in self.dir.iterdir() if p.name.startswith(".")]


class TestAtomicWrite(TempDirTest):
    def test_writes_content(self) -> None:
        target = self.dir / "f.toml"
        atomic_write_bytes(target, b"hello")
        self.assertEqual(target.read_bytes(), b"hello")

    def test_overwrites_existing(self) -> None:
        target = self.dir / "f.toml"
        target.write_bytes(b"old content that is longer")
        atomic_write_bytes(target, b"new")
        self.assertEqual(target.read_bytes(), b"new")

    def test_text_helper_is_utf8(self) -> None:
        target = self.dir / "f.txt"
        atomic_write_text(target, "café — ü")
        self.assertEqual(target.read_bytes().decode("utf-8"), "café — ü")

    def test_creates_missing_parents(self) -> None:
        target = self.dir / "a" / "b" / "f.txt"
        atomic_write_text(target, "x")
        self.assertEqual(target.read_text(encoding="utf-8"), "x")

    def test_empty_write_is_allowed(self) -> None:
        target = self.dir / "f"
        atomic_write_bytes(target, b"")
        self.assertEqual(target.read_bytes(), b"")

    def test_no_temp_file_left_behind_on_success(self) -> None:
        atomic_write_bytes(self.dir / "f", b"x")
        self.assertEqual(self.leftovers(), [])

    def test_failure_leaves_the_original_intact(self) -> None:
        # The invariant that matters: a reader during a failed write sees the
        # previous good file, never a truncated one.
        target = self.dir / "calibration.toml"
        target.write_bytes(b"previous good calibration")

        with mock.patch("boot_err_shim.lock.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                atomic_write_bytes(target, b"partial")

        self.assertEqual(target.read_bytes(), b"previous good calibration")

    def test_failure_leaves_no_temp_file(self) -> None:
        with mock.patch("boot_err_shim.lock.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                atomic_write_bytes(self.dir / "f", b"x")
        self.assertEqual(self.leftovers(), [])

    def test_interrupt_mid_write_leaves_no_temp_file(self) -> None:
        # KeyboardInterrupt derives from BaseException, not Exception -- an
        # `except Exception` in the cleanup path would miss exactly the case
        # this is here for (a signal arriving during shutdown).
        with mock.patch(
            "boot_err_shim.lock.os.replace", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                atomic_write_bytes(self.dir / "f", b"x")
        self.assertEqual(self.leftovers(), [])

    def test_temp_file_is_a_sibling_of_the_target(self) -> None:
        # A temp file in /tmp would make the rename cross-device, and a
        # cross-device rename is a copy -- which is not atomic.
        seen: list[str] = []
        real_mkstemp = tempfile.mkstemp

        def spy(*args, **kwargs):
            seen.append(kwargs.get("dir", ""))
            return real_mkstemp(*args, **kwargs)

        with mock.patch("boot_err_shim.lock.tempfile.mkstemp", side_effect=spy):
            atomic_write_bytes(self.dir / "sub" / "f", b"x")

        self.assertEqual(seen, [str(self.dir / "sub")])


class TestSingleInstanceLock(TempDirTest):
    def test_acquire_and_release(self) -> None:
        lock = SingleInstanceLock(self.dir / "l.lock")
        lock.acquire()
        lock.release()

    def test_records_the_holding_pid(self) -> None:
        path = self.dir / "l.lock"
        with SingleInstanceLock(path):
            self.assertEqual(path.read_text(encoding="utf-8").strip(), str(os.getpid()))

    def test_second_holder_is_refused(self) -> None:
        # The double-keypress invariant: two daemons must not both decide the
        # host is down and both press 'Y'.
        path = self.dir / "l.lock"
        with SingleInstanceLock(path):
            with self.assertRaises(LockError):
                SingleInstanceLock(path).acquire()

    def test_refusal_names_the_holder(self) -> None:
        path = self.dir / "l.lock"
        with SingleInstanceLock(path):
            with self.assertRaises(LockError) as caught:
                SingleInstanceLock(path).acquire()
        self.assertIn(str(os.getpid()), str(caught.exception))

    def test_lock_is_reusable_after_release(self) -> None:
        path = self.dir / "l.lock"
        with SingleInstanceLock(path):
            pass
        with SingleInstanceLock(path):
            pass

    def test_lock_file_survives_release(self) -> None:
        # Unlinking on release races: another process may have already opened
        # the same path, and the two would then lock different inodes.
        path = self.dir / "l.lock"
        with SingleInstanceLock(path):
            pass
        self.assertTrue(path.exists())

    def test_release_is_idempotent(self) -> None:
        lock = SingleInstanceLock(self.dir / "l.lock")
        lock.acquire()
        lock.release()
        lock.release()

    def test_creates_missing_parents(self) -> None:
        with SingleInstanceLock(self.dir / "deep" / "l.lock"):
            pass

    def test_exception_inside_the_context_still_releases(self) -> None:
        path = self.dir / "l.lock"
        with self.assertRaises(RuntimeError):
            with SingleInstanceLock(path):
                raise RuntimeError("boom")
        with SingleInstanceLock(path):
            pass


if __name__ == "__main__":
    unittest.main()
