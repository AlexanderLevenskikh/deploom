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
        # Block Sigma keeps a minimized graph shape diagnostic-only. Solver
        # authority is the freshly confirmed full exact assignment, so omitted
        # context dimensions can never be generalized away implicitly.
        self.assertIn("diagnostic_constraints: List[Dict[str, str]]", source)
        self.assertIn('clauseScope="context-diagnostic"', source)
        self.assertIn("global_exact_exclusions[project][mode].append(exact_nogood)", source)
        self.assertNotIn("learned[project][mode].append(generalized)", source)
        certification_index = source.index("if certified and stable_predicate:")
        family_split_index = source.index(
            "predicate_families = _adaptive_predicate_families(",
            certification_index,
        )
        family_results_index = source.index(
            "family_results: List[Tuple[str, Dict[str, str], NogoodMinimizationResult]]",
            family_split_index,
        )
        authority_loop_index = source.index(
            "global_exact_exclusions[project][mode].append(exact_nogood)",
            family_results_index,
        )
        self.assertLess(certification_index, family_split_index)
        self.assertLess(family_split_index, family_results_index)
        self.assertLess(family_results_index, authority_loop_index)


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


    def test_literal_budget_grows_finer_than_graph_radius(self) -> None:
        self.assertEqual(2, roadmap._graph_generalization_literal_budget(1, 2))
        self.assertEqual(4, roadmap._graph_generalization_literal_budget(2, 2))
        self.assertEqual(6, roadmap._graph_generalization_literal_budget(3, 2))
        self.assertEqual(8, roadmap._graph_generalization_literal_budget(4, 2))
        self.assertEqual(12, roadmap._graph_generalization_literal_budget(5, 2))
        self.assertEqual(16, roadmap._graph_generalization_literal_budget(6, 2))
        self.assertEqual(24, roadmap._graph_generalization_literal_budget(7, 2))
        self.assertEqual(32, roadmap._graph_generalization_literal_budget(8, 2))
        self.assertEqual(32, roadmap._graph_generalization_literal_budget(99, 2))

    def test_oversized_radius_is_sliced_instead_of_disappearing(self) -> None:
        names = [chr(ord("a") + index) for index in range(12)]
        rows = {
            name: SimpleNamespace(current_version="1.0.0")
            for name in names
        }
        assignment = {
            name: ("2.0.0" if name in {"a", "c", "d", "e"} else "1.0.0")
            for name in names
        }
        unit = roadmap.VerificationUnit(
            id="oversized",
            packages=tuple(names[:10]),
        )

        with mock.patch.object(
            roadmap,
            "_verification_units_for_assignment",
            return_value=[unit],
        ), mock.patch.object(
            roadmap,
            "_failure_package_hints",
            return_value={"a"},
        ):
            candidate = roadmap._bounded_graph_guided_generalization_candidate(
                rows,
                assignment,
                "yellow",
                mock.Mock(),
                [],
                BaselineVerifyResult(
                    False,
                    "dependency",
                    "ERESOLVE",
                    output="peer conflict",
                ),
                context_radius=3,
                literal_budget=6,
                required_packages=("a", "b"),
            )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(6, len(candidate))
        self.assertIn("a", candidate)
        self.assertIn("b", candidate)
        self.assertLess(len(candidate), len(assignment))
        self.assertTrue({"a", "c", "d", "e"} & set(candidate))

    def test_repeat_three_uses_bounded_slice_instead_of_returning_none(self) -> None:
        names = [chr(ord("a") + index) for index in range(12)]
        rows = {
            name: SimpleNamespace(current_version="1.0.0")
            for name in names
        }
        assignment = {
            name: ("2.0.0" if name in {"a", "c", "d", "e"} else "1.0.0")
            for name in names
        }
        result = BaselineVerifyResult(
            False,
            "dependency",
            "unable to resolve dependency tree",
            output="ERESOLVE peer conflict",
        )
        repeat_tracker: dict[str, int] = {}
        failed: set[tuple[str, str]] = set()
        seeds: dict[str, tuple[str, ...]] = {}

        def units(_rows, _assignment, _mode, _client, _learned, *, context_radius=1):
            if context_radius <= 1:
                packages = ("a", "b")
            elif context_radius == 2:
                packages = ("a", "b", "c")
            else:
                packages = tuple(names[:10])
            return [
                roadmap.VerificationUnit(
                    id=f"radius-{context_radius}",
                    packages=packages,
                )
            ]

        with mock.patch.object(
            roadmap,
            "_verification_units_for_assignment",
            side_effect=units,
        ), mock.patch.object(
            roadmap,
            "_failure_package_hints",
            return_value={"a"},
        ):
            proposals = []
            for expected_repeat in (1, 2, 3):
                proposal = roadmap._adaptive_graph_guided_generalization_proposal(
                    rows,
                    assignment,
                    "yellow",
                    mock.Mock(),
                    [],
                    result,
                    project_key="Demo",
                    repeat_tracker=repeat_tracker,
                    failed_candidates=failed,
                    seed_packages_by_family=seeds,
                )
                self.assertIsNotNone(proposal)
                assert proposal is not None
                proposals.append(proposal)
                self.assertEqual(expected_repeat, proposal.repeat_count)
                if expected_repeat < 3:
                    failed.add(
                        (
                            proposal.navigation_key,
                            roadmap.assignment_fingerprint(proposal.candidate),
                        )
                    )

        first, second, third = proposals
        self.assertEqual(2, len(first.candidate))
        self.assertEqual(3, len(second.candidate))
        self.assertEqual(3, third.context_radius)
        self.assertEqual(6, third.literal_budget)
        self.assertEqual(6, len(third.candidate))
        self.assertTrue(third.bounded_slice)
        self.assertLess(len(third.candidate), len(assignment))

    def test_seed_can_carry_forward_within_same_stable_failure_family(self) -> None:
        names = ("a", "b", "c", "d", "e", "f")
        rows = {
            name: SimpleNamespace(current_version="1.0.0")
            for name in names
        }
        assignment = {
            name: ("2.0.0" if name in {"a", "c"} else "1.0.0")
            for name in names
        }
        result = BaselineVerifyResult(
            False,
            "dependency",
            "npm resolver failed",
            output=(
                "npm ERR! code ERESOLVE\n"
                "npm ERR! Found: a@2.0.0\n"
                "npm ERR! Could not resolve dependency:\n"
                'npm ERR! peer a@"^1.0.0" from b@1.0.0'
            ),
        )
        repeat_tracker: dict[str, int] = {}
        failed: set[tuple[str, str]] = set()
        seeds: dict[str, tuple[str, ...]] = {}
        hints = {"value": {"a"}}

        def units(_rows, _assignment, _mode, _client, _learned, *, context_radius=1):
            packages = ("a", "b") if context_radius <= 1 else ("a", "b", "c", "d")
            return [
                roadmap.VerificationUnit(
                    id=f"radius-{context_radius}",
                    packages=packages,
                )
            ]

        with mock.patch.object(
            roadmap,
            "_verification_units_for_assignment",
            side_effect=units,
        ), mock.patch.object(
            roadmap,
            "_failure_package_hints",
            side_effect=lambda *_args, **_kwargs: set(hints["value"]),
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
                seed_packages_by_family=seeds,
            )
            assert first is not None
            self.assertEqual("fresh", first.seed_source)
            failed.add(
                (
                    first.navigation_key,
                    roadmap.assignment_fingerprint(first.candidate),
                )
            )

            hints["value"] = set()
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
                seed_packages_by_family=seeds,
            )

        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual("carry-forward", second.seed_source)
        self.assertEqual(2, second.repeat_count)
        self.assertIn("a", second.candidate)
        self.assertIn("b", second.candidate)

    def test_block_d_navigation_state_is_not_solver_authority(self) -> None:
        source = inspect.getsource(
            roadmap.resolve_peer_compatibility_with_verification
        )
        self.assertIn(
            "seed_packages_by_family=graph_generalization_seed_packages",
            source,
        )
        self.assertNotIn(
            "learned[project][mode].append(graph_generalization_seed_packages",
            source,
        )
        self.assertNotIn(
            "global_exact_exclusions[project][mode].append(graph_generalization_seed_packages",
            source,
        )
        self.assertIn("if certified and stable_predicate:", source)


