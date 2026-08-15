from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import manual_dependency_audit as manual


class WindowsCommandResolutionRegressionTests(unittest.TestCase):
    def test_run_resolves_package_manager_launcher_before_spawn(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[r"C:\\tools\\npm.cmd", "--version"],
            returncode=0,
            stdout="10.0.0\n",
            stderr="",
        )
        with patch.object(manual.shutil, "which", return_value=r"C:\\tools\\npm.cmd") as which_mock, \
             patch.object(manual.subprocess, "run", return_value=completed) as run_mock:
            code, stdout, stderr = manual.run(["npm", "--version"], Path.cwd())

        self.assertEqual(0, code)
        self.assertEqual("10.0.0\n", stdout)
        self.assertEqual("", stderr)
        which_mock.assert_called_once_with("npm")
        self.assertEqual(r"C:\\tools\\npm.cmd", run_mock.call_args.args[0][0])

    def test_run_reports_missing_launcher_without_spawning(self) -> None:
        with patch.object(manual.shutil, "which", return_value=None), \
             patch.object(manual.subprocess, "run") as run_mock:
            code, stdout, stderr = manual.run(["npm", "audit", "--json"], Path.cwd())

        self.assertEqual(127, code)
        self.assertEqual("", stdout)
        self.assertIn("executable not found on PATH", stderr)
        run_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
