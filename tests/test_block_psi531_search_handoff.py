from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dependency_live_roadmap_generator as generator
from block_psi_anytime import BaselineSearchMode


class Psi531NavigationObjectiveTests(unittest.TestCase):
    def test_diagnostic_preference_becomes_soft_stability_target(self) -> None:
        result = generator._solver_navigation_stability_targets(
            residual_targets={},
            diagnostic_preferences={"@vitejs/plugin-react": "4.3.2"},
            domains={
                "@vitejs/plugin-react": ["5.1.2", "4.3.2", "2.0.0"],
                "vite": ["4.3.9"],
            },
        )
        self.assertEqual({"@vitejs/plugin-react": "4.3.2"}, result)

    def test_residual_target_has_precedence(self) -> None:
        result = generator._solver_navigation_stability_targets(
            residual_targets={"pkg": "3.0.0"},
            diagnostic_preferences={"pkg": "2.0.0"},
            domains={"pkg": ["1.0.0", "2.0.0", "3.0.0"]},
        )
        self.assertEqual({"pkg": "3.0.0"}, result)

    def test_unavailable_diagnostic_version_is_ignored_not_unsat(self) -> None:
        result = generator._solver_navigation_stability_targets(
            residual_targets={},
            diagnostic_preferences={"pkg": "9.9.9"},
            domains={"pkg": ["1.0.0", "2.0.0"]},
        )
        self.assertEqual({}, result)

    def test_repeat_three_unlocks_pre_incumbent_deep_search(self) -> None:
        self.assertTrue(
            generator._baseline_deep_search_allowed(
                has_incumbent=False,
                search_mode=BaselineSearchMode.AUTO,
                repeated_predicate_count=3,
            )
        )


class Psi531SourceContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (ROOT / "dependency_live_roadmap_generator.py").read_text(
            encoding="utf-8"
        )

    def test_soft_preference_reaches_solver_objective(self) -> None:
        self.assertIn("_solver_navigation_stability_targets(", self.source)
        self.assertIn("solver_stability_targets", self.source)
        self.assertIn("diagnostic-navigation-objective", self.source)
        self.assertNotIn(
            "domains[preferred_name] = [preferred_version]",
            self.source,
        )

    def test_repeat_three_hands_off_to_certified_generalization(self) -> None:
        self.assertIn(
            "predicate_search_handoff_to_generalization = False",
            self.source,
        )
        self.assertIn(
            "predicate-search-handoff-to-generalization",
            self.source,
        )
        self.assertIn(
            "3 if predicate_search_handoff_to_generalization else 0",
            self.source,
        )
        self.assertIn(
            "pre-incumbent-repeated-predicate-escape",
            self.source,
        )

    def test_diagnostic_evidence_never_becomes_clause_authority(self) -> None:
        self.assertIn(
            "exactAssignmentAuthority=EVIDENCE_CONFIRMED_CONSTRAINT",
            self.source,
        )
        self.assertIn(
            "probeAuthority=EVIDENCE_DIAGNOSTIC_HINT",
            self.source,
        )
        self.assertIn(
            "objective-only, ",
            self.source,
        )
        self.assertIn(
            "domain remains complete",
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
