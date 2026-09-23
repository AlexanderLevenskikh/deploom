from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class Psi53ProgressiveControlUxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dialog = (ROOT / "desktop" / "src" / "components" / "BaselineIntentDialog.tsx").read_text(encoding="utf-8")
        cls.css = (ROOT / "desktop" / "src" / "App.css").read_text(encoding="utf-8")

    def test_product_control_and_algorithm_depth_are_independent(self) -> None:
        self.assertIn("const [controlMode, setControlMode]", self.dialog)
        self.assertIn("const [searchDepth, setSearchDepth] = useState<BaselineSearchMode>", self.dialog)
        self.assertIn("searchMode = searchDepth as BaselineIntent['searchMode']", self.dialog)
        self.assertIn("executionMode: nextControlMode === 'AUTONOMOUS' ? 'BACKGROUND' : 'FAST'", self.dialog)
        self.assertNotIn("const [executionMode, setExecutionMode]", self.dialog)

    def test_happy_path_exposes_progressive_semantics_and_freshness_goal(self) -> None:
        for sentinel in ("Как работать", "Автономно", "Подтверждать существенное", "Acceptance policy", "Бюджет Baseline / планирования"):
            self.assertIn(sentinel, self.dialog)
        # R9: the numeric freshness goal is a first-class product control, part
        # of the versioned policy hash (not a hidden or hardcoded threshold).
        self.assertIn("Минимум актуальности", self.dialog)
        self.assertIn("Minimum lag compliance", self.dialog)
        self.assertIn("Цель запуска", self.dialog)
        self.assertIn("targetLevel === 'green' ? 'green' : 'yellow'", self.dialog)
        self.assertIn("boundedInteger(nextMinLagOkPct, 80, 0, 100)", self.dialog)
        self.assertIn("Входит в policy hash", self.dialog)

    def test_search_depth_remains_technical_escape_hatch(self) -> None:
        self.assertIn("Техническая глубина поиска", self.dialog)
        self.assertIn("onClick={() => setSearchDepth('AUTO')}", self.dialog)
        self.assertIn("onClick={() => setSearchDepth('EXHAUSTIVE')}", self.dialog)
        self.assertNotIn("setExecutionMode('BACKGROUND')", self.dialog)

    def test_primary_action_does_not_sell_exhaustive_as_the_product(self) -> None:
        self.assertIn("Запустить автономно", self.dialog)
        self.assertIn("Запустить Baseline", self.dialog)
        self.assertNotIn("Запустить исчерпывающий Baseline", self.dialog)

    def test_v2_mutable_settings_participate_in_dirty_state_and_critical_is_fixed(self) -> None:
        for sentinel in (
            "controlMode !== normalizedControlMode(plan.intent)",
            "budgetMinutes !== boundedInteger",
            "maxKnownHigh !== boundedInteger",
            "searchDepth !== normalizedSearchMode",
            "cohortFingerprint(deferredCohorts)",
            # R9: the freshness goal participates in the dirty fingerprint and
            # buildIntent so a changed gate is never silently dropped.
            "targetLevel !== (plan.intent.targetLevel === 'green' ? 'green' : 'yellow')",
            "minLagOkPct !== boundedInteger(plan.intent.minLagOkPct, 80, 0, 100)",
            "nextTargetLevel = targetLevel",
            "nextMinLagOkPct = minLagOkPct",
        ):
            self.assertIn(sentinel, self.dialog)
        # Critical=0 is a backend/product invariant, not mutable dialog state.
        self.assertNotIn("maxKnownCritical !== boundedInteger", self.dialog)
        self.assertNotIn("setMaxKnownCritical", self.dialog)
        self.assertNotIn("value={maxKnownCritical}", self.dialog)
        self.assertIn("maxKnownCritical: 0", self.dialog)
        self.assertIn("schemaVersion: 2", self.dialog)

    def test_controls_keep_existing_layout_contract(self) -> None:
        for sentinel in (".baseline-run-controls", ".baseline-run-control", ".baseline-mode-toggle", ".baseline-mode-toggle button.active"):
            self.assertIn(sentinel, self.css)


if __name__ == "__main__":
    unittest.main()
