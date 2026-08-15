from __future__ import annotations

import itertools
import unittest

from peer_solver_model import PackageVariable, PeerOptimizationModel, RequiresAny, forbidden
from peer_solver_transition import refine_transition_safe_groups, transition_assignment_for_groups


def package(name: str) -> PackageVariable:
    return PackageVariable(
        name=name,
        current_version="1",
        domain=("2", "1"),
        scores=(("2", (1,)), ("1", (0,))),
    )


class PeerSolverTransitionTests(unittest.TestCase):
    def assert_all_transition_vertices_safe(self, model, current, target, groups):
        for mask in range(1 << len(groups)):
            applied = [index for index in range(len(groups)) if mask & (1 << index)]
            assignment = transition_assignment_for_groups(current, target, groups, applied)
            self.assertEqual("", model.assignment_issue(assignment), (groups, applied, assignment))

    def test_current_source_constraint_couples_target_companion(self):
        model = PeerOptimizationModel(
            packages=(package("plugin"), package("core")),
            constraints=(
                forbidden(
                    [("plugin", "1"), ("core", "2")],
                    reason="plugin@1 requires core@1",
                    provenance="peer-range-current-source",
                ),
            ),
            objective_width=1,
        )
        current = {"plugin": "1", "core": "1"}
        target = {"plugin": "2", "core": "2"}
        result = refine_transition_safe_groups(model, current, target)
        self.assertTrue(result.safe)
        self.assertEqual((("core", "plugin"),), result.groups)
        self.assertEqual(1, len(result.merges))
        self.assert_all_transition_vertices_safe(model, current, target, result.groups)

    def test_requires_any_couples_trigger_and_provider_when_provider_current_is_invalid(self):
        model = PeerOptimizationModel(
            packages=(package("plugin"), package("core")),
            requirements=(
                RequiresAny(
                    trigger=("plugin", "2"),
                    provider="core",
                    allowed_versions=("2",),
                    reason="plugin@2 requires core@2",
                ),
            ),
            objective_width=1,
        )
        current = {"plugin": "1", "core": "1"}
        target = {"plugin": "2", "core": "2"}
        result = refine_transition_safe_groups(model, current, target)
        self.assertEqual((("core", "plugin"),), result.groups)
        self.assert_all_transition_vertices_safe(model, current, target, result.groups)

    def test_safe_independent_packages_remain_separate(self):
        model = PeerOptimizationModel(
            packages=(package("a"), package("b"), package("c")),
            objective_width=1,
        )
        current = {"a": "1", "b": "1", "c": "1"}
        target = {"a": "2", "b": "2", "c": "2"}
        result = refine_transition_safe_groups(model, current, target)
        self.assertEqual((("a",), ("b",), ("c",)), result.groups)
        self.assertEqual((), result.merges)
        self.assert_all_transition_vertices_safe(model, current, target, result.groups)

    def test_existing_group_can_already_make_violation_unreachable(self):
        model = PeerOptimizationModel(
            packages=(package("a"), package("b"), package("c")),
            constraints=(forbidden([("a", "2"), ("b", "1")], reason="a/b ordering"),),
            objective_width=1,
        )
        current = {"a": "1", "b": "1", "c": "1"}
        target = {"a": "2", "b": "2", "c": "2"}
        result = refine_transition_safe_groups(model, current, target, initial_groups=[{"a", "b"}])
        self.assertEqual((("a", "b"), ("c",)), result.groups)
        self.assertEqual((), result.merges)
        self.assert_all_transition_vertices_safe(model, current, target, result.groups)

    def test_iterative_refinement_handles_multiple_constraints_without_subset_enumeration(self):
        model = PeerOptimizationModel(
            packages=tuple(package(name) for name in ("a", "b", "c", "d")),
            constraints=(
                forbidden([("a", "2"), ("b", "1")], reason="a requires b transition"),
                forbidden([("b", "2"), ("c", "1")], reason="b requires c transition"),
                forbidden([("c", "2"), ("d", "1")], reason="c requires d transition"),
            ),
            objective_width=1,
        )
        current = {name: "1" for name in ("a", "b", "c", "d")}
        target = {name: "2" for name in ("a", "b", "c", "d")}
        result = refine_transition_safe_groups(model, current, target)
        self.assertTrue(result.safe)
        self.assertEqual((("a", "b", "c", "d"),), result.groups)
        self.assertLessEqual(len(result.merges), 3)
        self.assert_all_transition_vertices_safe(model, current, target, result.groups)

    def test_invalid_target_endpoint_is_reported_not_hidden_by_merging(self):
        model = PeerOptimizationModel(
            packages=(package("a"), package("b")),
            constraints=(forbidden([("a", "2"), ("b", "2")], reason="bad final tuple"),),
            objective_width=1,
        )
        current = {"a": "1", "b": "1"}
        target = {"a": "2", "b": "2"}
        result = refine_transition_safe_groups(model, current, target)
        self.assertFalse(result.safe)
        self.assertTrue(any("INVALID_TARGET_ENDPOINT" in item for item in result.unresolved))


if __name__ == "__main__":
    unittest.main()
