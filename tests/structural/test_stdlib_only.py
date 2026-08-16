"""Tier 3: the runtime must import nothing outside the standard library.

This is not a style preference. Two shipped promises depend on it:

* ``make bundle`` produces a single-file zipapp. A third-party import would
  make that bundle fail at runtime on a machine that has not installed it --
  and the machine it fails on is a FreeBSD box at 3am.
* ``pyproject.toml`` declares ``dependencies = []``. Nothing else checks that
  the declaration is true.

Enforced mechanically rather than remembered, because the failure is invisible
on a development machine that happens to have the package installed.
"""

from __future__ import annotations

import ast
import sys
import tomllib
import unittest

from tests import REPO_ROOT

SRC = REPO_ROOT / "src" / "boot_err_shim"

#: Our own package, which is obviously importable from within itself.
_OWN = {"boot_err_shim"}


def top_level_imports(path) -> set[str]:
    """Every top-level module name imported by a file, absolute imports only."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import: our own package by definition.
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


class TestStdlibOnly(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = sorted(SRC.rglob("*.py"))

    def test_there_are_sources_to_check(self) -> None:
        # Guards against the whole file passing because a path went stale.
        self.assertGreaterEqual(len(self.sources), 5)

    def test_no_third_party_imports(self) -> None:
        allowed = set(sys.stdlib_module_names) | _OWN
        for path in self.sources:
            with self.subTest(module=path.name):
                foreign = top_level_imports(path) - allowed
                self.assertEqual(
                    foreign,
                    set(),
                    f"{path.name} imports non-stdlib module(s): {sorted(foreign)}",
                )

    def test_pyproject_declares_no_runtime_dependencies(self) -> None:
        data = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(data["project"]["dependencies"], [])

    def test_requires_python_covers_tomllib(self) -> None:
        # tomllib landed in 3.11; claiming support for anything older would be
        # a lie that only shows up at import time.
        data = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(data["project"]["requires-python"], ">=3.11")


class TestNoAccidentalSideEffects(unittest.TestCase):
    """Importing the package must not touch the filesystem or the network.

    ``configure`` and the test suite both import modules long before any
    config is loaded; work at import time would run with no logging set up and
    no way to report failure.
    """

    def test_no_module_level_calls_into_os_or_socket(self) -> None:
        forbidden = {
            ("os", "makedirs"),
            ("os", "mkdir"),
            ("os", "remove"),
            ("socket", "socket"),
            ("subprocess", "run"),
        }
        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                for inner in ast.walk(node):
                    if not isinstance(inner, ast.Call):
                        continue
                    func = inner.func
                    if (
                        isinstance(func, ast.Attribute)
                        and isinstance(func.value, ast.Name)
                        and (func.value.id, func.attr) in forbidden
                    ):
                        # Only flag it if it is genuinely at module level,
                        # i.e. not nested inside a def or class body.
                        if isinstance(
                            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                        ):
                            continue
                        self.fail(
                            f"{path.name}: {func.value.id}.{func.attr}() at import time"
                        )


if __name__ == "__main__":
    unittest.main()
