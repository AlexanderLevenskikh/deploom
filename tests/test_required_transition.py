from __future__ import annotations

import unittest

from peer_solver_model import PackageVariable, PeerOptimizationModel, forbidden
from peer_solver_transition import refine_transition_safe_groups
from peer_solver_z3 import solve_z3_exact


def package(name: str, current: str = "1.0.0", upgraded: str = "2.0.0") -> PackageVariable:
    return PackageVariable(
        name=name, current_version=current, domain=(upgraded, current),
        scores=((upgraded, (1,)), (current, (0,))),
    )


class RequiredUpdateTransitionTests(unittest.TestCase):
    def test_required_is_final_only_and_z3_enforces_it(self):
        current = {"router": "1.0.0"}
        target = {"router": "2.0.0"}
        model = PeerOptimizationModel(
            packages=(package("router"),),
            constraints=(forbidden(
                [("router", "1.0.0")],
                reason="USER_BASELINE_REQUIRED_UPDATE: router must change",
                provenance="baseline-intent",
            ),),
            objective_width=1,
        )
        self.assertIn("USER_BASELINE_REQUIRED_UPDATE", model.assignment_issue(current))
        self.assertEqual("", model.assignment_issue(target))
        solved = solve_z3_exact(model)
        self.assertEqual("optimal", solved.status, solved.detail)
        self.assertEqual(target, solved.assignment)
        transition = refine_transition_safe_groups(model, current, solved.assignment)
        self.assertTrue(transition.safe, transition.unresolved)
        self.assertEqual((("router",),), transition.groups)

    def test_source_peer_constraint_still_couples_transaction(self):
        current = {"router": "1.0.0", "adapter": "1.0.0"}
        target = {"router": "2.0.0", "adapter": "2.0.0"}
        model = PeerOptimizationModel(
            packages=(package("router"), package("adapter")),
            constraints=(
                forbidden([("router", "1.0.0")],
                          reason="USER_BASELINE_REQUIRED_UPDATE: router must change"),
                forbidden([("adapter", "1.0.0"), ("router", "2.0.0")],
                          reason="PEER_CONFLICT: adapter v1 requires router v1",
                          provenance="peer-range-current-source"),
            ),
            objective_width=1,
        )
        result = refine_transition_safe_groups(model, current, target)
        self.assertTrue(result.safe, result.unresolved)
        self.assertEqual((("adapter", "router"),), result.groups)
        self.assertEqual("", model.assignment_issue(target))

    def test_real_invalid_target_remains_invalid(self):
        current = {"router": "1.0.0", "adapter": "1.0.0"}
        target = {"router": "2.0.0", "adapter": "2.0.0"}
        model = PeerOptimizationModel(
            packages=(package("router"), package("adapter")),
            constraints=(
                forbidden([("router", "1.0.0")],
                          reason="USER_BASELINE_REQUIRED_UPDATE: router must change"),
                forbidden([("router", "2.0.0"), ("adapter", "2.0.0")],
                          reason="PEER_CONFLICT: invalid final tuple"),
            ),
            objective_width=1,
        )
        result = refine_transition_safe_groups(model, current, target)
        self.assertFalse(result.safe)
        self.assertTrue(any("INVALID_TARGET_ENDPOINT" in item for item in result.unresolved))
        self.assertIn("PEER_CONFLICT", model.assignment_issue(target))

    def test_keep_current_and_required_are_not_silently_compatible(self):
        model = PeerOptimizationModel(
            packages=(package("router"),),
            constraints=(
                forbidden([("router", "1.0.0")], reason="USER_BASELINE_REQUIRED_UPDATE: router"),
                forbidden([("router", "2.0.0")], reason="USER_BASELINE_KEEP_CURRENT: router"),
            ),
            objective_width=1,
        )
        self.assertEqual("unsat", solve_z3_exact(model).status)


if __name__ == "__main__":
    unittest.main()
