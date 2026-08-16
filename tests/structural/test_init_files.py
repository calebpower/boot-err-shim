"""Tier 3: the init files and the code must agree about where things live.

This is the tier's clearest case. `StateDirectory=boot-err-shim` in a systemd
unit and `STATE_DIR_NAME` in platform_.py are the same fact written twice, in
two languages, in two files neither of which imports the other. Change one and
nothing fails: the unit creates a directory the daemon never looks at, the
daemon writes somewhere ProtectSystem=strict forbids, and the first sign of
trouble is a host that stays down because its calibration went missing.

Nothing else in the suite can catch that, because on this machine neither file
is ever executed.
"""

from __future__ import annotations

import re
import unittest

from boot_err_shim.platform_ import (
    CONFIG_FILE_NAME,
    STATE_DIR_NAME,
    platform_defaults,
)
from tests import REPO_ROOT

RC_SCRIPT = REPO_ROOT / "init" / "rc.d" / "boot_err_shim"
UNIT = REPO_ROOT / "init" / "boot-err-shim.service"


def unit_settings() -> dict[str, list[str]]:
    """Parse the unit into {key: [values]}, ignoring comments."""
    settings: dict[str, list[str]] = {}
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "[", ";")):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        settings.setdefault(key.strip(), []).append(value.strip())
    return settings


def rc_defaults() -> dict[str, str]:
    """Extract `: ${name:="value"}` defaults from the rc script."""
    pattern = re.compile(r'^:\s*\$\{(\w+):="([^"]*)"\}')
    out = {}
    for line in RC_SCRIPT.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line.strip())
        if match:
            out[match.group(1)] = match.group(2)
    return out


class TestFilesExist(unittest.TestCase):
    def test_both_init_files_are_present(self) -> None:
        self.assertTrue(RC_SCRIPT.is_file(), f"{RC_SCRIPT} is missing")
        self.assertTrue(UNIT.is_file(), f"{UNIT} is missing")

    def test_the_rc_script_has_unix_line_endings(self) -> None:
        # A CRLF rc script fails on FreeBSD with a bewildering "not found"
        # pointing at the interpreter, because the \r becomes part of the
        # path. .gitattributes pins this; here is the assertion that it held.
        self.assertNotIn(b"\r\n", RC_SCRIPT.read_bytes())

    def test_the_unit_has_unix_line_endings(self) -> None:
        self.assertNotIn(b"\r\n", UNIT.read_bytes())

    def test_every_shell_script_has_unix_line_endings(self) -> None:
        """Not just the init files -- anything an interpreter reads.

        containers/stages.sh acquired CRLF from a tooling step on Windows and
        dash rejected it with `set: Illegal option -`, because the `\\r` had
        become part of the option. .gitattributes normalises this on the way
        into git, which is precisely why it does not protect the working tree,
        and the working tree is what the container mounts.
        """
        scripts = sorted(
            path
            for path in REPO_ROOT.rglob("*.sh")
            if ".git" not in path.parts and "build" not in path.parts
        )
        scripts.append(RC_SCRIPT)
        self.assertGreaterEqual(len(scripts), 2, "no scripts found to check")

        for path in scripts:
            with self.subTest(script=path.relative_to(REPO_ROOT).as_posix()):
                self.assertNotIn(b"\r\n", path.read_bytes())

    def test_every_shell_script_starts_with_an_interpreter_line(self) -> None:
        for path in sorted(REPO_ROOT.rglob("*.sh")):
            if ".git" in path.parts:
                continue
            with self.subTest(script=path.name):
                self.assertTrue(path.read_bytes().startswith(b"#!"))


