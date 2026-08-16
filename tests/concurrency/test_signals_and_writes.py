"""Tier 8: signals and concurrent writes.

Both invariants here exist because of what happens *after* the failure rather
than during it. A torn calibration is not a crash -- the daemon starts fine
next time and quietly refuses to press anything, so a clean shutdown becomes
an outage nobody connects to the shutdown. A sleep that ignores a signal is
not a crash either; it is a `service stop` that appears to hang.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "tools"))

from boot_err_shim.calibrate import Calibration, analyse  # noqa: E402
from boot_err_shim.daemon import SystemClock  # noqa: E402
from boot_err_shim.errors import CalibrationError  # noqa: E402
from boot_err_shim.lock import atomic_write_text  # noqa: E402
from render_frame import THE_MESSAGE, render  # noqa: E402


class TestConcurrentCalibrationWrites(unittest.TestCase):
    """A reader must never observe a half-written calibration."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "calibration.toml"
        self.calibration = analyse(render(THE_MESSAGE), THE_MESSAGE)
        self.calibration.save(self.path)

    #: Errno values Windows raises when a rename collides with an open reader.
    #:
    #: POSIX rename replaces a file no matter who has it open, which is the
    #: whole basis of the atomic-write design. Windows refuses instead. That
    #: is a platform property rather than a defect in the write path, and it
    #: is not worth a retry loop in the one code path where a partial write
    #: costs a keypress -- Windows is a development platform here, and the
    #: deployment targets both have POSIX rename semantics.
    WINDOWS_RENAME_COLLISION = (5, 32)

    def _tolerable(self, exc: OSError) -> bool:
        if os.name == "posix":
            return False
        import errno

        return (
            getattr(exc, "winerror", None) in self.WINDOWS_RENAME_COLLISION
            or exc.errno in (errno.EACCES, errno.EBUSY)
        )

    #: Writes the test insists on completing before it will draw a
    #: conclusion. Counted rather than timed: each atomic write fsyncs the
    #: file *and* its directory, and on a container overlay filesystem that
    #: is slow enough that a two-second budget managed three writes. A test
    #: whose coverage depends on how fast the disk is will pass on a laptop
    #: and fail in CI for no reason anybody can act on.
    REQUIRED_WRITES = 20

    def test_a_reader_never_sees_a_partial_file(self) -> None:
        stop = threading.Event()
        failures: list[str] = []
        reads = [0]
        writes = [0]
        payload = self.calibration.to_toml()

        def writer() -> None:
            try:
                while writes[0] < self.REQUIRED_WRITES and not stop.is_set():
                    try:
                        atomic_write_text(self.path, payload)
                    except OSError as exc:
                        if not self._tolerable(exc):
                            failures.append(f"write failed: {exc}")
                            return
                        continue
                    writes[0] += 1
            finally:
                stop.set()

        def reader() -> None:
            # Read the bytes first, then parse them separately. The two
            # failures mean opposite things and must not be conflated:
            # failing to *open* the file is a Windows sharing collision with
            # the rename, which is a retry. Parsing bytes we did successfully
            # read is the invariant under test, and any failure there is a
            # genuine torn read.
            while not stop.is_set():
                try:
                    raw = self.path.read_bytes()
                except OSError as exc:
                    if not self._tolerable(exc):
                        failures.append(f"unexpected {type(exc).__name__}: {exc}")
                        return
                    continue

                try:
                    loaded = Calibration.from_dict(tomllib.loads(raw.decode("utf-8")))
                except Exception as exc:  # noqa: BLE001
                    failures.append(
                        f"torn read of {len(raw)} bytes: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    return

                reads[0] += 1
                if loaded.cell_width != self.calibration.cell_width:
                    failures.append("read a calibration with the wrong geometry")
                    return

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        stop.set()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), "a worker thread did not finish")

        self.assertEqual(failures, [])
        self.assertEqual(
            writes[0], self.REQUIRED_WRITES, "the writer did not finish its work"
        )
        self.assertGreater(reads[0], 5, "the reader barely ran; test proves little")

    def test_the_file_is_never_zero_length_mid_write(self) -> None:
        stop = threading.Event()
        empties = [0]
        samples = [0]

        def writer() -> None:
            payload = self.calibration.to_toml()
            while not stop.is_set():
                try:
                    atomic_write_text(self.path, payload)
                except OSError as exc:
                    if not self._tolerable(exc):
                        raise

        def watcher() -> None:
            while not stop.is_set():
                try:
                    size = self.path.stat().st_size
                except OSError:
                    continue
                samples[0] += 1
                if size == 0:
                    empties[0] += 1

        threads = [threading.Thread(target=writer), threading.Thread(target=watcher)]
        for thread in threads:
            thread.start()
        time.sleep(1.5)
        stop.set()
        for thread in threads:
            thread.join(timeout=30)

        self.assertGreater(samples[0], 10)
        self.assertEqual(empties[0], 0, "observed a zero-length calibration")

    def test_no_temp_files_survive_the_storm(self) -> None:
        for _ in range(50):
            self.calibration.save(self.path)
        leftovers = [p.name for p in self.path.parent.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])