if __name__ == "__main__":
    unittest.main()
class BaselineLivenessBudgetTests(unittest.TestCase):
    def test_authoritative_exact_exclusions_and_fresh_learning_share_extension_budget(self) -> None:
        budget = roadmap.BaselineLivenessBudget(
            base_iterations=8,
            max_learning_extensions=8,
            starting_learned_constraints=3,
        )
        self.assertEqual(8, budget.allowed_iterations)
        self.assertEqual(16, budget.hard_iterations)

        # A freshly confirmed exact tuple exclusion strengthens the authoritative
        # formula just like a learned generalized clause, so it must buy the
        # follow-up solve that can actually consume that new fact.
        self.assertTrue(budget.record_exact_exclusion())
        self.assertEqual(9, budget.allowed_iterations)
        self.assertEqual(1, budget.exact_extension_credits)
        self.assertEqual(1, budget.certified_extensions)
        self.assertEqual(1, budget.exact_since_learning)

        # Fresh generalized authority uses the same bounded extension pool.
        budget.observe_learned_constraints(4)
        self.assertEqual(10, budget.allowed_iterations)
        self.assertEqual(2, budget.certified_extensions)
        self.assertEqual(1, budget.learned_extensions)
        self.assertEqual(0, budget.exact_since_learning)

    def test_learning_extensions_are_hard_capped_and_snapshot_is_complete(self) -> None:
        budget = roadmap.BaselineLivenessBudget(
            base_iterations=8,
            max_learning_extensions=8,
        )
        budget.record_generalization_attempt()
        budget.record_diagnostic()
        for learned in range(1, 25):
            budget.observe_learned_constraints(learned)

        snapshot = budget.snapshot(learned_constraints=24)
        self.assertEqual(16, budget.allowed_iterations)
        self.assertEqual(16, snapshot["hardIterations"])
        self.assertEqual(8, snapshot["certifiedExtensions"])
        self.assertEqual(24, snapshot["learnedConstraints"])
        self.assertEqual(1, snapshot["generalizationAttempts"])
        self.assertEqual(1, snapshot["diagnostics"])
