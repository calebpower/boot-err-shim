#!/usr/bin/env python3
"""End-to-end walk of the whole daemon against a fake iDRAC.

Runs inside a Linux container, where ping reports an unreachable host with a
nonzero exit -- on Windows it exits 0 for "destination unreachable", so the
failure path cannot be reached there at all.

Drives the real CLI over a real socket: configure, then --no-act to prove the
keystroke is suppressed, then for real to prove it is sent.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools"))

from fake_vnc_server import FakeVNCServer  # noqa: E402
from render_frame import THE_MESSAGE, render  # noqa: E402

#: TEST-NET-1. Reserved for documentation, so it is reliably unroutable.
UNREACHABLE = "192.0.2.1"

CONFIG = """
[target]
host = "{host}"

[ping]
threshold      = 1
interval       = 1
retry_interval = 1
timeout        = 5

[vnc]
host     = "127.0.0.1"
port     = {port}
password = "secret12"

[detect]
calibration = "{calibration}"
text = \"\"\"
Disabling writes to flash as the flash part has gone bad.
Please contact technical support to resolve this issue.
Please press 'Y' to continue.
\"\"\"

[recovery]
interval       = 1
post_fix_sleep = 1

[log]
screenshot_dir = "{snapshots}"
syslog         = "never"
"""


def main() -> int:
    frame = render(THE_MESSAGE)
    server = FakeVNCServer(
        width=frame.width,
        height=frame.height,
        pixels=frame.data,
        password="secret12",
    )
    port = server.start()

    workdir = Path(tempfile.mkdtemp())
    config = workdir / "boot-err-shim.conf"
    config.write_text(
        CONFIG.format(
            host=UNREACHABLE,
            port=port,
            calibration=workdir / "calibration.toml",
            snapshots=workdir / "snapshots",
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)

    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    failures: list[str] = []

    def cli(*args: str) -> subprocess.CompletedProcess:
        print(f"\n$ boot-err-shim {' '.join(args)}")
        result = subprocess.run(
            [sys.executable, "-m", "boot_err_shim.cli", *args, "-c", str(config)],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
            check=False,
        )
        print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        return result

    def check(label: str, condition: bool) -> None:
        print(f"  [{'ok' if condition else 'FAIL'}] {label}")
        if not condition:
            failures.append(label)

    try:
        print("=" * 70)
        print("1. ping must report the unreachable host as down")
        probe = subprocess.run(
            ["ping", "-c", "1", "-W", "2", UNREACHABLE],
            capture_output=True,
            check=False,
        )
        check("ping exits nonzero for TEST-NET-1", probe.returncode != 0)

        print("=" * 70)
        print("2. configure, over a real socket")
        result = cli("configure")
        check("exit 0", result.returncode == 0)
        check("verified exactly", "0 px differ" in result.stdout)
        check("calibration written", (workdir / "calibration.toml").exists())

        print("=" * 70)
        print("3. configure is idempotent -- byte-identical on a second run")
        first = (workdir / "calibration.toml").read_bytes()
        cli("configure")
        check(
            "second calibration is identical",
            (workdir / "calibration.toml").read_bytes() == first,
        )

        print("=" * 70)
        print("4. test-detect against the snapshot configure saved")
        result = cli("test-detect", str(workdir / "snapshots" / "configure.png"))
        check("reports MATCH", "MATCH" in result.stdout)
        check("exit 0", result.returncode == 0)

        print("=" * 70)
        print("5. run --once --no-act: host down, prompt on screen, no keystroke")
        before = len(server.keys)
        result = cli("run", "--once", "--no-act")
        check("exit 0", result.returncode == 0)
        check("no key was sent", len(server.keys) == before)
        check("suppression was logged", "key.suppressed" in result.stderr)

        print("=" * 70)
        print("6. run --once for real: the key is sent")
        before = len(server.keys)
        result = cli("run", "--once")
        check("exit 0", result.returncode == 0)
        check("key press and release sent", len(server.keys) == before + 2)
        check(
            "the key was Y",
            all(keysym == 0x59 for keysym, _ in server.keys[before:]),
        )
        check("keypress was logged", "key.pressed" in result.stderr)

        print("=" * 70)
        print("7. every examined frame reached the ring buffer")
        snapshots = sorted(p.name for p in (workdir / "snapshots").iterdir())
        print(f"  {snapshots}")
        check("a match frame was saved", any("match" in n for n in snapshots))

        print("=" * 70)
        print("8. a stale calibration is refused rather than trusted")
        stale = config.read_text(encoding="utf-8").replace(
            "Please press 'Y' to continue.", "Please press 'N' to continue."
        )
        config.write_text(stale, encoding="utf-8")
        before = len(server.keys)
        result = cli("run", "--once")
        check("no key was sent", len(server.keys) == before)
        check(
            "staleness was reported",
            "calibration" in (result.stderr + result.stdout).lower(),
        )

    finally:
        server.stop()

    print("\n" + "=" * 70)
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED:")
        for label in failures:
            print(f"  - {label}")
        return 1
    print("All end-to-end checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
