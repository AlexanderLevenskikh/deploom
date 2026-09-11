from __future__ import annotations

import unittest
from pathlib import Path

from block_psi_anytime import BestVerifiedIncumbent
from block_psi_progressive_baseline import plan_progressive_extension

ROOT = Path(__file__).resolve().parents[1]


def fp(assignment: dict[str, str]) -> str:
    return "|".join(f"{name}={assignment[name]}" for name in sorted(assignment))


class ProgressiveBaselinePlannerTests(unittest.TestCase):
    def test_failed_b_extension_preserves_a_and_selects_c(self) -> None:
        desired = {"A": "2", "B": "2", "C": "2"}
        incumbent = {"A": "2", "B": "1", "C": "1"}
        failed_b = fp({"A": "2", "B": "2", "C": "1"})

        plan = plan_progressive_extension(
            incumbent=incumbent,
            desired=desired,
            atomic_groups=(("A",), ("B",), ("C",)),
            blocked_fingerprints={failed_b},
            learned_nogoods=(),
            fingerprint_fn=fp,
        )

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.assignment_dict, {"A": "2", "B": "1", "C": "2"})
        self.assertEqual(plan.packages, ("C",))
        self.assertEqual(plan.remaining_distance, 1)

    def test_atomic_component_moves_together(self) -> None:
        plan = plan_progressive_extension(
            incumbent={"react": "18", "react-dom": "18", "tool": "1"},
            desired={"react": "19", "react-dom": "19", "tool": "2"},
            atomic_groups=(("react", "react-dom"), ("tool",)),
            blocked_fingerprints=(),
            learned_nogoods=(),
            fingerprint_fn=fp,
            priority_packages={"react"},
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.packages, ("react", "react-dom"))
        self.assertEqual(plan.assignment_dict["react"], "19")
        self.assertEqual(plan.assignment_dict["react-dom"], "19")

    def test_authoritative_nogood_is_never_scheduled(self) -> None:
        plan = plan_progressive_extension(
            incumbent={"A": "1", "B": "1", "C": "1"},
            desired={"A": "2", "B": "2", "C": "2"},
            atomic_groups=(("A", "B"), ("C",)),
            blocked_fingerprints=(),
            learned_nogoods=({"A": "2", "B": "2"},),
            fingerprint_fn=fp,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.packages, ("C",))
        self.assertEqual(plan.assignment_dict, {"A": "1", "B": "1", "C": "2"})

    def test_planner_cannot_introduce_transitive_package(self) -> None:
        plan = plan_progressive_extension(
            incumbent={"vite": "5", "vitest": "1"},
            desired={"vite": "6", "vitest": "2", "rollup": "4"},
            atomic_groups=(("vite",), ("vitest",), ("rollup",)),
            blocked_fingerprints=(),
            learned_nogoods=(),
            fingerprint_fn=fp,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertNotIn("rollup", plan.assignment_dict)
        self.assertEqual(set(plan.assignment_dict), {"vite", "vitest"})

    def test_overlapping_atomic_groups_are_conservatively_merged(self) -> None:
        plan = plan_progressive_extension(
            incumbent={"A": "1", "B": "1", "C": "1"},
            desired={"A": "2", "B": "2", "C": "2"},
            atomic_groups=(("A", "B"), ("B", "C")),
            blocked_fingerprints=(),
            learned_nogoods=(),
            fingerprint_fn=fp,
        )
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.packages, ("A", "B", "C"))
        self.assertEqual(plan.remaining_distance, 0)


class ProgressiveBaselineDurabilityTests(unittest.TestCase):
    def test_resolver_overrides_round_trip_with_incumbent(self) -> None:
        incumbent = BestVerifiedIncumbent(
            assignment={"vite": "6"},
            assignment_identity="verified-1",
            changed_dependency_count=1,
            policy_score=1.0,
            yellow_coverage=1.0,
            distance_from_desired=0,
            deferred_targets=(),
            verification_evidence="resolved-state",
            verified_at="2026-09-11T00:00:00+00:00",
            run_identity="run-1",
            objective_rank=(0, -1, 1),
            resolver_overrides=(("rollup", "4.52.0"),),
        )
        restored = BestVerifiedIncumbent.from_json(incumbent.to_json())
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(restored.resolver_overrides, (("rollup", "4.52.0"),))


class ProgressiveBaselineIntegrationContractTests(unittest.TestCase):
    def test_generator_uses_incumbent_as_handoff_authority(self) -> None:
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(
            encoding="utf-8"
        )
        for required in (
            "BLOCK_PSI_PROGRESSIVE_BASELINE_ENGINE_V1",
            "progressive-extension-selected",
            "progressive-incumbent-finalized",
            "SEARCH_BUDGET_EXHAUSTED_WITH_INCUMBENT",
            "_apply_proven_assignment_exact",
            "plan_progressive_extension",
        ):
            self.assertIn(required, source)
        self.assertNotIn("PROVEN_ASSIGNMENT_REOPENED", source)


if __name__ == "__main__":
    unittest.main()
