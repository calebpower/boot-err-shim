"""Test package.

Makes ``python -m unittest discover tests`` work from a bare checkout, without
requiring ``uv sync`` first. When the package is properly installed this shim
does nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

#: Repository root, for the structural tier which reads our own source.
REPO_ROOT = Path(__file__).resolve().parent.parent
