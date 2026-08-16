"""Tier 1: configuration parsing and validation.

Boundaries rather than middles -- threshold at 0/1/1000/1001, tolerance at the
exact limits, durations at zero and below. Plus the rule that makes the rest
trustworthy: unknown keys are rejected, so a typo can never masquerade as a
setting that took effect.
"""

from __future__ import annotations

import stat
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from boot_err_shim.config import (
    MAX_DURATION_SECONDS,
    _check_permissions,
    load_config,
    parse_config,
    parse_duration,
    resolve_keysym,
)
from boot_err_shim.errors import ConfigError
from boot_err_shim.platform_ import platform_defaults

MINIMAL = """
[target]
host = "10.0.0.50"

[vnc]
host = "10.0.0.51"

[detect]
text = "Please press 'Y' to continue."
"""

LINUX = platform_defaults("Linux")


def build(extra: str = "", base: str = MINIMAL):
    """Parse ``base``, overlay ``extra``, validate.

    The two fragments are parsed separately and merged one table deep, because
    concatenating them would redeclare table headers that TOML forbids.
    """
    data = tomllib.loads(base)
    for table, values in tomllib.loads(extra).items():
        if isinstance(values, dict) and isinstance(data.get(table), dict):
            data[table] = {**data[table], **values}
        else:
            data[table] = values
    return parse_config(data, defaults=LINUX)


class TestMinimalAndDefaults(unittest.TestCase):
    def test_minimal_config_is_valid(self) -> None:
        config = build()
        self.assertEqual(config.target.host, "10.0.0.50")
        self.assertEqual(config.vnc.host, "10.0.0.51")

    def test_defaults_match_the_documented_values(self) -> None:
        config = build()
        self.assertEqual(config.ping.interval, 120)
        self.assertEqual(config.ping.retry_interval, 120)
        self.assertEqual(config.ping.threshold, 3)
        self.assertEqual(config.vnc.port, 5901)
        self.assertFalse(config.vnc.tls)
        self.assertEqual(config.detect.key, "Y")
        self.assertEqual(config.detect.engine, "calibrated")
        self.assertAlmostEqual(config.detect.tolerance, 0.02)
        self.assertEqual(config.recovery.interval, 60)
        self.assertEqual(config.recovery.post_fix_sleep, 600)
        self.assertEqual(config.recovery.max_per_day, 3)
        self.assertEqual(config.log.level, "INFO")
        self.assertEqual(config.log.screenshot_keep, 20)

    def test_platform_defaults_flow_into_paths(self) -> None:
        config = build()
        self.assertEqual(config.state_dir, Path("/var/lib/boot-err-shim"))
        self.assertEqual(
            config.detect.calibration, Path("/var/lib/boot-err-shim/calibration.toml")
        )
        self.assertEqual(
            config.log.screenshot_dir, Path("/var/lib/boot-err-shim/snapshots")
        )

    def test_the_state_directory_is_configurable(self) -> None:
        # Found by the concurrency tier: with the state directory fixed to a
        # platform default, the lock file could not be pointed anywhere else,
        # so a second instance locked a path nobody else was using.
        config = build('\n[state]\ndir = "/srv/shim"\n')
        self.assertEqual(config.state_dir, Path("/srv/shim"))
        self.assertEqual(config.detect.calibration, Path("/srv/shim/calibration.toml"))
        self.assertEqual(config.log.screenshot_dir, Path("/srv/shim/snapshots"))
        self.assertEqual(config.lock_path.parent, Path("/srv/shim"))
        self.assertEqual(config.history_path.parent, Path("/srv/shim"))

    def test_explicit_paths_still_override_the_state_directory(self) -> None:
        config = build(
            '\n[state]\ndir = "/srv/shim"\n'
            '\n[detect]\ncalibration = "/etc/cal.toml"\n'
            '\n[log]\nscreenshot_dir = "/var/tmp/shots"\n'
        )
        self.assertEqual(config.detect.calibration, Path("/etc/cal.toml"))
        self.assertEqual(config.log.screenshot_dir, Path("/var/tmp/shots"))


