from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "validate_dependency_update.py"


class AuditWorkspaceCleanupValidatorRegressionTests(unittest.TestCase):
    def test_final_validator_fails_when_audit_workspace_remains(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            (project / "package.json").write_text(json.dumps({"dependencies": {"foo": "1.0.0"}}), encoding="utf-8")
            (project / ".dependency-roadmap-audit").mkdir()
            roadmap = root / "roadmap.json"
            roadmap.write_text(json.dumps({"projects": {"Demo": [{"project": "Demo", "name": "foo", "kind": "runtime", "current": "1.0.0", "target_yellow": "1.0.0"}]}}), encoding="utf-8")
            result = subprocess.run([
                sys.executable, str(VALIDATOR),
                "--roadmap-json", str(roadmap),
                "--project", "Demo",
                "--project-dir", str(project),
                "--target-mode", "yellow",
                "--audit-workspace", ".dependency-roadmap-audit",
                "--require-audit-workspace-removed",
                "--json",
            ], text=True, encoding="utf-8", capture_output=True, check=False)
            self.assertEqual(1, result.returncode)
            self.assertIn("audit-workspace-not-removed", result.stdout)


if __name__ == "__main__":
    unittest.main()
