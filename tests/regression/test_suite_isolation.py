from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import run_tool_tests


class ToolSuiteIsolationTests(unittest.TestCase):
    def relative(self, paths):
        return [path.relative_to(ROOT).as_posix() for path in paths]

    def test_unit_command_does_not_collect_regression_suite(self) -> None:
        files = self.relative(run_tool_tests.files_for("unit"))
        self.assertNotIn("tests/regression/test_prompt_test_contract.py", files)
        self.assertIn("tests/test_dependency_roadmap.py", files)
        self.assertTrue(all(not path.startswith("tests/regression/") for path in files))

    def test_regression_command_collects_only_regression_suite(self) -> None:
        files = self.relative(run_tool_tests.files_for("regression"))
        self.assertIn("tests/regression/test_prompt_test_contract.py", files)
        self.assertNotIn("tests/test_dependency_roadmap.py", files)
        self.assertTrue(all(path.startswith("tests/regression/") for path in files))


if __name__ == "__main__":
    unittest.main()
