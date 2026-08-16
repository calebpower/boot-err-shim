"""Tier 1: the escalation path, actually executed.

`notify_command` is how this program tells somebody that a controller is
failing repeatedly. It is the entire justification for the program being an
acceptable thing to run at all: it is a workaround for dying hardware, and the
argument that it is not merely hiding a fault rests on it being loud when the
fault recurs.

Until now the daemon tests injected a fake notifier, so the real subprocess
call had never run once. A documented feature whose implementation is never
executed is a feature nobody has checked works.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from boot_err_shim.daemon import Daemon, _run_notify_command
from boot_err_shim.history import InterventionHistory
from boot_err_shim.log import setup_logging
from tests.fakes import (
    FakeClock,
    FakeConsole,
    FakeConsoleFactory,
    RecordingFrameWriter,
    always_down,
    make_config,
    matching_detector,
)


class TestRunNotifyCommand(unittest.TestCase):
    """The real thing, with a real child process."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

    def test_it_actually_runs_the_command(self) -> None:
        marker = self.dir / "paged.txt"
        _run_notify_command(
            [
                sys.executable,
                "-c",
                f"open({str(marker)!r}, 'w').write('paged')",
            ]
        )
        self.assertTrue(marker.exists(), "the notify command did not run")
        self.assertEqual(marker.read_text(encoding="utf-8"), "paged")

    def test_arguments_are_passed_through(self) -> None:
        marker = self.dir / "args.txt"
        _run_notify_command(
            [
                sys.executable,
                "-c",
                f"import sys; open({str(marker)!r}, 'w').write(' '.join(sys.argv[1:]))",
                "--host",
                "10.0.0.50",
            ]
        )
        self.assertEqual(marker.read_text(encoding="utf-8"), "--host 10.0.0.50")

    def test_a_failing_command_does_not_raise(self) -> None:
        # The pager being down must not take the daemon with it.
        _run_notify_command([sys.executable, "-c", "import sys; sys.exit(3)"])

    def test_a_command_that_writes_to_stderr_does_not_raise(self) -> None:
        # Output is deliberately not captured: a pager's complaints should
        # reach the journal alongside everything else. The child's stderr is
        # redirected here only to keep the test run readable.
        devnull = os.open(os.devnull, os.O_WRONLY)
        saved = os.dup(2)
        try:
            os.dup2(devnull, 2)
            _run_notify_command(
                [sys.executable, "-c", "import sys; sys.stderr.write('boom')"]
            )
        finally:
            os.dup2(saved, 2)
            os.close(saved)
            os.close(devnull)

    def test_a_missing_command_raises_an_os_error(self) -> None:
        # Raised rather than swallowed here, because the daemon catches
        # OSError around the call and logs notify.failed. Swallowing it at
        # this level would make a typo in notify_command invisible.
        with self.assertRaises(OSError):
            _run_notify_command(["definitely-not-a-real-command-xyzzy"])

    def test_it_does_not_wait_forever(self) -> None:
        # A pager script that hangs would otherwise stall the watch loop.
        started = __import__("time").monotonic()
        with mock.patch("subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=30)
            with self.assertRaises(subprocess.TimeoutExpired):
                _run_notify_command(["anything"])
        self.assertLess(__import__("time").monotonic() - started, 10)

    def test_a_timeout_is_requested(self) -> None:
        with mock.patch("subprocess.run") as run:
            _run_notify_command(["x"])
        self.assertIn("timeout", run.call_args.kwargs)
        self.assertGreater(run.call_args.kwargs["timeout"], 0)


class TestNotifyThroughTheDaemon(unittest.TestCase):
    """End to end within the daemon, with a real command and no fake."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)
        setup_logging(stream=io.StringIO(), syslog="never")
        self.marker = self.dir / "paged.txt"

    def build(self, max_per_day: int = 2) -> Daemon:
        script = (
            f"import os; p = {str(self.marker)!r}; "
            "n = 0\n"
            "try:\n"
            "    n = int(open(p).read())\n"
            "except Exception:\n"
            "    pass\n"
            "open(p, 'w').write(str(n + 1))\n"
        )
        command = (
            "["
            + ", ".join(repr(part) for part in [sys.executable, "-c", script])
            + "]"
        )
        config = make_config(
            f"[recovery]\nmax_per_day = {max_per_day}\nnotify_command = {command}\n"
        )
        self.clock = FakeClock()
        return Daemon(
            config,
            probe=always_down,
            console_factory=FakeConsoleFactory(console=FakeConsole()),
            detector=matching_detector,
            clock=self.clock,
            history=InterventionHistory.load(self.dir / "history.json"),
            calibrated=True,
            frame_writer=RecordingFrameWriter(),
        )

    def fire(self, daemon: Daemon, times: int) -> None:
        for _ in range(times):
            daemon.failures = daemon.config.ping.threshold - 1
            daemon.step()

    def count(self) -> int:
        if not self.marker.exists():
            return 0
        return int(self.marker.read_text(encoding="utf-8"))

    def test_at_the_limit_nobody_is_paged(self) -> None:
        self.fire(self.build(max_per_day=2), 2)
        self.assertEqual(self.count(), 0)

    def test_past_the_limit_the_real_command_runs(self) -> None:
        # The whole escalation story, with no fake standing in for it.
        self.fire(self.build(max_per_day=2), 3)
        self.assertEqual(self.count(), 1)

    def test_it_keeps_paging_while_the_controller_keeps_failing(self) -> None:
        # A controller that has to be rescued repeatedly should not go quiet
        # after the first warning.
        self.fire(self.build(max_per_day=2), 5)
        self.assertEqual(self.count(), 3)

    def test_a_missing_pager_is_logged_and_survived(self) -> None:
        stream = io.StringIO()
        setup_logging(stream=stream, syslog="never")

        config = make_config(
            "[recovery]\nmax_per_day = 1\n"
            'notify_command = ["definitely-not-a-real-command-xyzzy"]\n'
        )
        console = FakeConsole()
        daemon = Daemon(
            config,
            probe=always_down,
            console_factory=FakeConsoleFactory(console=console),
            detector=matching_detector,
            clock=FakeClock(),
            history=InterventionHistory.load(self.dir / "history.json"),
            calibrated=True,
            frame_writer=RecordingFrameWriter(),
        )
        for _ in range(2):
            daemon.failures = config.ping.threshold - 1
            daemon.step()

        # The rescue still happened; only the paging failed.
        self.assertGreaterEqual(len(console.keys_sent), 2)
        self.assertIn("notify.failed", stream.getvalue())


class TestUnwritableLogFile(unittest.TestCase):
    """An explicitly configured file is not optional.

    Syslog degrades quietly, on purpose: losing that copy is a nuisance. A
    file the operator named is different -- carrying on without it leaves
    them tailing something that never gets written. And under rc(8) the
    daemon's stderr goes to /dev/null, so an OSError here would vanish
    entirely, taking the daemon with it and explaining nothing.
    """

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)
        self.addCleanup(self._reset)

    def _reset(self) -> None:
        import logging

        from boot_err_shim.log import LOGGER_NAME

        logger = logging.getLogger(LOGGER_NAME)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    def test_an_unopenable_file_is_a_typed_config_error(self) -> None:
        from boot_err_shim.errors import ConfigError
        from boot_err_shim.log import setup_logging

        # A directory where the file should be: open() fails on every
        # platform, without needing to manufacture a permission denial.
        target = self.dir / "logs"
        target.mkdir()

        with self.assertRaises(ConfigError) as caught:
            setup_logging(stream=io.StringIO(), syslog="never", file=target)

        message = str(caught.exception)
        self.assertIn(str(target), message)
        self.assertIn("chown", message, "the message should say how to fix it")

    def test_a_writable_file_still_works(self) -> None:
        import logging

        from boot_err_shim.log import event, get_logger, setup_logging

        target = self.dir / "sub" / "shim.log"
        setup_logging(stream=io.StringIO(), syslog="never", file=target)
        event(get_logger(), logging.WARNING, "key.pressed", key="Y")
        for handler in logging.getLogger("boot_err_shim").handlers:
            handler.flush()
        self.assertIn("key.pressed", target.read_text(encoding="utf-8"))


class TestSyslogHandler(unittest.TestCase):
    """The other sink, which only exists on the deployment platforms."""

    def test_a_missing_socket_yields_no_handler(self) -> None:
        from boot_err_shim.log import _syslog_handler

        self.assertIsNone(_syslog_handler(Path("/definitely/not/here")))

    def test_no_socket_configured_yields_no_handler(self) -> None:
        from boot_err_shim.log import _syslog_handler

        self.assertIsNone(_syslog_handler(None))

    def test_a_path_that_is_not_a_socket_does_not_break_setup(self) -> None:
        """The contract is "never fail", not "always return None".

        Asserted at the level that actually matters, because the layer below
        is not consistent: on Windows, constructing the handler raises
        AttributeError for the missing AF_UNIX; on Linux the standard library
        accepts a regular file and defers the connection. Requiring None
        would be asserting a detail of whichever platform the test happened
        to run on.

        What must hold everywhere is that logging setup does not stop the
        daemon starting, and that stderr still works afterwards.
        """
        import logging

        from boot_err_shim.log import LOGGER_NAME, event, get_logger, setup_logging

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notasocket"
            path.write_text("", encoding="utf-8")

            stream = io.StringIO()
            setup_logging(stream=stream, syslog="always", syslog_socket=path)
            self.addCleanup(
                lambda: [
                    (logging.getLogger(LOGGER_NAME).removeHandler(h), h.close())
                    for h in list(logging.getLogger(LOGGER_NAME).handlers)
                ]
            )

            event(get_logger(), logging.WARNING, "key.pressed", key="Y")
            self.assertIn("key.pressed", stream.getvalue())

    def test_a_real_socket_produces_a_working_handler(self) -> None:
        import logging
        import socket

        if os.name != "posix":
            self.skipTest(
                "AF_UNIX datagram sockets are POSIX-only; the container tier "
                "covers this branch, and the fallbacks above are checked on "
                "every platform"
            )

        from boot_err_shim.log import _syslog_handler

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            server.bind(str(path))
            server.settimeout(5)
            self.addCleanup(server.close)

            handler = _syslog_handler(path)
            self.assertIsNotNone(handler)
            self.addCleanup(handler.close)

            logger = logging.getLogger("syslog_probe")
            logger.handlers = [handler]
            logger.setLevel(logging.INFO)
            logger.propagate = False

            from boot_err_shim.log import event

            event(logger, logging.WARNING, "key.pressed", key="Y")

            received = server.recv(4096).decode("utf-8", "replace")
            self.assertIn("key.pressed", received)
            self.assertIn("key=Y", received)


if __name__ == "__main__":
    unittest.main()
