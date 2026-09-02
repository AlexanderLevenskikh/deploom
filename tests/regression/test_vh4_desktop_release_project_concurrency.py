from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class DesktopReleaseAndProjectConcurrencyRegression(unittest.TestCase):
    def test_release_helper_runs_public_sanitization_before_publish(self):
        text = (ROOT / "push-branch-and-tag.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("scripts/check-public-sanitization.py", text)
        self.assertIn("check-public-sanitization failed", text)
        self.assertLess(
            text.index("scripts/check-public-sanitization.py"),
            text.index("== Push branch =="),
        )

    def test_add_project_dialog_surfaces_submit_failures(self):
        text = (ROOT / "desktop/src/components/AddProjectDialog.tsx").read_text(encoding="utf-8")
        for sentinel in (
            "setSubmitError",
            "catch (error)",
            'role="alert"',
            'type="submit"',
        ):
            self.assertIn(sentinel, text)

    def test_baseline_is_project_scoped_but_shared_mutators_stay_workspace_scoped(self):
        text = (ROOT / "desktop/electron/main.ts").read_text(encoding="utf-8")
        for sentinel in (
            "PROJECT_BACKGROUND_ACTIONS",
            "WORKSPACE_GLOBAL_ACTIONS",
            "'preflight', 'baseline'",
            "'sync-tool', 'generate-all', 'commit-state', 'push-workspace'",
            "projectRunConflicts",
            "baseline-project-output",
            "'--no-history-snapshot'",
        ):
            self.assertIn(sentinel, text)
        self.assertIn("existing.projectName === project.name", text)
        self.assertIn("WORKSPACE_GLOBAL_ACTIONS.has(existing.action)", text)
        self.assertIn("PROJECT_BACKGROUND_ACTIONS.has(existing.action)", text)
        self.assertIn("projectRunConflicts(existing, workspace, project, 'recover')", text)


if __name__ == "__main__":
    unittest.main()
