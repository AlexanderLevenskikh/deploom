from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest import mock

import dependency_live_roadmap_generator as roadmap
from baseline_constraint_verifier import BaselineVerifyResult


class AdaptiveGraphGeneralizationTests(unittest.TestCase):
    def test_context_radius_expands_interaction_chain_without_changing_hard_components(self) -> None:
        component_for = {
            "a": ("a",),
            "b": ("b",),
            "c": ("c",),
            "d": ("d",),
        }
        interactions = roadmap.edge_index(
            [
                roadmap.InteractionEdge.create(
                    "a", "b", kind=roadmap.DIRECT_SHADOWING
                ),
                roadmap.InteractionEdge.create(
                    "b", "c", kind=roadmap.DIRECT_SHADOWING
                ),
                roadmap.InteractionEdge.create(
                    "c", "d", kind=roadmap.DIRECT_SHADOWING
                ),
            ]
        )

        radius1 = roadmap._expand_verification_component_context(
            ("a",), component_for, interactions, context_radius=1
        )
        radius2 = roadmap._expand_verification_component_context(
            ("a",), component_for, interactions, context_radius=2
        )
        radius3 = roadmap._expand_verification_component_context(
            ("a",), component_for, interactions, context_radius=3
        )

        self.assertEqual(("a", "b"), radius1)
        self.assertEqual(("a", "b", "c"), radius2)
        self.assertEqual(("a", "b", "c", "d"), radius3)

    def test_repeated_failure_expands_context_but_remains_navigation_only(self) -> None:
        rows = {
            "a": SimpleNamespace(current_version="1.0.0"),
            "b": SimpleNamespace(current_version="1.0.0"),
            "c": SimpleNamespace(current_version="1.0.0"),
            "d": SimpleNamespace(current_version="1.0.0"),
        }
        assignment = {
            "a": "2.0.0",
            "b": "1.0.0",
            "c": "1.0.0",
            "d": "1.0.0",
        }
        result = BaselineVerifyResult(
            False,
            "dependency",
            "unable to resolve dependency tree for a",
            output="ERESOLVE a conflict",
        )
        repeat_tracker: dict[str, int] = {}
        failed: set[tuple[str, str]] = set()

        def units(_rows, _assignment, _mode, _client, _learned, *, context_radius=1):
            packages = ("a", "b") if context_radius <= 1 else ("a", "b", "c")
            return [
                roadmap.VerificationUnit(
                    id=f"radius-{context_radius}",
                    packages=packages,
                )
            ]

        with mock.patch.object(
            roadmap, "_verification_units_for_assignment", side_effect=units
        ), mock.patch.object(
            roadmap, "_failure_package_hints", return_value={"a"}
        ):
            first = roadmap._adaptive_graph_guided_generalization_proposal(
                rows,
                assignment,
                "yellow",
                mock.Mock(),
                [],
                result,
                project_key="Demo",
                repeat_tracker=repeat_tracker,
                failed_candidates=failed,
            )
            self.assertIsNotNone(first)
            assert first is not None
            self.assertEqual(1, first.repeat_count)
            self.assertEqual(1, first.context_radius)
            self.assertEqual({"a", "b"}, set(first.candidate))

            failed.add(
                (
                    first.navigation_key,
                    roadmap.assignment_fingerprint(first.candidate),
                )
            )

            second = roadmap._adaptive_graph_guided_generalization_proposal(
                rows,
                assignment,
                "yellow",
                mock.Mock(),
                [],
                result,
                project_key="Demo",
                repeat_tracker=repeat_tracker,
                failed_candidates=failed,
            )
            self.assertIsNotNone(second)
            assert second is not None
            self.assertEqual(2, second.repeat_count)
            self.assertEqual(2, second.context_radius)
            self.assertEqual({"a", "b", "c"}, set(second.candidate))
            self.assertLess(len(second.candidate), len(assignment))

        self.assertEqual(1, len(repeat_tracker))
        self.assertEqual(1, len(failed))

    def test_failed_identical_radius_is_skipped_until_new_context_exists(self) -> None:
        rows = {
            "a": SimpleNamespace(current_version="1.0.0"),
            "b": SimpleNamespace(current_version="1.0.0"),
            "c": SimpleNamespace(current_version="1.0.0"),
            "d": SimpleNamespace(current_version="1.0.0"),
        }
        assignment = {
            "a": "2.0.0",
            "b": "1.0.0",
            "c": "1.0.0",
            "d": "1.0.0",
        }
        result = BaselineVerifyResult(
            False,
            "dependency",
            "unable to resolve dependency tree for a",
            output="ERESOLVE a conflict",
        )
        repeat_tracker: dict[str, int] = {}
        failed: set[tuple[str, str]] = set()

        def units(_rows, _assignment, _mode, _client, _learned, *, context_radius=1):
            packages = (
                ("a", "b")
                if context_radius <= 2
                else ("a", "b", "c")
            )
            return [
                roadmap.VerificationUnit(
                    id=f"radius-{context_radius}",
                    packages=packages,
                )
            ]

        with mock.patch.object(
            roadmap, "_verification_units_for_assignment", side_effect=units
        ), mock.patch.object(
            roadmap, "_failure_package_hints", return_value={"a"}
        ):
            first = roadmap._adaptive_graph_guided_generalization_proposal(
                rows,
                assignment,
                "yellow",
                mock.Mock(),
                [],
                result,
                project_key="Demo",
                repeat_tracker=repeat_tracker,
                failed_candidates=failed,
            )
            assert first is not None
            failed.add(
                (
                    first.navigation_key,
                    roadmap.assignment_fingerprint(first.candidate),
                )
            )

            second = roadmap._adaptive_graph_guided_generalization_proposal(
                rows,
                assignment,
                "yellow",
                mock.Mock(),
                [],
                result,
                project_key="Demo",
                repeat_tracker=repeat_tracker,
                failed_candidates=failed,
            )
            self.assertIsNotNone(second)
            assert second is not None
            self.assertEqual(2, second.repeat_count)
            self.assertEqual(3, second.context_radius)
            self.assertEqual({"a", "b", "c"}, set(second.candidate))
            self.assertLess(len(second.candidate), len(assignment))

    def test_production_wiring_keeps_certification_as_the_only_authority_gate(self) -> None:
        source = inspect.getsource(
            roadmap.resolve_peer_compatibility_with_verification
        )
        self.assertIn(
            "repeat_tracker=graph_generalization_repeats",
            source,
        )
        self.assertIn(
            "failed_candidates=graph_generalization_failed_candidates",
            source,
        )
        self.assertIn(
            "graph_generalization_failed_candidates.add(",
            source,
        )
        self.assertIn("if certified and stable_predicate:", source)
        self.assertIn(
            "learned[project][mode].append(generalized_nogood)",
            source,
        )
        self.assertNotIn(
            "learned[project][mode].append(graph_generalization",
            source,
        )


    def test_repeat_navigation_uses_structured_resolver_predicate(self) -> None:
        first = BaselineVerifyResult(
            False,
            "dependency",
            "npm resolver failed",
            output=(
                "npm ERR! code ERESOLVE\n"
                "npm ERR! Found: vite@4.3.9\n"
                "npm ERR! Could not resolve dependency:\n"
                'npm ERR! peer vite@"^5.0.0" from vitest@3.2.6\n'
                "full-assignment-only noise"
            ),
        )
        second = BaselineVerifyResult(
            False,
            "dependency",
            "npm resolver failed",
            output=(
                "candidate-only warning\n"
                "npm ERR! code ERESOLVE\n"
                "npm ERR! Found: vite@4.3.9\n"
                "npm ERR! Could not resolve dependency:\n"
                'npm ERR! peer vite@"^5.0.0" from vitest@3.2.6'
            ),
        )
        self.assertEqual(
            roadmap._graph_generalization_repeat_predicate(first),
            roadmap._graph_generalization_repeat_predicate(second),
        )

    def test_production_dependency_certification_uses_structured_matcher(self) -> None:
        source = inspect.getsource(
            roadmap.resolve_peer_compatibility_with_verification
        )
        self.assertGreaterEqual(
            source.count("matching_dependency_failure_signature("),
            2,
            source,
        )


if __name__ == "__main__":
    unittest.main()
