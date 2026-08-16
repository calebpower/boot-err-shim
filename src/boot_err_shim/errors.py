"""Typed error hierarchy.

Tier 7's oracle is "no unhandled exception, ever; every failure is a typed
error with a log record; the process is still alive afterwards". That only
means something if there is a single root to catch and every failure path
raises something underneath it. Bare ValueError/OSError escaping from a
module is a defect, not a style problem.
"""

from __future__ import annotations


class ShimError(Exception):
    """Root of every error this program raises deliberately.

    The fuzz tiers assert that anything reaching the top level is an instance
    of this class. Anything else means an unguarded code path.
    """

    #: Process exit code when this error reaches the CLI.
    exit_code = 1


class ConfigError(ShimError):
    """Configuration could not be loaded, parsed, or validated."""

    exit_code = 78  # EX_CONFIG


class CalibrationError(ShimError):
    """A calibration could not be produced, loaded, or verified."""

    exit_code = 65  # EX_DATAERR


class CalibrationNotFound(CalibrationError):
    """No calibration file exists where one was expected."""


class CalibrationStale(CalibrationError):
    """The calibration on disk does not describe the configured text."""


class AnalysisError(CalibrationError):
    """`configure` could not make sense of the framebuffer.

    Carries the partial findings so the operator gets a useful report rather
    than a bare failure: an analysis that got as far as the character grid but
    could not locate the message should say so.
    """

    def __init__(self, message: str, findings: object | None = None) -> None:
        super().__init__(message)
        self.findings = findings


class ImageError(ShimError):
    """An image file could not be read or decoded."""

    exit_code = 65  # EX_DATAERR


class ProtocolError(ShimError):
    """The peer spoke something other than the RFB protocol we expect."""

    exit_code = 69  # EX_UNAVAILABLE


class ConnectionFailed(ProtocolError):
    """Could not establish a transport to the VNC server."""


class AuthError(ProtocolError):
    """The VNC server rejected our credentials, or demanded a scheme we lack."""

    exit_code = 77  # EX_NOPERM


class Timeout(ProtocolError):
    """A read or connect exceeded its deadline.

    Distinct from ConnectionFailed because a server that accepts and then
    dribbles bytes is a different operational problem from one that refuses.
    """


class ProbeError(ShimError):
    """The reachability probe could not be executed at all.

    Note this is *not* raised when the host is simply down -- that is an
    ordinary negative result, not an error.
    """


class LockError(ShimError):
    """Another instance holds the lock, or the lock could not be taken."""

    exit_code = 75  # EX_TEMPFAIL
