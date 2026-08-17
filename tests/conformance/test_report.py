"""Tier 2: the `configure` report and the log line format, byte for byte."""

from __future__ import annotations

import io
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))

from boot_err_shim.bitmap import binarise  # noqa: E402
from boot_err_shim.calibrate import analyse  # noqa: E402
from boot_err_shim.errors import AnalysisError  # noqa: E402
from boot_err_shim.frame import Frame  # noqa: E402
from boot_err_shim.log import NOTICE, ShimFormatter, event  # noqa: E402
from boot_err_shim.report import (  # noqa: E402
    connection_report,
    failure_advice,
    findings_report,
    ink_sketch,
    success_report,
)
from boot_err_shim.rfb import ServerInfo  # noqa: E402
from render_frame import THE_MESSAGE, render  # noqa: E402
from tests.conformance import compare  # noqa: E402


def joined(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


class TestSuccessReport(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = render(THE_MESSAGE, cell_width=9, cell_height=16)
        self.calibration = analyse(self.frame, THE_MESSAGE)

    def test_connection_report(self) -> None:
        info = ServerInfo(
            width=640,
            height=400,
            name="idrac-console",
            security_types=(2,),
            security_used=2,
            tls=False,
        )
        compare(
            self,
            "configure-connection.txt",
            joined(connection_report("10.0.0.51", 5901, info)),
        )

    def test_findings_report(self) -> None:
        compare(
            self,
            "configure-findings.txt",
            joined(findings_report(self.calibration.findings)),
        )

    def test_success_report(self) -> None:
        compare(
            self,
            "configure-success.txt",
            joined(success_report(self.calibration, THE_MESSAGE)),
        )

    def test_the_whole_successful_report(self) -> None:
        info = ServerInfo(
            width=640, height=400, name="idrac-console",
            security_types=(2,), security_used=2, tls=True,
        )
        lines = [
            *connection_report("10.0.0.51", 5901, info),
            "",
            "analysing",
            *findings_report(self.calibration.findings),
            *success_report(self.calibration, THE_MESSAGE),
        ]
        compare(self, "configure-whole.txt", joined(lines))


class TestFailureReport(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = render(
            (
                "No boot device available.",
                "Press F1 to retry boot.",
                "Press F2 for setup utility.",
            )
        )
        try:
            analyse(self.frame, THE_MESSAGE)
        except AnalysisError as exc:
            self.error = exc
        else:  # pragma: no cover - the fixture must fail to be useful
            self.fail("expected the analysis to fail")

    def test_findings_on_failure(self) -> None:
        compare(
            self,
            "configure-failure-findings.txt",
            joined(findings_report(self.error.findings)),
        )

    def test_advice_on_failure(self) -> None:
        compare(
            self,
            "configure-failure-advice.txt",
            joined(failure_advice(self.error.findings)),
        )

    def test_ink_sketch(self) -> None:
        mask = binarise(self.frame).mask
        compare(
            self,
            "configure-failure-sketch.txt",
            joined(ink_sketch(mask, self.error.findings)),
        )


class TestBlankScreenReport(unittest.TestCase):
    def test_advice_for_a_blank_screen_is_different(self) -> None:
        # A blank screen has a specific cause and a specific next step; giving
        # the generic "try --cell WxH" advice would waste somebody's time.
        frame = Frame(320, 200, bytes(320 * 200 * 3))
        try:
            analyse(frame, THE_MESSAGE)
        except AnalysisError as exc:
            compare(
                self,
                "configure-blank-advice.txt",
                joined(failure_advice(exc.findings)),
            )
        else:  # pragma: no cover
            self.fail("expected the analysis to fail")


class TestLowContrastWarning(unittest.TestCase):
    def test_a_dim_console_is_flagged(self) -> None:
        # Computed, not eyeballed. Nobody can judge this from a PNG.
        frame = render(THE_MESSAGE, foreground=(70, 70, 70), background=(0, 0, 0))
        try:
            calibration = analyse(frame, THE_MESSAGE)
            findings = calibration.findings
        except AnalysisError as exc:
            findings = exc.findings
        rendered = joined(findings_report(findings))
        self.assertIn("WARNING", rendered)
        self.assertIn("contrast", rendered)
        compare(self, "configure-low-contrast.txt", rendered)


class TestLogLineFormat(unittest.TestCase):
    """The event format, which alerting may depend on."""

    def render_events(self) -> str:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(
            ShimFormatter(with_time=True, clock=lambda: "2026-08-16T14:02:11Z")
        )
        logger = logging.getLogger("conformance_events")
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        # NOTICE, not INFO: lifecycle events have to clear syslog's
        # notice threshold or a healthy daemon is invisible.
        event(logger, NOTICE, "daemon.start", host="10.0.0.50",
              vnc="10.0.0.51:5901", threshold=3, calibrated=True, no_act=False)
        event(logger, logging.INFO, "ping.up", host="10.0.0.50",
              reason="ok", failures=0, threshold=3)
        event(logger, logging.WARNING, "ping.down", host="10.0.0.50",
              reason="unreachable", failures=3, threshold=3)
        event(logger, logging.WARNING, "vnc.connect_failed", host="10.0.0.51",
              port=5901, error="ConnectionFailed", detail="connection refused")
        event(logger, logging.INFO, "vnc.captured", width=640, height=400)
        event(logger, logging.INFO, "detect.match", detail="region")
        event(logger, logging.INFO, "detect.no_match", detail="region-mismatch")
        event(logger, logging.WARNING, "key.pressed", key="Y", keysym=89,
              host="10.0.0.50")
        event(logger, logging.WARNING, "intervention.frequent", count=4, limit=3,
              detail="controller is failing repeatedly; replace it")
        event(logger, logging.ERROR, "key.refused", key="Y",
              detail="no calibration")
        event(logger, NOTICE, "daemon.stop")
        return stream.getvalue()

    def test_event_lines(self) -> None:
        compare(self, "log-events.txt", self.render_events())


if __name__ == "__main__":
    unittest.main()
