"""Tier 1: the command line.

Three things worth testing here that nothing else covers.

**Exit codes.** An init script decides whether to start the daemon based on
what `check-config` returns, so the difference between 0 and 78 is load
bearing, not cosmetic.

**Error presentation.** A typed failure must become one line on stderr, not a
traceback. `--debug` is the escape hatch, and it has to actually work or
diagnosing anything means editing the source.

**The snapshot ring buffer.** Every examined frame is written to disk, once a
minute, forever. If eviction is wrong the state directory fills up and the
first symptom is a host that stays down because the daemon cannot write
anything -- which is a diagnostic feature causing an outage.
"""

from __future__ import annotations

import argparse
import io
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))

from boot_err_shim import cli  # noqa: E402
from boot_err_shim.calibrate import analyse  # noqa: E402
from boot_err_shim.errors import ConfigError, ShimError  # noqa: E402
from boot_err_shim.frame import Frame  # noqa: E402
from boot_err_shim.platform_ import platform_defaults  # noqa: E402
from render_frame import THE_MESSAGE, render  # noqa: E402
from tests.fakes import make_config  # noqa: E402


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Invoke main(), capturing streams. Returns (code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    except SystemExit as exc:  # argparse exits directly
        code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


class TestParsePair(unittest.TestCase):
    def test_wxh_form(self) -> None:
        self.assertEqual(cli._parse_pair("9x16"), (9, 16))

    def test_comma_form(self) -> None:
        self.assertEqual(cli._parse_pair("72,208"), (72, 208))

    def test_uppercase_x(self) -> None:
        self.assertEqual(cli._parse_pair("9X16"), (9, 16))

    def test_zero_is_accepted_here(self) -> None:
        # An origin of 0,0 is perfectly legitimate; range checking belongs to
        # the analysis, not the argument parser.
        self.assertEqual(cli._parse_pair("0,0"), (0, 0))

    def test_rejects_nonsense(self) -> None:
        for value in ("", "9", "abc", "9x", "x16", "9x16x20", "9,16,20", "a,b"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    cli._parse_pair(value)

    def test_the_error_explains_the_expected_form(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError) as caught:
            cli._parse_pair("nonsense")
        self.assertIn("9x16", str(caught.exception))


class TestConfigPathResolution(unittest.TestCase):
    def test_explicit_wins(self) -> None:
        self.assertEqual(
            cli.resolve_config_path(Path("/tmp/x.conf")), Path("/tmp/x.conf")
        )

    def test_default_is_the_platform_path(self) -> None:
        self.assertEqual(
            cli.resolve_config_path(None), platform_defaults().config_path
        )


class TestArgumentHandling(unittest.TestCase):
    def test_no_command_is_a_usage_error(self) -> None:
        code, _out, _err = run_cli([])
        self.assertEqual(code, 2)

    def test_unknown_command_is_a_usage_error(self) -> None:
        code, _out, _err = run_cli(["nonsense"])
        self.assertEqual(code, 2)

    def test_version(self) -> None:
        from boot_err_shim import __version__

        code, out, _err = run_cli(["--version"])
        self.assertEqual(code, 0)
        self.assertIn(__version__, out)

    def test_every_command_in_the_table_is_reachable(self) -> None:
        # A command in COMMANDS with no subparser is dead code; a subparser
        # with no COMMANDS entry crashes at dispatch.
        parser = cli.build_parser()
        subparsers = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        self.assertEqual(len(subparsers), 1)
        self.assertEqual(set(subparsers[0].choices), set(cli.COMMANDS))

    def test_help_is_available_for_every_command(self) -> None:
        for command in cli.COMMANDS:
            with self.subTest(command=command):
                code, out, _err = run_cli([command, "--help"])
                self.assertEqual(code, 0)
                self.assertIn(command, out)


class CliFixture(unittest.TestCase):
    """A valid config, a calibration, and a matching frame on disk."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

        self.frame = render(THE_MESSAGE, cell_width=9, cell_height=16)
        self.calibration_path = self.dir / "calibration.toml"
        analyse(self.frame, THE_MESSAGE).save(self.calibration_path)

        from boot_err_shim.png import write_frame

        self.image = write_frame(self.dir / "screen.png", self.frame)

        self.config_path = self.dir / "boot-err-shim.conf"
        self.write_config()

    def write_config(self, extra: str = "") -> None:
        posix = str(self.dir).replace("\\", "/")
        self.config_path.write_text(
            f"""
[state]
dir = "{posix}"

[target]
host = "10.0.0.50"

[vnc]
host = "10.0.0.51"

[detect]
calibration = "{posix}/calibration.toml"
text = \"\"\"
{THE_MESSAGE[0]}
{THE_MESSAGE[1]}
{THE_MESSAGE[2]}
\"\"\"
{extra}
""",
            encoding="utf-8",
        )
        import os

        if os.name == "posix":
            self.config_path.chmod(0o600)


class TestCheckConfig(CliFixture):
    def test_a_valid_config_exits_zero(self) -> None:
        code, out, _err = run_cli(["check-config", "-c", str(self.config_path)])
        self.assertEqual(code, 0)
        self.assertIn("OK", out)

    def test_it_reports_the_settings_that_matter(self) -> None:
        _code, out, _err = run_cli(["check-config", "-c", str(self.config_path)])
        self.assertIn("10.0.0.50", out)
        self.assertIn("10.0.0.51", out)
        self.assertIn("threshold", out)
        self.assertIn("0x59", out)

    def test_a_missing_file_exits_with_the_config_code(self) -> None:
        # An init script keys off this. EX_CONFIG is 78.
        code, _out, err = run_cli(["check-config", "-c", str(self.dir / "nope.conf")])
        self.assertEqual(code, ConfigError.exit_code)
        self.assertIn("boot-err-shim:", err)

    def test_an_invalid_config_exits_with_the_config_code(self) -> None:
        self.config_path.write_text("[target]\nhost = \n", encoding="utf-8")
        code, _out, _err = run_cli(["check-config", "-c", str(self.config_path)])
        self.assertEqual(code, ConfigError.exit_code)

    def test_an_unknown_key_is_reported_by_name(self) -> None:
        self.write_config(extra="\n[ping]\nthreshhold = 3\n")
        code, _out, err = run_cli(["check-config", "-c", str(self.config_path)])
        self.assertEqual(code, ConfigError.exit_code)
        self.assertIn("threshhold", err)

    def test_a_present_calibration_is_not_flagged_as_missing(self) -> None:
        _code, out, _err = run_cli(["check-config", "-c", str(self.config_path)])
        self.assertNotIn("[MISSING]", out)

    def test_an_absent_calibration_is_flagged_and_explained(self) -> None:
        self.calibration_path.unlink()
        code, out, err = run_cli(["check-config", "-c", str(self.config_path)])
        # Still a valid config -- just one the daemon will not act on.
        self.assertEqual(code, 0)
        self.assertIn("[MISSING]", out)
        self.assertIn("configure", err)


class TestErrorPresentation(CliFixture):
    def test_a_typed_error_is_one_line_not_a_traceback(self) -> None:
        _code, _out, err = run_cli(["check-config", "-c", str(self.dir / "nope.conf")])
        self.assertNotIn("Traceback", err)
        self.assertIn("boot-err-shim:", err)

    def test_debug_re_raises_so_the_error_can_be_diagnosed(self) -> None:
        with self.assertRaises(ShimError):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                cli.main(["--debug", "check-config", "-c", str(self.dir / "nope.conf")])

    def test_an_interrupt_exits_one_hundred_and_thirty(self) -> None:
        # The conventional 128 + SIGINT, so a shell reports it correctly.
        with mock.patch.dict(cli.COMMANDS, {"capture": mock.Mock(side_effect=KeyboardInterrupt)}):
            code, _out, err = run_cli(["capture", "-c", str(self.config_path)])
        self.assertEqual(code, 130)
        self.assertIn("interrupted", err)

    def test_each_error_class_keeps_its_own_exit_code(self) -> None:
        from boot_err_shim.errors import AuthError, CalibrationError, LockError

        for error in (ConfigError, CalibrationError, AuthError, LockError):
            with self.subTest(error=error.__name__):
                with mock.patch.dict(
                    cli.COMMANDS, {"capture": mock.Mock(side_effect=error("x"))}
                ):
                    code, _out, _err = run_cli(["capture", "-c", str(self.config_path)])
                self.assertEqual(code, error.exit_code)


class TestTestDetect(CliFixture):
    def test_a_matching_frame_exits_zero(self) -> None:
        code, out, _err = run_cli(
            ["test-detect", "-c", str(self.config_path), str(self.image)]
        )
        self.assertEqual(code, 0)
        self.assertIn("MATCH", out)

    def test_a_non_matching_frame_exits_one(self) -> None:
        # Nonzero on purpose: this is usable from a shell script.
        from boot_err_shim.png import write_frame

        other = write_frame(
            self.dir / "other.png", render(("No boot device available.",))
        )
        code, out, _err = run_cli(
            ["test-detect", "-c", str(self.config_path), str(other)]
        )
        self.assertEqual(code, 1)
        self.assertIn("NO MATCH", out)

    def test_a_missing_image_is_an_image_error(self) -> None:
        from boot_err_shim.errors import ImageError

        code, _out, _err = run_cli(
            ["test-detect", "-c", str(self.config_path), str(self.dir / "nope.png")]
        )
        self.assertEqual(code, ImageError.exit_code)

    def test_annotate_writes_an_outlined_copy(self) -> None:
        target = self.dir / "annotated.png"
        code, out, _err = run_cli(
            [
                "test-detect",
                "-c",
                str(self.config_path),
                str(self.image),
                "--annotate",
                str(target),
            ]
        )
        self.assertEqual(code, 0)
        self.assertTrue(target.exists())
        self.assertIn("annotated", out)

    def test_a_stale_calibration_is_refused(self) -> None:
        from boot_err_shim.errors import CalibrationStale

        # Reword detect.text in place, so the stored calibration no longer
        # describes it. Adding a second [detect] table instead would be
        # invalid TOML and fail as a config error long before the staleness
        # check ever ran.
        text = self.config_path.read_text(encoding="utf-8").replace(
            "Please press 'Y' to continue.", "Press any key to continue."
        )
        self.config_path.write_text(text, encoding="utf-8")
        self.assertIn("Press any key", self.config_path.read_text(encoding="utf-8"))
        code, _out, _err = run_cli(
            ["test-detect", "-c", str(self.config_path), str(self.image)]
        )
        self.assertEqual(code, CalibrationStale.exit_code)


class TestAsciiOutput(CliFixture):
    """Printing must not be the thing that fails.

    Found by the containerised tier, which runs under LANG=C. An unknown
    glyph decodes to U+FFFD, ASCII stdout cannot encode it, and `test-detect`
    on a non-matching screen died with a UnicodeEncodeError traceback instead
    of reporting NO MATCH -- an untyped exception escaping the CLI, which is
    exactly what the fuzz tier's oracle forbids.
    """

    def run_with_ascii_stdout(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="ascii", newline="")
        real_stdout, real_stderr = sys.stdout, sys.stderr
        sys.stdout = stream
        sys.stderr = io.StringIO()
        try:
            code = cli.main(argv)
        finally:
            stream.flush()
            sys.stdout, sys.stderr = real_stdout, real_stderr
        return code, buffer.getvalue().decode("ascii")

    def non_matching_image(self) -> Path:
        from boot_err_shim.png import write_frame

        # Characters the calibration has never seen, so the decoder emits
        # replacement characters into the text it prints.
        return write_frame(
            self.dir / "unknown.png", render(("ZZZZ QQQQ XXXX 12345",))
        )

    def test_test_detect_survives_an_ascii_only_stdout(self) -> None:
        code, out = self.run_with_ascii_stdout(
            ["test-detect", "-c", str(self.config_path), str(self.non_matching_image())]
        )
        self.assertEqual(code, 1)
        self.assertIn("NO MATCH", out)

    def test_unencodable_characters_are_escaped_not_dropped(self) -> None:
        _code, out = self.run_with_ascii_stdout(
            ["test-detect", "-c", str(self.config_path), str(self.non_matching_image())]
        )
        # Escaped rather than silently replaced: a bare '?' could equally be a
        # question mark that was really on the console.
        self.assertIn("\\ufffd", out)

    def test_a_matching_frame_also_survives(self) -> None:
        code, out = self.run_with_ascii_stdout(
            ["test-detect", "-c", str(self.config_path), str(self.image)]
        )
        self.assertEqual(code, 0)
        self.assertIn("MATCH", out)

    def test_show_calibration_survives_an_ascii_only_stdout(self) -> None:
        code, out = self.run_with_ascii_stdout(
            ["show-calibration", "-c", str(self.config_path), "--glyphs"]
        )
        self.assertEqual(code, 0)
        self.assertIn("glyphs", out)


class TestShowCalibration(CliFixture):
    def test_it_prints_a_summary(self) -> None:
        code, out, _err = run_cli(["show-calibration", "-c", str(self.config_path)])
        self.assertEqual(code, 0)
        self.assertIn("contrast", out)
        self.assertIn("verify", out)

    def test_glyphs_are_only_printed_when_asked(self) -> None:
        _code, without, _err = run_cli(
            ["show-calibration", "-c", str(self.config_path)]
        )
        _code, with_glyphs, _err = run_cli(
            ["show-calibration", "-c", str(self.config_path), "--glyphs"]
        )
        self.assertNotIn("#####", without)
        self.assertIn("#", with_glyphs)
        self.assertGreater(len(with_glyphs), len(without))

    def test_a_missing_calibration_is_a_typed_error(self) -> None:
        from boot_err_shim.errors import CalibrationError

        self.calibration_path.unlink()
        code, _out, _err = run_cli(["show-calibration", "-c", str(self.config_path)])
        self.assertEqual(code, CalibrationError.exit_code)

    def test_never_calibrated_is_distinguished_from_corrupt(self) -> None:
        """Both end in `configure`; only one of them is a fault.

        The class existed and was never raised, so the program named a
        distinction it did not actually make -- which reads as a guarantee to
        anyone catching it.
        """
        from boot_err_shim.errors import CalibrationNotFound

        self.calibration_path.unlink()
        with self.assertRaises(CalibrationNotFound) as caught:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                cli.main(
                    ["--debug", "show-calibration", "-c", str(self.config_path)]
                )
        message = str(caught.exception)
        self.assertIn("no calibration yet", message)
        self.assertIn("configure", message)

    def test_a_corrupt_calibration_is_not_reported_as_missing(self) -> None:
        from boot_err_shim.errors import CalibrationError, CalibrationNotFound

        self.calibration_path.write_text("this is not toml {{{", encoding="utf-8")
        with self.assertRaises(CalibrationError) as caught:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                cli.main(
                    ["--debug", "show-calibration", "-c", str(self.config_path)]
                )
        self.assertNotIsInstance(caught.exception, CalibrationNotFound)
        self.assertIn("not valid TOML", str(caught.exception))

    def test_a_missing_calibration_does_not_stop_the_daemon_watching(self) -> None:
        # It refuses to press keys, but it must still watch: exiting would
        # leave the host unwatched as well as unrescued.
        self.calibration_path.unlink()
        code, _out, err = run_cli(["check-config", "-c", str(self.config_path)])
        self.assertEqual(code, 0)
        self.assertIn("configure", err)


class TestSnapshotRingBuffer(CliFixture):
    """Eviction. Untested until now, and it runs once a minute forever."""

    def config(self, keep: int):
        posix = str(self.dir / "snapshots").replace("\\", "/")
        return make_config(
            f'[log]\nscreenshot_keep = {keep}\nscreenshot_dir = "{posix}"\n'
        )

    def small(self, value: int = 0) -> Frame:
        return Frame(2, 2, bytes([value]) * 12)

    def names(self) -> list[str]:
        directory = self.dir / "snapshots"
        return sorted(p.name for p in directory.iterdir()) if directory.exists() else []

    def test_the_first_write_creates_the_directory(self) -> None:
        config = self.config(5)
        self.assertFalse((self.dir / "snapshots").exists())
        cli._write_ring(config, self.small(), "match")
        self.assertTrue((self.dir / "snapshots").exists())

    def test_it_returns_the_path_it_wrote(self) -> None:
        path = cli._write_ring(self.config(5), self.small(), "match")
        self.assertTrue(path.exists())
        self.assertIn("match", path.name)

    def test_the_label_appears_in_the_filename(self) -> None:
        # So an operator can tell at a glance which frames matched.
        path = cli._write_ring(self.config(5), self.small(), "no-match")
        self.assertIn("no-match", path.name)

    def test_writes_in_the_same_second_do_not_clobber_each_other(self) -> None:
        config = self.config(10)
        first = cli._write_ring(config, self.small(1), "match")
        second = cli._write_ring(config, self.small(2), "match")
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_it_never_keeps_more_than_configured(self) -> None:
        config = self.config(5)
        for _ in range(20):
            cli._write_ring(config, self.small(), "match")
        self.assertEqual(len(self.names()), 5)

    def test_a_keep_of_one_holds_exactly_one(self) -> None:
        config = self.config(1)
        for _ in range(6):
            cli._write_ring(config, self.small(), "match")
        self.assertEqual(len(self.names()), 1)

    def test_the_most_recent_frame_always_survives(self) -> None:
        # Evicting the frame just written would defeat the entire point.
        config = self.config(3)
        for _ in range(10):
            path = cli._write_ring(config, self.small(), "match")
            self.assertTrue(path.exists(), f"{path.name} was evicted immediately")

    def test_the_configure_snapshot_is_never_evicted(self) -> None:
        # `configure --from` refers to it by name; rotating it away would
        # break the documented way of iterating on a calibration.
        directory = self.dir / "snapshots"
        directory.mkdir(parents=True, exist_ok=True)
        from boot_err_shim.png import write_frame

        write_frame(directory / "configure.png", self.small())

        config = self.config(2)
        for _ in range(8):
            cli._write_ring(config, self.small(), "match")

        self.assertIn("configure.png", self.names())

    def test_the_configure_snapshot_does_not_count_against_the_budget(self) -> None:
        directory = self.dir / "snapshots"
        directory.mkdir(parents=True, exist_ok=True)
        from boot_err_shim.png import write_frame

        write_frame(directory / "configure.png", self.small())

        config = self.config(3)
        for _ in range(8):
            cli._write_ring(config, self.small(), "match")

        rotated = [name for name in self.names() if name != "configure.png"]
        self.assertEqual(len(rotated), 3)

    def test_unrelated_files_are_left_alone(self) -> None:
        directory = self.dir / "snapshots"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "notes.txt").write_text("keep me", encoding="utf-8")

        config = self.config(2)
        for _ in range(6):
            cli._write_ring(config, self.small(), "match")

        self.assertIn("notes.txt", self.names())

    def test_an_undeletable_file_does_not_break_the_write(self) -> None:
        # Losing a snapshot is a nuisance; failing the cycle over it would
        # cost the rescue.
        config = self.config(2)
        for _ in range(3):
            cli._write_ring(config, self.small(), "match")

        with mock.patch.object(Path, "unlink", side_effect=OSError("busy")):
            path = cli._write_ring(config, self.small(), "match")
        self.assertTrue(path.exists())

    def advancing_clock(self, step: float = 0.004):
        """A clock that moves, because real ones do.

        An earlier version of these tests froze the clock, which produced a
        situation no real machine reaches: with time stopped, eviction frees
        a name and the very next write claims it again, so names cycle. That
        is a property of the fixture, not of the program, and testing against
        it would have driven the design somewhere strange.
        """
        state = {"now": 1_800_000_000.0}

        def tick() -> float:
            state["now"] += step
            return state["now"]

        return mock.patch("time.time", side_effect=tick)

    def test_eviction_removes_the_oldest_not_an_arbitrary_subset(self) -> None:
        """Which files go, not merely how many.

        Counting survivors cannot see this: a ring buffer keeping three
        arbitrary frames holds exactly as many as one keeping the three most
        recent, and only one of them is any use when you are trying to work
        out why a console did not match.
        """
        config = self.config(3)
        written = []
        with self.advancing_clock():
            for index in range(8):
                written.append(cli._write_ring(config, self.small(index), "match"))

        survivors = set(self.names())
        self.assertEqual(len(survivors), 3)
        self.assertEqual(
            survivors,
            {path.name for path in written[-3:]},
            "eviction did not keep the three most recent frames",
        )

    def test_names_read_chronologically(self) -> None:
        # For a person listing the directory. Not what eviction relies on --
        # that uses modification time -- but it should still be true.
        config = self.config(20)
        with self.advancing_clock():
            written = [
                cli._write_ring(config, self.small(index), "match")
                for index in range(12)
            ]
        names = [path.name for path in written]
        self.assertEqual(names, sorted(names))

    def test_names_read_chronologically_across_differing_labels(self) -> None:
        config = self.config(20)
        with self.advancing_clock():
            written = [
                cli._write_ring(config, self.small(index), label)
                for index, label in enumerate(
                    ["no-match", "match", "no-match", "match"]
                )
            ]
        names = [path.name for path in written]
        self.assertEqual(names, sorted(names))

    def test_two_frames_in_the_same_microsecond_get_distinct_names(self) -> None:
        # The one thing a frozen clock is the right fixture for.
        config = self.config(20)
        with mock.patch("time.time", return_value=1_800_000_000.5):
            paths = [cli._write_ring(config, self.small(i), "match") for i in range(4)]
        self.assertEqual(len({path.name for path in paths}), 4)
        for path in paths:
            self.assertTrue(path.exists())

    def test_eviction_is_correct_even_when_names_are_misleading(self) -> None:
        """Ordering comes from the filesystem, not from the filenames.

        Constructed directly: files whose names sort in the opposite order to
        the times they were written. A name-sorted eviction keeps the wrong
        ones; an mtime-sorted eviction does not care what they are called.
        """
        import os

        directory = self.dir / "snapshots"
        directory.mkdir(parents=True, exist_ok=True)
        from boot_err_shim.png import write_frame

        # zzz is oldest, aaa is newest -- the reverse of alphabetical order.
        base = 1_700_000_000
        for offset, name in enumerate(["zzz.png", "mmm.png", "aaa.png"]):
            path = write_frame(directory / name, self.small())
            stamp = base + offset
            os.utime(path, (stamp, stamp))

        newest = cli._write_ring(self.config(2), self.small(), "match")

        survivors = set(self.names())
        self.assertIn(newest.name, survivors)
        self.assertIn("aaa.png", survivors, "the newest existing frame was evicted")
        self.assertNotIn("zzz.png", survivors, "the oldest frame was not evicted")

    def test_eviction_survives_a_second_boundary(self) -> None:
        # Names are timestamped to the second, so the sort key changes as the
        # clock ticks; the count must hold across that.
        config = self.config(4)
        for _ in range(6):
            cli._write_ring(config, self.small(), "match")
        time.sleep(1.1)
        for _ in range(6):
            cli._write_ring(config, self.small(), "match")
        self.assertEqual(len(self.names()), 4)

    def test_the_collision_loop_is_bounded(self) -> None:
        """It must terminate even if the name stops varying.

        Found by a mutant that removed the sub-second component from the
        filename: nudging the timestamp then changes nothing, the loop cannot
        find a free name, and the daemon spins silently. Overwriting one
        snapshot is a nuisance; a daemon that stops watching the host is an
        outage.
        """
        config = self.config(20)
        directory = self.dir / "snapshots"
        directory.mkdir(parents=True, exist_ok=True)

        # Every candidate name is taken, forever.
        with mock.patch.object(Path, "exists", return_value=True):
            with mock.patch("time.time", return_value=1_800_000_000.5):
                path = cli._write_ring(config, self.small(), "match")

        # It gave up and wrote something rather than looping.
        self.assertTrue(path.exists())

    def test_written_frames_can_be_read_back(self) -> None:
        # The ring buffer exists so frames can be fed to `configure --from`.
        from boot_err_shim.png import read_frame

        frame = render(("Please press 'Y' to continue.",), width=120, height=48)
        path = cli._write_ring(self.config(5), frame, "match")
        self.assertEqual(read_frame(path), frame)


if __name__ == "__main__":
    unittest.main()
