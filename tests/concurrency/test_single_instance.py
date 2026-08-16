"""Tier 8: two daemons, one console.

The oracle throughout is the console's own record of what it received, not
what either process claims to have done.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO / "tools"))

from boot_err_shim.calibrate import analyse  # noqa: E402
from boot_err_shim.errors import LockError  # noqa: E402
from boot_err_shim.lock import SingleInstanceLock  # noqa: E402
from fake_vnc_server import FakeVNCServer  # noqa: E402
from render_frame import THE_MESSAGE, render  # noqa: E402

#: TEST-NET-1: reserved for documentation, so reliably unroutable. On Linux
#: and FreeBSD ping reports it unreachable; on Windows ping exits 0 for
#: "destination net unreachable", which is why the subprocess tests below
#: force the failure with an unrunnable ping command instead of relying on it.
UNREACHABLE = "192.0.2.1"

CONFIG = """
[state]
dir = "{state}"

[target]
host = "{host}"

[ping]
threshold      = 1
interval       = 1
retry_interval = 1
timeout        = 5
command        = {ping_command}

[vnc]
host     = "127.0.0.1"
port     = {port}
password = "secret12"

[detect]
calibration = "{calibration}"
text = \"\"\"
Disabling writes to flash as the flash part has gone bad.
Please contact technical support to resolve this issue.
Please press 'Y' to continue.
\"\"\"

[recovery]
interval       = 1
post_fix_sleep = 1

[log]
screenshot_dir = "{snapshots}"
syslog         = "never"
"""

def toml_path(path: Path) -> str:
    return str(path).replace("\\", "/")


#: A ping command that cannot succeed, so "the host is down" is deterministic
#: rather than depending on how the local ping reports an unroutable address.
#:
#: Spelled with sys.executable rather than "python": Ubuntu 26.04 ships only
#: python3, so a hardcoded "python" makes the probe fail to execute at all,
#: which is a different code path (ProbeError) from the one under test. Note
#: `{host}` survives into the config verbatim -- str.format does not recurse
#: into substituted values -- which config validation requires.
ALWAYS_DOWN = (
    f'["{toml_path(Path(sys.executable))}", "-c", '
    '"import sys; sys.exit(1)", "{host}"]'
)


class ConsoleFixture(unittest.TestCase):
    """A fake iDRAC showing the error, plus a config and a calibration."""

    def setUp(self) -> None:
        frame = render(THE_MESSAGE)
        self.server = FakeVNCServer(
            width=frame.width,
            height=frame.height,
            pixels=frame.data,
            password="secret12",
        )
        self.server.start()
        self.addCleanup(self.server.stop)

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

        calibration = analyse(frame, THE_MESSAGE)
        self.calibration_path = self.dir / "calibration.toml"
        calibration.save(self.calibration_path)

        self.config_path = self.dir / "boot-err-shim.conf"
        self.config_path.write_text(
            CONFIG.format(
                state=toml_path(self.dir),
                host=UNREACHABLE,
                ping_command=ALWAYS_DOWN,
                port=self.server.port,
                calibration=toml_path(self.calibration_path),
                snapshots=toml_path(self.dir / "snapshots"),
            ),
            encoding="utf-8",
        )
        if os.name == "posix":
            self.config_path.chmod(0o600)

        # The lock is named after the console, not the machine: see
        # Config.lock_path. Ask the config where it will be rather than
        # guessing, so this test cannot drift out of agreement with the
        # daemon the way it did when it hard-coded a filename.
        from boot_err_shim.config import load_config

        self.lock_path = load_config(self.config_path).lock_path

    def run_daemon(self, *extra: str, timeout: int = 120):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "boot_err_shim.cli",
                "run",
                "--once",
                "-c",
                str(self.config_path),
                *extra,
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(REPO / "src")},
            timeout=timeout,
            check=False,
        )


class TestTwoDaemonsOneConsole(ConsoleFixture):
    def test_one_daemon_presses_the_key(self) -> None:
        # The control: without contention the key is sent, so a later
        # assertion that it was *not* sent means something.
        result = self.run_daemon()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.server.keys), 2, "expected press and release")

    def test_a_second_daemon_cannot_press_while_the_first_holds_the_lock(self) -> None:
        """The invariant, asserted on the console rather than on exit codes.

        The lock is held here rather than by racing two subprocesses: a race
        would pass or fail on timing, and a test that only sometimes exercises
        the thing it names is worse than no test.
        """
        with SingleInstanceLock(self.lock_path):
            before = len(self.server.keys)
            result = self.run_daemon()

            self.assertNotEqual(result.returncode, 0, "the second instance ran anyway")
            self.assertEqual(
                len(self.server.keys),
                before,
                "a second daemon pressed the key while another held the lock",
            )
            self.assertIn("already running", result.stderr)

    def test_the_lock_is_released_so_the_next_run_works(self) -> None:
        with SingleInstanceLock(self.lock_path):
            self.run_daemon()
        before = len(self.server.keys)
        result = self.run_daemon()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.server.keys), before + 2)

    def test_the_refusal_names_the_holding_process(self) -> None:
        with SingleInstanceLock(self.lock_path):
            result = self.run_daemon()
        self.assertIn(str(os.getpid()), result.stderr)


class TestLockContention(unittest.TestCase):
    """Threads racing for the same lock, released at a barrier."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "contended.lock"

    def test_exactly_one_of_many_contenders_wins(self) -> None:
        contenders = 8
        barrier = threading.Barrier(contenders)
        acquired: list[int] = []
        refused: list[int] = []
        guard = threading.Lock()

        def attempt(index: int) -> None:
            lock = SingleInstanceLock(self.path)
            barrier.wait()
            try:
                lock.acquire()
            except LockError:
                with guard:
                    refused.append(index)
                return
            with guard:
                acquired.append(index)
            # Hold long enough that the others are certainly contending.
            time.sleep(0.2)
            lock.release()

        threads = [
            threading.Thread(target=attempt, args=(index,))
            for index in range(contenders)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(acquired), 1, f"acquired={acquired} refused={refused}")
        self.assertEqual(len(refused), contenders - 1)

    def test_the_winner_can_be_succeeded_by_another(self) -> None:
        first = SingleInstanceLock(self.path)
        first.acquire()
        second = SingleInstanceLock(self.path)
        with self.assertRaises(LockError):
            second.acquire()
        first.release()
        second.acquire()
        second.release()


if __name__ == "__main__":
    unittest.main()