class ProofPreservingNogoodMinimizationTests(unittest.TestCase):
    def test_minimizer_finds_two_literal_conflict_with_fresh_certification(self) -> None:
        clause = {
            "a": "2.0.0",
            "b": "3.0.0",
            "c": "4.0.0",
            "d": "5.0.0",
            "e": "6.0.0",
            "f": "7.0.0",
            "g": "8.0.0",
            "h": "9.0.0",
        }
        calls: list[tuple[str, ...]] = []

        def certify(candidate: dict[str, str], _check: int) -> str:
            shape = tuple(sorted(candidate))
            calls.append(shape)
            return "peer:a+b" if {"a", "b"} <= set(candidate) else ""

        result = roadmap._proof_preserving_minimize_nogood(
            clause,
            certify,
            initial_predicate="peer:a+b",
            max_checks=12,
        )

        self.assertEqual({"a": "2.0.0", "b": "3.0.0"}, result.minimized)
        self.assertEqual("peer:a+b", result.predicate)
        self.assertGreaterEqual(result.accepted_shrinks, 1)
        self.assertEqual(8, result.shrink_history[0])
        self.assertEqual(2, result.shrink_history[-1])
        self.assertLessEqual(result.checks, 12)
        self.assertTrue(calls)

    def test_minimizer_never_accepts_shrink_without_certifier(self) -> None:
        clause = {
            "a": "2.0.0",
            "b": "3.0.0",
            "c": "4.0.0",
            "d": "5.0.0",
        }
        result = roadmap._proof_preserving_minimize_nogood(
            clause,
            lambda _candidate, _check: "",
            initial_predicate="stable-proof",
            max_checks=8,
        )
        self.assertEqual(clause, result.minimized)
        self.assertEqual(0, result.accepted_shrinks)
        self.assertEqual((4,), result.shrink_history)

    def test_localized_clause_requires_literal_level_initial_certification(self) -> None:
        clause = {"a": "2.0.0", "b": "3.0.0"}
        calls = 0

        def certify(candidate: dict[str, str], _check: int) -> str:
            nonlocal calls
            calls += 1
            return "stable" if set(candidate) == {"a", "b"} else ""

        result = roadmap._proof_preserving_minimize_nogood(
            clause,
            certify,
            initial_predicate="",
            max_checks=4,
        )
        self.assertGreaterEqual(calls, 1)
        self.assertEqual("stable", result.predicate)
        self.assertEqual(clause, result.minimized)

    def test_minimization_budget_scales_but_is_hard_bounded(self) -> None:
        self.assertEqual(0, roadmap._nogood_minimization_check_budget(1))
        self.assertGreaterEqual(roadmap._nogood_minimization_check_budget(8), 4)
        self.assertLessEqual(
            roadmap._nogood_minimization_check_budget(128),
            roadmap.NOGOOD_MINIMIZATION_MAX_CHECKS,
        )

    def test_production_wires_minimization_before_solver_authority(self) -> None:
        source = inspect.getsource(
            roadmap.resolve_peer_compatibility_with_verification
        )
        self.assertGreaterEqual(
            source.count("_proof_preserving_minimize_nogood("),
            2,
            source,
        )
        graph_minimization_index = source.index(
            "minimization = _proof_preserving_minimize_nogood("
        )
        family_authority_loop_index = source.index(
            "global_exact_exclusions[project][mode].append(exact_nogood)",
            graph_minimization_index,
        )
        self.assertLess(graph_minimization_index, family_authority_loop_index)
        localized_minimization_index = source.index(
            "localized_minimization = _proof_preserving_minimize_nogood("
        )
        localized_exact_authority_index = source.index(
            "global_exact_exclusions[project][mode].append(dict(exact_nogood))",
            localized_minimization_index,
        )
        self.assertLess(localized_minimization_index, localized_exact_authority_index)
        self.assertNotIn("learned[project][mode].append(nogood)", source)

    def test_convergence_has_typed_plateau_and_hard_safety_terminal_codes(self) -> None:
        source = inspect.getsource(
            roadmap.resolve_peer_compatibility_with_verification
        )
        self.assertIn("BASELINE_VERIFICATION_PLATEAU", source)
        self.assertIn("BASELINE_VERIFICATION_HARD_SAFETY_LIMIT", source)
        self.assertIn("BaselineTerminalStatus.PLATEAU", source)
        self.assertIn("BaselineTerminalStatus.HARD_SAFETY_LIMIT", source)
        self.assertIn("BASELINE_SOLVER_REPEATED_FAILED_ASSIGNMENT", source)
        self.assertNotIn("BASELINE_VERIFICATION_HARD_BUDGET_EXHAUSTED", source)
