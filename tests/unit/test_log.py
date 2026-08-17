"""Tier 1: structured log formatting.

The line format is an interface. Tier 2 asserts golden output against it and
anything alerting on these lines depends on it, so the rendering rules get
pinned here rather than left to whatever str() happens to do.
"""

from __future__ import annotations

import io
import logging
import os
import unittest
from unittest import mock

from boot_err_shim.log import (
    LOGGER_NAME,
    ShimFormatter,
    event,
    get_logger,
    setup_logging,
    under_journald,
)


def render(name: str, level: int = logging.INFO, **fields) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ShimFormatter(with_time=False))
    logger = logging.getLogger("test_log_render")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    event(logger, level, name, **fields)
    return stream.getvalue().strip()


class TestFormatting(unittest.TestCase):
    def test_event_with_no_fields(self) -> None:
        self.assertEqual(render("ping.up"), "INFO ping.up")

    def test_fields_render_as_key_value(self) -> None:
        self.assertEqual(
            render("ping.down", host="10.0.0.50", failures=1),
            "INFO ping.down host=10.0.0.50 failures=1",
        )

    def test_field_order_is_preserved(self) -> None:
        # Golden-file comparisons depend on this; dict ordering is the
        # guarantee that makes it hold.
        self.assertEqual(
            render("e", z=1, a=2, m=3),
            "INFO e z=1 a=2 m=3",
        )

    def test_level_name_is_included(self) -> None:
        self.assertEqual(render("e", logging.WARNING), "WARNING e")
        self.assertEqual(render("e", logging.ERROR), "ERROR e")

    def test_none_renders_as_a_dash(self) -> None:
        self.assertEqual(render("e", reason=None), "INFO e reason=-")

    def test_booleans_render_lowercase(self) -> None:
        self.assertEqual(render("e", tls=True, acted=False), "INFO e tls=true acted=false")

    def test_floats_do_not_leak_binary_noise(self) -> None:
        # 0.1 + 0.02 would otherwise render as 0.12000000000000001 and break
        # a golden file for no reason.
        self.assertEqual(render("e", tolerance=0.1 + 0.02), "INFO e tolerance=0.12")

    def test_small_and_large_floats(self) -> None:
        self.assertEqual(render("e", v=0.02), "INFO e v=0.02")
        self.assertEqual(render("e", v=1234.5), "INFO e v=1234.5")

    def test_values_with_spaces_are_quoted(self) -> None:
        self.assertEqual(
            render("e", detail="host is down"), 'INFO e detail="host is down"'
        )

    def test_embedded_quotes_are_escaped(self) -> None:
        self.assertEqual(render("e", t='say "hi"'), 'INFO e t="say \\"hi\\""')

    def test_backslashes_are_escaped(self) -> None:
        self.assertEqual(render("e", p="a\\b c"), 'INFO e p="a\\\\b c"')

    def test_empty_string_is_visibly_empty(self) -> None:
        # Bare `key=` is ambiguous with a missing value.
        self.assertEqual(render("e", v=""), 'INFO e v=""')

    def test_newline_in_a_value_is_quoted_not_broken_across_lines(self) -> None:
        self.assertNotIn("\n", render("e", v="a\nb"))

    def test_paths_render_as_strings(self) -> None:
        from pathlib import PurePosixPath

        self.assertEqual(
            render("e", path=PurePosixPath("/var/lib/x")), "INFO e path=/var/lib/x"
        )


class TestNoticeLevel(unittest.TestCase):
    """Lifecycle events must clear syslog's notice threshold.

    FreeBSD's stock /etc/syslog.conf routes *.notice and above to
    /var/log/messages and drops the rest. With "I have started" at INFO, a
    healthy daemon was completely silent -- so silence meant either running
    perfectly or dead, with no way to tell from the log.
    """

    def test_notice_sits_between_info_and_warning(self) -> None:
        from boot_err_shim.log import NOTICE

        self.assertGreater(NOTICE, logging.INFO)
        self.assertLess(NOTICE, logging.WARNING)

    def test_it_renders_with_its_own_name(self) -> None:
        from boot_err_shim.log import NOTICE

        self.assertEqual(render("daemon.start", NOTICE), "NOTICE daemon.start")

    def test_it_is_visible_at_the_default_level(self) -> None:
        from boot_err_shim.log import NOTICE

        stream = io.StringIO()
        setup_logging(stream=stream, syslog="never", level="INFO")
        event(get_logger(), NOTICE, "daemon.start")
        self.assertIn("daemon.start", stream.getvalue())

    def test_the_syslog_handler_maps_it_to_notice(self) -> None:
        # SysLogHandler keys its priority map on the level name and knows
        # nothing about one we invented; without the mapping it would fall
        # back to warning, which is the wrong severity for "I started".
        import tempfile
        from pathlib import Path

        from boot_err_shim.log import _syslog_handler

        if os.name != "posix":
            self.skipTest(
                "needs a real AF_UNIX socket; the Linux container covers it, "
                "and the mapping itself is asserted below on every platform"
            )

        import socket

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log"
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            server.bind(str(path))
            self.addCleanup(server.close)

            handler = _syslog_handler(path)
            self.addCleanup(handler.close)
            self.assertEqual(handler.priority_map["NOTICE"], "notice")

    def test_the_mapping_is_declared_for_the_handler_to_find(self) -> None:
        self.assertEqual(logging.getLevelName(25), "NOTICE")


