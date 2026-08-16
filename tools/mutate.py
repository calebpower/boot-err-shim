#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Acceptance test for the test suite: break the code, expect red.

The methodology's own acceptance criterion is that a harness which cannot
rediscover known bugs is not measuring anything. This is that check, run
against deliberate defects rather than a defect history we do not have yet.

Each mutant is a defect somebody could plausibly introduce -- a swapped
constant, a dropped guard, an inverted comparison -- paired with the tier that
should catch it. A mutant that *survives* is the interesting result: it means
the suite has a hole exactly where the comment claims coverage.

Usage:
    uv run tools/mutate.py                # every mutant
    uv run tools/mutate.py --list         # just show them
    uv run tools/mutate.py -k ping        # only mutants matching a substring
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Mutant:
    name: str
    #: Repo-relative file to edit.
    path: str
    #: Exact text to replace. Must appear exactly once.
    old: str
    #: What to replace it with.
    new: str
    #: Which tier is expected to notice, for the report.
    tier: str
    #: Why this defect is worth guarding against.
    rationale: str
    #: os.name this mutant is meaningful on, or None for all.
    #:
    #: Platform-specific code paths are dead on the other platform, so
    #: mutating them proves nothing there. Reported as unverified rather than
    #: quietly counted as caught -- an untested assertion that looks tested is
    #: worse than one that admits it.
    platform: str | None = None
    #: Test modules to run for this mutant. Empty means the whole suite.
    #:
    #: Purely a speed optimisation, and one with a trap: naming too narrow a
    #: subset turns "no test covers this" into "the covering test was not
    #: run", which looks identical. --full ignores this and is what CI uses.
    suites: tuple[str, ...] = ()


