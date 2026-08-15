from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import dependency_live_roadmap_generator as roadmap


class PromptCommentPolicyRegression(unittest.TestCase):
    def test_dashboard_exports_explicit_yes_and_no_comment_policies(self) -> None:
        row = roadmap.DependencyRow(
            project="Demo", package_dir=".", name="vite", kind="dev", requested_spec="4.0.0",
            current_version="4.0.0", current_source="yarn.lock", latest_version="5.0.0",
            current_vulns="0", min_no_critical="4.0.0", min_no_high="4.0.0", min_no_vuln="4.0.0",
            min_lag_12m="5.0.0", min_lag_9m="5.0.0", min_lag_6m="5.0.0", min_lag_3m="5.0.0",
            group=4, reason="build migration", notes="", lag_threshold_months=12,
            target_default="5.0.0", target_yellow="5.0.0", target_green="5.0.0",
            target_default_reason="lag", target_yellow_reason="lag", target_green_reason="lag",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "report.html"
            roadmap.write_html(
                {"Demo": [row]}, out,
                history_dir=root / "history",
                project_specs={"Demo": roadmap.ProjectSpec("Demo", root / "demo", source_branch="main")},
                roadmap_json_path=root / "roadmap.json",
                history_snapshots=[],
            )
            html = out.read_text(encoding="utf-8")
            self.assertIn('id="detailedCodeComments"', html)
            self.assertIn("mode: 'detailed-why-comments'", html)
            self.assertIn("mode: 'minimal-comments'", html)
            self.assertIn("не оставляй временный migration diary", html)
            self.assertIn("Не добавляй подробные поясняющие комментарии", html)
            self.assertIn("codeCommentPolicy: commentPolicy.mode", html)
            self.assertIn("Сохрани выбранную policy в run state/evidence", html)
            self.assertIn(r"Carry \`codeCommentPolicy=${commentPolicy.mode}\`", html)
            self.assertIn("'detailedCodeComments'", html)
            self.assertIn("savePromptOptionsToDashboardState", html)
            self.assertIn("dashboardState.promptOptions?.detailedCodeComments", html)


if __name__ == "__main__":
    unittest.main()
