from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from block_psi591_unified_cohort_handoff import plan_cohort_handoff


class Psi591PlanningTests(unittest.TestCase):
    def fake_infer(self, **kwargs):
        self.infer_kwargs = kwargs
        return SimpleNamespace(
            cohort_id="vite-build",
            packages=("vite", "@vitejs/plugin-react"),
            confidence=0.8,
        )

    @staticmethod
    def fake_fallback(**kwargs):
        assignment = dict(kwargs["assignment"])
        for package in kwargs["cohort_packages"]:
            if package in kwargs["current_versions"]:
                assignment[package] = kwargs["current_versions"][package]
        return SimpleNamespace(
            changed=True,
            assignment_dict=assignment,
            deferred_packages=tuple(sorted(kwargs["cohort_packages"])),
        )

    @staticmethod
    def fake_decision(fingerprint, **kwargs):
        return SimpleNamespace(queue=True, reason="new-exact-candidate")

    @staticmethod
    def fake_control(decision):
        return SimpleNamespace(
            queue_fallback=bool(decision.queue),
            publish_current_exact_now=True,
            steer_outer_iteration=True,
            continue_predicate_loop=False,
            reason="queue-new-fallback",
        )

    def plan(self, *, focus_package=""):
        return plan_cohort_handoff(
            predicate="ts-module-resolution:@vitejs/plugin-react",
            direct_packages=("vite", "@vitejs/plugin-react", "axios"),
            focus_package=focus_package,
            assignment={
                "vite": "7.0.0",
                "@vitejs/plugin-react": "6.1.1",
                "axios": "2.0.0",
            },
            current_versions={
                "vite": "4.3.9",
                "@vitejs/plugin-react": "4.3.2",
                "axios": "1.0.0",
            },
            subject_consumers={},
            interaction_graph={},
            policy_by_package={},
            repeated_count=2,
            confirmed_failed_fingerprints=("old",),
            exact_exclusion_fingerprints=("excluded",),
            assignment_fingerprint_fn=lambda assignment: "|".join(
                f"{k}={assignment[k]}" for k in sorted(assignment)
            ),
            _infer_cohort=self.fake_infer,
            _build_fallback=self.fake_fallback,
            _queue_decision=self.fake_decision,
            _handoff_control=self.fake_control,
        )

    def test_direct_and_transitive_share_same_planner_contract(self):
        direct = self.plan(focus_package="@vitejs/plugin-react")
        self.assertTrue(direct.actionable)
        self.assertEqual(
            "@vitejs/plugin-react",
            self.infer_kwargs["focus_package"],
        )
        transitive = self.plan(focus_package="")
        self.assertTrue(transitive.actionable)
        self.assertEqual("", self.infer_kwargs["focus_package"])

    def test_fallback_resets_only_cohort_and_preserves_unrelated_upgrade(self):
        plan = self.plan(focus_package="@vitejs/plugin-react")
        candidate = plan.assignment_dict
        self.assertEqual("4.3.9", candidate["vite"])
        self.assertEqual("4.3.2", candidate["@vitejs/plugin-react"])
        self.assertEqual("2.0.0", candidate["axios"])

    def test_duplicate_control_is_preserved_by_common_planner(self):
        def duplicate_decision(fingerprint, **kwargs):
            return SimpleNamespace(queue=False, reason="confirmed-failure")

        def duplicate_control(decision):
            return SimpleNamespace(
                queue_fallback=False,
                publish_current_exact_now=False,
                steer_outer_iteration=False,
                continue_predicate_loop=True,
                reason="skip-known-fallback:confirmed-failure",
            )

        plan = plan_cohort_handoff(
            predicate="ts-module-resolution:@vitejs/plugin-react",
            direct_packages=("vite", "@vitejs/plugin-react"),
            assignment={"vite": "7.0.0", "@vitejs/plugin-react": "6.1.1"},
            current_versions={"vite": "4.3.9", "@vitejs/plugin-react": "4.3.2"},
            assignment_fingerprint_fn=lambda _assignment: "same",
            _infer_cohort=self.fake_infer,
            _build_fallback=self.fake_fallback,
            _queue_decision=duplicate_decision,
            _handoff_control=duplicate_control,
        )
        self.assertTrue(plan.actionable)
        self.assertFalse(plan.control.queue_fallback)
        self.assertFalse(plan.control.publish_current_exact_now)
        self.assertFalse(plan.control.steer_outer_iteration)

    def test_no_inferred_cohort_falls_back_to_existing_common_path(self):
        plan = plan_cohort_handoff(
            predicate="unknown:x",
            direct_packages=("a",),
            assignment={"a": "2.0.0"},
            current_versions={"a": "1.0.0"},
            assignment_fingerprint_fn=lambda _assignment: "x",
            _infer_cohort=lambda **_kwargs: None,
            _build_fallback=self.fake_fallback,
            _queue_decision=self.fake_decision,
            _handoff_control=self.fake_control,
        )
        self.assertFalse(plan.actionable)
        self.assertEqual("cohort-inference-unavailable", plan.reason)


class Psi591StaticIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generator = (
            ROOT / "dependency_live_roadmap_generator.py"
        ).read_text(encoding="utf-8")
        cls.helper = (
            ROOT / "block_psi591_unified_cohort_handoff.py"
        ).read_text(encoding="utf-8")

    def test_both_exhaustion_routes_use_common_planner(self):
        self.assertGreaterEqual(
            self.generator.count("plan_cohort_handoff("),
            2,
        )
        self.assertIn(
            "# BLOCK_PSI591_DIRECT_COHORT_HANDOFF_V1",
            self.generator,
        )
        self.assertIn(
            "# BLOCK_PSI591_UNIFIED_COHORT_HANDOFF_V1",
            self.generator,
        )

    def test_direct_exhaustion_can_create_real_exact_fallback(self):
        marker = self.generator.index(
            "# BLOCK_PSI591_DIRECT_COHORT_HANDOFF_V1"
        )
        tail = self.generator[marker : marker + 14000]
        plan = tail.index("plan_cohort_handoff(")
        offer = tail.index("pending_promising_assignments.offer(")
        steer = tail.index("predicate_search_steered = (")
        self.assertLess(plan, offer)
        self.assertLess(offer, steer)
        self.assertIn('"direct-subject-exhausted"', tail)

    def test_duplicate_direct_fallback_does_not_prepublish_exact(self):
        marker = self.generator.index(
            "# BLOCK_PSI591_DIRECT_COHORT_HANDOFF_V1"
        )
        tail = self.generator[marker : marker + 14000]
        skip = tail.index("if not cohort_control.queue_fallback:")
        publish = tail.index(
            "if cohort_control.publish_current_exact_now:"
        )
        self.assertLess(skip, publish)

    def test_common_planner_is_proof_neutral(self):
        self.assertIn('AUTHORITY = "DIAGNOSTIC_HINT"', self.helper)
        self.assertNotIn("persist_verified_nogood", self.helper)
        self.assertNotIn("solve_z3", self.helper)


if __name__ == "__main__":
    unittest.main()
