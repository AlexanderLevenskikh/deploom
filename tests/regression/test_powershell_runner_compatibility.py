from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PowerShellRunnerCompatibilityTests(unittest.TestCase):
    def assert_compatible_runner(self, script_name: str, suite_name: str) -> None:
        script = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")

        # Windows PowerShell 5.1 Join-Path accepts only Path + ChildPath here;
        # a third positional segment raises PositionalParameterNotFound.
        self.assertNotIn("Join-Path $PSScriptRoot '..' 'run_tool_tests.py'", script)
        self.assertIn("$toolRoot = Split-Path -Parent $PSScriptRoot", script)
        self.assertIn("$runner = Join-Path $toolRoot 'run_tool_tests.py'", script)
        self.assertIn(f"& python $runner --suite {suite_name}", script)

    def test_unit_runner_is_windows_powershell_compatible(self) -> None:
        self.assert_compatible_runner("test-tool.ps1", "unit")

    def test_regression_runner_is_windows_powershell_compatible(self) -> None:
        self.assert_compatible_runner("test-tool-regression.ps1", "regression")


if __name__ == "__main__":
    unittest.main()
