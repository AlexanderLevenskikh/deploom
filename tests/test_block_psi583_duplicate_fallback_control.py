from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from block_psi582_pre_run_closure import CohortQueueDecision
from block_psi583_duplicate_fallback_control import cohort_handoff_control


class Psi583ControlTests(unittest.TestCase):
    def test_duplicate_fallback_leaves_exact_exclusion_to_common_path(self):
        control = cohort_handoff_control(
            CohortQueueDecision(False, "confirmed-failure")
        )
        self.assertFalse(control.queue_fallback)
        self.assertFalse(control.publish_current_exact_now)
        self.assertFalse(control.steer_outer_iteration)
        self.assertTrue(control.continue_predicate_loop)

    def test_new_fallback_publishes_exact_and_steers_outer_iteration(self):
        control = cohort_handoff_control(
            CohortQueueDecision(True, "new-exact-candidate")
        )
        self.assertTrue(control.queue_fallback)
        self.assertTrue(control.publish_current_exact_now)
        self.assertTrue(control.steer_outer_iteration)
        self.assertFalse(control.continue_predicate_loop)

    def test_duplicate_orchestration_cannot_self_trigger_loop_stuck(self):
        current_exact = ("vite@7", "vitest@4")
        exclusions = []

        control = cohort_handoff_control(
            CohortQueueDecision(False, "exact-exclusion")
        )

        if control.publish_current_exact_now:
            exclusions.append(current_exact)

        self.assertFalse(control.steer_outer_iteration)
        self.assertTrue(control.continue_predicate_loop)

        loop_stuck = current_exact in exclusions
        if not loop_stuck:
            exclusions.append(current_exact)

        self.assertFalse(loop_stuck)
        self.assertEqual([current_exact], exclusions)

    def test_new_fallback_does_not_reenter_common_exclusion_path(self):
        current_exact = ("vite@7", "vitest@4")
        exclusions = []

        control = cohort_handoff_control(
            CohortQueueDecision(True, "new-exact-candidate")
        )
        if control.publish_current_exact_now:
            exclusions.append(current_exact)

        self.assertTrue(control.steer_outer_iteration)
        self.assertEqual([current_exact], exclusions)


class Psi583IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generator = (
            ROOT / "dependency_live_roadmap_generator.py"
        ).read_text(encoding="utf-8")

    def test_generator_uses_explicit_handoff_control(self):
        self.assertIn("cohort_handoff_control(", self.generator)
        self.assertIn("if not cohort_control.queue_fallback:", self.generator)
        self.assertIn(
            "if cohort_control.publish_current_exact_now:",
            self.generator,
        )
        self.assertIn(
            "cohort_control.steer_outer_iteration",
            self.generator,
        )

    def test_duplicate_skip_precedes_local_exact_publication(self):
        skip_pos = self.generator.index(
            "if not cohort_control.queue_fallback:"
        )
        publish_pos = self.generator.index(
            "if cohort_control.publish_current_exact_now:"
        )
        offer_pos = self.generator.index(
            "pending_promising_assignments.offer(",
            publish_pos,
        )
        self.assertLess(skip_pos, publish_pos)
        self.assertLess(publish_pos, offer_pos)


if __name__ == "__main__":
    unittest.main()