MUTANTS: list[Mutant] = [
    Mutant(
        name="ping-units-freebsd",
        path="src/boot_err_shim/platform_.py",
        old='ping_command=("ping", "-c", "1", "-W", "2000", "{host}"),\n'
        '            syslog_socket=Path("/var/run/log"),',
        new='ping_command=("ping", "-c", "1", "-W", "2", "{host}"),\n'
        '            syslog_socket=Path("/var/run/log"),',
        tier="1 platform",
        rationale="FreeBSD -W is milliseconds; 2ms means every ping fails and "
        "a healthy host reads as down.",
        suites=("tests.unit.test_platform",),
    ),
    Mutant(
        name="ping-units-linux",
        path="src/boot_err_shim/platform_.py",
        old='ping_command=("ping", "-c", "1", "-W", "2", "{host}"),\n'
        '            syslog_socket=Path("/dev/log"),',
        new='ping_command=("ping", "-c", "1", "-W", "2000", "{host}"),\n'
        '            syslog_socket=Path("/dev/log"),',
        tier="1 platform",
        rationale="iputils -W is seconds; 2000 means a ping that hangs for "
        "half an hour.",
        suites=("tests.unit.test_platform",),
    ),
    Mutant(
        name="unknown-system-guesses-a-flag",
        path="src/boot_err_shim/platform_.py",
        old='ping_command=("ping", "-c", "1", "{host}"),\n'
        "        syslog_socket=None,\n    )",
        new='ping_command=("ping", "-c", "1", "-W", "2", "{host}"),\n'
        "        syslog_socket=None,\n    )",
        tier="1 platform",
        rationale="Guessing the units on an unknown OS reintroduces the exact "
        "footgun the fallback exists to avoid.",
        suites=("tests.unit.test_platform",),
    ),
    Mutant(
        name="threshold-off-by-one",
        path="src/boot_err_shim/config.py",
        old='threshold=ping_t.int_("threshold", 3, 1, 1000),',
        new='threshold=ping_t.int_("threshold", 3, 0, 1000),',
        tier="1 config",
        rationale="A threshold of 0 means acting before a single failed ping.",
        suites=("tests.unit.test_config",),
    ),
    Mutant(
        name="unknown-keys-ignored",
        path="src/boot_err_shim/config.py",
        old="    def finish(self) -> None:\n        if self._data:",
        new="    def finish(self) -> None:\n        if False:",
        tier="1 config + 3 structural",
        rationale="A typo silently ignored is a setting the operator believes "
        "is in force but is not.",
        suites=("tests.unit.test_config", "tests.structural.test_config_sample",),
    ),
    Mutant(
        name="password-permission-check-dropped",
        path="src/boot_err_shim/config.py",
        old="    if mode & (stat.S_IRGRP | stat.S_IROTH):",
        new="    if False:",
        tier="1 config",
        rationale="A world-readable config hands console access to any local "
        "user.",
        suites=("tests.unit.test_config",),
    ),
    Mutant(
        name="ping-command-placeholder-unchecked",
        path="src/boot_err_shim/config.py",
        old='if not any("{host}" in part for part in ping.command):',
        new="if False:",
        tier="1 config",
        rationale="A command with no {host} pings nothing and reports every "
        "host as up forever.",
        suites=("tests.unit.test_config",),
    ),
    Mutant(
        name="probe-timeout-reads-as-up",
        path="src/boot_err_shim/probe.py",
        old='return ProbeResult(up=False, reason="timeout")',
        new='return ProbeResult(up=True, reason="timeout")',
        tier="1 probe",
        rationale="A hung ping would mask a genuinely dead host indefinitely.",
        suites=("tests.unit.test_probe",),
    ),
    Mutant(
        name="probe-nonzero-exit-reads-as-up",
        path="src/boot_err_shim/probe.py",
        old='return ProbeResult(up=False, reason="unreachable", output=output)',
        new='return ProbeResult(up=True, reason="unreachable", output=output)',
        tier="1 probe",
        rationale="Inverting the core signal the whole daemon is built on.",
        suites=("tests.unit.test_probe",),
    ),
    Mutant(
        name="atomic-write-leaves-temp-files",
        path="src/boot_err_shim/lock.py",
        old="    except BaseException:\n        # Includes KeyboardInterrupt/SystemExit: a signal mid-write must not\n"
        "        # leave the temp file behind either.\n        tmp.unlink(missing_ok=True)\n        raise",
        new="    except BaseException:\n        raise",
        tier="1 lock",
        rationale="Repeated failed writes would fill the state directory with "
        "debris.",
        suites=("tests.unit.test_lock",),
    ),
    Mutant(
        name="atomic-write-becomes-truncating-write",
        path="src/boot_err_shim/lock.py",
        old="        os.replace(tmp, path)",
        new="        path.write_bytes(data)\n        tmp.unlink(missing_ok=True)",
        tier="1 lock",
        rationale="A signal mid-write then leaves a truncated calibration, and "
        "the daemon refuses to press keys on next start.",
        suites=("tests.unit.test_lock",),
    ),
    Mutant(
        name="lock-allows-two-holders-posix",
        path="src/boot_err_shim/lock.py",
        old="fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)",
        new="fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)",
        tier="1 lock + 8 concurrency",
        rationale="Two daemons on one iDRAC can each press 'Y' -- the worst "
        "thing this program can do.",
        platform="posix",
        suites=("tests.unit.test_lock",),
    ),
    Mutant(
        name="lock-allows-two-holders-windows",
        path="src/boot_err_shim/lock.py",
        old="                handle.seek(_WINDOWS_LOCK_OFFSET)\n"
        "                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)",
        new="                pass",
        tier="1 lock + 8 concurrency",
        rationale="Same invariant as the posix mutant, on the branch this "
        "development machine actually executes.",
        platform="nt",
        suites=("tests.unit.test_lock",),
    ),
    # -- step 2: the state machine ------------------------------------
    Mutant(
        name="press-without-a-match",
        path="src/boot_err_shim/daemon.py",
        old="    return connected and matched and calibrated and not no_act",
        new="    return connected and calibrated and not no_act",
        tier="4 safety matrix",
        rationale="Pressing because the host stopped answering pings, rather "
        "than because the prompt is on screen. The single worst defect "
        "this program could have.",
        suites=("tests.contract.test_safety_matrix", "tests.contract.test_daemon_loop",),
    ),
    Mutant(
        name="press-without-a-calibration",
        path="src/boot_err_shim/daemon.py",
        old="    return connected and matched and calibrated and not no_act",
        new="    return connected and matched and not no_act",
        tier="4 safety matrix",
        rationale="Acting on an uncalibrated guess is exactly the guesswork "
        "the calibration design exists to avoid.",
        suites=("tests.contract.test_safety_matrix", "tests.contract.test_daemon_loop",),
    ),
    Mutant(
        name="no-act-still-presses",
        path="src/boot_err_shim/daemon.py",
        old="    return connected and matched and calibrated and not no_act",
        new="    return connected and matched and calibrated",
        tier="4 safety matrix",
        rationale="An operator who asked to observe only would get a "
        "keystroke sent to a live console.",
        suites=("tests.contract.test_safety_matrix", "tests.contract.test_daemon_loop",),
    ),
    Mutant(
        name="threshold-comparison-off-by-one",
        path="src/boot_err_shim/daemon.py",
        old="    if failures < threshold:",
        new="    if failures <= threshold:",
        tier="4 decision matrix",
        rationale="Acts one cycle later than configured, every time.",
        suites=("tests.contract.test_decision_matrix",),
    ),
    Mutant(
        name="intervals-swapped",
        path="src/boot_err_shim/daemon.py",
        old="            sleep_for=ping_interval,\n            reason=\"host.up\",",
        new="            sleep_for=retry_interval,\n            reason=\"host.up\",",
        tier="4 decision matrix",
        rationale="Hammers a healthy host and dawdles over a failing one.",
        suites=("tests.contract.test_decision_matrix",),
    ),
    Mutant(
        name="refusal-resets-the-failure-counter",
        path="src/boot_err_shim/daemon.py",
        old='            action=Action.SLEEP, sleep_for=recovery_interval, reason="no.match"',
        new='            action=Action.SLEEP, sleep_for=recovery_interval, reason="no.match",\n'
        "            reset_failures=True",
        tier="4 daemon loop",
        rationale="Drops the daemon out of recovery back to routine polling "
        "while the host is still stuck at the prompt.",
        suites=("tests.contract.test_safety_matrix", "tests.contract.test_daemon_loop",),
    ),
    Mutant(
        name="frame-not-written-on-no-match",
        path="src/boot_err_shim/daemon.py",
        old="        if frame is not None and self.frame_writer is not None:",
        new="        if matched and frame is not None and self.frame_writer is not None:",
        tier="4 daemon loop",
        rationale="A false negative becomes undiagnosable: the log says 'not "
        "found' and there is no frame to explain why.",
        suites=("tests.contract.test_daemon_loop",),
    ),
    Mutant(
        name="capture-failure-falls-through-to-no-match",
        path="src/boot_err_shim/daemon.py",
        old="                connected = False\n                event(\n                    log,\n"
        '                    logging.WARNING,\n                    "vnc.capture_failed",',
        new="                connected = True\n                event(\n                    log,\n"
        '                    logging.WARNING,\n                    "vnc.capture_failed",',
        tier="4 daemon loop",
        rationale="Reports a claim about screen contents we never actually "
        "saw.",
        suites=("tests.contract.test_daemon_loop",),
    ),
    Mutant(
        name="console-not-closed",
        path="src/boot_err_shim/daemon.py",
        old="        if console is not None:\n            try:\n                console.close()",
        new="        if False:\n            try:\n                console.close()",
        tier="4 daemon loop",
        rationale="Leaks a VNC session per cycle; iDRAC allows very few.",
        suites=("tests.contract.test_daemon_loop",),
    ),
    Mutant(
        name="history-not-persisted",
        path="src/boot_err_shim/history.py",
        old="        self.prune(when)\n        self.save()",
        new="        self.prune(when)",
        tier="1 history + 4 daemon loop",
        rationale="A restart resets the count, hiding exactly the repeated "
        "failure pattern worth escalating.",
        suites=("tests.unit.test_history", "tests.contract.test_daemon_loop",),
    ),
    Mutant(
        name="damaged-history-crashes-the-daemon",
        path="src/boot_err_shim/history.py",
        old="        except (ValueError, TypeError, KeyError):\n            return cls(path=path, timestamps=[])",
        new="        except (ValueError, TypeError, KeyError):\n            raise",
        tier="1 history",
        rationale="A truncated JSON file would stop the daemon rescuing the "
        "host -- an outage caused by diagnostics.",
        suites=("tests.unit.test_history",),
    ),
    Mutant(
        name="frame-accepts-short-buffers",
        path="src/boot_err_shim/frame.py",
        old="        if len(self.data) != expected:",
        new="        if False:",
        tier="1 frame",
        rationale="A truncated FramebufferUpdate would surface as an "
        "IndexError deep in glyph matching instead of at the boundary.",
        suites=("tests.unit.test_frame",),
    ),
    # -- step 3: DES, PNG, RFB ----------------------------------------
    Mutant(
        name="des-key-schedule-rotation",
        path="src/boot_err_shim/des.py",
        old="_SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)",
        new="_SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1)",
        tier="1 des",
        rationale="One wrong rotation in the key schedule. Round-trip tests "
        "would not notice; the published vectors do.",
        suites=("tests.unit.test_des",),
    ),
    Mutant(
        name="des-vnc-bit-reversal-dropped",
        path="src/boot_err_shim/des.py",
        old="    return bytes(_reverse_bits(byte) for byte in padded)",
        new="    return padded",
        tier="1 des + 5 fake server",
        rationale="Authentication then fails against every real VNC server "
        "while our own round-trip tests stay green.",
        suites=("tests.unit.test_des", "tests.fake.test_rfb_client",),
    ),
    Mutant(
        name="png-paeth-predictor",
        path="src/boot_err_shim/png.py",
        old="    if pb <= pc:\n        return b\n    return c",
        new="    if pb <= pc:\n        return c\n    return b",
        tier="1 png",
        rationale="Silently corrupts any PNG another tool wrote with filter "
        "type 4, which is the common default.",
        suites=("tests.unit.test_png",),
    ),
    Mutant(
        name="png-crc-not-checked",
        path="src/boot_err_shim/png.py",
        old="        if declared != actual:",
        new="        if False:",
        tier="1 png",
        rationale="A corrupt snapshot would decode to garbage and be analysed "
        "as though it were real.",
        suites=("tests.unit.test_png",),
    ),
    Mutant(
        name="png-interlaced-accepted",
        path="src/boot_err_shim/png.py",
        old="    if interlace != 0:",
        new="    if False:",
        tier="1 png",
        rationale="An Adam7 image would decode to a scrambled frame that then "
        "fails to calibrate for reasons nobody can diagnose.",
        suites=("tests.unit.test_png",),
    ),
    Mutant(
        name="rfb-no-read-deadline",
        path="src/boot_err_shim/rfb.py",
        old="            self.sock.settimeout(deadline.check(what))",
        new="            self.sock.settimeout(30)",
        tier="5 fake server",
        rationale="A per-recv timeout resets on every byte, so a dribbling "
        "iDRAC holds the daemon forever. The exact wedge this design "
        "exists to prevent.",
        suites=("tests.fake.test_rfb_client",),
    ),
    Mutant(
        name="rfb-red-and-blue-swapped",
        path="src/boot_err_shim/rfb.py",
        old="            pixels[t] = data[s + 2]  # red\n"
        "            pixels[t + 1] = data[s + 1]  # green\n"
        "            pixels[t + 2] = data[s]  # blue",
        new="            pixels[t] = data[s]  # red\n"
        "            pixels[t + 1] = data[s + 1]  # green\n"
        "            pixels[t + 2] = data[s + 2]  # blue",
        tier="5 fake server",
        rationale="Invisible on a greyscale console and wrong everywhere "
        "else, including any colour-based calibration.",
        suites=("tests.fake.test_rfb_client",),
    ),
    Mutant(
        name="rfb-key-release-not-sent",
        path="src/boot_err_shim/rfb.py",
        old="        events = (True, False) if pressed is None else (pressed,)",
        new="        events = (True,) if pressed is None else (pressed,)",
        tier="5 fake server",
        rationale="A key held down forever at a firmware prompt; some "
        "firmware ignores a press with no release.",
        suites=("tests.fake.test_rfb_client",),
    ),
    Mutant(
        name="rfb-empty-update-becomes-a-black-screen",
        path="src/boot_err_shim/rfb.py",
        old='            raise ProtocolError("server sent an update containing no rectangles")',
        new="            pass",
        tier="5 fake server",
        rationale="Far worse than failing: the detector finds no match on an "
        "all-zero frame and the daemon concludes the prompt is absent.",
        suites=("tests.fake.test_rfb_client",),
    ),
    Mutant(
        name="rfb-rectangle-bounds-unchecked",
        path="src/boot_err_shim/rfb.py",
        old="            if x + w > width or y + h > height:",
        new="            if False:",
        tier="5 fake server",
        rationale="A rectangle past the edge writes outside the frame buffer "
        "we allocated.",
        suites=("tests.fake.test_rfb_client",),
    ),
    Mutant(
        name="rfb-absurd-rectangle-count-unchecked",
        path="src/boot_err_shim/rfb.py",
        old="        if count > MAX_RECTANGLES:",
        new="        if False:",
        tier="5 fake server",
        rationale="A corrupt count would have us loop sixty thousand times "
        "waiting on a peer that has nothing left to say.",
        suites=("tests.fake.test_rfb_client",),
    ),
    Mutant(
        name="rfb-desktop-name-length-unchecked",
        path="src/boot_err_shim/rfb.py",
        old="        if name_length > 8192:",
        new="        if False:",
        tier="5 fake server",
        rationale="A four-gigabyte name length would be read as a length.",
        suites=("tests.fake.test_rfb_client",),
    ),
    Mutant(
        name="rfb-accepts-old-protocol-versions",
        path="src/boot_err_shim/rfb.py",
        old="        if (major, minor) < (3, 8):",
        new="        if False:",
        tier="5 fake server",
        rationale="RFB 3.3 has a different security handshake; proceeding "
        "would desynchronise the stream rather than fail cleanly.",
        suites=("tests.fake.test_rfb_client",),
    ),
    # -- step 4: calibration and detection ----------------------------
    Mutant(
        name="near-region-match-accepted-uncorroborated",
        path="src/boot_err_shim/detect.py",
        old="        if region.matched and region.difference == 0.0:",
        new="        if region.matched:",
        tier="1 detect",
        rationale="The false positive this design is built to avoid. One "
        "wrong character is about 0.2% of the region, so any tolerance "
        "loose enough for a speck of dust also accepts 'press N' as "
        "'press Y'.",
        suites=("tests.unit.test_detect",),
    ),
    Mutant(
        name="region-matcher-ignores-resolution-change",
        path="src/boot_err_shim/detect.py",
        old="        if (frame.width, frame.height) != (calibration.width, calibration.height):",
        new="        if False:",
        tier="1 detect",
        rationale="After a video mode change the stored rectangle refers to a "
        "different part of the screen entirely.",
        suites=("tests.unit.test_detect",),
    ),
    Mutant(
        name="glyph-match-needs-only-one-line",
        path="src/boot_err_shim/detect.py",
        old="        if all(line in haystack for line in wanted):",
        new="        if any(line in haystack for line in wanted):",
        tier="1 detect",
        rationale="'Please contact technical support to resolve this issue' "
        "appears in other Dell firmware messages; matching on one line "
        "would fire on them.",
        suites=("tests.unit.test_detect",),
    ),
    Mutant(
        name="calibration-accepts-approximate",
        path="src/boot_err_shim/calibrate.py",
        old="            if candidate.delta == 0:\n                calibration = _build(",
        new="            if 0 <= candidate.delta <= 40:\n                calibration = _build(",
        tier="1 calibrate",
        rationale="Settling for a nearly-right grid produces a calibration "
        "that cannot reproduce the screen it came from, and then "
        "authorises keystrokes on that basis.",
        suites=("tests.unit.test_calibrate",),
    ),
    Mutant(
        name="stale-calibration-not-detected",
        path="src/boot_err_shim/calibrate.py",
        old="    if not calibration.matches_text(lines):",
        new="    if False:",
        tier="1 calibrate",
        rationale="Changing detect.text would leave the old region mask in "
        "force, so the daemon would match the previous wording.",
        suites=("tests.unit.test_calibrate",),
    ),
    Mutant(
        name="normalise-is-case-sensitive",
        path="src/boot_err_shim/calibrate.py",
        old='    return " ".join(text.split()).casefold()',
        new='    return " ".join(text.split())',
        tier="1 calibrate",
        rationale="Matching would depend on the operator reproducing the "
        "console's capitalisation exactly.",
        suites=("tests.unit.test_calibrate", "tests.unit.test_detect"),
    ),
    Mutant(
        name="contrast-floor-never-warns",
        path="src/boot_err_shim/report.py",
        old="CONTRAST_FLOOR = 3.0",
        new="CONTRAST_FLOOR = 0.0",
        tier="2 conformance",
        rationale="A dim console makes binarisation unreliable, and that is "
        "not something an operator can judge by looking at a PNG.",
        suites=("tests.conformance.test_report",),
    ),
    # -- step 5: found by the fuzz and concurrency tiers ---------------
    Mutant(
        name="duration-accepts-infinity",
        path="src/boot_err_shim/config.py",
        old="        if not math.isfinite(parsed):\n"
        '            raise ConfigError(f"{where}: duration must be finite, got {value!r}")\n'
        "        seconds = int(parsed * unit)",
        new="        seconds = int(parsed * unit)",
        tier="1 config + 7 fuzz",
        rationale='Found by fuzzing: "1e400" parses as inf, and int(inf) '
        "raises OverflowError -- untyped, so it escaped the CLI's error "
        "handling as a traceback.",
        suites=("tests.unit.test_config", "tests.fuzz.test_fuzz_config"),
    ),
    Mutant(
        name="duration-has-no-upper-bound",
        path="src/boot_err_shim/config.py",
        old="    if seconds > MAX_DURATION_SECONDS:",
        new="    if False:",
        tier="1 config",
        rationale="A stray digit would park the daemon until long after the "
        "hardware has been replaced, silently.",
        suites=("tests.unit.test_config",),
    ),
    Mutant(
        name="lock-is-shared-across-consoles",
        path="src/boot_err_shim/config.py",
        old='        return self.state_dir / f"{slug(self.vnc.host)}-{self.vnc.port}.lock"',
        new='        return self.state_dir / "boot-err-shim.lock"',
        tier="1 config + 8 concurrency",
        rationale="A single global lock name stops two daemons watching two "
        "different hosts from running side by side.",
        suites=("tests.unit.test_config",),
    ),
    Mutant(
        name="lock-is-per-target-not-per-console",
        path="src/boot_err_shim/config.py",
        old='        return self.state_dir / f"{slug(self.vnc.host)}-{self.vnc.port}.lock"',
        new='        return self.state_dir / f"{slug(self.target.host)}.lock"',
        tier="1 config",
        rationale="Two configs watching different hosts through one iDRAC "
        "would both press keys at the same console.",
        suites=("tests.unit.test_config",),
    ),
    Mutant(
        name="history-is-shared-across-targets",
        path="src/boot_err_shim/config.py",
        old='        return self.state_dir / f"{slug(self.target.host)}.history.json"',
        new='        return self.state_dir / "history.json"',
        tier="1 config",
        rationale="Two daemons would pool their intervention counts and warn "
        "about the wrong controller.",
        suites=("tests.unit.test_config",),
    ),
    Mutant(
        name="hostnames-not-sanitised-into-filenames",
        path="src/boot_err_shim/config.py",
        old='    return "".join(char if char.isalnum() or char in "-." else "_" for char in value)',
        new="    return value",
        tier="1 config",
        rationale="An IPv6 literal or a hostname with a slash would put "
        "directory separators in the middle of the lock path.",
        suites=("tests.unit.test_config",),
    ),
    Mutant(
        name="state-directory-not-configurable",
        path="src/boot_err_shim/config.py",
        old='    state_dir = state_t.path("dir", plat.state_dir)',
        new="    state_dir = plat.state_dir",
        tier="8 concurrency",
        rationale="Found by the concurrency tier: with the state directory "
        "pinned to a platform default, the lock file cannot be pointed "
        "anywhere, and a second instance locks a path nobody else uses.",
        suites=("tests.unit.test_config", "tests.concurrency.test_single_instance"),
    ),
    Mutant(
        name="sleep-ignores-a-stop-request",
        path="src/boot_err_shim/daemon.py",
        old="    def sleep(self, seconds: float) -> bool:\n        return self.stop.wait(seconds)",
        new="    def sleep(self, seconds: float) -> bool:\n"
        "        import time as _t\n\n        _t.sleep(min(seconds, 3))\n"
        "        return self.stop.is_set()",
        tier="8 concurrency",
        rationale="A ten-minute post-fix sleep would mean a ten-minute "
        "`service stop`.",
        suites=("tests.concurrency.test_signals_and_writes",),
    ),
    Mutant(
        name="press-does-not-reset-failures",
        path="src/boot_err_shim/daemon.py",
        old='            reason="match.pressed",\n            reset_failures=True,',
        new='            reason="match.pressed",\n            reset_failures=False,',
        tier="4 safety matrix + 9 simulation",
        rationale="Staying in recovery after a successful press is how a "
        "daemon presses again at a console already on its way up.",
        suites=(
            "tests.contract.test_safety_matrix",
            "tests.contract.test_daemon_loop",
        ),
    ),
    # -- found by the CLI and ring-buffer tests ------------------------
    Mutant(
        name="ring-buffer-evicts-the-frame-just-written",
        path="src/boot_err_shim/cli.py",
        old="            if candidate.name != \"configure.png\" and candidate != path",
        new="            if candidate.name != \"configure.png\"",
        tier="1 cli + 6 ring-buffer",
        rationale="Two frames captured in the same second sorted the newer "
        "one first, so eviction deleted the frame just captured -- the "
        "most interesting one in the directory.",
        suites=("tests.unit.test_cli",),
    ),
    Mutant(
        name="eviction-orders-by-name-not-by-time",
        path="src/boot_err_shim/cli.py",
        old="        key=age,",
        new="        key=lambda candidate: candidate.name,",
        tier="1 cli",
        rationale="Any naming scheme derived from a clock misorders "
        "somewhere -- a name reused after eviction, a backwards NTP step. "
        "The filesystem already records when each file was written.",
        suites=("tests.unit.test_cli",),
    ),
    Mutant(
        name="ring-buffer-loses-sub-second-precision",
        path="src/boot_err_shim/cli.py",
        old='        return directory / f"{stamp}.{int(round((at % 1) * 1_000_000)):06d}-{label}.png"',
        new='        return directory / f"{stamp}-{label}.png"',
        tier="1 cli",
        rationale="Whole-second names collide constantly, and the collision "
        "loop cannot then escape, because nudging the timestamp no longer "
        "changes the name.",
        suites=("tests.unit.test_cli",),
    ),
    Mutant(
        name="configure-snapshot-is-rotated-away",
        path="src/boot_err_shim/cli.py",
        old='            if candidate.name != "configure.png" and candidate != path',
        new="            if candidate != path",
        tier="1 cli + 6 ring-buffer",
        rationale="`configure --from` refers to that file by name; rotating "
        "it away breaks the documented way to iterate on a calibration.",
        suites=("tests.unit.test_cli",),
    ),
    Mutant(
        name="output-crashes-on-an-ascii-terminal",
        path="src/boot_err_shim/cli.py",
        old="def main(argv: list[str] | None = None) -> int:\n    _make_output_lossy()",
        new="def main(argv: list[str] | None = None) -> int:",
        tier="1 cli + 6 vectors",
        rationale="Under LANG=C, printing a decoded screen containing an "
        "unknown glyph raised UnicodeEncodeError -- an untyped exception "
        "out of the CLI, on the exact path an operator uses to diagnose "
        "a non-matching console.",
        suites=("tests.unit.test_cli",),
    ),
    Mutant(
        name="log-newline-not-escaped",
        path="src/boot_err_shim/log.py",
        old='            .replace("\\n", "\\\\n")',
        new='            .replace("\\n", "\\n")',
        tier="1 log",
        rationale="A multi-line ping error splits one event across lines and "
        "desyncs anything parsing the log.",
        suites=("tests.unit.test_log",),
    ),
    Mutant(
        name="log-handlers-accumulate-on-reload",
        path="src/boot_err_shim/log.py",
        old="    for existing in list(logger.handlers):\n"
        "        logger.removeHandler(existing)\n"
        "        existing.close()",
        new="    pass",
        tier="1 log",
        rationale="Every SIGHUP would duplicate every subsequent log line.",
        suites=("tests.unit.test_log",),
    ),
]


