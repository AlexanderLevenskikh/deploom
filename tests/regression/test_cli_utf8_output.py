from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class CliUtf8OutputRegressionTests(unittest.TestCase):
    def test_stdio_is_reconfigured_from_legacy_code_page_to_utf8(self) -> None:
        env = dict(os.environ)
        # Reproduce Windows PowerShell/Python 3.10 behavior even on non-Windows CI.
        env["PYTHONIOENCODING"] = "cp1251:strict"
        program = (
            "import sys; "
            "from cli_io import configure_utf8_stdio; "
            "configure_utf8_stdio(); "
            "print('≤ → —'); "
            "print('UTF-8 ✓', file=sys.stderr)"
        )
        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=ROOT,
            env=env,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", errors="replace"))
        self.assertEqual("≤ → —", result.stdout.decode("utf-8").strip())
        self.assertEqual("UTF-8 ✓", result.stderr.decode("utf-8").strip())

    def test_manual_audit_configures_utf8_before_printing_report(self) -> None:
        source = (ROOT / "manual_dependency_audit.py").read_text(encoding="utf-8")
        main_body = source[source.index("def main("):source.index("if __name__ == \"__main__\":")]
        self.assertLess(main_body.index("configure_utf8_stdio()"), main_body.index("print(md)"))


if __name__ == "__main__":
    unittest.main()
