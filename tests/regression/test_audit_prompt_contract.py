from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "dependency_live_roadmap_generator.py"
MANUAL_AUDIT = ROOT / "manual_dependency_audit.py"


class AuditPromptContractRegressionTests(unittest.TestCase):
    def test_prompts_use_one_manager_and_bundled_manual_audit(self) -> None:
        source = GENERATOR.read_text(encoding="utf-8")
        for token in (
            "automatic audit bootstrap is not part of generation",
            "manual-only",
            "manual_dependency_audit.py",
            "yarn.lock",
            "package-lock.json/npm-shrinkwrap.json",
            "dependency_release_branch.py",
            "git_hook_policy.py",
            "releaseBranch",
            "RELEASE_COMMIT_OR_HOOK_FAILED",
        ):
            self.assertIn(token, source)
        self.assertNotIn("scripts/audit-project", source)
        self.assertNotIn("dependency_audit_branch.py capture", source)
        self.assertNotIn("current Nexus audit", source)

    def test_manual_audit_tool_and_report_metadata_are_shipped_together(self) -> None:
        self.assertTrue(MANUAL_AUDIT.is_file())
        source = GENERATOR.read_text(encoding="utf-8")
        self.assertIn('"toolPath": str(Path(__file__).resolve().parent / "manual_dependency_audit.py")', source)
        self.assertIn('"mode": "manual-only"', source)

    def test_generator_marks_legacy_audit_setting_as_ignored(self) -> None:
        source = GENERATOR.read_text(encoding="utf-8")
        self.assertIn("auditBootstrap is deprecated and ignored", source)
        self.assertIn("generate does not run vulnerability audit", source)


if __name__ == "__main__":
    unittest.main()