class TestInterruptibleSleep(unittest.TestCase):
    """A ten-minute sleep must not mean a ten-minute shutdown."""

    def test_a_long_sleep_ends_as_soon_as_a_stop_is_requested(self) -> None:
        clock = SystemClock()
        started = time.monotonic()

        timer = threading.Timer(0.2, clock.request_stop)
        timer.start()
        self.addCleanup(timer.cancel)

        interrupted = clock.sleep(600)
        elapsed = time.monotonic() - started

        self.assertTrue(interrupted, "sleep did not report the interruption")
        # Event.wait returns within milliseconds of the set, so the bound is
        # tight on purpose. A looser one lets a "sleep in short chunks and
        # check afterwards" implementation pass -- which is exactly the shape
        # a mutant took, and it survived a ten-second bound.
        self.assertLess(
            elapsed, 2.0, f"took {elapsed:.2f}s to notice a stop request"
        )

    def test_an_uninterrupted_sleep_reports_no_stop(self) -> None:
        clock = SystemClock()
        self.assertFalse(clock.sleep(0.05))

    def test_a_sleep_that_is_not_interrupted_lasts_its_full_length(self) -> None:
        # The other side of the bound above: a sleep must not return early
        # when nothing asked it to.
        clock = SystemClock()
        started = time.monotonic()
        clock.sleep(0.4)
        self.assertGreaterEqual(time.monotonic() - started, 0.35)

    def test_a_stop_already_requested_returns_immediately(self) -> None:
        clock = SystemClock()
        clock.request_stop()
        started = time.monotonic()
        self.assertTrue(clock.sleep(600))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_stopping_is_visible_on_the_clock(self) -> None:
        clock = SystemClock()
        self.assertFalse(clock.stopping)
        clock.request_stop()
        self.assertTrue(clock.stopping)


class TestSignalShutdown(unittest.TestCase):
    """The actual signal wiring, in a real process."""

    def setUp(self) -> None:
        if os.name != "posix":
            self.skipTest(
                "SIGTERM delivery is POSIX-specific; on Windows "
                "Popen.terminate() calls TerminateProcess, which no handler "
                "can observe. The Linux container tier covers this branch, "
                "and TestInterruptibleSleep covers the mechanism itself on "
                "every platform."
            )

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

        self.config = self.dir / "boot-err-shim.conf"
        self.config.write_text(
            f"""
[state]
dir = "{self.dir}"
[target]
host = "127.0.0.1"
[ping]
interval = 600
threshold = 1
command = ["true", "{{host}}"]
[vnc]
host = "127.0.0.1"
port = 1
[detect]
text = "Please press 'Y' to continue."
[log]
syslog = "never"
""",
            encoding="utf-8",
        )

    def test_sigterm_during_a_long_sleep_exits_promptly(self) -> None:
        # ping.interval is ten minutes, so the daemon is asleep almost
        # immediately and stays that way.
        process = subprocess.Popen(
            [sys.executable, "-m", "boot_err_shim.cli", "run", "-c", str(self.config)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        )
        self.addCleanup(process.kill)

        # Give it long enough to reach the sleep.
        time.sleep(3)
        self.assertIsNone(process.poll(), "the daemon exited on its own")

        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.fail("SIGTERM did not stop the daemon within 30s")
        elapsed = time.monotonic() - started

        self.assertLess(
            elapsed, 15, f"took {elapsed:.1f}s to stop while sleeping for 600s"
        )

    def test_shutdown_is_clean_rather_than_a_traceback(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-m", "boot_err_shim.cli", "run", "-c", str(self.config)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        )
        self.addCleanup(process.kill)
        time.sleep(3)
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=30)

        text = stderr.decode("utf-8", "replace")
        self.assertNotIn("Traceback", text)
        self.assertIn("daemon.stop", text)

    def test_the_lock_is_released_after_a_signal(self) -> None:
        from boot_err_shim.config import load_config
        from boot_err_shim.lock import SingleInstanceLock

        process = subprocess.Popen(
            [sys.executable, "-m", "boot_err_shim.cli", "run", "-c", str(self.config)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        )
        self.addCleanup(process.kill)
        time.sleep(3)
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=30)

        # A lock left held would stop the supervisor restarting the daemon.
        lock = SingleInstanceLock(load_config(self.config).lock_path)
        lock.acquire()
        lock.release()


if __name__ == "__main__":
    unittest.main()
