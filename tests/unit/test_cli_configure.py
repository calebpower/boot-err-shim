"""Tier 1: what `configure` prints, especially when it fails.

The analysis layer is tested thoroughly elsewhere. What is not is the CLI
assembling a failure into something usable: findings, then an ink map, then
advice, then a nonzero exit.

That assembly matters more than the success path. A successful calibration
needs no explanation. A failed one is somebody standing in front of a server
that will not boot, and the difference between "COULD NOT CALIBRATE" and the
same message plus the resolution, the detected grid and a picture of what the
analyser thought it saw is the difference between a next step and a dead end.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "tools"))

from boot_err_shim import cli  # noqa: E402
from boot_err_shim.errors import CalibrationError, ImageError  # noqa: E402
from boot_err_shim.frame import Frame  # noqa: E402
from boot_err_shim.png import write_frame  # noqa: E402
from render_frame import THE_MESSAGE, blur, render  # noqa: E402

OTHER_SCREEN = (
    "No boot device available.",
    "Press F1 to retry boot, F2 for setup.",
    "Press F5 for diagnostics.",
)


class ConfigureFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)
        self.calibration_path = self.dir / "calibration.toml"
        self.config_path = self.dir / "boot-err-shim.conf"
        self.write_config()

    def write_config(self) -> None:
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
""",
            encoding="utf-8",
        )
        import os

        if os.name == "posix":
            self.config_path.chmod(0o600)

    def configure(self, image: Path, *extra: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = ["configure", "-c", str(self.config_path), "--from", str(image), *extra]
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        return code, out.getvalue(), err.getvalue()

    def image(self, name: str, frame) -> Path:
        return write_frame(self.dir / name, frame)


class TestSuccess(ConfigureFixture):
    def test_it_writes_a_calibration_and_says_where(self) -> None:
        code, out, _err = self.configure(self.image("ok.png", render(THE_MESSAGE)))
        self.assertEqual(code, 0)
        self.assertTrue(self.calibration_path.exists())
        self.assertIn(str(self.calibration_path), out)

    def test_it_reports_the_geometry_and_the_verification(self) -> None:
        _code, out, _err = self.configure(self.image("ok.png", render(THE_MESSAGE)))
        self.assertIn("9x16", out)
        self.assertIn("0 px differ", out)
        self.assertIn("25 distinct glyphs", out)

    def test_it_lists_every_configured_line(self) -> None:
        _code, out, _err = self.configure(self.image("ok.png", render(THE_MESSAGE)))
        for index in range(1, 4):
            self.assertIn(f"{index}/3", out)

    def test_reading_a_file_does_not_save_a_snapshot(self) -> None:
        # The frame is already on disk; copying it into the ring buffer would
        # just push a real capture out.
        self.configure(self.image("ok.png", render(THE_MESSAGE)))
        snapshots = self.dir / "snapshots"
        self.assertFalse(
            snapshots.exists() and any(snapshots.iterdir()),
            "configure --from wrote to the snapshot ring buffer",
        )

    def test_dry_run_analyses_without_writing(self) -> None:
        code, out, _err = self.configure(
            self.image("ok.png", render(THE_MESSAGE)), "--dry-run"
        )
        self.assertEqual(code, 0)
        self.assertIn("0 px differ", out)
        self.assertIn("not writing", out)
        self.assertFalse(self.calibration_path.exists())

    def test_an_explicit_output_path_is_honoured(self) -> None:
        target = self.dir / "elsewhere.toml"
        code, _out, _err = self.configure(
            self.image("ok.png", render(THE_MESSAGE)), "-o", str(target)
        )
        self.assertEqual(code, 0)
        self.assertTrue(target.exists())
        self.assertFalse(self.calibration_path.exists())


class TestFailureOutput(ConfigureFixture):
    """The half that has to be useful."""

    def failing(self, *extra: str) -> tuple[int, str, str]:
        return self.configure(self.image("other.png", render(OTHER_SCREEN)), *extra)

    def test_it_exits_nonzero(self) -> None:
        code, _out, _err = self.failing()
        self.assertEqual(code, CalibrationError.exit_code)

    def test_it_says_plainly_that_it_failed(self) -> None:
        _code, _out, err = self.failing()
        self.assertIn("COULD NOT CALIBRATE", err)

    def test_it_still_reports_what_it_determined(self) -> None:
        # Resolution, colours, contrast and the band count are all knowable
        # even when alignment failed, and all of them are diagnostic.
        _code, out, _err = self.failing()
        self.assertIn("640x400", out)
        self.assertIn("contrast", out)
        self.assertIn("text rows found:", out)

    def test_it_draws_the_ink_map(self) -> None:
        _code, out, _err = self.failing()
        self.assertIn("ink map", out)
        self.assertIn("#", out)

    def test_it_offers_a_next_step(self) -> None:
        _code, _out, err = self.failing()
        self.assertIn("What to try", err)
        self.assertIn("--cell", err)

    def test_it_does_not_write_a_calibration(self) -> None:
        self.failing()
        self.assertFalse(self.calibration_path.exists())

    def test_a_failure_leaves_a_previous_calibration_intact(self) -> None:
        """The property that makes iterating safe.

        An operator with a working calibration tries `configure` again after
        a firmware update; it fails; the daemon must still be able to rescue
        the host in the meantime.
        """
        self.configure(self.image("ok.png", render(THE_MESSAGE)))
        before = self.calibration_path.read_bytes()

        code, _out, _err = self.failing()
        self.assertNotEqual(code, 0)
        self.assertEqual(self.calibration_path.read_bytes(), before)

    def test_a_blank_screen_gets_the_blank_screen_advice(self) -> None:
        blank = Frame(320, 200, bytes(320 * 200 * 3))
        code, out, err = self.configure(self.image("blank.png", blank))
        self.assertNotEqual(code, 0)
        self.assertIn("blank", err)
        self.assertIn("text rows found: 0", out)
        # No ink map to draw, and an empty box would be worse than nothing.
        self.assertNotIn("ink map", out)

    def test_a_blurred_console_is_refused_with_advice(self) -> None:
        code, _out, err = self.configure(
            self.image("blur.png", blur(render(THE_MESSAGE)))
        )
        self.assertNotEqual(code, 0)
        self.assertIn("ocr", err)

    def test_a_forced_cell_size_that_cannot_work_is_refused(self) -> None:
        code, _out, err = self.configure(
            self.image("ok.png", render(THE_MESSAGE)), "--cell", "5x5"
        )
        self.assertNotEqual(code, 0)
        self.assertIn("COULD NOT CALIBRATE", err)

    def test_an_unreadable_image_is_an_image_error(self) -> None:
        path = self.dir / "notanimage.png"
        path.write_text("hello", encoding="utf-8")
        code, _out, err = self.configure(path)
        self.assertEqual(code, ImageError.exit_code)
        self.assertIn("boot-err-shim:", err)

    def test_a_missing_image_is_an_image_error(self) -> None:
        code, _out, _err = self.configure(self.dir / "absent.png")
        self.assertEqual(code, ImageError.exit_code)


class TestHints(ConfigureFixture):
    def test_a_correct_forced_cell_size_succeeds(self) -> None:
        code, out, _err = self.configure(
            self.image("ok.png", render(THE_MESSAGE, cell_width=9, cell_height=16)),
            "--cell",
            "9x16",
        )
        self.assertEqual(code, 0)
        self.assertIn("0 px differ", out)

    def test_a_forced_threshold_is_reported(self) -> None:
        code, out, _err = self.configure(
            self.image("ok.png", render(THE_MESSAGE)), "--threshold", "96"
        )
        self.assertEqual(code, 0)
        self.assertIn("threshold 96", out)

    def test_invert_works_on_a_light_console(self) -> None:
        light = render(THE_MESSAGE, foreground=(0, 0, 0), background=(255, 255, 255))
        code, out, _err = self.configure(self.image("light.png", light), "--invert")
        self.assertEqual(code, 0)
        self.assertIn("0 px differ", out)

    def test_a_malformed_cell_argument_is_a_usage_error(self) -> None:
        code, _out, _err = self.configure(
            self.image("ok.png", render(THE_MESSAGE)), "--cell", "nonsense"
        )
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
