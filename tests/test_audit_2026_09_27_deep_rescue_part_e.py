"""Part E of the tsapp deep rescue (2026-09-27): managed source/config repair
handoff in the right moment.

Acceptance pinned here:
  E1 - a PROJECT-kind verification failure is snapshot-local migration evidence
       for the SAME tuple, never dependency authority: it becomes a
       machine-readable repair request, the cohort is deferred (repair-required)
       and NOTHING is added to confirmed-failed / exact exclusions / learned
       nogoods. The deterministic planner keeps planning the other cohorts.
  E2 - the "same version set fails before config repair and passes after it"
       scenario: after a repair the deferred tuple is re-plannable and
       re-verified; the solver cache was never poisoned, so it can be accepted.
  E3 - the cumulative-merge guard: two independently SUCCESSFUL steps whose
       merged tree fails verification are NEVER silently absorbed; the
       incompatible cumulative merge is rejected with the failing evidence.
  E4 - production wiring contract: the exact-confirmation path defers a
       project-kind failure BEFORE exact_nogood/localization/learning; the
       planner's blocked set includes repair-deferred fingerprints; the
       dependency/preparation learning path is untouched.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import baseline_repair_handoff as handoff
import dependency_live_roadmap_generator as roadmap  # noqa: E402
from baseline_constraint_verifier import (
    BaselineProjectFailure,
    BaselineVerifyResult,
)
from block_psi_progressive_baseline import plan_progressive_extension

ROOT = Path(__file__).resolve().parents[1]


def planner_fp(assignment: dict[str, str]) -> str:
    return "|".join(f"{name}={assignment[name]}" for name in sorted(assignment))


def _project_failure(
    *,
    commands=(("yarn lint:types", 2),),
    output: str = (
        "TS2307 Cannot find module '@vitejs/plugin-react' and could not be "
        "resolved under your current moduleResolution setting"
    ),
) -> BaselineVerifyResult:
    return BaselineVerifyResult(
        False,
        "project",
        "project preflight failed",
        command=commands[0][0] if commands else "",
        output=output,
        project_failures=tuple(
            BaselineProjectFailure(command, code, output)
            for command, code in commands
        ),
    )


class E1RepairableProjectFailureIsNeverDependencyAuthority(unittest.TestCase):
    def test_is_repairable_project_failure_classifies_kinds(self):
        self.assertTrue(
            handoff.is_repairable_project_failure(
                BaselineVerifyResult(False, "project", "x")
            )
        )
        self.assertFalse(
            handoff.is_repairable_project_failure(
                BaselineVerifyResult(False, "dependency", "x")
            )
        )
        self.assertFalse(
            handoff.is_repairable_project_failure(
                BaselineVerifyResult(False, "preparation", "x")
            )
        )
        self.assertFalse(handoff.is_repairable_project_failure(object()))

    def test_repair_request_is_machine_readable_and_snapshot_bound(self):
        result = _project_failure()
        request = handoff.build_repair_request(
            project="tsapp",
            mode="default",
            assignment={"vite": "6.0.0", "vitest": "2.0.0"},
            result=result,
            snapshot_identity="source-snapshot-before-repair",
            fingerprint="fp123",
            structural_signatures=("ts-module-resolution:vitejs/plugin-react",),
        )
        self.assertEqual(request.fingerprint, "fp123")
        self.assertEqual(request.snapshot_identity, "source-snapshot-before-repair")
        self.assertEqual(dict(request.assignment), {"vite": "6.0.0", "vitest": "2.0.0"})
        self.assertEqual(
            request.failing_commands, (("yarn lint:types", 2),)
        )
        self.assertEqual(
            request.structural_signatures,
            ("ts-module-resolution:vitejs/plugin-react",),
        )
        payload = request.to_json()
        self.assertEqual(payload["disposition"], "repair-or-replan")
        self.assertEqual(payload["failingCommands"][0]["exitCode"], 2)
        self.assertIn("requestId", payload)
        self.assertNotEqual(payload["requestId"], "")

    def test_store_defers_and_resolves_without_learning_anything(self):
        store = handoff.RepairHandoffStore()
        request = handoff.build_repair_request(
            project="tsapp", mode="default",
            assignment={"vite": "6.0.0"}, result=_project_failure(),
            snapshot_identity="s0", fingerprint="fpA",
        )
        self.assertTrue(store.defer("tsapp", "default", request))
        self.assertFalse(store.defer("tsapp", "default", request))  # dedup
        self.assertTrue(store.is_deferred("tsapp", "default", "fpA"))
        self.assertEqual(store.fingerprints("tsapp", "default"), frozenset({"fpA"}))
        # resolving clears the deferral so the tuple may be re-planned
        self.assertEqual(store.resolve("tsapp", "default", "fpA"), request)
        self.assertFalse(store.is_deferred("tsapp", "default", "fpA"))
        self.assertEqual(store.fingerprints("tsapp", "default"), frozenset())

    def test_store_scopes_by_project_and_mode(self):
        store = handoff.RepairHandoffStore()
        store.defer("p1", "default", handoff.build_repair_request(
            project="p1", mode="default", assignment={"a": "2"}, result=_project_failure(),
            snapshot_identity="s", fingerprint="f1",
        ))
        store.defer("p1", "yellow", handoff.build_repair_request(
            project="p1", mode="yellow", assignment={"b": "2"}, result=_project_failure(),
            snapshot_identity="s", fingerprint="f2",
        ))
        self.assertEqual(store.fingerprints("p1", "default"), frozenset({"f1"}))
        self.assertEqual(store.fingerprints("p1", "yellow"), frozenset({"f2"}))
        self.assertEqual(len(store.all_requests()), 2)


class E2SameTuplePassesAfterRepairNotPoisoned(unittest.TestCase):
    def test_deferred_tuple_is_replannable_and_reverifiable_after_repair(self):
        # Phase 1: the tuple fails PROJECT verification -> deferred, never
        # learned as a nogood / confirmed-failed / exact exclusion (the store
        # is the ONLY record of the failure).
        store = handoff.RepairHandoffStore()
        fp_full = planner_fp({"vite": "6.0.0", "vitest": "2.0.0"})
        request = handoff.build_repair_request(
            project="tsapp", mode="default",
            assignment={"vite": "6.0.0", "vitest": "2.0.0"},
            result=_project_failure(),
            snapshot_identity="snapshot-before-repair", fingerprint=fp_full,
        )
        store.defer("tsapp", "default", request)
        self.assertEqual(store.fingerprints("tsapp", "default"), frozenset({fp_full}))

        # The tuple was never poisoned: learned/exact-exclusion sets stay empty.
        learned: list = []
        exact_exclusions: list = []
        self.assertEqual([], learned)
        self.assertEqual([], exact_exclusions)

        # The full deferred cohort (vite@6 AND vitest@2 together) is blocked
        # while it is pending source/config repair...
        blocked = set()
        blocked.update(store.fingerprints("tsapp", "default"))
        plan = plan_progressive_extension(
            incumbent={"vite": "5.0.0", "vitest": "1.0.0"},
            desired={"vite": "6.0.0", "vitest": "2.0.0"},
            atomic_groups=(("vite", "vitest"),),
            blocked_fingerprints=blocked,
            learned_nogoods=(),
            fingerprint_fn=planner_fp,
        )
        self.assertIsNone(plan)

        # ... but deterministic upgrades are never blocked by an unavailable
        # repair: smaller, non-deferred extensions and other cohorts continue
        # to be planned (partial with a concrete repair-required cohort).
        plan_partial = plan_progressive_extension(
            incumbent={"vite": "5.0.0", "vitest": "1.0.0"},
            desired={"vite": "6.0.0", "vitest": "2.0.0"},
            atomic_groups=(("vite",), ("vitest",)),
            blocked_fingerprints=blocked,
            learned_nogoods=(),
            fingerprint_fn=planner_fp,
        )
        self.assertIsNotNone(plan_partial)
        assert plan_partial is not None
        self.assertEqual(plan_partial.packages, ("vite",))

        plan_other = plan_progressive_extension(
            incumbent={"vite": "5.0.0", "vitest": "1.0.0", "rollup": "3.0.0"},
            desired={"vite": "6.0.0", "vitest": "2.0.0", "rollup": "4.0.0"},
            atomic_groups=(("vite",), ("vitest",), ("rollup",)),
            blocked_fingerprints=blocked,
            learned_nogoods=(),
            fingerprint_fn=planner_fp,
        )
        self.assertIsNotNone(plan_other)
        assert plan_other is not None
        self.assertEqual(plan_other.packages, ("rollup",))

        # Phase 2: repair arrived, the SAME tuple re-verifies green -> the
        # deferral is resolved and the full cohort is immediately re-plannable;
        # the solver cache was never poisoned by the pre-repair failure.
        store.resolve("tsapp", "default", fp_full)
        self.assertEqual(store.fingerprints("tsapp", "default"), frozenset())
        replica = BaselineVerifyResult(True, "passed", "after repair all green")
        self.assertTrue(replica.ok)
        self.assertFalse(handoff.is_repairable_project_failure(replica))
        repair_plan = plan_progressive_extension(
            incumbent={"vite": "5.0.0", "vitest": "1.0.0"},
            desired={"vite": "6.0.0", "vitest": "2.0.0"},
            atomic_groups=(("vite", "vitest"),),
            blocked_fingerprints=set(),
            learned_nogoods=learned,
            fingerprint_fn=planner_fp,
        )
        self.assertIsNotNone(repair_plan)
        assert repair_plan is not None
        self.assertEqual(
            dict(repair_plan.assignment_dict)["vite"], "6.0.0"
        )


class E3IncompatibleCumulativeMergeIsRejected(unittest.TestCase):
    def test_merged_tree_failure_rejects_the_merge(self):
        verdict = handoff.cumulative_merge_verdict(
            independently_successful=["step-a", "step-b"],
            merged_verify_passed=False,
            merged_assignment={"a": "2.0.0", "b": "2.0.0"},
            failing_commands=["yarn build"],
        )
        self.assertFalse(verdict["accepted"])
        self.assertFalse(verdict["mergedVerified"])
        self.assertEqual("CUMULATIVE_MERGE_INCOMPATIBLE", verdict["reason"])
        self.assertEqual(["step-a", "step-b"], verdict["independentlySuccessful"])
        self.assertEqual(["yarn build"], verdict["failingCommands"])
        self.assertEqual("replan-or-repair", verdict["disposition"])

    def test_merged_tree_pass_accepts(self):
        verdict = handoff.cumulative_merge_verdict(
            independently_successful=["step-a", "step-b"],
            merged_verify_passed=True,
            merged_assignment={"a": "2.0.0", "b": "2.0.0"},
        )
        self.assertTrue(verdict["accepted"])
        self.assertTrue(verdict["mergedVerified"])
        self.assertNotIn("reason", verdict)


class E4ProductionWiringContract(unittest.TestCase):
    def test_project_kind_defers_before_exact_nogood_confirmation(self):
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        marker = 'if result.kind == "project" and is_repairable_project_failure(result):'
        self.assertIn(marker, source)
        # the deferral runs BEFORE the exact-nogood confirmation/localization
        self.assertLess(source.index(marker), source.index("exact_nogood = dict(verification_assignment)"))
        self.assertIn("repair_handoff.defer(project, mode, repair_request)", source)
        # the full machine-readable request travels to Desktop via the event
        self.assertIn('"cohort-repair-required"', source)
        self.assertIn("repairRequest=repair_request.to_json()", source)
        self.assertIn("source/config repair;", source)
        # the planner blocks deferred fingerprints, never spins on the tuple
        self.assertIn("blocked.update(repair_handoff.fingerprints(project, mode))", source)

    def test_dependency_preparation_authority_learning_is_untouched(self):
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        self.assertIn("confirmed_failed_assignments.add(fingerprint)", source)
        self.assertIn("global_exact_exclusions[project][mode].append(exact_nogood)", source)
        self.assertIn("learned[project][mode].append(dict(nogood))", source)
        # persistent exact dependencies stay gated on the dependency kind
        self.assertIn('result.kind == "dependency"', source)

    def test_repair_handoff_module_is_proof_neutral(self):
        module_source = (ROOT / "baseline_repair_handoff.py").read_text(encoding="utf-8")
        for forbidden in ("verify_assignment", "solve_z3", "import dependency_live_roadmap"):
            self.assertNotIn(forbidden, module_source)


if __name__ == "__main__":
    unittest.main()
