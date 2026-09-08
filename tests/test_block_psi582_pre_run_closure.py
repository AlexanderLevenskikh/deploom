from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from block_psi58_bounded_causal_search import CausalSearchPolicy
from block_psi581_probe_budget_and_cohort import CausalPhysicalProbeLedger
from block_psi582_pre_run_closure import (
    CausalFamilyBudgetHydrator,
    cohort_fallback_queue_decision,
    reserve_causal_physical_probe,
)
from block_v_predicate_search import PredicateObservation, PredicateProbePolicy
from block_vf_active_search import (
    PROBE_OUTCOME_PRESENT,
    ProbeExecution,
    run_active_predicate_search,
)


PREDICATE = "duplicate-type-universe:rollup"


class FakePredicateStateStore:
    def __init__(self, attempts=None, *, fail_mark=False):
        self.attempts = {
            str(package): set(map(str, versions))
            for package, versions in (attempts or {}).items()
        }
        self.fail_mark = fail_mark
        self.events = []

    def load_session(
        self, project, mode, *, run_identity, package, predicate
    ):
        self.events.append(("load", str(package)))
        return SimpleNamespace(
            attempted_versions=tuple(sorted(self.attempts.get(str(package), set())))
        )

    def mark_attempt(
        self, project, mode, *, run_identity, package, predicate, version
    ):
        self.events.append(("mark", str(package), str(version)))
        if self.fail_mark:
            raise OSError("synthetic persistence failure")
        self.attempts.setdefault(str(package), set()).add(str(version))


class Psi582CohortDedupTests(unittest.TestCase):
    def test_confirmed_failed_fallback_is_not_queued(self):
        decision = cohort_fallback_queue_decision(
            "same",
            confirmed_failed_fingerprints={"same"},
            exact_exclusion_fingerprints=(),
        )
        self.assertFalse(decision.queue)
        self.assertEqual("confirmed-failure", decision.reason)

    def test_exact_excluded_fallback_is_not_queued(self):
        decision = cohort_fallback_queue_decision(
            "same",
            confirmed_failed_fingerprints=(),
            exact_exclusion_fingerprints={"same"},
        )
        self.assertFalse(decision.queue)
        self.assertEqual("exact-exclusion", decision.reason)

    def test_new_fallback_remains_schedulable(self):
        decision = cohort_fallback_queue_decision(
            "new",
            confirmed_failed_fingerprints={"old-a"},
            exact_exclusion_fingerprints={"old-b"},
        )
        self.assertTrue(decision.queue)
        self.assertEqual("new-exact-candidate", decision.reason)


class Psi582ResumeBudgetTests(unittest.TestCase):
    def setUp(self):
        self.policy = CausalSearchPolicy(
            per_package_probe_budget=2,
            family_probe_budget=3,
            max_parents=3,
            representative_limit=2,
        )
        self.ledger = CausalPhysicalProbeLedger(self.policy)
        self.hydrator = CausalFamilyBudgetHydrator()

    def reserve(self, store, package, version):
        return reserve_causal_physical_probe(
            ledger=self.ledger,
            hydrator=self.hydrator,
            predicate_state_store=store,
            run_identity="run",
            project="partner-form",
            mode="yellow",
            predicate=PREDICATE,
            package=package,
            version=version,
            family_packages=(
                "@storybook/builder-vite",
                "vite",
                "vitest",
            ),
        )

    def test_branch_first_after_resume_hydrates_other_parent_cost(self):
        # Simulates a new process: the in-memory ledger starts empty, while the
        # persisted Storybook parent has already spent the entire family budget.
        store = FakePredicateStateStore(
            {
                "@storybook/builder-vite": ("7.6.7", "8.0.0"),
                "vite": ("5.0.0",),
            }
        )
        denied = self.reserve(store, "vitest", "4.0.0")
        self.assertFalse(denied.granted)
        self.assertEqual("family-physical-budget-exhausted", denied.reason)
        self.assertNotIn(("mark", "vitest", "4.0.0"), store.events)

    def test_full_family_is_hydrated_only_once(self):
        store = FakePredicateStateStore()
        first = self.reserve(store, "vite", "5.0.0")
        second = self.reserve(store, "vitest", "4.0.0")
        self.assertTrue(first.granted)
        self.assertTrue(second.granted)
        loads = [event for event in store.events if event[0] == "load"]
        self.assertEqual(3, len(loads))

    def test_reservation_is_persisted_before_physical_runner(self):
        store = FakePredicateStateStore()
        observations = (
            PredicateObservation(
                package="vite",
                version="4.3.9",
                predicate=PREDICATE,
                present=True,
                assignment_fingerprint="a",
            ),
            PredicateObservation(
                package="vite",
                version="4.3.9",
                predicate=PREDICATE,
                present=True,
                assignment_fingerprint="b",
            ),
        )

        def permit(version):
            return self.reserve(store, "vite", version).granted

        physical_calls = []

        def runner(version, assignment):
            self.assertIn(("mark", "vite", version), store.events)
            physical_calls.append(version)
            return ProbeExecution(
                version=version,
                outcome=PROBE_OUTCOME_PRESENT,
                assignment_fingerprint=version,
            )

        result = run_active_predicate_search(
            package="vite",
            predicate=PREDICATE,
            base_assignment={"vite": "4.3.9"},
            project_current_version="4.3.9",
            versions=("5.0.0", "6.0.0"),
            observations=observations,
            attempted_versions=(),
            policy=PredicateProbePolicy(
                enabled=True,
                repeat_threshold=2,
                probe_budget=2,
            ),
            run_probe=runner,
            permit_probe=permit,
        )
        self.assertEqual(2, len(result.executions))
        self.assertEqual(2, len(physical_calls))
        self.assertEqual({"5.0.0", "6.0.0"}, store.attempts["vite"])

    def test_persistence_failure_denies_probe_before_verifier(self):
        store = FakePredicateStateStore(fail_mark=True)
        permit = self.reserve(store, "vite", "5.0.0")
        self.assertFalse(permit.granted)
        self.assertTrue(
            permit.reason.startswith("reservation-persist-failed:")
        )


class Psi582StaticIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generator = (
            ROOT / "dependency_live_roadmap_generator.py"
        ).read_text(encoding="utf-8")

    def test_main_and_branch_use_shared_reservation_helper(self):
        self.assertGreaterEqual(
            self.generator.count("reserve_causal_physical_probe("),
            2,
        )
        self.assertIn(
            "causal_family_hydrator = CausalFamilyBudgetHydrator()",
            self.generator,
        )

    def test_duplicate_cohort_fallback_is_explicitly_skipped(self):
        self.assertIn(
            "cohort_fallback_queue_decision(",
            self.generator,
        )
        self.assertIn(
            '"causal-cohort.assignment-skipped-known-failed"',
            self.generator,
        )


if __name__ == "__main__":
    unittest.main()
