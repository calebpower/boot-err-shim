"""Structured logging with a stable, greppable line format.

Every log line is an *event name* plus key=value fields:

    2026-08-16T14:02:11Z INFO ping.down host=10.0.0.50 failures=1

Event names are stable identifiers, not prose. That matters twice over: tier 2
asserts exact output against golden files, and anything downstream that alerts
on these lines should not break because somebody reworded a sentence.

Timestamps are emitted only when the sink does not already add its own, so the
same code produces byte-identical output under test, under syslog, and under
journald.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

LOGGER_NAME = "boot_err_shim"

#: Sentinel field values that would otherwise render ambiguously.
_EMPTY = "-"


def _render_value(value: Any) -> str:
    if value is None:
        return _EMPTY
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        # Avoid 0.020000000000000004 drifting into golden files.
        return f"{value:g}"
    text = str(value)
    if text == "":
        return '""'
    if any(ch.isspace() or ch == '"' for ch in text):
        escaped = (
            text.replace("\\", "\\\\")
            .replace('"', '\\"')
            # Newlines must become an escape, not a literal break: a value
            # containing one would otherwise split a single event across two
            # lines and desync anything reading the log line by line. ping
            # output, which we log verbatim on failure, is multi-line.
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    return text


class ShimFormatter(logging.Formatter):
    """Render ``event key=value ...``, optionally with a timestamp."""

    def __init__(self, *, with_time: bool = True, clock: Callable[[], str] | None = None):
        super().__init__()
        self.with_time = with_time
        self._clock = clock

    def _timestamp(self, record: logging.LogRecord) -> str:
        if self._clock is not None:
            return self._clock()
        return self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ")

    def format(self, record: logging.LogRecord) -> str:
        parts: list[str] = []
        if self.with_time:
            parts.append(self._timestamp(record))
        parts.append(record.levelname)
        parts.append(record.getMessage())

        fields = getattr(record, "fields", None)
        if fields:
            for key in fields:
                parts.append(f"{key}={_render_value(fields[key])}")

        line = " ".join(parts)
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def event(
    logger: logging.Logger,
    level: int,
    name: str,
    /,
    **fields: Any,
) -> None:
    """Emit a structured event.

    Field order is preserved as given, so golden-file comparisons are stable.
    """
    logger.log(level, name, extra={"fields": fields})


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")


def under_journald() -> bool:
    """True when systemd is capturing our stderr into the journal."""
    return "JOURNAL_STREAM" in os.environ


def _syslog_handler(address: Path | None) -> logging.Handler | None:
    """Try the platform syslog socket; return None if it is not usable.

    Deliberately silent on failure. A machine without a usable syslog socket
    should still get logs on stderr rather than refusing to start -- losing
    the syslog copy is a nuisance, and a daemon that will not run is an
    outage.

    The guard is broad because the ways this can fail are not all OSError.
    A path that exists but is a regular file, a platform with no AF_UNIX at
    all: the second raises AttributeError from inside the standard library,
    which sailed straight past an `except OSError` and crashed setup. Since
    the contract here is "never fail", the guard has to actually mean it.
    """
    if address is None or not address.exists():
        return None
    try:
        handler = logging.handlers.SysLogHandler(address=str(address))
    except Exception:  # noqa: BLE001 - see above; this must never propagate
        return None
    handler.setFormatter(ShimFormatter(with_time=False))
    handler.ident = "boot-err-shim: "
    return handler


def setup_logging(
    *,
    level: str = "INFO",
    syslog: str = "auto",
    syslog_socket: Path | None = None,
    file: Path | None = None,
    stream: Any = None,
    with_time: bool | None = None,
) -> logging.Logger:
    """Configure and return the package logger.

    Handlers are replaced wholesale on each call so that a SIGHUP reload does
    not accumulate duplicate sinks -- an easy way to end up logging every event
    four times after a few reloads.
    """
    logger = get_logger()
    logger.setLevel(getattr(logging, level))
    logger.propagate = False

    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        existing.close()

    journald = under_journald()
    if with_time is None:
        with_time = not journald

    console = logging.StreamHandler(stream if stream is not None else sys.stderr)
    console.setFormatter(ShimFormatter(with_time=with_time))
    logger.addHandler(console)

    want_syslog = syslog == "always" or (syslog == "auto" and not journald)
    if want_syslog:
        handler = _syslog_handler(syslog_socket)
        if handler is not None:
            logger.addHandler(handler)

    if file is not None:
        file.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            str(file), maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        rotating.setFormatter(ShimFormatter(with_time=True))
        logger.addHandler(rotating)

    return logger
