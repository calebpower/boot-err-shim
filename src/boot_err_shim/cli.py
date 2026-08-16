"""Command line entry point.

Every subcommand funnels failures through one handler, so a typed error
becomes a one-line message and a meaningful exit status rather than a
traceback. Tracebacks are available with --debug when something is genuinely
unexpected.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .bitmap import binarise, contrast_ratio
from .calibrate import Calibration, Findings, analyse, check_calibration
from .config import Config, load_config
from .detect import CalibratedDetector
from .errors import AnalysisError, CalibrationError, ShimError
from .frame import Frame
from .log import setup_logging
from .platform_ import platform_defaults
from .png import read_frame, write_frame
from .report import (
    failure_advice,
    findings_report,
    ink_sketch,
    success_report,
)
from .rfb import client_from_config


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="config file (default: platform-specific location)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="boot-err-shim",
        description=(
            "Watch a host and, when it stops answering, look at its console "
            "over VNC and press the key that gets it past the PERC "
            "flash-failure prompt."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--debug", action="store_true", help="show tracebacks on unexpected errors"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser(
        "check-config", help="validate the config and exit nonzero if it is wrong"
    )
    _add_config_argument(check)

    capture = subparsers.add_parser(
        "capture", help="grab one frame from the console and write it as a PNG"
    )
    _add_config_argument(capture)
    capture.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("screen.png"),
        metavar="PATH",
        help="where to write the PNG (default: screen.png)",
    )

    configure = subparsers.add_parser(
        "configure",
        help="learn a calibration from a console showing the error",
        description=(
            "Grab the console, work out the character grid and font from the "
            "message the config says should be on screen, and write a "
            "calibration. Never sends a keypress, so it is safe to run "
            "against a live stuck console."
        ),
    )
    _add_config_argument(configure)
    configure.add_argument(
        "--from",
        dest="from_image",
        type=Path,
        metavar="PNG",
        help="analyse a saved image instead of connecting",
    )
    configure.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="where to write the calibration (default: from config)",
    )
    configure.add_argument(
        "--cell", type=_parse_pair, metavar="WxH", help="force the cell size"
    )
    configure.add_argument(
        "--origin", type=_parse_pair, metavar="X,Y", help="force the grid origin"
    )
    configure.add_argument(
        "--threshold", type=int, metavar="N", help="force the luminance threshold"
    )
    configure.add_argument(
        "--invert", action="store_true", help="treat dark pixels as the text"
    )
    configure.add_argument(
        "--dry-run", action="store_true", help="analyse but do not write anything"
    )

    detect = subparsers.add_parser(
        "test-detect", help="run the detector against a saved PNG"
    )
    _add_config_argument(detect)
    detect.add_argument("image", type=Path, help="PNG to examine")
    detect.add_argument(
        "--annotate",
        type=Path,
        metavar="PNG",
        help="write a copy with the compared region outlined",
    )

    show = subparsers.add_parser(
        "show-calibration",
        help="print the calibration, including the learned font",
        description=(
            "Everything the calibration knows, in a form a person can check. "
            "The pixel delta says the glyphs are self-consistent; only "
            "looking at them says they are the letters they claim to be."
        ),
    )
    _add_config_argument(show)
    show.add_argument(
        "--glyphs", action="store_true", help="also print the learned font"
    )

    run = subparsers.add_parser("run", help="the daemon")
    _add_config_argument(run)
    run.add_argument(
        "--no-act",
        action="store_true",
        help="do everything except actually send the key",
    )
    run.add_argument(
        "--once", action="store_true", help="run a single cycle and exit"
    )

    return parser


def _parse_pair(value: str) -> tuple[int, int]:
    """Accept WxH or X,Y."""
    separator = "x" if "x" in value.lower() else ","
    try:
        first, second = value.lower().split(separator)
        return int(first), int(second)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected two numbers like 9x16 or 72,208; got {value!r}"
        ) from None


def resolve_config_path(explicit: Path | None) -> Path:
    return explicit if explicit is not None else platform_defaults().config_path


def load(args: argparse.Namespace) -> Config:
    return load_config(resolve_config_path(args.config))


def command_check_config(args: argparse.Namespace) -> int:
    path = resolve_config_path(args.config)
    config = load_config(path)
    print(f"{path}: OK")
    print(f"  target      {config.target.host}")
    print(f"  vnc         {config.vnc.host}:{config.vnc.port}", end="")
    print(" over TLS" if config.vnc.tls else "")
    print(f"  threshold   {config.ping.threshold} failed pings")
    print(f"  ping        {' '.join(config.ping.command)}")
    print(f"  key         {config.detect.key} (keysym 0x{config.detect.keysym:02x})")
    print(f"  calibration {config.detect.calibration}", end="")
    print("" if config.detect.calibration.exists() else "  [MISSING]")
    for index, line in enumerate(config.detect.lines, 1):
        print(f"  text {index}/{len(config.detect.lines)}  {line!r}")

    if not config.detect.calibration.exists():
        print(
            "\nNo calibration yet, so keys will NOT be pressed.\n"
            "Reboot the host, let it stop at the error, then run: "
            "boot-err-shim configure",
            file=sys.stderr,
        )
    return 0


def command_capture(args: argparse.Namespace) -> int:
    config = load(args)
    setup_logging(level=config.log.level, syslog="never")

    client = client_from_config(config)
    try:
        info = client.connect()
        print(f"connected to {config.vnc.host}:{config.vnc.port}")
        print(f"  RFB 3.8, security offered: [{info.security_description}]")
        print(f"  security used: {info.security_used}, TLS: {'yes' if info.tls else 'no'}")
        print(f"  desktop: {info.name!r}")
        print(f"  framebuffer: {info.width}x{info.height}")
        frame = client.capture()
    finally:
        client.close()

    write_frame(args.output, frame)
    print(f"wrote {args.output} ({frame.width}x{frame.height})")
    return 0


def _grab_frame(config: Config, from_image: Path | None) -> tuple[Frame, list[str]]:
    """Get a frame either from a file or from the console, plus report lines."""
    if from_image is not None:
        frame = read_frame(from_image)
        return frame, [
            f"reading {from_image}",
            f"  {frame.width}x{frame.height}",
        ]

    from .report import connection_report

    client = client_from_config(config)
    try:
        info = client.connect()
        frame = client.capture()
    finally:
        client.close()
    return frame, connection_report(config.vnc.host, config.vnc.port, info)


def _snapshot_path(config: Config) -> Path:
    """A stable name for the frame configure just looked at.

    Deliberately not timestamped: `configure` is iterative, and overwriting
    one file means `--from` always refers to the most recent attempt without
    the operator having to copy a filename around.
    """
    return config.log.screenshot_dir / "configure.png"


def command_configure(args: argparse.Namespace) -> int:
    config = load(args)
    setup_logging(level=config.log.level, syslog="never")

    frame, header = _grab_frame(config, args.from_image)
    for line in header:
        print(line)

    if args.from_image is None and not args.dry_run:
        try:
            saved = write_frame(_snapshot_path(config), frame)
            print(f"saved snapshot -> {saved}")
        except OSError as exc:
            print(f"  (could not save snapshot: {exc})", file=sys.stderr)

    print()
    print("analysing")

    try:
        calibration = analyse(
            frame,
            config.detect.lines,
            cell=args.cell,
            origin=args.origin,
            threshold=args.threshold,
            invert=True if args.invert else None,
        )
    except AnalysisError as exc:
        if exc.findings is not None:
            for line in findings_report(exc.findings):
                print(line)
            binarised = binarise(
                frame, threshold=args.threshold, invert=True if args.invert else None
            )
            for line in ink_sketch(binarised.mask, exc.findings):
                print(line)
        print(f"\nCOULD NOT CALIBRATE: {exc}", file=sys.stderr)
        for line in failure_advice(exc.findings) if exc.findings else []:
            print(line, file=sys.stderr)
        return exc.exit_code

    findings = calibration.findings or Findings(
        width=frame.width,
        height=frame.height,
        background=calibration.background,
        foreground=calibration.foreground,
        threshold=calibration.threshold,
        inverted=calibration.inverted,
        ink_fraction=binarise(
            frame, threshold=calibration.threshold, invert=calibration.inverted
        ).ink_fraction,
        contrast=contrast_ratio(calibration.foreground, calibration.background),
    )
    for line in findings_report(findings):
        print(line)
    for line in success_report(calibration, config.detect.lines):
        print(line)

    destination = args.output or config.detect.calibration
    if args.dry_run:
        print(f"\n--dry-run: not writing {destination}")
        return 0

    calibration.save(destination)
    print(f"\nwrote {destination}")
    print("detector: calibrated-glyph (exact)" if calibration.exact
          else "detector: calibrated-glyph (approximate)")
    return 0


def command_test_detect(args: argparse.Namespace) -> int:
    config = load(args)
    setup_logging(level=config.log.level, syslog="never")

    frame = read_frame(args.image)
    calibration = Calibration.load(config.detect.calibration)
    check_calibration(calibration, config.detect.lines)

    detector = CalibratedDetector(calibration, config.detect.tolerance)
    result = detector.detect(frame)

    print(f"{args.image}: {frame.width}x{frame.height}")
    print(f"  calibration: {config.detect.calibration}")
    print(f"  matcher: {result.detail}")
    if result.difference is not None:
        print(
            f"  region difference: {result.difference:.4%} "
            f"(tolerance {config.detect.tolerance:.2%})"
        )
    if result.text:
        print("  screen reads:")
        for line in result.text.splitlines():
            print(f"    | {line}")

    if args.annotate is not None:
        # Evidence a person can check at a glance: if the box is around the
        # wrong part of the screen, that is obvious here and invisible in the
        # difference percentage above.
        write_frame(args.annotate, frame.outlined(calibration.region))
        print(f"  annotated copy: {args.annotate}")

    print()
    print("MATCH" if result.matched else "NO MATCH")
    return 0 if result.matched else 1


def command_show_calibration(args: argparse.Namespace) -> int:
    from .report import calibration_summary, glyph_sheet

    config = load(args)
    calibration = Calibration.load(config.detect.calibration)

    print(f"{config.detect.calibration}")
    for line in calibration_summary(calibration):
        print(line)

    try:
        check_calibration(calibration, config.detect.lines)
    except CalibrationError as exc:
        print(f"\nSTALE: {exc}", file=sys.stderr)
        return exc.exit_code

    if args.glyphs:
        print()
        for line in glyph_sheet(calibration):
            print(line)
    else:
        print("\n(--glyphs to print the learned font)")

    return 0


def command_run(args: argparse.Namespace) -> int:
    import threading

    from .daemon import Daemon, SystemClock
    from .history import InterventionHistory
    from .lock import SingleInstanceLock
    from .probe import Prober

    config = load(args)
    setup_logging(
        level=config.log.level,
        syslog=config.log.syslog,
        syslog_socket=config.defaults.syslog_socket,
        file=config.log.file,
    )

    calibration: Calibration | None = None
    try:
        calibration = Calibration.load(config.detect.calibration)
        check_calibration(calibration, config.detect.lines)
    except CalibrationError as exc:
        # Not fatal: the daemon still watches and still reports, it simply
        # refuses to press anything. Exiting would leave the host unwatched
        # as well as unrescued.
        print(f"boot-err-shim: {exc}", file=sys.stderr)
        calibration = None

    if calibration is not None:
        detector = CalibratedDetector(calibration, config.detect.tolerance)
    else:
        from .daemon import DetectResult as _DetectResult

        def detector(frame):  # type: ignore[misc]
            return _DetectResult(matched=False, detail="uncalibrated")

    prober = Prober(config.ping.command, config.ping.timeout)
    clock = SystemClock()

    def console_factory():
        client = client_from_config(config)
        client.connect()
        return client

    def frame_writer(frame, label):
        return _write_ring(config, frame, label)

    stop = clock.stop
    _install_signal_handlers(stop, threading)

    with SingleInstanceLock(config.lock_path):
        daemon = Daemon(
            config,
            probe=prober.probe,
            console_factory=console_factory,
            detector=detector,
            clock=clock,
            history=InterventionHistory.load(config.history_path),
            calibrated=calibration is not None,
            no_act=args.no_act,
            frame_writer=frame_writer,
        )
        if args.once:
            daemon.step()
        else:
            daemon.run()
    return 0


def _install_signal_handlers(stop, threading_module) -> None:
    import signal

    def handle(signum, _frame):
        stop.set()

    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), handle)


def _write_ring(config: Config, frame: Frame, label: str) -> Path:
    """Write a frame into the snapshot ring buffer, evicting the oldest.

    Names are microsecond timestamps -- ``20260816T124820.031457-match.png``
    -- so a directory listing reads chronologically for a person. Two frames
    landing in the same microsecond get the next free one.

    **Eviction orders by modification time, not by name**, and that
    distinction was arrived at the hard way. A first version put a counter
    after the seconds (``...124820-1-match.png``), which sorts *before*
    ``...124820-match.png`` because '1' precedes 'm', so eviction deleted the
    newest frame. Moving the counter into the prefix fixed the comparison but
    not the scheme: once eviction frees an index, the next write claims it
    again, and names cycle instead of increasing. Any naming scheme derived
    from a clock has this problem somewhere, a backwards NTP step being the
    obvious one. The filesystem already records when each file was written,
    so ask it.
    """
    import time

    directory = config.log.screenshot_dir
    directory.mkdir(parents=True, exist_ok=True)

    def named(at: float) -> Path:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(at))
        return directory / f"{stamp}.{int(round((at % 1) * 1_000_000)):06d}-{label}.png"

    now = time.time()
    path = named(now)
    # Bounded. Nudging the timestamp only finds a free name while the name
    # actually depends on it; if that ever stops being true the loop cannot
    # terminate, and a daemon spinning silently is far worse than one that
    # overwrites a single snapshot.
    for _ in range(1000):
        if not path.exists():
            break
        now += 0.000001
        path = named(now)

    write_frame(path, frame)

    def age(candidate: Path) -> tuple[int, str]:
        # Name breaks ties on filesystems whose timestamps are coarse.
        try:
            return candidate.stat().st_mtime_ns, candidate.name
        except OSError:  # pragma: no cover - vanished under us; treat as old
            return 0, candidate.name

    existing = sorted(
        (
            candidate
            for candidate in directory.glob("*.png")
            # configure.png is referred to by name by `configure --from`, and
            # the frame we just wrote is never a candidate for eviction
            # whatever the ordering says.
            if candidate.name != "configure.png" and candidate != path
        ),
        key=age,
    )

    # Budget counts the new frame, so keep-1 of the older ones survive.
    surplus = max(0, len(existing) - (config.log.screenshot_keep - 1))
    for stale in existing[:surplus]:
        try:
            stale.unlink()
        except OSError:
            # Losing a snapshot is a nuisance; failing the cycle over one
            # would cost the rescue.
            pass
    return path


COMMANDS = {
    "check-config": command_check_config,
    "capture": command_capture,
    "configure": command_configure,
    "test-detect": command_test_detect,
    "show-calibration": command_show_calibration,
    "run": command_run,
}


def _make_output_lossy() -> None:
    """Never let printing be the thing that fails.

    Daemons run under LANG=C, where stdout encodes as ASCII. The decoded
    screen text can contain U+FFFD, because that is what an unknown glyph
    becomes, and printing it raised UnicodeEncodeError -- so `test-detect` on
    a non-matching screen died with a traceback instead of reporting NO MATCH.

    Escaping rather than substituting keeps the output honest: on a UTF-8
    terminal the character appears as itself, and on an ASCII one it appears
    as an escape rather than being silently replaced by something that might
    also be real text on the console.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (ValueError, OSError):  # pragma: no cover - exotic streams
                pass


def main(argv: list[str] | None = None) -> int:
    _make_output_lossy()
    parser = build_parser()
    args = parser.parse_args(argv)

    handler = COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse rejects these first
        parser.error(f"unknown command {args.command}")

    try:
        return handler(args)
    except ShimError as exc:
        print(f"boot-err-shim: {exc}", file=sys.stderr)
        if args.debug:
            raise
        return exc.exit_code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
