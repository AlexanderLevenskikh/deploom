from __future__ import annotations

import unittest
from pathlib import Path


class VerifiedPartialGraphErgonomicsContracts(unittest.TestCase):
    def test_verified_partial_has_finish_and_improve_paths(self) -> None:
        root = Path(__file__).resolve().parents[2]
        flow = (root / "desktop/src/components/FlowWorkspace.tsx").read_text(encoding="utf-8")
        dialog = (root / "desktop/src/components/BaselineIntentDialog.tsx").read_text(encoding="utf-8")

        # FlowWorkspace owns the product decision and the hand-off into the
        # Baseline intent dialog.
        for required in (
            "VERIFIED_PARTIAL_SCOPE",
            "Завершить с текущим verified результатом",
            "Продолжить улучшение",
            "openDeferredImprovementDialog",
            "onGetBaselineIntentPlan",
            "resume: 'restart'",
        ):
            self.assertIn(required, flow)

        # The persisted deferred-cohort queue is intentionally owned/rendered
        # by BaselineIntentDialog, not duplicated in FlowWorkspace.
        for required in (
            "deferredCohorts",
            "reactivateCohort",
            "Вернуться к этой группе",
            "cohortAction",
            "REACTIVATE",
        ):
            self.assertIn(required, dialog)

    def test_graph_can_take_over_window_or_hide_side_panes(self) -> None:
        root = Path(__file__).resolve().parents[2]
        app = (root / "desktop/src/App.tsx").read_text(encoding="utf-8")
        graph = (root / "desktop/src/components/DependencyGraphWorkspace.tsx").read_text(encoding="utf-8")
        css = (root / "desktop/src/App.css").read_text(encoding="utf-8")
        for required in ("graphFullscreen", "graphLeftHidden", "graphRightHidden", "graph-hide-project-rail", "graph-hide-monitor"):
            self.assertIn(required, app)
        for required in ("onToggleFullscreen", "onToggleLeftPane", "onToggleRightPane", "Maximize2", "PanelLeftClose", "PanelRightClose"):
            self.assertIn(required, graph)
        self.assertIn(".app-shell.graph-fullscreen", css)
        self.assertIn(".app-grid.graph-hide-project-rail.graph-hide-monitor", css)

    def test_graph_remains_projection_only(self) -> None:
        root = Path(__file__).resolve().parents[2]
        contract = (root / "desktop/electron/dependency-graph.ts").read_text(encoding="utf-8")
        self.assertIn("proofAuthority: false", contract)
        self.assertIn("OBSERVED_LOCAL_MANIFEST", contract)


if __name__ == "__main__":
    unittest.main()
