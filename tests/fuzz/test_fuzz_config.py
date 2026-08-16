"""Tier 7: mutated configuration files.

A config is edited by hand at three in the morning, so it will be malformed in
every way a person can manage. Every one of those must produce a ConfigError
naming the problem, never a traceback and never -- the dangerous case -- a
Config object with a nonsense value in it that the daemon then acts on.
"""

from __future__ import annotations

import tomllib
import unittest

from boot_err_shim.config import Config, parse_config
from boot_err_shim.errors import ConfigError
from boot_err_shim.platform_ import platform_defaults
from tests.fuzz import iterations, rng

LINUX = platform_defaults("Linux")

VALID = """
[target]
host = "10.0.0.50"

[ping]
interval       = 120
retry_interval = 120
threshold      = 3
timeout        = 15

[vnc]
host            = "10.0.0.51"
port            = 5901
password        = "secret12"
tls             = false
connect_timeout = 10
read_timeout    = 30

[detect]
text = "Please press 'Y' to continue."
key       = "Y"
engine    = "calibrated"
tolerance = 0.02

[recovery]
interval       = 60
post_fix_sleep = 600
max_per_day    = 3
notify_command = []

[log]
level           = "INFO"
syslog          = "auto"
screenshot_keep = 20
"""

#: Values chosen to be the kinds of wrong a person actually types.
NASTY_VALUES = [
    "0", "-1", '""', '"   "', "true", "false", "1.5", "[]", "{}",
    '"1e400"', "999999999999999999999", '"-0"', '"0s"', '"abc"',
    '"NaN"', '"inf"', "[1, 2]", '["a"]', '"\\u0000"', '"' + "x" * 500 + '"',
]

NASTY_KEYS = [
    "threshold", "interval", "port", "tolerance", "text", "key", "engine",
    "level", "syslog", "max_per_day", "screenshot_keep", "host", "password",
    "tls", "command", "notify_command", "post_fix_sleep", "timeout",
]


def build(random) -> str:
    """Produce a config file that is valid-ish but probably wrong somewhere."""
    lines = VALID.splitlines()
    for _ in range(random.randrange(1, 4)):
        choice = random.randrange(5)

        if choice == 0:  # replace a value
            candidates = [
                index
                for index, line in enumerate(lines)
                if "=" in line and not line.strip().startswith("#")
            ]
            index = random.choice(candidates)
            key = lines[index].split("=")[0].rstrip()
            lines[index] = f"{key}= {random.choice(NASTY_VALUES)}"
        elif choice == 1:  # delete a line
            if lines:
                del lines[random.randrange(len(lines))]
        elif choice == 2:  # add an unexpected key
            lines.append(f"{random.choice(NASTY_KEYS)}_typo = 1")
        elif choice == 3:  # add a whole unexpected table
            lines.append(f"[section{random.randrange(100)}]")
            lines.append("x = 1")
        else:  # duplicate a line
            if lines:
                index = random.randrange(len(lines))
                lines.insert(index, lines[index])

    return "\n".join(lines)


class TestFuzzConfig(unittest.TestCase):
    def test_no_mutated_config_escapes_as_an_untyped_exception(self) -> None:
        random = rng("config")
        accepted = 0
        rejected = 0

        for _ in range(iterations()):
            text = build(random)
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                # Malformed TOML is load_config's problem, not parse_config's;
                # covered by tier 1.
                continue

            try:
                config = parse_config(data, defaults=LINUX)
            except ConfigError:
                rejected += 1
            except Exception as exc:  # noqa: BLE001
                self.fail(
                    f"untyped {type(exc).__name__}: {exc}\n--- config ---\n{text}"
                )
            else:
                accepted += 1
                self.check_sane(config, text)

        self.assertGreater(rejected, 0, "no mutation was ever rejected")
        self.assertGreater(accepted, 0, "no mutation was ever accepted")

    def check_sane(self, config: Config, text: str) -> None:
        """Anything accepted must be safe for the daemon to act on.

        This is the half that matters. A rejected config is harmless; an
        accepted one with a zero interval is a busy loop against somebody's
        iDRAC, and an accepted one with a zero threshold acts before the first
        failed ping.
        """
        context = f"\n--- accepted config ---\n{text}"
        self.assertGreater(config.ping.interval, 0, context)
        self.assertGreater(config.ping.retry_interval, 0, context)
        self.assertGreater(config.ping.timeout, 0, context)
        self.assertGreaterEqual(config.ping.threshold, 1, context)
        self.assertGreater(config.recovery.interval, 0, context)
        self.assertGreater(config.recovery.post_fix_sleep, 0, context)
        self.assertGreaterEqual(config.recovery.max_per_day, 0, context)
        self.assertTrue(1 <= config.vnc.port <= 65535, context)
        self.assertGreater(config.vnc.connect_timeout, 0, context)
        self.assertGreater(config.vnc.read_timeout, 0, context)
        self.assertTrue(0.0 <= config.detect.tolerance <= 1.0, context)
        self.assertGreaterEqual(config.log.screenshot_keep, 1, context)
        self.assertTrue(config.target.host.strip(), context)
        self.assertTrue(config.vnc.host.strip(), context)
        self.assertTrue(config.detect.lines, context)
        self.assertTrue(
            any("{host}" in part for part in config.ping.command), context
        )
        self.assertIn(config.detect.engine, ("calibrated", "ocr"), context)

    def test_every_rejection_names_something(self) -> None:
        random = rng("config-messages")
        for _ in range(iterations(150)):
            text = build(random)
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                continue
            try:
                parse_config(data, defaults=LINUX)
            except ConfigError as exc:
                message = str(exc)
                self.assertTrue(message.strip(), f"empty message for:\n{text}")
                # Should point at a setting, not just say "invalid".
                self.assertGreater(len(message), 10, message)


if __name__ == "__main__":
    unittest.main()
