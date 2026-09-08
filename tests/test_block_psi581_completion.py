from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from block_psi58_bounded_causal_search import CausalSearchPolicy
from block_psi581_probe_budget_and_cohort import (
    CausalPhysicalProbeLedger,
    build_cohort_fallback_assignment,
)
from block_v_predicate_search import PredicateObservation, PredicateProbePolicy
from block_vf_active_search import (
    PROBE_OUTCOME_PRESENT,
    ProbeExecution,
    run_active_predicate_search,
)


class Psi581SharedBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = CausalSearchPolicy(
            per_package_probe_budget=2,
            family_probe_budget=3,
            max_parents=3,
            representative_limit=2,
        )
        self.ledger = CausalPhysicalProbeLedger(self.policy)

    def permit(self, package: str):
        return self.ledger.try_acquire(
            run_identity="run",
            project="p",
            mode="yellow",
            predicate="duplicate-type-universe:rollup",
            package=package,
        )

    def test_dropped_parent_still_counts_toward_family_budget(self) -> None:
        self.ledger.sync(
            run_identity="run", project="p", mode="yellow",
            predicate="duplicate-type-universe:rollup",
            package="@storybook/builder-vite",
            attempted_versions=("7.6.7", "8.0.0"),
        )
        # Even if Storybook disappears from a later projection, its two physical
        # attempts remain in the family ledger.
        vite = self.permit("vite")
        self.assertTrue(vite.granted)
        self.assertEqual(3, vite.family_used)
        vitest = self.permit("vitest")
        self.assertFalse(vitest.granted)
        self.assertEqual("family-physical-budget-exhausted", vitest.reason)

    def test_main_and_branch_share_per_package_budget(self) -> None:
        observations = (
            PredicateObservation(
                package="vite", version="4.0.0",
                predicate="duplicate-type-universe:rollup",
                present=True, assignment_fingerprint="a",
            ),
            PredicateObservation(
                package="vite", version="4.0.0",
                predicate="duplicate-type-universe:rollup",
                present=True, assignment_fingerprint="b",
            ),
        )
        calls = []

        def runner(version, assignment):
            calls.append(version)
            return ProbeExecution(
                version=version,
                outcome=PROBE_OUTCOME_PRESENT,
                assignment_fingerprint=version,
            )

        result = run_active_predicate_search(
            package="vite",
            predicate="duplicate-type-universe:rollup",
            base_assignment={"vite": "4.0.0"},
            project_current_version="4.0.0",
            versions=("5.0.0", "6.0.0", "7.0.0"),
            observations=observations,
            policy=PredicateProbePolicy(
                enabled=True, repeat_threshold=2, probe_budget=3
            ),
            run_probe=runner,
            permit_probe=lambda _version: self.permit("vite").granted,
        )
        self.assertEqual(2, len(result.executions))
        self.assertEqual(2, len(calls))
        # A subsequent branch continuation for the same parent cannot sneak in
        # a third physical probe.
        branch = self.permit("vite")
        self.assertFalse(branch.granted)
        self.assertEqual("per-package-physical-budget-exhausted", branch.reason)

    def test_permit_is_reserved_before_physical_call(self) -> None:
        first = self.permit("vite")
        second = self.permit("vite")
        third = self.permit("vite")
        self.assertTrue(first.granted)
        self.assertTrue(second.granted)
        self.assertFalse(third.granted)


class Psi581CohortFallbackTests(unittest.TestCase):
    def test_cohort_candidate_defers_only_inferred_packages(self) -> None:
        result = build_cohort_fallback_assignment(
            assignment={
                "vite": "7.0.0",
                "vitest": "4.0.0",
                "axios": "2.0.0",
                "@storybook/builder-vite": "10.0.0",
            },
            current_versions={
                "vite": "4.3.9",
                "vitest": "0.30.1",
                "axios": "1.0.0",
                "@storybook/builder-vite": "7.6.7",
            },
            cohort_packages=("vite", "vitest", "@storybook/builder-vite"),
        )
        candidate = result.assignment_dict
        self.assertTrue(result.changed)
        self.assertEqual("4.3.9", candidate["vite"])
        self.assertEqual("0.30.1", candidate["vitest"])
        self.assertEqual("7.6.7", candidate["@storybook/builder-vite"])
        self.assertEqual("2.0.0", candidate["axios"])
        self.assertEqual(
            ("@storybook/builder-vite", "vite", "vitest"),
            result.deferred_packages,
        )


class Psi581StaticIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.generator = (ROOT / "dependency_live_roadmap_generator.py").read_text(
            encoding="utf-8"
        )
        cls.verifier = (ROOT / "baseline_constraint_verifier.py").read_text(
            encoding="utf-8"
        )
        cls.seed = (ROOT / "block_psi57_yarn1_resolver_seed.py").read_text(
            encoding="utf-8"
        )

    def test_branch_uses_same_budget_and_bucketing(self) -> None:
        self.assertIn("# BLOCK_PSI581_SHARED_BRANCH_BUDGET_V1", self.generator)
        self.assertIn("branch_bounded_domain = (", self.generator)
        self.assertIn("bounded_causal_probe_domain(", self.generator)
        self.assertIn("branch_permit = (", self.generator)
        # Ψ.5.8.2 supersedes the direct ledger acquisition with the shared
        # crash-safe reservation helper. The helper hydrates the whole family,
        # acquires the same ledger budget and persists the exact attempt before
        # verifier execution.
        self.assertIn("# BLOCK_PSI582_BRANCH_SHARED_RESERVATION_V1", self.generator)
        self.assertIn("reserve_causal_physical_probe(", self.generator)

    def test_handoff_creates_exact_cohort_fallback_candidate(self) -> None:
        self.assertIn("# BLOCK_PSI581_REAL_COHORT_HANDOFF_V1", self.generator)
        self.assertIn("build_cohort_fallback_assignment(", self.generator)
        self.assertIn('"causal-cohort.assignment-created"', self.generator)

    def test_seed_failed_publish_and_clone_are_timed(self) -> None:
        self.assertIn('"resolver-seed.publish-failed"', self.verifier)
        self.assertIn('"resolver-seed.clone.finish"', self.seed)


if __name__ == "__main__":
    unittest.main()
