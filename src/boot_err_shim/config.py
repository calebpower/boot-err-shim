"""Configuration loading and validation.

Two rules shape this module.

**Unknown keys are errors.** A typo in a config file that gets silently ignored
means the operator believes a setting is in force when it is not. For a program
whose job is to press a key at a console, "I thought I set the threshold to 5"
is not an acceptable failure mode. Every table and every key is consumed
explicitly and anything left over is reported.

**Validation is total and happens once, at load.** Nothing downstream re-checks
a value, so nothing downstream needs to decide what to do with a negative
interval at three in the morning.
"""

from __future__ import annotations

import math
import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .platform_ import PlatformDefaults, platform_defaults

# Keysyms for keys somebody might plausibly need to send to a firmware prompt.
# Printable ASCII maps to its own code point, so only the specials need names.
NAMED_KEYSYMS: dict[str, int] = {
    "Return": 0xFF0D,
    "Enter": 0xFF0D,
    "Escape": 0xFF1B,
    "Esc": 0xFF1B,
    "Space": 0x0020,
    "Tab": 0xFF09,
    "BackSpace": 0xFF08,
    "Delete": 0xFFFF,
    "F1": 0xFFBE,
    "F2": 0xFFBF,
    "F3": 0xFFC0,
    "F4": 0xFFC1,
    "F10": 0xFFC7,
    "F11": 0xFFC8,
    "F12": 0xFFC9,
}

VALID_ENGINES = ("calibrated", "ocr")
VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
VALID_SYSLOG = ("auto", "always", "never")

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

#: Thirty days. Not a limit anybody should reach -- it exists so that a
#: fat-fingered `interval = 999999999999` is reported rather than silently
#: parking the daemon until long after the hardware has been replaced.
MAX_DURATION_SECONDS = 30 * 86400


def parse_duration(value: Any, where: str) -> int:
    """Accept ``90``, ``90.0``, ``"90"``, ``"90s"``, ``"2m"``, ``"1h"`` -> seconds."""
    if isinstance(value, bool):
        raise ConfigError(f"{where}: expected a duration, got a boolean")
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigError(f"{where}: duration must be finite, got {value!r}")
        if value != int(value):
            raise ConfigError(f"{where}: duration must be whole seconds, got {value!r}")
        seconds = int(value)
    elif isinstance(value, str):
        text = value.strip().lower()
        if not text:
            raise ConfigError(f"{where}: duration is empty")
        unit = 1
        if text[-1] in _DURATION_UNITS:
            unit = _DURATION_UNITS[text[-1]]
            text = text[:-1].strip()
        try:
            parsed = float(text)
        except ValueError:
            raise ConfigError(f"{where}: cannot parse duration {value!r}") from None
        # "1e400" parses as inf and "nan" as NaN; int() of either blows up
        # with OverflowError or ValueError rather than anything a caller
        # would think to catch.
        if not math.isfinite(parsed):
            raise ConfigError(f"{where}: duration must be finite, got {value!r}")
        seconds = int(parsed * unit)
    else:
        raise ConfigError(f"{where}: expected a duration, got {type(value).__name__}")

    if seconds <= 0:
        raise ConfigError(f"{where}: duration must be positive, got {value!r}")
    if seconds > MAX_DURATION_SECONDS:
        raise ConfigError(
            f"{where}: duration {value!r} exceeds the {MAX_DURATION_SECONDS}s "
            "(30 day) maximum; check for a stray digit"
        )
    return seconds


def resolve_keysym(spec: Any) -> int:
    """Turn a config key spec into an X11 keysym."""
    if not isinstance(spec, str) or not spec:
        raise ConfigError("detect.key: expected a key name or single character")
    if len(spec) == 1:
        code = ord(spec)
        if code < 0x20 or code > 0x7E:
            raise ConfigError(
                f"detect.key: {spec!r} is not printable ASCII; use a key name"
            )
        return code
    for name, keysym in NAMED_KEYSYMS.items():
        if name.lower() == spec.lower():
            return keysym
    known = ", ".join(sorted(NAMED_KEYSYMS))
    raise ConfigError(f"detect.key: unknown key {spec!r}; known names: {known}")


