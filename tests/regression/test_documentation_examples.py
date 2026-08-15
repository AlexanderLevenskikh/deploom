from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"


class DocumentationExamplesRegressionTests(unittest.TestCase):
    def test_all_tool_json_templates_are_valid_objects(self) -> None:
        paths = sorted(TEMPLATES.glob("*.json"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path.name):
                self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)

    def _assert_current_settings(self, settings: dict) -> None:
        self.assertTrue(settings["sourceCheckoutGuard"])
        self.assertNotIn("auditBootstrap", settings)
        project = settings["projects"][0]
        self.assertEqual("origin", project["git"]["remote"])
        for key in ("sourceBranch", "baseBranch", "branchPrefix", "mergedBranch", "releaseBranch", "push"):
            self.assertIn(key, project["git"])
        self.assertEqual("skip", settings["gitHooks"]["intermediateCommits"])
        self.assertEqual("run", settings["gitHooks"]["releaseCommit"])
        self.assertEqual("squash", settings["release"]["strategy"])
        self.assertEqual("validate", settings["lockfileSync"]["baselineMode"])
        self.assertEqual("update", settings["lockfileSync"]["currentMode"])
        self.assertFalse(settings["lockfileSync"]["allowExtraLockfiles"])
        self.assertEqual("auto", settings["lockfileSync"]["yarnDeduplicate"])

    def test_tool_settings_example_matches_current_contract(self) -> None:
        settings = json.loads((TEMPLATES / "settings.example.json").read_text(encoding="utf-8"))
        self._assert_current_settings(settings)
        for project in settings["projects"]:
            self.assertEqual("origin", project["git"]["remote"])
            self.assertIn("releaseBranch", project["git"])

    def test_readmes_document_unified_product_and_state_repository(self) -> None:
        root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
        desktop_readme = (ROOT / "desktop" / "README.md").read_text(encoding="utf-8")
        for token in ("VERSION", "desktop/package.json", "knowledge", "history", "artifacts"):
            self.assertIn(token, root_readme)
        for token in ("team-state repository", "resources/tool", "electron-updater"):
            self.assertIn(token, desktop_readme)
        self.assertNotIn("scripts/audit-project", root_readme)


if __name__ == "__main__":
    unittest.main()
