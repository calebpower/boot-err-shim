"""Atomic file writes and a single-instance lock.

Both exist because of tier 8 invariants rather than because the happy path
needs them.

*Atomic writes*: a SIGTERM landing between "open for write" and "write the
last byte" leaves a truncated calibration on disk. The daemon would then start
up, fail to parse it, and refuse to press keys -- turning a clean shutdown into
an outage. Write to a sibling temp file, fsync, rename.

*Single-instance lock*: two daemons watching one iDRAC can each decide the host
is down, each connect, and each press 'Y'. One stray keypress at a firmware
prompt is the worst thing this program can do, so the second instance must
refuse to start rather than race.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import TracebackType

from .errors import LockError

if os.name == "posix":
    import fcntl
else:  # pragma: no cover - development convenience on Windows
    import msvcrt

#: Byte offset the Windows lock is taken at.
#:
#: msvcrt.locking takes a mandatory byte-range lock, so locking from offset 0
#: would also stop anyone reading the pid we just wrote there. POSIX flock is
#: advisory and has no such problem. Locking a byte well past the pid text
#: keeps the file readable on both.
_WINDOWS_LOCK_OFFSET = 1024

#: How much of the lock file to read when reporting who holds it. Must stay
#: well under _WINDOWS_LOCK_OFFSET -- see _read_holder.
_HOLDER_READ_BYTES = 64


def _read_holder(path: Path) -> str:
    """Best-effort read of the pid recorded in a lock file.

    Unbuffered and bounded on purpose. A buffered read would pull in a whole
    block, and on Windows that block spans the mandatory lock at
    _WINDOWS_LOCK_OFFSET, so the read itself would fail with EACCES. Never
    raises: failing to name the holder must not mask the LockError.
    """
    try:
        with open(path, "rb", buffering=0) as handle:
            return handle.read(_HOLDER_READ_BYTES).decode("utf-8", "replace").strip()
    except OSError:
        return ""


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` such that readers see all of it or none.

    A reader may see the old file or the new one, never a partial one, and
    never a zero-length one.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        # Includes KeyboardInterrupt/SystemExit: a signal mid-write must not
        # leave the temp file behind either.
        tmp.unlink(missing_ok=True)
        raise

    if os.name == "posix":
        # Rename durability: without this the rename itself can be lost across
        # a power failure even though the data was fsynced.
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)


def atomic_write_text(path: Path, text: str, *, mode: int = 0o644) -> None:
    """UTF-8 wrapper around :func:`atomic_write_bytes`."""
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


class SingleInstanceLock:
    """Advisory whole-file lock, held for the lifetime of the context.

    The lock file is never unlinked. Deleting it on release opens a window
    where a second process has opened the same path just before the first
    unlinks it, and both then believe they hold a lock on files that are no
    longer the same inode.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = open(self.path, "a+b")
        except OSError as exc:
            raise LockError(f"{self.path}: cannot open lock file: {exc}") from exc

        try:
            if os.name == "posix":
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # pragma: no cover - development convenience on Windows
                handle.seek(_WINDOWS_LOCK_OFFSET)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            handle.close()
            holder = _read_holder(self.path)
            detail = f" (held by pid {holder})" if holder else ""
            raise LockError(
                f"{self.path}: another instance is already running{detail}"
            ) from exc

        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n".encode())
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "posix":
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:  # pragma: no cover - development convenience on Windows
                handle.seek(_WINDOWS_LOCK_OFFSET)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            handle.close()

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