class _Table:
    """Consuming reader over a TOML table; complains about anything left."""

    def __init__(self, data: Any, path: str) -> None:
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: expected a table")
        self._data = dict(data)
        self._path = path

    def _take(self, key: str, default: Any) -> Any:
        return self._data.pop(key, default)

    def where(self, key: str) -> str:
        return f"{self._path}.{key}" if self._path else key

    def str_(self, key: str, default: str | None = None) -> str:
        value = self._take(key, default)
        if value is None:
            raise ConfigError(f"{self.where(key)}: required")
        if not isinstance(value, str):
            raise ConfigError(f"{self.where(key)}: expected a string")
        return value

    def opt_str(self, key: str) -> str | None:
        value = self._take(key, None)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ConfigError(f"{self.where(key)}: expected a string")
        return value

    def int_(self, key: str, default: int, minimum: int, maximum: int) -> int:
        value = self._take(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{self.where(key)}: expected an integer")
        if not minimum <= value <= maximum:
            raise ConfigError(
                f"{self.where(key)}: must be between {minimum} and {maximum}, "
                f"got {value}"
            )
        return value

    def float_(self, key: str, default: float, minimum: float, maximum: float) -> float:
        value = self._take(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{self.where(key)}: expected a number")
        value = float(value)
        if not minimum <= value <= maximum:
            raise ConfigError(
                f"{self.where(key)}: must be between {minimum} and {maximum}, "
                f"got {value}"
            )
        return value

    def bool_(self, key: str, default: bool) -> bool:
        value = self._take(key, default)
        if not isinstance(value, bool):
            raise ConfigError(f"{self.where(key)}: expected true or false")
        return value

    def duration(self, key: str, default: int) -> int:
        value = self._take(key, default)
        return parse_duration(value, self.where(key))

    def str_list(self, key: str, default: tuple[str, ...] | None) -> tuple[str, ...]:
        value = self._take(key, None)
        if value is None:
            if default is None:
                raise ConfigError(f"{self.where(key)}: required")
            return tuple(default)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{self.where(key)}: expected a list of strings")
        return tuple(value)

    def choice(self, key: str, default: str, options: tuple[str, ...]) -> str:
        value = self.str_(key, default)
        if value not in options:
            raise ConfigError(
                f"{self.where(key)}: must be one of {', '.join(options)}, "
                f"got {value!r}"
            )
        return value

    def path(self, key: str, default: Path) -> Path:
        value = self._take(key, None)
        if value is None:
            return default
        if not isinstance(value, str):
            raise ConfigError(f"{self.where(key)}: expected a path string")
        return Path(value)

    def opt_path(self, key: str) -> Path | None:
        value = self.opt_str(key)
        return Path(value) if value is not None else None

    def table(self, key: str) -> _Table:
        return _Table(self._take(key, {}), self.where(key))

    def finish(self) -> None:
        if self._data:
            leftover = ", ".join(sorted(self._data))
            label = self._path or "top level"
            raise ConfigError(f"{label}: unknown key(s): {leftover}")


@dataclass(frozen=True)
class TargetConfig:
    host: str


@dataclass(frozen=True)
class PingConfig:
    interval: int
    retry_interval: int
    threshold: int
    command: tuple[str, ...]
    timeout: int


@dataclass(frozen=True)
class VncConfig:
    host: str
    port: int
    password: str | None
    tls: bool
    tls_verify: bool
    tls_ca: Path | None
    connect_timeout: int
    read_timeout: int


@dataclass(frozen=True)
class DetectConfig:
    text: str
    key: str
    keysym: int
    engine: str
    calibration: Path
    tolerance: float

    @property
    def lines(self) -> tuple[str, ...]:
        """The configured message as non-empty, stripped lines."""
        return tuple(
            line.strip() for line in self.text.splitlines() if line.strip()
        )


@dataclass(frozen=True)
class RecoveryConfig:
    interval: int
    post_fix_sleep: int
    max_per_day: int
    notify_command: tuple[str, ...]


@dataclass(frozen=True)
class LogConfig:
    level: str
    syslog: str
    file: Path | None
    screenshot_dir: Path
    screenshot_keep: int


def slug(value: str) -> str:
    """Make a string safe to embed in a filename, on any filesystem."""
    return "".join(char if char.isalnum() or char in "-." else "_" for char in value)


@dataclass(frozen=True)
class Config:
    target: TargetConfig
    ping: PingConfig
    vnc: VncConfig
    detect: DetectConfig
    recovery: RecoveryConfig
    log: LogConfig
    state_dir: Path
    source_path: Path | None = None
    defaults: PlatformDefaults = field(
        default_factory=lambda: platform_defaults(),
        repr=False,
        compare=False,
    )

    @property
    def history_path(self) -> Path:
        """Per target host, so two instances keep separate intervention counts."""
        return self.state_dir / f"{slug(self.target.host)}.history.json"

    @property
    def lock_path(self) -> Path:
        """Per console, because the console is the resource being protected.

        Keyed on the VNC endpoint rather than on the config file or the
        machine. The invariant is "two daemons must not both press a key at
        one console", so the lock has to name that console: two instances
        watching two different hosts should run happily side by side, and two
        instances pointed at the same iDRAC must not, whether or not they were
        started from the same config file.
        """
        return self.state_dir / f"{slug(self.vnc.host)}-{self.vnc.port}.lock"


def _check_permissions(path: Path, has_password: bool) -> None:
    """A config holding a VNC password must not be world-readable.

    Skipped where POSIX modes are not meaningful. This is a real check, not a
    warning: the password grants console access to a server.

    Group-readable is allowed, and that is deliberate rather than lax. The
    daemon runs as an unprivileged service user and has to read this file, so
    forbidding the group bit leaves exactly one arrangement that works --
    owned by the service user, mode 0600 -- and that is the *weaker* of the
    two options, because a compromised daemon could then rewrite its own
    config to point at another host and press keys at it.

    Allowing 0640 root:boot-err-shim means the daemon can read the file and
    cannot alter it. Refusing that pushed operators towards the arrangement
    with fewer guarantees, which is the opposite of what a permission check
    is for. World-readable is still refused outright: that is the case where
    any local user learns the password.
    """
    if not has_password or os.name != "posix":
        return
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ConfigError(f"{path}: cannot stat: {exc}") from exc
    if mode & stat.S_IROTH:
        raise ConfigError(
            f"{path}: contains vnc.password but is world-readable "
            f"(mode {stat.S_IMODE(mode):04o}); run: chmod o-r {path}"
        )


def parse_config(
    data: dict[str, Any],
    *,
    source_path: Path | None = None,
    defaults: PlatformDefaults | None = None,
) -> Config:
    """Validate a parsed TOML mapping into a :class:`Config`."""
    plat = defaults or platform_defaults()
    root = _Table(data, "")

    state_t = root.table("state")
    state_dir = state_t.path("dir", plat.state_dir)
    state_t.finish()

    target_t = root.table("target")
    host = target_t.str_("host")
    if not host.strip():
        raise ConfigError("target.host: must not be empty")
    target_t.finish()

    ping_t = root.table("ping")
    ping = PingConfig(
        interval=ping_t.duration("interval", 120),
        retry_interval=ping_t.duration("retry_interval", 120),
        threshold=ping_t.int_("threshold", 3, 1, 1000),
        command=ping_t.str_list("command", plat.ping_command),
        timeout=ping_t.duration("timeout", 15),
    )
    ping_t.finish()
    if not ping.command:
        raise ConfigError("ping.command: must not be empty")
    if not any("{host}" in part for part in ping.command):
        raise ConfigError("ping.command: must contain a {host} placeholder")

    vnc_t = root.table("vnc")
    vnc = VncConfig(
        host=vnc_t.str_("host"),
        port=vnc_t.int_("port", 5901, 1, 65535),
        password=vnc_t.opt_str("password"),
        tls=vnc_t.bool_("tls", False),
        tls_verify=vnc_t.bool_("tls_verify", False),
        tls_ca=vnc_t.opt_path("tls_ca"),
        connect_timeout=vnc_t.duration("connect_timeout", 10),
        read_timeout=vnc_t.duration("read_timeout", 30),
    )
    vnc_t.finish()
    if not vnc.host.strip():
        raise ConfigError("vnc.host: must not be empty")
    if (vnc.tls_verify or vnc.tls_ca is not None) and not vnc.tls:
        # Otherwise the operator believes the connection is verified when it
        # is not even encrypted.
        raise ConfigError("vnc.tls_verify/vnc.tls_ca require vnc.tls = true")

    detect_t = root.table("detect")
    text = detect_t.str_("text")
    key = detect_t.str_("key", "Y")
    detect = DetectConfig(
        text=text,
        key=key,
        keysym=resolve_keysym(key),
        engine=detect_t.choice("engine", "calibrated", VALID_ENGINES),
        calibration=detect_t.path("calibration", state_dir / "calibration.toml"),
        tolerance=detect_t.float_("tolerance", 0.02, 0.0, 1.0),
    )
    detect_t.finish()
    if not detect.lines:
        raise ConfigError("detect.text: must contain at least one non-blank line")

    recovery_t = root.table("recovery")
    recovery = RecoveryConfig(
        interval=recovery_t.duration("interval", 60),
        post_fix_sleep=recovery_t.duration("post_fix_sleep", 600),
        max_per_day=recovery_t.int_("max_per_day", 3, 0, 100000),
        notify_command=recovery_t.str_list("notify_command", ()),
    )
    recovery_t.finish()

    log_t = root.table("log")
    log = LogConfig(
        level=log_t.choice("level", "INFO", VALID_LEVELS),
        syslog=log_t.choice("syslog", "auto", VALID_SYSLOG),
        file=log_t.opt_path("file"),
        screenshot_dir=log_t.path("screenshot_dir", state_dir / "snapshots"),
        screenshot_keep=log_t.int_("screenshot_keep", 20, 1, 100000),
    )
    log_t.finish()

    root.finish()

    return Config(
        target=TargetConfig(host=host),
        ping=ping,
        vnc=vnc,
        detect=detect,
        recovery=recovery,
        log=log,
        state_dir=state_dir,
        source_path=source_path,
        defaults=plat,
    )


def load_config(
    path: Path | str,
    *,
    defaults: PlatformDefaults | None = None,
) -> Config:
    """Read and validate a config file."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: not valid UTF-8: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML: {exc}") from exc

    config = parse_config(data, source_path=path, defaults=defaults)
    _check_permissions(path, config.vnc.password is not None)
    return config
