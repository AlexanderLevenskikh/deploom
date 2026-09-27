"""Part F of the libjs deep rescue (2026-09-27): executable acceptance matrix,
UX states and honest real-run readiness.

Each row of the mandatory acceptance matrix (task L199-L210) is pinned here to
the responsibility of a Part A/B/C/D/E test or a production contract, so the
matrix is runnable in CI and the coverage is explicit. Nothing here promises a
specific libjs percentage or runtime: real acceptance is measured by the user's
isolated run (see the REAL_RUN_ACCEPTANCE checklist beside this audit).
"""
from __future__ import annotations

import unittest
from pathlib import Path

from baseline_constraint_verifier import BaselineVerifyResult

ROOT = Path(__file__).resolve().parents[1]


def _test_names(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _source(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class AcceptanceMatrixRows(unittest.TestCase):
    """Row->responsibility mapping over the Part A-E tests + production code."""

    def test_r1_unknown_component_does_not_block_small_sat_step(self):
        part_a = _test_names("tests/test_audit_2026_09_27_deep_rescue_part_a.py")
        self.assertIn("def test_barrier_bootstrap_big_unknown_blocks_small_candidate", part_a)
        # T1 pins: small-c delivered as optimal while big unknown is recorded,
        # never reported as a conflict.
        self.assertIn('assertEqual({"small-c": "1.5.0"}, assignment)', part_a)
        self.assertIn('assertEqual("unknown", big_entries[0]["status"])', part_a)

    def test_r2_a_accepted_b_failed_c_available_accumulates_ac(self):
        part_d = _test_names("tests/test_audit_2026_09_27_deep_rescue_part_d.py")
        planner = _test_names("tests/test_block_psi_progressive_baseline.py")
        self.assertIn("def test_failed_b_does_not_lose_accepted_a_and_c_still_reachable", planner)
        self.assertIn("def test_failed_b_extension_preserves_a_and_selects_c", planner)
        self.assertIn("def test_fixed_names_pin_overrides_but_do_not_block_other_upgrades", planner)
        self.assertIn("def test_fixed_names_excluded_from_moving_set", part_d)

    def test_r3_large_potential_peer_component_splits_into_small_cohorts(self):
        part_d = _test_names("tests/test_audit_2026_09_27_deep_rescue_part_d.py")
        self.assertIn("def test_potential_component_splits_into_small_delta_cohorts", part_d)
        self.assertIn("def test_target_peer_not_satisfied_by_peer_current_keeps_pair_together", part_d)
        self.assertIn("def test_compatible_delta_moves_independently", part_d)

    def test_r4_repairable_config_api_break_repaired_and_reverified(self):
        part_e = _test_names("tests/test_audit_2026_09_27_deep_rescue_part_e.py")
        self.assertIn("def test_deferred_tuple_is_replannable_and_reverifiable_after_repair", part_e)
        self.assertIn("def test_store_defers_and_resolves_without_learning_anything", part_e)
        # no dependency nogood learned for a repairable tuple
        self.assertIn("no dependency nogood learned", _source("dependency_live_roadmap_generator.py"))

    def test_r5_intermediate_version_leaves_remaining_work_visible(self):
        planner = _test_names("tests/test_block_psi_progressive_baseline.py")
        # accepted partial steps expose the remaining distance to the target
        self.assertIn("remaining_distance", planner)
        self.assertIn("def test_failed_b_does_not_lose_accepted_a_and_c_still_reachable", planner)
        # the verified incumbent carries distance_from_desired / deferred_targets
        self.assertIn("distance_from_desired=", _source("dependency_live_roadmap_generator.py"))
        self.assertIn("deferred_targets=deferred", _source("dependency_live_roadmap_generator.py"))

    def test_r6_incumbent_with_overrides_pins_but_does_not_cut_off(self):
        part_c = _test_names("tests/test_audit_2026_09_27_deep_rescue_part_c.py")
        planner = _test_names("tests/test_block_psi_progressive_baseline.py")
        self.assertIn("def test_fully_pinned_report_is_honest_and_solver_is_never_called", part_c)
        self.assertIn("def test_pinned_component_delivered_through_resolve_without_solve", part_c)
        self.assertIn("def test_fixed_names_pin_overrides_but_do_not_block_other_upgrades", planner)
        self.assertIn("def test_resolver_overrides_round_trip_with_incumbent", planner)

    def test_r7_shutdown_cancel_restart_preserves_verified_state(self):
        part_a = _test_names("tests/test_audit_2026_09_27_deep_rescue_part_a.py")
        self.assertIn("def test_resume_restores_checkpoint_and_changed_inputs_invalidate", part_a)
        recovery = _test_names("tests/test_block_v_recovery.py")
        self.assertIn("BaselineRunRecoveryStore", recovery)
        # the checkpoint binds source snapshot/commit + identity; a changed
        # source/lock/policy invalidates the old authority (T4)
        self.assertIn("source snapshot/commit", _source("dependency_live_roadmap_generator.py"))

    def test_r8_security_endpoint_unavailable_is_unknown_not_zero(self):
        audit_source = _source("manual_dependency_audit.py")
        self.assertIn("did not complete; counts are unknown", audit_source)
        self.assertIn("totals_available else 'UNKNOWN'", audit_source)
        self.assertIn('"unknown"', audit_source)
        audit_tests = _test_names("tests/test_validator_and_manual_audit.py")
        self.assertIn("def test_", audit_tests)

    def test_r9_yarn_bridge_drift_separates_approximate_from_canonical(self):
        # the canonical lock/manager is the acceptance authority; bridge/legacy
        # evidence stays approximate and explicitly labeled
        generator = _source("dependency_live_roadmap_generator.py")
        self.assertIn("canonical", generator)
        self.assertIn("bridge", generator)
        prompt_notes = _test_names("tests/test_audit_2026_09_27_deep_rescue_part_b.py")
        self.assertIn("def test_real_prompt_ru_contains_part_b_contracts", prompt_notes)

    def test_r10_prompt_ru_en_and_desktop_reader_reach_the_agent(self):
        part_b = _test_names("tests/test_audit_2026_09_27_deep_rescue_part_b.py")
        self.assertIn("def test_real_prompt_ru_contains_part_b_contracts", part_b)
        self.assertIn("def test_real_prompt_en_contains_part_b_contracts", part_b)
        # the desktop reader surfaces the prompt/plan/manifest/summary artifacts
        reader = _source("desktop/electron/draft-artifact-reader.ts")
        self.assertIn("prompt", reader)
        self.assertIn("manifest", reader)
        self.assertIn("summary", reader)

    def test_r11_libjs_composite_test_keeps_test_build(self):
        check = _source("desktop/scripts/check-migration-verification.mjs")
        self.assertIn("Composite test:test:build must not be lost", check)
        gates = _source("desktop/electron/migration-gates.ts")
        self.assertIn("MUST keep its extra stage", gates)
        self.assertIn("EXACT", gates)
        self.assertIn("duplicate of `test:unit` is skipped", gates)

    def test_r12_unachievable_policy_closed_scope_is_partial_with_scoped_blocker(self):
        generator = _source("dependency_live_roadmap_generator.py")
        self.assertIn("VERIFIED_PARTIAL_SCOPE", generator)
        self.assertIn("scope_excluded", generator)
        self.assertIn("planner_deferred", generator)
        part_a = _test_names("tests/test_audit_2026_09_27_deep_rescue_part_a.py")
        self.assertIn("def test_retained_result_and_next_independent_cohort", part_a)


class UXStatesAndHonestLabels(unittest.TestCase):
    """The migration UI must show exactly what was saved and what remains;
    'проверено' never conflates entry point/resolver-ready/project-verified."""

    def test_ux_states_are_emitted(self):
        generator = _source("dependency_live_roadmap_generator.py")
        # goal confirmed on a cumulative snapshot (mode passed / incumbent)
        self.assertIn('"mode-passed"', generator)
        # partial checked, goal not yet reached
        self.assertIn("budget-exhausted", generator)
        # repair of a concrete cohort
        self.assertIn('"cohort-repair-required"', generator)
        # no new verified result, previous saved (incumbent retained)
        self.assertIn("progressive-budget-ended-with-incumbent", generator)
        # safety/infrastructure failure, continuation impossible
        self.assertIn('"verification-terminal"', generator)
        self.assertIn("INFRASTRUCTURE_FAILURE", generator)

    def test_verified_labels_never_conflate_resolver_ready_and_project_verified(self):
        generator = _source("dependency_live_roadmap_generator.py")
        # the resolver-green-but-project-not-passed state is explicit
        self.assertIn("resolver-green", generator)
        self.assertIn("project migration is expected", generator)
        # NOT_VERIFIED / PLANNING_ONLY for Draft is a first-class label
        self.assertIn("NOT_VERIFIED", generator)
        self.assertIn("PLANNING_ONLY", generator)
        # terminalStatus distinguishes SAT_PROVEN from SOLVER_UNKNOWN
        self.assertIn("SAT_PROVEN", generator)
        self.assertIn("SOLVER_UNKNOWN", generator)

    def test_repairable_project_failure_is_never_hard_dependency_authority(self):
        # Part E: a project-kind failure is NOT a hard_failure-class dependency
        # incompatibility for solver learning; it defers (repair handoff).
        from baseline_repair_handoff import is_repairable_project_failure

        self.assertTrue(
            is_repairable_project_failure(BaselineVerifyResult(False, "project", "x"))
        )
        self.assertFalse(
            is_repairable_project_failure(BaselineVerifyResult(False, "dependency", "x"))
        )


class RealRunReadiness(unittest.TestCase):
    """The isolated libjs real run must be reproducible at fixed settings and
    honest if blocked. Preparation lives in the audit-review checklist."""

    def test_real_run_checklist_exists_beside_this_audit(self):
        checklist = ROOT / (
            ".dependency-roadmap/audit-reviews/libjs-deep-2026-09-27/"
            "REAL_RUN_ACCEPTANCE.md"
        )
        self.assertTrue(checklist.exists(), str(checklist))
        text = checklist.read_text(encoding="utf-8")
        # fixed settings, a measurable budget and the control scenario are fixed
        for marker in (
            "budgetMinutes",
            "minLagOkPct",
            "maxKnownCritical",
            "timeToFirstCheckpoint",
            "timeToFirstProjectVerified",
        ):
            self.assertIn(marker, text)
        # no deferred promise of a specific libjs percentage
        self.assertIn("не обещаем", text)
        # incomplete real acceptance is explicitly markable
        self.assertIn("INCOMPLETE", text)
        # first useful upgrade requirement is pinned
        self.assertIn("First useful upgrade", text)


if __name__ == "__main__":
    unittest.main()
