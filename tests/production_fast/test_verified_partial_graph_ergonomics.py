from __future__ import annotations

import unittest
from pathlib import Path


class VerifiedPartialGraphErgonomicsContracts(unittest.TestCase):
    def test_accepted_result_has_finish_and_improve_paths(self) -> None:
        root = Path(__file__).resolve().parents[2]
        flow = (root / "desktop/src/components/FlowWorkspace.tsx").read_text(encoding="utf-8")
        dialog = (root / "desktop/src/components/BaselineIntentDialog.tsx").read_text(encoding="utf-8")

        panel = (root / "desktop/src/components/IterativeTaskPanel.tsx").read_text(encoding="utf-8")
        scenario = (root / "desktop/electron/iterative-scenario.ts").read_text(encoding="utf-8")
        # The iterative result owns completion and restart; deferred scope is
        # still configurable without reviving the removed separate FLOW panel.
        for required in (
            "IterativeTaskPanel", "onConfigureScope", "onGetBaselineIntentPlan",
            "acceptanceAccepted", "openDeferredImprovementDialog",
        ):
            self.assertIn(required, flow)
        for required in ("Посмотреть результат", "Начать заново", "restart: true"):
            self.assertIn(required, panel)
        self.assertIn('case "result":', scenario)
        self.assertIn("independently-audited, policy-satisfied", scenario)
        self.assertNotIn("Прежний FLOW / история (отдельный запуск)", flow)
        for forbidden in (
            "VERIFIED_PARTIAL_SCOPE",
            "Завершить с текущим verified результатом",
        ):
            self.assertNotIn(forbidden, flow)

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

    def test_graph_can_take_over_window_or_hide_project_rail(self) -> None:
        root = Path(__file__).resolve().parents[2]
        app = (root / "desktop/src/App.tsx").read_text(encoding="utf-8")
        graph = (root / "desktop/src/components/DependencyGraphWorkspace.tsx").read_text(encoding="utf-8")
        css = (root / "desktop/src/App.css").read_text(encoding="utf-8")

        # Human FLOW no longer mounts a permanent MonitoringPanel. Graph may
        # still take over the window or hide the project rail, but there is no
        # dead right-pane toggle/state for a monitor that no longer exists.
        for required in ("graphFullscreen", "graphLeftHidden", "graph-hide-project-rail"):
            self.assertIn(required, app)
        for forbidden in ("graphRightHidden", "graph-hide-monitor"):
            self.assertNotIn(forbidden, app)
        for required in ("onToggleFullscreen", "onToggleLeftPane", "Maximize2", "PanelLeftClose"):
            self.assertIn(required, graph)
        for forbidden in ("onToggleRightPane", "rightPaneHidden", "PanelRightClose", "PanelRightOpen", "Скрыть мониторинг"):
            self.assertNotIn(forbidden, graph)
        self.assertIn(".app-shell.graph-fullscreen", css)
        self.assertIn(".app-grid.graph-hide-project-rail { grid-template-columns: minmax(0, 1fr); }", css)
        self.assertNotIn("graph-hide-monitor", css)

    def test_graph_remains_projection_only(self) -> None:
        root = Path(__file__).resolve().parents[2]
        contract = (root / "desktop/electron/dependency-graph.ts").read_text(encoding="utf-8")
        self.assertIn("proofAuthority: false", contract)
        self.assertIn("OBSERVED_LOCAL_MANIFEST", contract)


if __name__ == "__main__":
    unittest.main()
