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
    ),
    Mutant(
        name="threshold-off-by-one",
        path="src/boot_err_shim/config.py",
        old='threshold=ping_t.int_("threshold", 3, 1, 1000),',
        new='threshold=ping_t.int_("threshold", 3, 0, 1000),',
        tier="1 config",
        rationale="A threshold of 0 means acting before a single failed ping.",
    ),
    Mutant(
        name="unknown-keys-ignored",
        path="src/boot_err_shim/config.py",
        old="    def finish(self) -> None:\n        if self._data:",
        new="    def finish(self) -> None:\n        if False:",
        tier="1 config + 3 structural",
        rationale="A typo silently ignored is a setting the operator believes "
        "is in force but is not.",
    ),
    Mutant(
        name="password-permission-check-dropped",
        path="src/boot_err_shim/config.py",
        old="    if mode & (stat.S_IRGRP | stat.S_IROTH):",
        new="    if False:",
        tier="1 config",
        rationale="A world-readable config hands console access to any local "
        "user.",
    ),
    Mutant(
        name="ping-command-placeholder-unchecked",
        path="src/boot_err_shim/config.py",
        old='if not any("{host}" in part for part in ping.command):',
        new="if False:",
        tier="1 config",
        rationale="A command with no {host} pings nothing and reports every "
        "host as up forever.",
    ),
    Mutant(
        name="probe-timeout-reads-as-up",
        path="src/boot_err_shim/probe.py",
        old='return ProbeResult(up=False, reason="timeout")',
        new='return ProbeResult(up=True, reason="timeout")',
        tier="1 probe",
        rationale="A hung ping would mask a genuinely dead host indefinitely.",
    ),
    Mutant(
        name="probe-nonzero-exit-reads-as-up",
        path="src/boot_err_shim/probe.py",
        old='return ProbeResult(up=False, reason="unreachable", output=output)',
        new='return ProbeResult(up=True, reason="unreachable", output=output)',
        tier="1 probe",
        rationale="Inverting the core signal the whole daemon is built on.",
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
    ),
    Mutant(
        name="atomic-write-becomes-truncating-write",
        path="src/boot_err_shim/lock.py",
        old="        os.replace(tmp, path)",
        new="        path.write_bytes(data)\n        tmp.unlink(missing_ok=True)",
        tier="1 lock",
        rationale="A signal mid-write then leaves a truncated calibration, and "
        "the daemon refuses to press keys on next start.",
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
    ),
    Mutant(
        name="log-newline-not-escaped",
        path="src/boot_err_shim/log.py",
        old='            .replace("\\n", "\\\\n")',
        new='            .replace("\\n", "\\n")',
        tier="1 log",
        rationale="A multi-line ping error splits one event across lines and "
        "desyncs anything parsing the log.",
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
    ),
]


def run_suite() -> tuple[bool, str]:
    """Run the suite. Returns (passed, output tail)."""
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
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