#: Seconds a single suite run may take before the mutant is judged to have
#: hung. Generous enough for the whole suite on a slow machine.
SUITE_TIMEOUT = 1800


def run_suite(suites: tuple[str, ...] = ()) -> tuple[bool, str]:
    """Run the suite, or just the named modules. Returns (passed, output).

    A mutant that makes the program loop forever must not take the harness
    with it. One did: removing the sub-second component from a snapshot
    filename left a collision loop that could never find a free name, and the
    run simply stopped. Timing out counts as failing, which is the right
    answer -- a hang is a defect the suite noticed.
    """
    if suites:
        command = [sys.executable, "-m", "unittest", *suites]
    else:
        command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]

    try:
        completed = subprocess.run(
            command,
            cwd=REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=SUITE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as expired:
        partial = (expired.stdout or b"").decode("utf-8", "replace")
        return False, partial + f"\nTIMED OUT after {SUITE_TIMEOUT}s (the mutant hangs)"

    output = completed.stdout.decode("utf-8", "replace")
    return completed.returncode == 0, output


def failing_tests(output: str) -> list[str]:
    names = []
    for line in output.splitlines():
        if line.startswith(("FAIL: ", "ERROR: ")):
            names.append(line.split(" ", 1)[1].split(" ")[0])
    return names


def apply_mutant(mutant: Mutant) -> str:
    """Apply and return the original text, for restoration."""
    path = REPO / mutant.path
    original = path.read_text(encoding="utf-8")
    count = original.count(mutant.old)
    if count != 1:
        raise SystemExit(
            f"mutant {mutant.name!r}: pattern occurs {count} times in "
            f"{mutant.path}, expected exactly 1"
        )
    path.write_text(original.replace(mutant.old, mutant.new), encoding="utf-8")
    return original


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list mutants and exit")
    parser.add_argument("-k", metavar="SUBSTRING", help="only mutants matching this")
    parser.add_argument(
        "--full",
        action="store_true",
        help="run the whole suite per mutant, ignoring per-mutant suite hints",
    )
    args = parser.parse_args()

    matched = [m for m in MUTANTS if not args.k or args.k in m.name]

    if args.list:
        for mutant in matched:
            gate = f" [{mutant.platform} only]" if mutant.platform else ""
            print(f"{mutant.name:40s} {mutant.tier}{gate}")
        return 0

    if not matched:
        print("no mutants matched", file=sys.stderr)
        return 2

    import os as _os

    selected = [m for m in matched if m.platform in (None, _os.name)]
    deferred = [m for m in matched if m.platform not in (None, _os.name)]

    print("Baseline: the suite must be green before mutating.")
    passed, output = run_suite()
    if not passed:
        print(output[-3000:])
        print("\nBaseline is RED. Fix the suite before running mutants.")
        return 2
    print("Baseline green.\n")

    survivors: list[Mutant] = []
    results: list[tuple[Mutant, list[str]]] = []

    for index, mutant in enumerate(selected, 1):
        print(f"[{index}/{len(selected)}] {mutant.name} ... ", end="", flush=True)
        original = apply_mutant(mutant)
        try:
            passed, output = run_suite(() if args.full else mutant.suites)
            if passed and mutant.suites and not args.full:
                # A narrow subset missing it proves nothing on its own -- the
                # covering test may simply not have run. Confirm against the
                # whole suite before calling it a survivor.
                passed, output = run_suite()
        finally:
            (REPO / mutant.path).write_text(original, encoding="utf-8")

        if passed:
            survivors.append(mutant)
            print("SURVIVED")
        else:
            caught_by = failing_tests(output)
            results.append((mutant, caught_by))
            print(f"caught by {len(caught_by)} test(s)")

    print("\n" + "=" * 72)
    for mutant, caught_by in results:
        print(f"\n{mutant.name}  [{mutant.tier}]")
        print(f"  why it matters: {mutant.rationale}")
        for name in caught_by[:4]:
            print(f"  caught by: {name}")
        if len(caught_by) > 4:
            print(f"  ... and {len(caught_by) - 4} more")

    if deferred:
        print("\n" + "=" * 72)
        print(
            f"\n{len(deferred)} mutant(s) NOT VERIFIED on this platform "
            f"(os.name={_os.name}).\nThe code they target is dead here, so "
            "mutating it proves nothing. Run the\nLinux container tier to close "
            "these:\n"
        )
        for mutant in deferred:
            print(f"  {mutant.name} [{mutant.platform} only]")
            print(f"      {mutant.rationale}")

    if survivors:
        print("\n" + "=" * 72)
        print(f"\n{len(survivors)} MUTANT(S) SURVIVED -- the suite has a hole:\n")
        for mutant in survivors:
            print(f"  {mutant.name}: {mutant.rationale}")
        return 1

    print(f"\nAll {len(selected)} applicable mutants caught.")
    if deferred:
        print(f"{len(deferred)} deferred to another platform, listed above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
