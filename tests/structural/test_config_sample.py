"""Tier 3: the sample config and the parser must describe the same program.

The two rot in opposite directions and neither is detectably wrong alone:

* A key the parser accepts but the sample omits is an undiscoverable feature.
* A key the sample shows but the parser rejects is worse -- the file we ship as
  a starting point fails to load, and since unknown keys are a hard error, it
  fails at startup rather than being ignored.

Rather than maintain a third list that would rot too, the parser's key set is
recovered by parsing ``config.py`` as data.
"""

from __future__ import annotations

import ast
import re
import tomllib
import unittest

from tests import REPO_ROOT

CONFIG_PY = REPO_ROOT / "src" / "boot_err_shim" / "config.py"
SAMPLE = REPO_ROOT / "boot-err-shim.conf.sample"

#: _Table methods that consume a named key. `table` returns a sub-table and
#: `finish` takes no key, so both are excluded.
_READER_METHODS = {
    "str_",
    "opt_str",
    "int_",
    "float_",
    "bool_",
    "duration",
    "str_list",
    "choice",
    "path",
    "opt_path",
}


def parser_keys() -> dict[str, set[str]]:
    """Recover ``{table: {key, ...}}`` from the source of ``parse_config``."""
    tree = ast.parse(CONFIG_PY.read_text(encoding="utf-8"))

    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "parse_config"
    )

    # local variable name -> table name, from `x_t = root.table("name")`
    tables: dict[str, str] = {}
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target, value = node.targets[0], node.value
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
            continue
        func = value.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "table"
            and isinstance(func.value, ast.Name)
            and func.value.id == "root"
            and value.args
            and isinstance(value.args[0], ast.Constant)
        ):
            tables[target.id] = value.args[0].value

    keys: dict[str, set[str]] = {name: set() for name in tables.values()}
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _READER_METHODS
            and isinstance(func.value, ast.Name)
            and func.value.id in tables
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            keys[tables[func.value.id]].add(node.args[0].value)
    return keys


def sample_keys() -> dict[str, set[str]]:
    """``{table: {key, ...}}`` from the sample, counting commented-out keys.

    A key that is commented out is still documented -- that is how the
    platform-detected settings are presented -- so it counts as present.
    """
    text = SAMPLE.read_text(encoding="utf-8")
    live = {
        table: set(values)
        for table, values in tomllib.loads(text).items()
        if isinstance(values, dict)
    }

    section = None
    commented = re.compile(r"^#\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
    header = re.compile(r"^\[([A-Za-z_][A-Za-z0-9_]*)\]\s*$")
    for line in text.splitlines():
        stripped = line.strip()
        match = header.match(stripped)
        if match:
            section = match.group(1)
            live.setdefault(section, set())
            continue
        match = commented.match(stripped)
        if match and section is not None:
            live[section].add(match.group(1))
    return live


class TestSampleMatchesParser(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = parser_keys()
        self.sample = sample_keys()

    def test_the_extractor_found_something(self) -> None:
        # An extractor that silently matches nothing would make every other
        # assertion in this file vacuously true.
        self.assertGreaterEqual(len(self.parser), 6)
        self.assertIn("ping", self.parser)
        self.assertIn("threshold", self.parser["ping"])
        self.assertIn("text", self.parser["detect"])

    def test_same_tables(self) -> None:
        self.assertEqual(set(self.parser), set(self.sample))

    def test_no_parser_key_is_undocumented(self) -> None:
        for table, keys in self.parser.items():
            with self.subTest(table=table):
                self.assertEqual(
                    keys - self.sample.get(table, set()),
                    set(),
                    f"[{table}] accepts keys the sample never mentions",
                )

    def test_no_sample_key_is_unrecognized(self) -> None:
        for table, keys in self.sample.items():
            with self.subTest(table=table):
                self.assertEqual(
                    keys - self.parser.get(table, set()),
                    set(),
                    f"[{table}] documents keys the parser would reject",
                )


class TestSampleContent(unittest.TestCase):
    def test_sample_documents_the_real_error_text(self) -> None:
        data = tomllib.loads(SAMPLE.read_text(encoding="utf-8"))
        text = data["detect"]["text"]
        self.assertIn("flash part has gone bad", text)
        self.assertIn("Please press 'Y' to continue.", text)

    def test_sample_warns_about_the_ping_flag_units(self) -> None:
        # If this comment ever disappears, the single most dangerous
        # cross-platform footgun becomes undocumented.
        text = SAMPLE.read_text(encoding="utf-8")
        self.assertIn("MILLISECONDS", text)
        self.assertIn("SECONDS", text)

    def test_sample_tells_the_operator_to_protect_the_password(self) -> None:
        self.assertIn("chmod 600", SAMPLE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
