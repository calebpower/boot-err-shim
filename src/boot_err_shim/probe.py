"""Reachability probing by shelling out to the system ``ping``.

Shelling out rather than opening a raw socket is deliberate: raw ICMP needs
privilege we otherwise do not want, and ``ping(8)`` is already setuid on
FreeBSD and permitted for ordinary users on Linux via
``net.ipv4.ping_group_range``. The daemon therefore runs unprivileged.

The distinction this module is careful about: **a host that is down is not an
error.** It is the ordinary negative result the whole program is built around.
:class:`~boot_err_shim.errors.ProbeError` is raised only when the probe could
not be *performed* -- no ping binary, or it could not be spawned.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .errors import ProbeError
from .platform_ import render_ping_command

#: What a runner must return: (exit status, combined output).
RunnerResult = tuple[int, str]
Runner = Callable[[Sequence[str], int], RunnerResult]


class ProbeTimeout(Exception):
    """Internal signal from a runner that the probe exceeded its timeout."""


@dataclass(frozen=True)
class ProbeResult:
    up: bool
    #: Stable reason token for logging: ok | unreachable | timeout
    reason: str
    output: str = ""


def _default_runner(command: Sequence[str], timeout: int) -> RunnerResult:
    try:
        completed = subprocess.run(  # noqa: S603 - command comes from config
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProbeError(f"ping command not found: {command[0]!r}") from exc
    except PermissionError as exc:
        raise ProbeError(f"cannot execute {command[0]!r}: {exc}") from exc
    except OSError as exc:
        raise ProbeError(f"cannot run ping: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeTimeout from exc

    return completed.returncode, completed.stdout.decode("utf-8", "replace")


class Prober:
    """Runs one ping per call and reports whether the host answered."""

    def __init__(
        self,
        command: Sequence[str],
        timeout: int,
        runner: Runner | None = None,
    ) -> None:
        self.command = tuple(command)
        self.timeout = timeout
        self._runner = runner or _default_runner

    def probe(self, host: str) -> ProbeResult:
        command = render_ping_command(self.command, host)
        try:
            status, output = self._runner(command, self.timeout)
        except ProbeTimeout:
            # ping outlived its own -W as well as our backstop. From the
            # daemon's point of view that is indistinguishable from down, and
            # treating it as an error would stall the state machine.
            return ProbeResult(up=False, reason="timeout")

        output = output.strip()
        if status == 0:
            return ProbeResult(up=True, reason="ok", output=output)
        return ProbeResult(up=False, reason="unreachable", output=output)
