"""Every operating-system difference in the program, in one module.

Nothing else branches on the OS. That is not tidiness for its own sake: the
structural tier parses this file and asserts the paths here agree with the
systemd unit and the rc script, which is only possible if there is exactly one
place to look.

The trap this module exists to defuse
-------------------------------------
FreeBSD's ``ping -W`` takes **milliseconds**. iputils' ``ping -W`` takes
**seconds**. A config copied from a FreeBSD box to an Ubuntu box therefore
turns a 2-second timeout into a 2000-second one, and a config copied the other
way turns it into 2 milliseconds -- which never succeeds, so the daemon decides
a perfectly healthy host is down.

That second direction is the dangerous one. It is why unknown systems get no
``-W`` at all rather than a guess: a missing flag falls back to ping's own
default and the subprocess timeout, which is merely suboptimal. A wrong flag is
a false "host is down", and false "host is down" is how this program ends up
pressing keys at a console it should have left alone.
"""

from __future__ import annotations

import os
import platform as _platform
from dataclasses import dataclass
from pathlib import Path

#: Directory leaf used for state under whatever the platform's state root is.
#: The systemd unit's ``StateDirectory=`` must equal this; a structural test
#: enforces it.
STATE_DIR_NAME = "boot-err-shim"

#: Config file leaf, likewise cross-checked against the init scripts.
CONFIG_FILE_NAME = "boot-err-shim.conf"


@dataclass(frozen=True)
class PlatformDefaults:
    """Defaults derived from the host OS. Every field is overridable in config."""

    system: str
    config_path: Path
    state_dir: Path
    ping_command: tuple[str, ...]
    syslog_socket: Path | None

    @property
    def calibration_path(self) -> Path:
        return self.state_dir / "calibration.toml"

    @property
    def snapshot_dir(self) -> Path:
        return self.state_dir / "snapshots"

    @property
    def history_path(self) -> Path:
        return self.state_dir / "history.json"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "boot-err-shim.lock"


def _windows_state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / STATE_DIR_NAME
    return Path.home() / f".{STATE_DIR_NAME}"


def platform_defaults(system: str | None = None) -> PlatformDefaults:
    """Return defaults for ``system``, or for the running host if omitted.

    ``system`` matches :func:`platform.system` output ("FreeBSD", "Linux",
    "Darwin", "Windows"). It is a parameter so tests can exercise every branch
    on one machine -- the FreeBSD defaults must be verifiable without a
    FreeBSD box, since we do not have one.
    """
    if system is None:
        system = _platform.system()

    if system == "FreeBSD":
        return PlatformDefaults(
            system=system,
            config_path=Path("/usr/local/etc") / CONFIG_FILE_NAME,
            state_dir=Path("/var/db") / STATE_DIR_NAME,
            # -W is MILLISECONDS on FreeBSD.
            ping_command=("ping", "-c", "1", "-W", "2000", "{host}"),
            syslog_socket=Path("/var/run/log"),
        )

    if system == "Linux":
        return PlatformDefaults(
            system=system,
            config_path=Path("/etc") / CONFIG_FILE_NAME,
            state_dir=Path("/var/lib") / STATE_DIR_NAME,
            # -W is SECONDS on iputils.
            ping_command=("ping", "-c", "1", "-W", "2", "{host}"),
            syslog_socket=Path("/dev/log"),
        )

    if system == "Darwin":
        # Not a supported deployment target; present so the program is usable
        # on a Mac during development. macOS ping is BSD-derived: -W is ms.
        return PlatformDefaults(
            system=system,
            config_path=Path("/usr/local/etc") / CONFIG_FILE_NAME,
            state_dir=Path("/usr/local/var") / STATE_DIR_NAME,
            ping_command=("ping", "-c", "1", "-W", "2000", "{host}"),
            syslog_socket=None,
        )

    if system == "Windows":
        # Development only. Windows ping: -n count, -w timeout in ms.
        return PlatformDefaults(
            system=system,
            config_path=_windows_state_dir() / CONFIG_FILE_NAME,
            state_dir=_windows_state_dir(),
            ping_command=("ping", "-n", "1", "-w", "2000", "{host}"),
            syslog_socket=None,
        )

    # Unknown system. Deliberately omit -W rather than guess its units; see
    # the module docstring. ping's own default plus the subprocess timeout
    # bound the call.
    return PlatformDefaults(
        system=system,
        config_path=Path("/etc") / CONFIG_FILE_NAME,
        state_dir=Path("/var/lib") / STATE_DIR_NAME,
        ping_command=("ping", "-c", "1", "{host}"),
        syslog_socket=None,
    )


def render_ping_command(template: tuple[str, ...] | list[str], host: str) -> list[str]:
    """Substitute ``{host}`` in a ping command template.

    Only ``{host}`` is substituted, and only as a whole-token or embedded
    literal -- there is no format-string evaluation here, so a host name
    containing braces cannot reach into the template.
    """
    return [part.replace("{host}", host) for part in template]
