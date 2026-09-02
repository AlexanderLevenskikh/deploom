from __future__ import annotations

import unittest

from block_psi_anytime import (
    AutomaticBudgetPolicy,
    BaselineAnytimeState,
    BaselineCompletionStatus,
    BaselineSearchMode,
    BestVerifiedIncumbent,
    ContinuationReason,
)
from dependency_live_roadmap_generator import BaselineLivenessBudget


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def incumbent(identity: str, distance: int = 0) -> BestVerifiedIncumbent:
    return BestVerifiedIncumbent(
        assignment={"a": "2", "b": "1"},
        assignment_identity=identity,
        changed_dependency_count=1,
        policy_score=1.0 if not distance else 0.5,
        yellow_coverage=1.0,
        distance_from_desired=distance,
        deferred_targets=() if not distance else ("b",),
        verification_evidence="project-proof:key",
        verified_at="2026-09-02T00:00:00+00:00",
        run_identity="run",
        objective_rank=(distance, -1, 1),
    )


class BlockPsiAnytimeTests(unittest.TestCase):
    def test_ideal_pass_is_target_complete(self) -> None:
        state = BaselineAnytimeState()
        self.assertTrue(state.update_incumbent(incumbent("desired")))
        self.assertEqual(BaselineCompletionStatus.VERIFIED_TARGET_COMPLETE, state.completion_status("desired"))

    def test_relaxed_pass_is_good_enough_and_retained_after_failures(self) -> None:
        state = BaselineAnytimeState()
        state.update_incumbent(incumbent("relaxed", 1))
        state.observe_candidate(duration_seconds=12, passed=False, predicate="x", learned_constraints=0)
        self.assertEqual(BaselineCompletionStatus.VERIFIED_GOOD_ENOUGH, state.completion_status("desired"))
        self.assertEqual("relaxed", state.incumbent.assignment_identity)

    def test_certified_evidence_never_silently_extends_automatic_iterations(self) -> None:
        budget = BaselineLivenessBudget(base_iterations=8, max_learning_extensions=8)
        for _ in range(8):
            budget.record_exact_exclusion()
        self.assertEqual(8, budget.allowed_iterations)
        self.assertEqual(8, budget.certified_extensions)
        self.assertTrue(budget.snapshot(learned_constraints=0)["certifiedContinuationAvailable"])

    def test_explicit_bounded_and_exhaustive_authorization(self) -> None:
        budget = BaselineLivenessBudget(base_iterations=8, max_learning_extensions=16)
        self.assertEqual(8, budget.grant_user_extensions(8))
        self.assertEqual(16, budget.allowed_iterations)
        budget.exhaustive_authorized = True
        self.assertEqual(24, budget.allowed_iterations)

    def test_repeated_predicate_switches_strategy_without_becoming_proof(self) -> None:
        state = BaselineAnytimeState(policy=AutomaticBudgetPolicy(stagnation_rejections=3))
        for _ in range(2):
            self.assertFalse(state.observe_candidate(duration_seconds=10, passed=False, predicate="ts-module-resolution:@vitejs/plugin-react", learned_constraints=0))
        self.assertTrue(state.observe_candidate(duration_seconds=10, passed=False, predicate="ts-module-resolution:@vitejs/plugin-react", learned_constraints=0))
        self.assertEqual("target-cohort-relaxation", state.search_strategy)
        self.assertEqual(ContinuationReason.STAGNATION, state.automatic_continuation_reason())
        self.assertEqual(0, state.learned_constraints_at_last_rejection)

    def test_wall_clock_and_forecast_exhaust_automatic_budget(self) -> None:
        clock = FakeClock()
        state = BaselineAnytimeState(policy=AutomaticBudgetPolicy(wall_clock_seconds=100), clock=clock)
        state.observe_candidate(duration_seconds=60, passed=False, predicate="a", learned_constraints=1)
        clock.now = 50
        self.assertEqual(ContinuationReason.AUTOMATIC_BUDGET_EXHAUSTED, state.automatic_continuation_reason())

    def test_continuation_payload_has_required_actions_and_cost(self) -> None:
        state = BaselineAnytimeState()
        state.observe_candidate(duration_seconds=1900, passed=False, predicate="x", learned_constraints=0)
        payload = state.continuation_payload(project="p", mode="yellow", iteration=1, reason=ContinuationReason.AUTOMATIC_BUDGET_EXHAUSTED)
        self.assertEqual("BASELINE_CONTINUATION_REQUIRED", payload["event"])
        self.assertEqual("FIND_GOOD_ENOUGH", payload["recommendedAction"])
        self.assertIn("CONTINUE_EXHAUSTIVE", payload["availableActions"])
        self.assertEqual("hours", payload["continuationCostClass"])

    def test_resume_preserves_waiting_state_and_incumbent(self) -> None:
        original = BaselineAnytimeState(search_mode=BaselineSearchMode.EXHAUSTIVE)
        original.update_incumbent(incumbent("relaxed", 1))
        original.desired_assignment = {"a": "2", "b": "2"}
        original.desired_identity = "desired"
        original.continuation_reason = ContinuationReason.STAGNATION
        restored = BaselineAnytimeState()
        restored.restore(original.snapshot())
        self.assertTrue(restored.exhaustive_authorized)
        self.assertEqual("relaxed", restored.incumbent.assignment_identity)
        self.assertEqual(ContinuationReason.STAGNATION, restored.continuation_reason)
        self.assertEqual("desired", restored.desired_identity)
        self.assertEqual({"a": "2", "b": "2"}, restored.desired_assignment)

    def test_better_incumbent_replaces_atomically_worse_does_not(self) -> None:
        state = BaselineAnytimeState()
        self.assertTrue(state.update_incumbent(incumbent("relaxed", 1)))
        self.assertFalse(state.update_incumbent(incumbent("worse", 2)))
        self.assertTrue(state.update_incumbent(incumbent("desired", 0)))
        self.assertEqual("desired", state.incumbent.assignment_identity)


if __name__ == "__main__":
    unittest.main()
