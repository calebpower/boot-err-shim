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
from .config import Config, load_config
from .errors import ShimError
from .log import setup_logging
from .platform_ import platform_defaults
from .png import write_frame
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

    return parser


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


COMMANDS = {
    "check-config": command_check_config,
    "capture": command_capture,
}


def main(argv: list[str] | None = None) -> int:
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
