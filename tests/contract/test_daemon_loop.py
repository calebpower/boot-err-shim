"""Tier 4: the executor, driven end to end with no I/O.

The decision functions are pure and tested as tables elsewhere. This file
covers the part that carries decisions out: does the counter advance, does the
console get closed, does the frame get written even when nothing matched, does
a stop request actually stop the loop.
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path

from boot_err_shim.daemon import Action, Daemon
from boot_err_shim.errors import ProbeError, ProtocolError
from boot_err_shim.history import InterventionHistory
from boot_err_shim.log import setup_logging
from tests.fakes import (
    FakeClock,
    FakeConsole,
    FakeConsoleFactory,
    RecordingFrameWriter,
    RecordingNotifier,
    ScriptedProbe,
    always_down,
    always_up,
    make_config,
    matching_detector,
    non_matching_detector,
    refusing_factory,
)

KEYSYM_Y = 0x59


class DaemonTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)
        # Silence the daemon's own logging into a throwaway stream.
        import io

        setup_logging(stream=io.StringIO(), syslog="never")

    def build(
        self,
        *,
        probe=always_down,
        factory=None,
        detector=matching_detector,
        calibrated: bool = True,
        no_act: bool = False,
        overlay: str = "",
        clock: FakeClock | None = None,
        frame_writer=None,
        notifier=None,
    ) -> Daemon:
        self.clock = clock or FakeClock()
        self.factory = factory if factory is not None else FakeConsoleFactory()
        self.writer = frame_writer if frame_writer is not None else RecordingFrameWriter()
        self.notifier = notifier if notifier is not None else RecordingNotifier()
        self.history = InterventionHistory.load(self.dir / "history.json")
        return Daemon(
            make_config(overlay),
            probe=probe,
            console_factory=self.factory,
            detector=detector,
            clock=self.clock,
            history=self.history,
            calibrated=calibrated,
            no_act=no_act,
            frame_writer=self.writer,
            notifier=self.notifier,
        )


class TestHealthyHost(DaemonTest):
    def test_never_touches_the_console(self) -> None:
        daemon = self.build(probe=always_up)
        for _ in range(5):
            daemon.step()
        self.assertEqual(self.factory.attempts, 0)

    def test_sleeps_the_long_interval(self) -> None:
        daemon = self.build(probe=always_up)
        daemon.step()
        self.assertEqual(self.clock.sleeps, [120])

    def test_counter_stays_at_zero(self) -> None:
        daemon = self.build(probe=always_up)
        for _ in range(3):
            daemon.step()
        self.assertEqual(daemon.failures, 0)

    def test_no_frames_written(self) -> None:
        daemon = self.build(probe=always_up)
        daemon.step()
        self.assertEqual(self.writer.written, [])


class TestFailureAccumulation(DaemonTest):
    def test_console_is_untouched_below_the_threshold(self) -> None:
        daemon = self.build(probe=always_down)
        daemon.step()
        daemon.step()
        self.assertEqual(self.factory.attempts, 0)
        self.assertEqual(daemon.failures, 2)

    def test_third_failure_reaches_the_console(self) -> None:
        daemon = self.build(probe=always_down)
        for _ in range(3):
            decision = daemon.step()
        self.assertEqual(self.factory.attempts, 1)
        self.assertIs(decision.action, Action.PRESS_KEY)

    def test_the_retry_interval_is_used_while_below_threshold(self) -> None:
        daemon = self.build(probe=always_down)
        daemon.step()
        daemon.step()
        self.assertEqual(self.clock.sleeps, [90, 90])

    def test_a_single_success_resets_the_count(self) -> None:
        probe = ScriptedProbe([False, False, True, False])
        daemon = self.build(probe=probe)
        daemon.step()
        daemon.step()
        self.assertEqual(daemon.failures, 2)
        daemon.step()
        self.assertEqual(daemon.failures, 0)
        daemon.step()
        self.assertEqual(daemon.failures, 1)
        self.assertEqual(self.factory.attempts, 0)

    def test_a_flapping_host_never_reaches_the_console(self) -> None:
        # Two down, one up, repeatedly: the counter must never reach three.
        probe = ScriptedProbe([False, False, True] * 4)
        daemon = self.build(probe=probe)
        for _ in range(12):
            daemon.step()
        self.assertEqual(self.factory.attempts, 0)


class TestSuccessfulRecovery(DaemonTest):
    def press(self, **kwargs) -> Daemon:
        console = FakeConsole()
        daemon = self.build(factory=FakeConsoleFactory(console=console), **kwargs)
        self.console = console
        for _ in range(3):
            daemon.step()
        return daemon

    def test_key_is_sent(self) -> None:
        self.press()
        self.assertEqual(self.console.keys_sent, [KEYSYM_Y])

    def test_console_is_closed_afterwards(self) -> None:
        self.press()
        self.assertTrue(self.console.closed)

    def test_failure_counter_is_cleared(self) -> None:
        daemon = self.press()
        self.assertEqual(daemon.failures, 0)

    def test_the_long_post_fix_sleep_follows(self) -> None:
        self.press()
        self.assertEqual(self.clock.sleeps, [90, 90, 600])

    def test_the_frame_is_written_and_labelled(self) -> None:
        self.press()
        self.assertEqual(len(self.writer.written), 1)
        self.assertEqual(self.writer.written[0][1], "match")

    def test_the_intervention_is_recorded(self) -> None:
        self.press()
        self.assertEqual(len(self.history.timestamps), 1)

    def test_the_history_survives_a_restart(self) -> None:
        self.press()
        reloaded = InterventionHistory.load(self.dir / "history.json")
        self.assertEqual(len(reloaded.timestamps), 1)


class TestRefusals(DaemonTest):
    def run_recovery(self, **kwargs) -> Daemon:
        daemon = self.build(**kwargs)
        for _ in range(3):
            self.decision = daemon.step()
        return daemon

    def test_connect_failure_presses_nothing_and_waits(self) -> None:
        self.run_recovery(factory=refusing_factory())
        self.assertEqual(self.decision.reason, "connect.failed")
        self.assertEqual(self.clock.sleeps, [90, 90, 60])

    def test_connect_failure_writes_no_frame(self) -> None:
        # There is no frame; writing an empty one would pollute the ring
        # buffer with useless entries during an outage.
        self.run_recovery(factory=refusing_factory())
        self.assertEqual(self.writer.written, [])

    def test_no_match_presses_nothing(self) -> None:
        console = FakeConsole()
        self.run_recovery(
            factory=FakeConsoleFactory(console=console), detector=non_matching_detector
        )
        self.assertEqual(console.keys_sent, [])
        self.assertEqual(self.decision.reason, "no.match")

    def test_no_match_still_writes_the_frame(self) -> None:
        # The whole point of the ring buffer: a false negative is
        # undiagnosable without the frame that produced it.
        self.run_recovery(detector=non_matching_detector)
        self.assertEqual(len(self.writer.written), 1)
        self.assertEqual(self.writer.written[0][1], "no-match")

    def test_no_match_closes_the_console(self) -> None:
        console = FakeConsole()
        self.run_recovery(
            factory=FakeConsoleFactory(console=console), detector=non_matching_detector
        )
        self.assertTrue(console.closed)

    def test_uncalibrated_refuses_even_on_a_match(self) -> None:
        console = FakeConsole()
        self.run_recovery(
            factory=FakeConsoleFactory(console=console), calibrated=False
        )
        self.assertEqual(console.keys_sent, [])
        self.assertEqual(self.decision.reason, "not.calibrated")

    def test_no_act_refuses_even_on_a_match(self) -> None:
        console = FakeConsole()
        self.run_recovery(factory=FakeConsoleFactory(console=console), no_act=True)
        self.assertEqual(console.keys_sent, [])
        self.assertEqual(self.decision.reason, "match.no_act")

    def test_no_act_does_not_record_an_intervention(self) -> None:
        self.run_recovery(no_act=True)
        self.assertEqual(self.history.timestamps, [])

    def test_a_refusal_leaves_the_counter_alone(self) -> None:
        # Clearing it would drop us out of recovery while the host is stuck.
        daemon = self.run_recovery(detector=non_matching_detector)
        self.assertEqual(daemon.failures, 3)

    def test_it_stays_in_recovery_on_later_cycles(self) -> None:
        daemon = self.build(detector=non_matching_detector)
        for _ in range(6):
            daemon.step()
        # Three cycles below threshold... no: threshold is reached on the
        # third, and every cycle after that also attempts recovery.
        self.assertEqual(self.factory.attempts, 4)
        self.assertEqual(daemon.failures, 6)


class TestCaptureFailure(DaemonTest):
    """Connected, then the transport died before we saw anything."""

    def setUp(self) -> None:
        super().setUp()
        self.console = FakeConsole(capture_error=ProtocolError("reset mid-frame"))
        self.daemon = self.build(factory=FakeConsoleFactory(console=self.console))
        for _ in range(3):
            self.decision = self.daemon.step()

    def test_it_is_treated_as_a_failed_connection(self) -> None:
        # There is nothing on screen we can trust, so it must not fall through
        # to "no match" -- which would be a claim about the screen contents.
        self.assertEqual(self.decision.reason, "connect.failed")

    def test_no_key_is_sent(self) -> None:
        self.assertEqual(self.console.keys_sent, [])

    def test_the_console_is_still_closed(self) -> None:
        self.assertTrue(self.console.closed)

    def test_no_frame_is_written(self) -> None:
        self.assertEqual(self.writer.written, [])


class TestFrameWriterFailures(DaemonTest):
    def test_a_full_disk_does_not_stop_the_keypress(self) -> None:
        # The screenshot is diagnostics. Losing it must not cost us the fix.
        console = FakeConsole()
        writer = RecordingFrameWriter(fail_with=OSError("No space left on device"))
        daemon = self.build(
            factory=FakeConsoleFactory(console=console), frame_writer=writer
        )
        for _ in range(3):
            decision = daemon.step()
        self.assertIs(decision.action, Action.PRESS_KEY)
        self.assertEqual(console.keys_sent, [KEYSYM_Y])

    def test_no_writer_configured_is_fine(self) -> None:
        daemon = self.build(frame_writer=None)
        # frame_writer=None in build() substitutes a recorder; construct the
        # daemon directly to get a genuinely absent writer.
        daemon.frame_writer = None
        for _ in range(3):
            decision = daemon.step()
        self.assertIs(decision.action, Action.PRESS_KEY)


class TestCloseFailure(DaemonTest):
    def test_a_failing_close_does_not_break_the_cycle(self) -> None:
        console = FakeConsole(close_error=OSError("already gone"))
        daemon = self.build(factory=FakeConsoleFactory(console=console))
        for _ in range(3):
            decision = daemon.step()
        self.assertIs(decision.action, Action.PRESS_KEY)


class TestInterventionWarnings(DaemonTest):
    def press_n_times(self, n: int, overlay: str = "") -> Daemon:
        daemon = self.build(overlay=overlay)
        for _ in range(n):
            daemon.failures = daemon.config.ping.threshold - 1
            daemon.step()
        return daemon

    def test_each_press_is_recorded(self) -> None:
        self.press_n_times(3)
        self.assertEqual(len(self.history.timestamps), 3)

    def test_notifier_stays_quiet_at_or_below_the_limit(self) -> None:
        self.press_n_times(3, overlay="[recovery]\nnotify_command = ['/bin/page']\n")
        self.assertEqual(self.notifier.calls, [])

    def test_notifier_fires_once_past_the_limit(self) -> None:
        self.press_n_times(4, overlay="[recovery]\nnotify_command = ['/bin/page']\n")
        self.assertEqual(self.notifier.calls, [["/bin/page"]])

    def test_no_notifier_configured_means_no_call(self) -> None:
        self.press_n_times(5)
        self.assertEqual(self.notifier.calls, [])

    def test_a_failing_notifier_does_not_break_the_cycle(self) -> None:
        notifier = RecordingNotifier(fail_with=OSError("pager unreachable"))
        daemon = self.build(
            overlay="[recovery]\nnotify_command = ['/bin/page']\n", notifier=notifier
        )
        for _ in range(4):
            daemon.failures = 2
            decision = daemon.step()
        self.assertIs(decision.action, Action.PRESS_KEY)

    def test_max_per_day_zero_disables_the_warning(self) -> None:
        self.press_n_times(
            6,
            overlay="[recovery]\nmax_per_day = 0\nnotify_command = ['/bin/page']\n",
        )
        self.assertEqual(self.notifier.calls, [])

    def test_presses_outside_the_window_do_not_count(self) -> None:
        # The clock advances 600s per press; 200 presses would still be inside
        # a day, so push time forward explicitly between them.
        daemon = self.build(overlay="[recovery]\nnotify_command = ['/bin/page']\n")
        for _ in range(6):
            daemon.failures = 2
            daemon.step()
            self.clock.time += 86400 * 2
        self.assertEqual(self.notifier.calls, [])


class TestRunLoop(DaemonTest):
    def test_stops_when_the_clock_reports_a_stop(self) -> None:
        clock = FakeClock(stop_after=3)
        daemon = self.build(probe=always_up, clock=clock)
        daemon.run()
        self.assertEqual(len(clock.sleeps), 3)

    def test_a_typed_error_does_not_kill_the_daemon(self) -> None:
        # A bad cycle is a bad cycle. Exiting would hand the supervisor a
        # restart loop and lose the failure counter every time.
        calls = {"n": 0}

        def flaky(host):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ProbeError("ping vanished")
            return always_up(host)

        clock = FakeClock(stop_after=3)
        daemon = self.build(probe=flaky, clock=clock)
        daemon.run()
        self.assertGreaterEqual(calls["n"], 2)
        # First sleep is the recovery interval used after the failed cycle.
        self.assertEqual(clock.sleeps[0], 60)

    def test_an_uncalibrated_start_is_announced(self) -> None:
        import io

        stream = io.StringIO()
        setup_logging(stream=stream, syslog="never", level="DEBUG")
        clock = FakeClock(stop_after=1)
        daemon = self.build(probe=always_up, clock=clock, calibrated=False)
        daemon.run()
        self.assertIn("daemon.uncalibrated", stream.getvalue())

    def test_startup_line_reports_the_operating_mode(self) -> None:
        import io

        stream = io.StringIO()
        setup_logging(stream=stream, syslog="never", level="DEBUG")
        clock = FakeClock(stop_after=1)
        daemon = self.build(probe=always_up, clock=clock, no_act=True)
        daemon.run()
        output = stream.getvalue()
        self.assertIn("daemon.start", output)
        self.assertIn("no_act=true", output)


class TestLogging(DaemonTest):
    def capture_log(self, **kwargs) -> str:
        import io

        stream = io.StringIO()
        setup_logging(stream=stream, syslog="never", level="DEBUG")
        daemon = self.build(**kwargs)
        for _ in range(3):
            daemon.step()
        return stream.getvalue()

    def test_a_keypress_is_logged_at_warning(self) -> None:
        # Pressing a key at somebody's console is not routine information.
        output = self.capture_log()
        self.assertIn("WARNING key.pressed", output)
        self.assertIn("key=Y", output)

    def test_a_refusal_for_lack_of_calibration_is_logged_at_error(self) -> None:
        output = self.capture_log(calibrated=False)
        self.assertIn("ERROR key.refused", output)

    def test_no_act_suppression_is_logged(self) -> None:
        output = self.capture_log(no_act=True)
        self.assertIn("key.suppressed", output)

    def test_ping_failures_are_logged_with_the_running_count(self) -> None:
        output = self.capture_log(probe=always_down, detector=non_matching_detector)
        self.assertIn("ping.down", output)
        self.assertIn("failures=1", output)
        self.assertIn("failures=3", output)


if __name__ == "__main__":
    unittest.main()