class CrossIterationConflictHistoryTests(unittest.TestCase):
    def _proposal(
        self,
        candidate: dict[str, str],
        *,
        navigation_key: str = "family",
        seed_source: str = "fresh",
    ) -> roadmap.GraphGeneralizationProposal:
        return roadmap.GraphGeneralizationProposal(
            candidate=candidate,
            seed_candidate_fingerprint=roadmap.assignment_fingerprint(candidate),
            navigation_key=navigation_key,
            context_radius=2,
            repeat_count=2,
            predicate_key=navigation_key,
            family_key=navigation_key,
            literal_budget=max(4, len(candidate)),
            bounded_slice=False,
            seed_source=seed_source,
        )

    def test_consensus_uses_recurrent_core_but_remains_only_a_proposal(self) -> None:
        history: dict[str, list[tuple[str, ...]]] = {}
        assignment = {
            "a": "2.0.0",
            "b": "3.0.0",
            "c": "4.0.0",
            "d": "5.0.0",
        }

        first = roadmap._cross_iteration_consensus_proposal(
            self._proposal(
                {"a": "2.0.0", "b": "3.0.0", "c": "4.0.0"}
            ),
            assignment,
            history,
            set(),
        )
        self.assertEqual("fresh", first.seed_source)

        second = roadmap._cross_iteration_consensus_proposal(
            self._proposal(
                {"a": "2.0.0", "b": "3.0.0", "d": "5.0.0"}
            ),
            assignment,
            history,
            set(),
        )
        self.assertEqual("cross-iteration-consensus", second.seed_source)
        self.assertEqual(
            {"a": "2.0.0", "b": "3.0.0"},
            second.candidate,
        )
        self.assertTrue(second.bounded_slice)

    def test_failed_consensus_candidate_is_not_retried(self) -> None:
        assignment = {
            "a": "2.0.0",
            "b": "3.0.0",
            "c": "4.0.0",
            "d": "5.0.0",
        }
        history = {
            "family": [
                ("a", "b", "c"),
                ("a", "b", "d"),
            ]
        }
        consensus = {"a": "2.0.0", "b": "3.0.0"}
        failed = {
            ("family", roadmap.assignment_fingerprint(consensus))
        }
        original = self._proposal(
            {"a": "2.0.0", "b": "3.0.0", "c": "4.0.0"}
        )

        result = roadmap._cross_iteration_consensus_proposal(
            original,
            assignment,
            history,
            failed,
        )
        self.assertEqual(original.candidate, result.candidate)
        self.assertNotEqual("cross-iteration-consensus", result.seed_source)

    def test_history_is_bounded(self) -> None:
        history: dict[str, list[tuple[str, ...]]] = {}
        assignment = {
            chr(ord("a") + index): "2.0.0"
            for index in range(12)
        }
        for index in range(12):
            names = {
                "a": "2.0.0",
                chr(ord("b") + (index % 10)): "2.0.0",
                chr(ord("b") + ((index + 1) % 10)): "2.0.0",
            }
            roadmap._cross_iteration_consensus_proposal(
                self._proposal(names),
                assignment,
                history,
                set(),
            )
        self.assertLessEqual(
            len(history["family"]),
            roadmap.GRAPH_GENERALIZATION_HISTORY_LIMIT,
        )

    def test_production_consensus_still_flows_through_fresh_certification(self) -> None:
        source = inspect.getsource(
            roadmap.resolve_peer_compatibility_with_verification
        )
        consensus_index = source.index(
            "_cross_iteration_consensus_proposal("
        )
        certification_index = source.index(
            "for proof_index in range(proof_count):",
            consensus_index,
        )
        minimization_index = source.index(
            "minimization = _proof_preserving_minimize_nogood(",
            certification_index,
        )
        diagnostic_index = source.index(
            "diagnostic_constraints: List[Dict[str, str]]",
            minimization_index,
        )
        authority_index = source.index(
            "global_exact_exclusions[project][mode].append(exact_nogood)",
            diagnostic_index,
        )
        self.assertLess(consensus_index, certification_index)
        self.assertLess(certification_index, minimization_index)
        self.assertLess(minimization_index, diagnostic_index)
        self.assertLess(diagnostic_index, authority_index)
        helper = inspect.getsource(
            roadmap._cross_iteration_consensus_proposal
        )
        self.assertNotIn("learned[", helper)
        self.assertIn('seed_source="cross-iteration-consensus"', helper)