class TestTimestamp(unittest.TestCase):
    def test_timestamp_can_be_injected_for_deterministic_output(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(
            ShimFormatter(with_time=True, clock=lambda: "2026-08-16T14:02:11Z")
        )
        logger = logging.getLogger("test_log_clock")
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        event(logger, logging.INFO, "ping.up", host="h")
        self.assertEqual(
            stream.getvalue().strip(), "2026-08-16T14:02:11Z INFO ping.up host=h"
        )

    def test_real_timestamp_looks_like_an_instant(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(ShimFormatter(with_time=True))
        logger = logging.getLogger("test_log_realclock")
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        event(logger, logging.INFO, "e")
        first = stream.getvalue().split()[0]
        self.assertRegex(first, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TestExceptionRendering(unittest.TestCase):
    def test_traceback_is_appended_not_inlined(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(ShimFormatter(with_time=False))
        logger = logging.getLogger("test_log_exc")
        logger.handlers = [handler]
        logger.propagate = False
        try:
            raise ValueError("boom")
        except ValueError:
            logger.error("vnc.failed", exc_info=True, extra={"fields": {"host": "h"}})
        output = stream.getvalue()
        self.assertTrue(output.startswith("ERROR vnc.failed host=h\n"))
        self.assertIn("ValueError: boom", output)


class TestSetupLogging(unittest.TestCase):
    def tearDown(self) -> None:
        self._close_handlers()

    def _close_handlers(self) -> None:
        """Detach every sink.

        Called explicitly, not only from tearDown, by tests that log into a
        TemporaryDirectory: the rotating handler holds the file open, and on
        Windows an open file cannot be deleted, so cleanup raises
        PermissionError while the assertions themselves were fine.
        """
        logger = logging.getLogger(LOGGER_NAME)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    def test_returns_the_package_logger(self) -> None:
        self.assertIs(setup_logging(stream=io.StringIO()), logging.getLogger(LOGGER_NAME))

    def test_child_loggers_share_the_configuration(self) -> None:
        stream = io.StringIO()
        setup_logging(stream=stream, syslog="never")
        event(get_logger("daemon"), logging.INFO, "x.y", a=1)
        self.assertIn("x.y a=1", stream.getvalue())

    def test_reload_does_not_accumulate_handlers(self) -> None:
        # SIGHUP calls this again; without the teardown every event would be
        # logged once more per reload.
        for _ in range(4):
            setup_logging(stream=io.StringIO(), syslog="never")
        self.assertEqual(len(logging.getLogger(LOGGER_NAME).handlers), 1)

    def test_level_is_applied(self) -> None:
        stream = io.StringIO()
        setup_logging(level="WARNING", stream=stream, syslog="never")
        logger = get_logger()
        event(logger, logging.INFO, "quiet")
        event(logger, logging.WARNING, "loud")
        self.assertNotIn("quiet", stream.getvalue())
        self.assertIn("loud", stream.getvalue())

    def test_never_means_no_syslog_handler_even_if_the_socket_exists(self) -> None:
        with mock.patch("boot_err_shim.log._syslog_handler") as handler:
            setup_logging(stream=io.StringIO(), syslog="never")
        handler.assert_not_called()

    def test_missing_syslog_socket_is_not_fatal(self) -> None:
        from pathlib import Path

        setup_logging(
            stream=io.StringIO(),
            syslog="always",
            syslog_socket=Path("/definitely/not/here"),
        )
        self.assertEqual(len(logging.getLogger(LOGGER_NAME).handlers), 1)

    def test_under_journald_suppresses_our_timestamp(self) -> None:
        # journald stamps every line itself; ours would be duplicated noise.
        stream = io.StringIO()
        with mock.patch.dict("os.environ", {"JOURNAL_STREAM": "8:12345"}):
            self.assertTrue(under_journald())
            setup_logging(stream=stream, syslog="never")
            event(get_logger(), logging.INFO, "e")
        self.assertEqual(stream.getvalue().strip(), "INFO e")

    def test_without_journald_we_stamp_our_own(self) -> None:
        stream = io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(under_journald())
            setup_logging(stream=stream, syslog="never")
            event(get_logger(), logging.INFO, "e")
        self.assertRegex(stream.getvalue().strip(), r"^\d{4}-\d{2}-\d{2}T")

    def test_stderr_is_dropped_when_a_supervisor_would_duplicate_it(self) -> None:
        """Off a terminal, with a real sink configured, stderr is redundant.

        This is what lets the rc script capture daemon(8)'s output with -o
        instead of throwing it away with -f. Without it the capture file
        receives every event twice over and grows alongside the log; with it,
        the file holds startup failures only.
        """
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shim.log"
            fake_stderr = io.StringIO()  # StringIO.isatty() is False
            with (
                mock.patch.dict("os.environ", {}, clear=True),
                mock.patch("sys.stderr", fake_stderr),
            ):
                setup_logging(syslog="never", file=path)
                event(get_logger(), logging.INFO, "e")

            self._close_handlers()
            self.assertEqual(fake_stderr.getvalue(), "")
            self.assertIn("e", path.read_text(encoding="utf-8"))

    def test_stderr_survives_when_it_is_the_only_sink(self) -> None:
        # Dropping it here would log precisely nowhere.
        fake_stderr = io.StringIO()
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("sys.stderr", fake_stderr),
        ):
            setup_logging(syslog="never")
            event(get_logger(), logging.INFO, "e")
        self.assertIn("e", fake_stderr.getvalue())

    def test_stderr_survives_under_journald(self) -> None:
        # Here stderr *is* the sink, even though it is not a terminal and a
        # file is configured as well.
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            fake_stderr = io.StringIO()
            with (
                mock.patch.dict("os.environ", {"JOURNAL_STREAM": "8:12345"}),
                mock.patch("sys.stderr", fake_stderr),
            ):
                setup_logging(syslog="never", file=Path(tmp) / "shim.log")
                event(get_logger(), logging.INFO, "e")
            self._close_handlers()
            self.assertIn("e", fake_stderr.getvalue())

    def test_stderr_survives_on_a_terminal(self) -> None:
        # Somebody is watching it, and they asked for the output.
        import tempfile
        from pathlib import Path

        class Tty(io.StringIO):
            def isatty(self) -> bool:
                return True

        with tempfile.TemporaryDirectory() as tmp:
            fake_stderr = Tty()
            with (
                mock.patch.dict("os.environ", {}, clear=True),
                mock.patch("sys.stderr", fake_stderr),
            ):
                setup_logging(syslog="never", file=Path(tmp) / "shim.log")
                event(get_logger(), logging.INFO, "e")
            self._close_handlers()
            self.assertIn("e", fake_stderr.getvalue())

    def test_an_explicit_stream_is_always_kept(self) -> None:
        # Callers that pass a stream -- the test suite, mainly -- are asking
        # for it by name, and the supervisor reasoning does not apply.
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            stream = io.StringIO()
            with mock.patch.dict("os.environ", {}, clear=True):
                setup_logging(stream=stream, syslog="never", file=Path(tmp) / "shim.log")
                event(get_logger(), logging.INFO, "e")
            self._close_handlers()
            self.assertIn("e", stream.getvalue())

    def test_a_stream_that_cannot_answer_isatty_is_not_fatal(self) -> None:
        closed = io.StringIO()
        closed.close()  # isatty() on a closed StringIO raises ValueError
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("sys.stderr", closed),
        ):
            setup_logging(syslog="never")
        self.assertEqual(len(logging.getLogger(LOGGER_NAME).handlers), 1)

    def test_file_handler_is_added_and_writes(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "shim.log"
            setup_logging(stream=io.StringIO(), syslog="never", file=path)
            event(get_logger(), logging.INFO, "written", a=1)
            logger = logging.getLogger(LOGGER_NAME)
            for handler in list(logger.handlers):
                handler.flush()
            content = path.read_text(encoding="utf-8")
            # Close before the temp dir is removed; Windows will not unlink a
            # file that still has an open handle.
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
        self.assertIn("written a=1", content)


if __name__ == "__main__":
    unittest.main()
