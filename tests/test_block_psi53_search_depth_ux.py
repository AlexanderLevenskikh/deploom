from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class Psi53SearchDepthUxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dialog = (
            ROOT / "desktop" / "src" / "components" / "BaselineIntentDialog.tsx"
        ).read_text(encoding="utf-8")
        cls.css = (ROOT / "desktop" / "src" / "App.css").read_text(
            encoding="utf-8"
        )

    def test_execution_and_search_depth_are_independent_state(self) -> None:
        self.assertIn(
            "const [executionMode, setExecutionMode]",
            self.dialog,
        )
        self.assertIn(
            "const [searchDepth, setSearchDepth] = useState<BaselineSearchMode>",
            self.dialog,
        )
        self.assertIn(
            "searchMode = searchDepth as BaselineIntent['searchMode']",
            self.dialog,
        )
        self.assertIn(
            "searchMode: mode === 'decision' ? 'BOUNDED_IMPROVEMENT' : searchDepth",
            self.dialog,
        )

    def test_prepare_ui_exposes_both_dimensions(self) -> None:
        for sentinel in (
            "Как выполнять",
            "С контролем",
            "Работать автономно",
            "Глубина поиска",
            "AUTO · Рекомендуется",
            "EXHAUSTIVE",
            "Incumbent-first",
            "глубокую локализацию",
        ):
            self.assertIn(sentinel, self.dialog)

    def test_buttons_do_not_conflate_background_with_exhaustive(self) -> None:
        self.assertIn("onClick={() => setExecutionMode('FAST')}", self.dialog)
        self.assertIn(
            "onClick={() => setExecutionMode('BACKGROUND')}",
            self.dialog,
        )
        self.assertIn("onClick={() => setSearchDepth('AUTO')}", self.dialog)
        self.assertIn(
            "onClick={() => setSearchDepth('EXHAUSTIVE')}",
            self.dialog,
        )
        self.assertNotIn(
            "setExecutionMode('BACKGROUND'); setSearchDepth('EXHAUSTIVE')",
            self.dialog,
        )

    def test_primary_action_names_match_actual_combination(self) -> None:
        for sentinel in (
            "Запустить Fast Baseline",
            "Запустить AUTO автономно",
            "Запустить исчерпывающий Baseline",
            "Запустить исчерпывающий Baseline в фоне",
        ):
            self.assertIn(sentinel, self.dialog)
        self.assertNotIn("Запустить глубокий поиск", self.dialog)

    def test_selection_is_part_of_dirty_and_plan_sync(self) -> None:
        self.assertIn(
            "setSearchDepth(normalizedSearchMode(plan.intent.searchMode))",
            self.dialog,
        )
        self.assertIn(
            "searchDepth !== normalizedSearchMode(plan.intent.searchMode)",
            self.dialog,
        )

    def test_mode_controls_have_dedicated_layout(self) -> None:
        for sentinel in (
            ".baseline-run-controls",
            ".baseline-run-control",
            ".baseline-mode-toggle",
            ".baseline-mode-toggle button.active",
        ):
            self.assertIn(sentinel, self.css)


if __name__ == "__main__":
    unittest.main()