class TestPerInstancePaths(unittest.TestCase):
    """The lock names the console; the history names the target.

    Both were a single fixed filename until the concurrency tier showed what
    that costs: two daemons watching different hosts on one machine would
    contend for one lock and share one intervention count.
    """

    def test_the_lock_is_named_after_the_console(self) -> None:
        # The console is the resource being protected -- two daemons must not
        # both press a key at the same iDRAC, whatever they are watching.
        config = build()
        self.assertEqual(
            config.lock_path, Path("/var/lib/boot-err-shim/10.0.0.51-5901.lock")
        )

    def test_different_consoles_get_different_locks(self) -> None:
        first = build('\n[vnc]\nhost = "10.0.0.51"\n')
        second = build('\n[vnc]\nhost = "10.0.0.52"\n')
        self.assertNotEqual(first.lock_path, second.lock_path)

    def test_the_same_console_on_a_different_port_is_a_different_lock(self) -> None:
        first = build("\n[vnc]\nport = 5901\n")
        second = build("\n[vnc]\nport = 5902\n")
        self.assertNotEqual(first.lock_path, second.lock_path)

    def test_the_same_console_from_two_configs_shares_one_lock(self) -> None:
        # The case that matters: two different config files aimed at one
        # iDRAC must still be mutually exclusive.
        first = build('\n[target]\nhost = "10.0.0.60"\n')
        second = build('\n[target]\nhost = "10.0.0.61"\n')
        self.assertEqual(first.lock_path, second.lock_path)

    def test_the_history_is_named_after_the_target(self) -> None:
        config = build()
        self.assertEqual(
            config.history_path,
            Path("/var/lib/boot-err-shim/10.0.0.50.history.json"),
        )

    def test_different_targets_get_different_histories(self) -> None:
        first = build('\n[target]\nhost = "a.example"\n')
        second = build('\n[target]\nhost = "b.example"\n')
        self.assertNotEqual(first.history_path, second.history_path)

    def test_hostnames_are_made_safe_for_a_filename(self) -> None:
        # IPv6 literals and anything else with separators in it must not
        # produce a path with directories in the middle of it.
        config = build('\n[vnc]\nhost = "fe80::1%eth0"\n')
        self.assertEqual(config.lock_path.parent, config.state_dir)
        self.assertNotIn("/", config.lock_path.name)
        self.assertNotIn("\\", config.lock_path.name)
        self.assertNotIn(":", config.lock_path.name)

    def test_password_defaults_to_absent_not_empty(self) -> None:
        # None and "" mean different things to the RFB handshake: no password
        # at all versus a zero-length one.
        self.assertIsNone(build().vnc.password)


