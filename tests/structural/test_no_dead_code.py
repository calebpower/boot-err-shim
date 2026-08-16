"""Tier 3: nothing in src/ is defined and never used.

This is an application, not a library. Nothing imports it, so a public
function nobody calls is not an API -- it is a claim about behaviour that does
not happen.

The one that prompted this check was `errors.CalibrationNotFound`: a
plausible, well-named exception class that was never raised anywhere. A reader
would reasonably assume a missing calibration produced it. It did not; the
generic CalibrationError was raised instead, and the distinction the class
promised did not exist. That is worse than the class being absent, because it
reads as a guarantee.

Dead code is also untested code by definition, so it quietly drags coverage
claims down with it.
"""

from __future__ import annotations

import ast
import unittest

from tests import REPO_ROOT

SRC = REPO_ROOT / "src" / "boot_err_shim"

#: Places a name can legitimately be referenced from.
SEARCHED = (
    ("src", "*.py"),
    ("tests", "*.py"),
    ("tools", "*.py"),
    ("containers", "*.py"),
)

#: Files that reference names in prose or shell rather than Python.
EXTRA_FILES = (
    "containers/stages.sh",
    "README.md",
    "docs/testing.md",
    "boot-err-shim.conf.sample",
)

#: Names that are entry points or protocol members, referenced by machinery
#: rather than by a caller we can see. Each needs a reason.
ALLOWED = {
    # console_scripts entry point in pyproject.toml.
    "main",
}


def public_definitions() -> dict[str, str]:
    """Top-level public functions and classes in src/, name -> file."""
    found: dict[str, str] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.name.startswith("_"):
                    found[node.name] = path.name
    return found


def reference_corpus() -> str:
    chunks = []
    for directory, pattern in SEARCHED:
        root = REPO_ROOT / directory
        if not root.is_dir():
            continue
        for path in root.rglob(pattern):
            if "__pycache__" in path.parts:
                continue
            chunks.append(path.read_text(encoding="utf-8"))
    for name in EXTRA_FILES:
        path = REPO_ROOT / name
        if path.exists():
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


class TestNoDeadCode(unittest.TestCase):
    def setUp(self) -> None:
        self.definitions = public_definitions()
        self.corpus = reference_corpus()

    def test_the_scan_found_the_source(self) -> None:
        # Guards the rest of the file from passing because a path went stale.
        self.assertGreaterEqual(len(self.definitions), 20)
        self.assertIn("Calibration", self.definitions)
        self.assertGreater(len(self.corpus), 100_000)

    def test_every_public_definition_is_referenced(self) -> None:
        dead = []
        for name, where in sorted(self.definitions.items()):
            if name in ALLOWED:
                continue
            uses = self.corpus.count(name)
            declarations = self.corpus.count(f"def {name}") + self.corpus.count(
                f"class {name}"
            )
            if uses - declarations <= 0:
                dead.append(f"{where}:{name}")

        self.assertEqual(
            dead,
            [],
            "defined in src/ and referenced nowhere. An application has no "
            "external callers, so this is not API -- delete it, or wire it up.",
        )

    def test_every_allowance_is_still_needed(self) -> None:
        # An allowance for a name that no longer exists is a stale excuse.
        for name in ALLOWED:
            with self.subTest(name=name):
                self.assertIn(name, self.definitions)


class TestEveryErrorClassIsRaised(unittest.TestCase):
    """A defined-but-never-raised error is a promise the code does not keep."""

    def error_classes(self) -> set[str]:
        tree = ast.parse((SRC / "errors.py").read_text(encoding="utf-8"))
        return {
            node.name
            for node in tree.body
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_")
        }

    def raised_or_caught(self) -> set[str]:
        names: set[str] = set()
        for path in sorted(SRC.rglob("*.py")):
            if path.name == "errors.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Raise) and node.exc is not None:
                    target = node.exc
                    if isinstance(target, ast.Call):
                        target = target.func
                    if isinstance(target, ast.Name):
                        names.add(target.id)
                elif isinstance(node, ast.ExceptHandler) and node.type is not None:
                    handlers = (
                        node.type.elts
                        if isinstance(node.type, ast.Tuple)
                        else [node.type]
                    )
                    for handler in handlers:
                        if isinstance(handler, ast.Name):
                            names.add(handler.id)
        return names

    def test_the_scan_found_the_hierarchy(self) -> None:
        self.assertIn("ShimError", self.error_classes())
        self.assertGreaterEqual(len(self.error_classes()), 8)

    def test_every_error_class_is_raised_or_handled_somewhere(self) -> None:
        # ShimError is the root: caught at the CLI, never raised directly.
        base = {"ShimError"}
        unused = self.error_classes() - self.raised_or_caught() - base
        self.assertEqual(
            unused,
            set(),
            "declared in errors.py but never raised or caught. A named error "
            "nobody produces reads as a distinction the program makes, and it "
            "does not.",
        )


if __name__ == "__main__":
    unittest.main()