class TestSystemdUnit(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = unit_settings()
        self.linux = platform_defaults("Linux")

    def test_state_directory_matches_the_code(self) -> None:
        self.assertEqual(self.settings["StateDirectory"], [STATE_DIR_NAME])

    def test_state_directory_resolves_to_the_linux_default(self) -> None:
        # systemd puts StateDirectory under /var/lib for a system service.
        # Compared as POSIX strings: these are paths on the target, and on a
        # Windows development machine Path renders them with backslashes.
        from pathlib import PurePosixPath

        self.assertEqual(
            PurePosixPath("/var/lib") / self.settings["StateDirectory"][0],
            PurePosixPath(self.linux.state_dir.as_posix()),
        )

    def test_the_credential_points_at_the_linux_config_path(self) -> None:
        credential = self.settings["LoadCredential"][0]
        _name, source = credential.split(":", 1)
        self.assertEqual(source, self.linux.config_path.as_posix())
        self.assertTrue(source.endswith(CONFIG_FILE_NAME))

    def test_exec_lines_use_the_credential_rather_than_etc(self) -> None:
        # Reading /etc directly would fail under DynamicUser, since the file
        # is root-owned and mode 0600.
        for key in ("ExecStartPre", "ExecStart"):
            with self.subTest(key=key):
                self.assertIn("%d/config", self.settings[key][0])

    def test_the_config_is_validated_before_the_daemon_starts(self) -> None:
        self.assertIn("check-config", self.settings["ExecStartPre"][0])

    def test_the_daemon_command_is_run(self) -> None:
        self.assertIn(" run ", self.settings["ExecStart"][0])

    def test_it_runs_unprivileged(self) -> None:
        self.assertEqual(self.settings["DynamicUser"], ["yes"])
        self.assertEqual(self.settings["NoNewPrivileges"], ["yes"])
        self.assertEqual(self.settings["CapabilityBoundingSet"], [""])
        self.assertEqual(self.settings["AmbientCapabilities"], [""])

    def test_it_restarts(self) -> None:
        self.assertEqual(self.settings["Restart"], ["always"])

    def test_it_does_not_ask_systemd_to_daemonize_it(self) -> None:
        # The program runs in the foreground and never forks; Type=forking
        # would leave systemd waiting for a pid that never arrives.
        self.assertEqual(self.settings["Type"], ["simple"])

    def test_network_is_required(self) -> None:
        self.assertIn("network-online.target", self.settings["After"][0])

    def test_it_is_installable(self) -> None:
        self.assertEqual(self.settings["WantedBy"], ["multi-user.target"])


class TestRcScript(unittest.TestCase):
    def setUp(self) -> None:
        self.text = RC_SCRIPT.read_text(encoding="utf-8")
        self.defaults = rc_defaults()
        self.freebsd = platform_defaults("FreeBSD")

    def test_config_default_matches_the_code(self) -> None:
        self.assertEqual(
            self.defaults["boot_err_shim_config"],
            self.freebsd.config_path.as_posix(),
        )

    def test_rcvar_matches_the_name(self) -> None:
        self.assertIn('name="boot_err_shim"', self.text)
        self.assertIn('rcvar="boot_err_shim_enable"', self.text)

    def test_it_defaults_to_disabled(self) -> None:
        self.assertEqual(self.defaults["boot_err_shim_enable"], "NO")

    def test_it_declares_the_rc_metadata(self) -> None:
        for directive in ("# PROVIDE:", "# REQUIRE:", "# KEYWORD:"):
            with self.subTest(directive=directive):
                self.assertIn(directive, self.text)

    def test_it_provides_its_own_name(self) -> None:
        self.assertIn("# PROVIDE: boot_err_shim", self.text)

    def test_it_sources_rc_subr(self) -> None:
        self.assertIn(". /etc/rc.subr", self.text)
        self.assertIn('run_rc_command "$1"', self.text)

    def test_it_runs_unprivileged(self) -> None:
        self.assertIn("-u ${boot_err_shim_user}", self.text)
        self.assertNotEqual(self.defaults["boot_err_shim_user"], "root")

    def test_it_validates_the_config_before_forking(self) -> None:
        self.assertIn("check-config", self.text)
        self.assertIn("start_precmd=", self.text)

    def test_the_pidfile_is_not_directly_in_var_run(self) -> None:
        """It must live somewhere the service user can write.

        daemon(8) drops to the service user before opening the -P file, and
        /var/run is root-owned 0755, so a pidfile placed straight in there
        fails with "ppid file: Permission denied" -- a message that names
        neither this service nor the reason.
        """
        self.assertNotIn('pidfile="/var/run/${name}.pid"', self.text)
        self.assertIn('piddir="/var/run/${name}"', self.text)
        self.assertIn('pidfile="${piddir}/', self.text)

    def test_the_pidfile_directory_is_created_owned_by_the_service_user(self) -> None:
        # /var/run does not survive a reboot, so this has to happen on every
        # start rather than at install time.
        self.assertIn('install -d -o "${boot_err_shim_user}"', self.text)
        self.assertIn('"${piddir}"', self.text)

        # It has to be in start_precmd specifically, not merely somewhere in
        # the file: anywhere later and daemon(8) has already tried to write.
        body = self.text.split("boot_err_shim_precmd()", 1)[1]
        body = body.split("boot_err_shim_postcmd()", 1)[0]
        self.assertIn("install -d", body)
        self.assertIn("${piddir}", body)

    def test_it_supervises_with_daemon(self) -> None:
        self.assertIn("/usr/sbin/daemon", self.text)
        self.assertIn("-r ", self.text)

    def test_the_binary_path_matches_the_makefile(self) -> None:
        # install-common puts it in $(PREFIX)/sbin with PREFIX=/usr/local.
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("$(DESTDIR)$(PREFIX)/sbin/boot-err-shim", makefile)
        self.assertIn("/usr/local/sbin/boot-err-shim", self.text)

    def test_the_unit_uses_the_same_binary_path(self) -> None:
        for value in unit_settings()["ExecStart"]:
            self.assertTrue(value.startswith("/usr/local/sbin/boot-err-shim"))


class TestMakefileInstallsWhatExists(unittest.TestCase):
    def setUp(self) -> None:
        self.makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    def test_freebsd_target_installs_the_rc_script(self) -> None:
        self.assertIn("init/rc.d/boot_err_shim", self.makefile)

    def test_linux_target_installs_the_unit(self) -> None:
        self.assertIn("init/boot-err-shim.service", self.makefile)

    def test_both_targets_install_the_sample_config(self) -> None:
        self.assertGreaterEqual(
            self.makefile.count("boot-err-shim.conf.sample"), 2
        )


class TestReadmeMatchesTheCli(unittest.TestCase):
    """Every subcommand documented, and nothing documented that does not exist."""

    def setUp(self) -> None:
        from boot_err_shim.cli import COMMANDS

        self.commands = set(COMMANDS)
        self.readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    def test_the_command_table_is_not_empty(self) -> None:
        self.assertGreaterEqual(len(self.commands), 5)

    def test_every_command_is_documented(self) -> None:
        for command in self.commands:
            with self.subTest(command=command):
                self.assertIn(
                    f"`{command}`",
                    self.readme,
                    f"{command} is not mentioned in README.md",
                )

    def test_the_readme_documents_no_command_that_does_not_exist(self) -> None:
        # Commands are listed in a table as | `name` | description |
        documented = set(re.findall(r"^\| `([a-z-]+)` \|", self.readme, re.M))
        self.assertTrue(documented, "no command table found in README.md")
        self.assertEqual(documented - self.commands, set())


if __name__ == "__main__":
    unittest.main()