class TestUnknownKeys(unittest.TestCase):
    """A silently ignored typo is a setting the operator wrongly believes in."""

    def test_unknown_top_level_table(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            build("\n[nonsense]\nx = 1\n")
        self.assertIn("nonsense", str(caught.exception))

    def test_unknown_key_in_ping(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            build("\n[ping]\nthreshhold = 3\n")
        self.assertIn("threshhold", str(caught.exception))

    def test_unknown_key_in_every_table(self) -> None:
        for table in ("target", "vnc", "detect", "recovery", "log"):
            with self.subTest(table=table), self.assertRaises(ConfigError):
                build(f"\n[{table}]\nbogus_key = 1\n")

    def test_error_names_the_table(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            build("\n[recovery]\nbogus = 1\n")
        self.assertIn("recovery", str(caught.exception))


class TestThresholdBoundaries(unittest.TestCase):
    def test_zero_is_rejected(self) -> None:
        # A threshold of 0 would mean acting before any failed ping at all.
        with self.assertRaises(ConfigError):
            build("\n[ping]\nthreshold = 0\n")

    def test_one_is_the_minimum_accepted(self) -> None:
        self.assertEqual(build("\n[ping]\nthreshold = 1\n").ping.threshold, 1)

    def test_three_is_the_documented_default(self) -> None:
        self.assertEqual(build("\n[ping]\nthreshold = 3\n").ping.threshold, 3)

    def test_upper_bound_and_one_past_it(self) -> None:
        self.assertEqual(build("\n[ping]\nthreshold = 1000\n").ping.threshold, 1000)
        with self.assertRaises(ConfigError):
            build("\n[ping]\nthreshold = 1001\n")

    def test_negative_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            build("\n[ping]\nthreshold = -1\n")

    def test_boolean_is_not_an_integer(self) -> None:
        # bool is a subclass of int in Python; `threshold = true` must not
        # quietly become 1.
        with self.assertRaises(ConfigError):
            build("\n[ping]\nthreshold = true\n")


class TestToleranceBoundaries(unittest.TestCase):
    def test_exact_limits_are_accepted(self) -> None:
        self.assertEqual(build("\n[detect]\ntolerance = 0.0\n").detect.tolerance, 0.0)
        self.assertEqual(build("\n[detect]\ntolerance = 1.0\n").detect.tolerance, 1.0)

    def test_just_outside_the_limits_is_rejected(self) -> None:
        for value in ("-0.01", "1.01"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                build(f"\n[detect]\ntolerance = {value}\n")

    def test_integer_is_accepted_as_a_number(self) -> None:
        self.assertEqual(build("\n[detect]\ntolerance = 1\n").detect.tolerance, 1.0)

    def test_boolean_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            build("\n[detect]\ntolerance = true\n")


class TestPortBoundaries(unittest.TestCase):
    def test_valid_edges(self) -> None:
        self.assertEqual(build("\n[vnc]\nport = 1\n").vnc.port, 1)
        self.assertEqual(build("\n[vnc]\nport = 65535\n").vnc.port, 65535)

    def test_invalid_edges(self) -> None:
        for value in ("0", "65536", "-1"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                build(f"\n[vnc]\nport = {value}\n")


class TestDurations(unittest.TestCase):
    def test_plain_seconds(self) -> None:
        self.assertEqual(parse_duration(90, "x"), 90)

    def test_suffixes(self) -> None:
        self.assertEqual(parse_duration("90s", "x"), 90)
        self.assertEqual(parse_duration("2m", "x"), 120)
        self.assertEqual(parse_duration("1h", "x"), 3600)
        self.assertEqual(parse_duration("1d", "x"), 86400)

    def test_string_without_suffix(self) -> None:
        self.assertEqual(parse_duration("45", "x"), 45)

    def test_whitespace_and_case(self) -> None:
        self.assertEqual(parse_duration("  10 M  ", "x"), 600)

    def test_fractional_unit_resolves_to_whole_seconds(self) -> None:
        self.assertEqual(parse_duration("1.5m", "x"), 90)

    def test_zero_and_negative_are_rejected(self) -> None:
        # A zero interval is a busy loop against somebody's iDRAC.
        for value in (0, -1, "0s", "-5m"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse_duration(value, "x")

    def test_unparseable_is_rejected(self) -> None:
        for value in ("abc", "", "   ", "m", "1x"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse_duration(value, "x")

    def test_boolean_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            parse_duration(True, "x")

    def test_non_integral_float_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            parse_duration(1.5, "x")

    def test_infinity_is_rejected(self) -> None:
        # Found by the fuzz tier: float("1e400") is inf, and int(inf) raises
        # OverflowError, which is not a ConfigError and so escaped the CLI's
        # error handling entirely as a traceback.
        for value in ("1e400", "-1e400", "inf", "-inf", "Infinity"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse_duration(value, "x")

    def test_nan_is_rejected(self) -> None:
        for value in ("nan", "NaN", "-nan"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse_duration(value, "x")

    def test_non_finite_floats_are_rejected(self) -> None:
        for value in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse_duration(value, "x")

    def test_absurdly_long_durations_are_rejected(self) -> None:
        # A stray digit should be reported, not silently park the daemon
        # until after the hardware has been replaced.
        for value in (999999999999999999999, "9999d", MAX_DURATION_SECONDS + 1):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse_duration(value, "x")

    def test_the_maximum_itself_is_accepted(self) -> None:
        self.assertEqual(
            parse_duration(MAX_DURATION_SECONDS, "x"), MAX_DURATION_SECONDS
        )

    def test_error_names_the_setting(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            parse_duration("nope", "ping.interval")
        self.assertIn("ping.interval", str(caught.exception))

    def test_durations_work_through_the_config(self) -> None:
        config = build('\n[recovery]\npost_fix_sleep = "10m"\n')
        self.assertEqual(config.recovery.post_fix_sleep, 600)


class TestKeysym(unittest.TestCase):
    def test_single_printable_character(self) -> None:
        self.assertEqual(resolve_keysym("Y"), 0x59)
        self.assertEqual(resolve_keysym("y"), 0x79)
        self.assertEqual(resolve_keysym(" "), 0x20)
        self.assertEqual(resolve_keysym("~"), 0x7E)

    def test_named_keys(self) -> None:
        self.assertEqual(resolve_keysym("Return"), 0xFF0D)
        self.assertEqual(resolve_keysym("Enter"), 0xFF0D)
        self.assertEqual(resolve_keysym("Escape"), 0xFF1B)
        self.assertEqual(resolve_keysym("F12"), 0xFFC9)

    def test_names_are_case_insensitive(self) -> None:
        self.assertEqual(resolve_keysym("return"), 0xFF0D)
        self.assertEqual(resolve_keysym("ESCAPE"), 0xFF1B)

    def test_empty_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            resolve_keysym("")

    def test_unknown_name_is_rejected_and_lists_the_known_ones(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            resolve_keysym("Meta")
        self.assertIn("Return", str(caught.exception))

    def test_non_printable_single_character_is_rejected(self) -> None:
        for char in ("\n", "\t", "\x00", "\x7f", "é"):
            with self.subTest(char=repr(char)), self.assertRaises(ConfigError):
                resolve_keysym(char)

    def test_non_string_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            resolve_keysym(89)

    def test_keysym_reaches_the_config(self) -> None:
        self.assertEqual(build().detect.keysym, 0x59)
        self.assertEqual(build('\n[detect]\nkey = "Return"\n').detect.keysym, 0xFF0D)


class TestDetectText(unittest.TestCase):
    def test_lines_are_stripped_and_blanks_dropped(self) -> None:
        config = build(
            base="""
[target]
host = "h"
[vnc]
host = "v"
[detect]
text = \"\"\"

  Disabling writes to flash as the flash part has gone bad.

    Please press 'Y' to continue.
\"\"\"
"""
        )
        self.assertEqual(
            config.detect.lines,
            (
                "Disabling writes to flash as the flash part has gone bad.",
                "Please press 'Y' to continue.",
            ),
        )

    def test_blank_text_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            build(
                base='[target]\nhost="h"\n[vnc]\nhost="v"\n[detect]\ntext = "  \\n  "\n'
            )

    def test_missing_text_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            build(base='[target]\nhost="h"\n[vnc]\nhost="v"\n')

    def test_apostrophe_survives(self) -> None:
        # The real message contains one, and this suite also runs under LANG=C.
        self.assertIn("'Y'", build().detect.lines[0])


class TestRequiredAndEmptyValues(unittest.TestCase):
    def test_missing_target_host(self) -> None:
        with self.assertRaises(ConfigError):
            build(base='[vnc]\nhost="v"\n[detect]\ntext="x"\n')

    def test_blank_target_host(self) -> None:
        with self.assertRaises(ConfigError):
            build(base='[target]\nhost="  "\n[vnc]\nhost="v"\n[detect]\ntext="x"\n')

    def test_blank_vnc_host(self) -> None:
        with self.assertRaises(ConfigError):
            build(base='[target]\nhost="h"\n[vnc]\nhost=""\n[detect]\ntext="x"\n')

    def test_wrong_type_for_host(self) -> None:
        with self.assertRaises(ConfigError):
            build(base='[target]\nhost=5\n[vnc]\nhost="v"\n[detect]\ntext="x"\n')


class TestPingCommand(unittest.TestCase):
    def test_override_is_accepted(self) -> None:
        config = build('\n[ping]\ncommand = ["/sbin/ping", "-c", "1", "{host}"]\n')
        self.assertEqual(config.ping.command, ("/sbin/ping", "-c", "1", "{host}"))

    def test_empty_command_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            build("\n[ping]\ncommand = []\n")

    def test_command_without_host_placeholder_is_rejected(self) -> None:
        # Otherwise the daemon would cheerfully ping nothing forever and
        # report the target as up.
        with self.assertRaises(ConfigError) as caught:
            build('\n[ping]\ncommand = ["ping", "-c", "1"]\n')
        self.assertIn("{host}", str(caught.exception))

    def test_non_string_elements_are_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            build('\n[ping]\ncommand = ["ping", 1, "{host}"]\n')

    def test_default_comes_from_the_platform(self) -> None:
        self.assertEqual(
            build().ping.command, platform_defaults("Linux").ping_command
        )


class TestChoices(unittest.TestCase):
    def test_engine(self) -> None:
        self.assertEqual(build('\n[detect]\nengine = "ocr"\n').detect.engine, "ocr")
        with self.assertRaises(ConfigError):
            build('\n[detect]\nengine = "magic"\n')

    def test_level(self) -> None:
        self.assertEqual(build('\n[log]\nlevel = "DEBUG"\n').log.level, "DEBUG")
        with self.assertRaises(ConfigError):
            build('\n[log]\nlevel = "debug"\n')  # case matters; be strict

    def test_syslog(self) -> None:
        for value in ("auto", "always", "never"):
            with self.subTest(value=value):
                self.assertEqual(build(f'\n[log]\nsyslog = "{value}"\n').log.syslog, value)
        with self.assertRaises(ConfigError):
            build('\n[log]\nsyslog = "maybe"\n')

    def test_boolean_for_syslog_is_rejected_with_a_useful_message(self) -> None:
        # It used to be a bool in an earlier draft of the sample config.
        with self.assertRaises(ConfigError) as caught:
            build("\n[log]\nsyslog = true\n")
        self.assertIn("syslog", str(caught.exception))


class TestScreenshotAndRateBoundaries(unittest.TestCase):
    def test_screenshot_keep_lower_bound(self) -> None:
        self.assertEqual(build("\n[log]\nscreenshot_keep = 1\n").log.screenshot_keep, 1)
        with self.assertRaises(ConfigError):
            build("\n[log]\nscreenshot_keep = 0\n")

    def test_max_per_day_zero_means_never_warn(self) -> None:
        self.assertEqual(build("\n[recovery]\nmax_per_day = 0\n").recovery.max_per_day, 0)

    def test_max_per_day_negative_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            build("\n[recovery]\nmax_per_day = -1\n")

    def test_notify_command_defaults_empty_and_accepts_a_list(self) -> None:
        self.assertEqual(build().recovery.notify_command, ())
        self.assertEqual(
            build('\n[recovery]\nnotify_command = ["/bin/page", "-n"]\n')
            .recovery.notify_command,
            ("/bin/page", "-n"),
        )


class TestLoadConfig(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

    def write(self, text: str, name: str = "c.toml") -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_round_trip(self) -> None:
        config = load_config(self.write(MINIMAL), defaults=LINUX)
        self.assertEqual(config.target.host, "10.0.0.50")
        self.assertEqual(config.source_path, self.dir / "c.toml")

    def test_missing_file(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            load_config(self.dir / "absent.toml", defaults=LINUX)
        self.assertIn("absent.toml", str(caught.exception))

    def test_invalid_toml(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            load_config(self.write("[target\nhost = "), defaults=LINUX)
        self.assertIn("not valid TOML", str(caught.exception))

    def test_invalid_utf8(self) -> None:
        path = self.dir / "bad.toml"
        path.write_bytes(b'[target]\nhost = "\xff\xfe"\n')
        with self.assertRaises(ConfigError) as caught:
            load_config(path, defaults=LINUX)
        self.assertIn("UTF-8", str(caught.exception))

    def test_the_shipped_sample_is_valid(self) -> None:
        # Deployed the way the README tells you to: copied into place and
        # chmod 600. Loading it straight out of the repo would fail on POSIX
        # for the right reason -- a checked-in file is world-readable, and it
        # carries a placeholder password -- which says nothing about whether
        # the sample itself is well-formed.
        import os
        import shutil

        from tests import REPO_ROOT

        deployed = self.dir / "boot-err-shim.conf"
        shutil.copyfile(REPO_ROOT / "boot-err-shim.conf.sample", deployed)
        if os.name == "posix":
            deployed.chmod(0o600)

        config = load_config(deployed, defaults=LINUX)
        self.assertEqual(config.detect.key, "Y")
        self.assertEqual(len(config.detect.lines), 3)

    def test_the_shipped_sample_is_rejected_when_left_world_readable(self) -> None:
        # The other half of the same claim: an operator who skips the chmod
        # gets told, rather than silently running with an exposed password.
        import os
        import shutil

        from tests import REPO_ROOT

        if os.name != "posix":
            self.skipTest(
                "POSIX file modes only; the Linux container tier covers this "
                "branch, and test_world_readable_with_a_password_is_rejected "
                "covers the logic itself on every platform"
            )

        deployed = self.dir / "loose.conf"
        shutil.copyfile(REPO_ROOT / "boot-err-shim.conf.sample", deployed)
        deployed.chmod(0o644)
        with self.assertRaises(ConfigError):
            load_config(deployed, defaults=LINUX)


class _FakeStat:
    def __init__(self, mode: int) -> None:
        self.st_mode = stat.S_IFREG | mode


class _FakePath:
    """Just enough Path for the permission check, so the POSIX-only branch is
    testable on any machine -- same reasoning as platform_defaults(system=...)."""

    def __init__(self, mode: int) -> None:
        self._mode = mode

    def stat(self) -> _FakeStat:
        return _FakeStat(self._mode)

    def __str__(self) -> str:
        return "/etc/boot-err-shim.conf"


class TestPasswordFilePermissions(unittest.TestCase):
    def test_world_readable_with_a_password_is_rejected(self) -> None:
        with mock.patch("boot_err_shim.config.os.name", "posix"), self.assertRaises(
            ConfigError
        ) as caught:
            _check_permissions(_FakePath(0o644), has_password=True)
        message = str(caught.exception)
        self.assertIn("0644", message)
        self.assertIn("chmod o-r", message)

    def test_group_readable_with_a_password_is_allowed(self) -> None:
        """0640 root:service is the better deployment, not a lapse.

        The daemon runs unprivileged and must read this file. Forbidding the
        group bit leaves only "owned by the service user, mode 0600", which
        is weaker: a compromised daemon could rewrite its own config to point
        at another host and press keys at it. Under 0640 root:boot-err-shim
        it can read and cannot alter.
        """
        with mock.patch("boot_err_shim.config.os.name", "posix"):
            _check_permissions(_FakePath(0o640), has_password=True)

    def test_world_readable_is_still_rejected(self) -> None:
        # The case that actually leaks: any local user learns the password.
        for mode in (0o604, 0o644, 0o666, 0o777):
            with self.subTest(mode=oct(mode)):
                with mock.patch("boot_err_shim.config.os.name", "posix"):
                    with self.assertRaises(ConfigError):
                        _check_permissions(_FakePath(mode), has_password=True)

    def test_the_rejection_says_how_to_fix_it(self) -> None:
        with mock.patch("boot_err_shim.config.os.name", "posix"):
            with self.assertRaises(ConfigError) as caught:
                _check_permissions(_FakePath(0o644), has_password=True)
        self.assertIn("chmod o-r", str(caught.exception))

    def test_owner_only_is_accepted(self) -> None:
        with mock.patch("boot_err_shim.config.os.name", "posix"):
            _check_permissions(_FakePath(0o600), has_password=True)
            _check_permissions(_FakePath(0o400), has_password=True)

    def test_loose_permissions_are_fine_without_a_password(self) -> None:
        with mock.patch("boot_err_shim.config.os.name", "posix"):
            _check_permissions(_FakePath(0o644), has_password=False)

    def test_skipped_where_posix_modes_are_meaningless(self) -> None:
        with mock.patch("boot_err_shim.config.os.name", "nt"):
            _check_permissions(_FakePath(0o777), has_password=True)


if __name__ == "__main__":
    unittest.main()
