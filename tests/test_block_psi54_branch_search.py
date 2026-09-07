from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from block_psi54_branch_search import (
    PredicateBranchPolicy,
    choose_followup_version,
    fresh_predicate_branch_seed,
    select_followup_predicate,
)
from block_v_predicate_search import ProbeCandidate
from block_vf_active_search import (
    PROBE_OUTCOME_ABSENT,
    PROBE_OUTCOME_INCONCLUSIVE,
    PROBE_OUTCOME_PRESENT,
    ProbeExecution,
)


def candidate(version: str) -> ProbeCandidate:
    return ProbeCandidate(
        package="rollup",
        version=version,
        predicate="duplicate-type-universe:rollup",
        score=(0, 0, 0, 0, (0, 0, 0, 0, version)),
        reasons=(),
    )


class Psi54PureBranchTests(unittest.TestCase):
    def test_fresh_absent_point_with_remaining_predicate_seeds_branch(self) -> None:
        seed = fresh_predicate_branch_seed(
            package="@vitejs/plugin-react",
            predicate="ts-module-resolution:@vitejs/plugin-react",
            preferred_version="4.3.2",
            executions=(
                ProbeExecution(
                    version="4.3.2",
                    outcome=PROBE_OUTCOME_ABSENT,
                    assignment_fingerprint="fresh-point",
                    other_predicates=("duplicate-type-universe:rollup",),
                ),
            ),
        )
        self.assertIsNotNone(seed)
        assert seed is not None
        self.assertEqual("fresh-point", seed.assignment_fingerprint)
        self.assertEqual(
            ("duplicate-type-universe:rollup",),
            seed.other_predicates,
        )

    def test_persisted_preference_without_fresh_execution_cannot_seed(self) -> None:
        self.assertIsNone(
            fresh_predicate_branch_seed(
                package="@vitejs/plugin-react",
                predicate="ts-module-resolution:@vitejs/plugin-react",
                preferred_version="4.3.2",
                executions=(),
            )
        )

    def test_present_inconclusive_and_empty_tail_cannot_seed(self) -> None:
        for execution in (
            ProbeExecution(
                version="4.3.2",
                outcome=PROBE_OUTCOME_PRESENT,
                assignment_fingerprint="a",
                other_predicates=("duplicate-type-universe:rollup",),
            ),
            ProbeExecution(
                version="4.3.2",
                outcome=PROBE_OUTCOME_INCONCLUSIVE,
                assignment_fingerprint="b",
                other_predicates=("duplicate-type-universe:rollup",),
            ),
            ProbeExecution(
                version="4.3.2",
                outcome=PROBE_OUTCOME_ABSENT,
                assignment_fingerprint="c",
                other_predicates=(),
            ),
        ):
            with self.subTest(outcome=execution.outcome):
                self.assertIsNone(
                    fresh_predicate_branch_seed(
                        package="@vitejs/plugin-react",
                        predicate="ts-module-resolution:@vitejs/plugin-react",
                        preferred_version="4.3.2",
                        executions=(execution,),
                    )
                )

    def test_followup_prefers_different_unvisited_direct_package(self) -> None:
        selected = select_followup_predicate(
            predicates=(
                "ts-module-resolution:@vitejs/plugin-react",
                "duplicate-type-universe:rollup",
            ),
            source_package="@vitejs/plugin-react",
            assignment={
                "@vitejs/plugin-react": "4.3.2",
                "rollup": "3.29.5",
            },
            visited_packages=("@vitejs/plugin-react",),
        )
        self.assertEqual(
            ("duplicate-type-universe:rollup", "rollup"),
            selected,
        )

    def test_followup_does_not_ping_pong_to_visited_package(self) -> None:
        self.assertIsNone(
            select_followup_predicate(
                predicates=("ts-module-resolution:@vitejs/plugin-react",),
                source_package="rollup",
                assignment={
                    "@vitejs/plugin-react": "4.3.2",
                    "rollup": "4.50.0",
                },
                visited_packages=("@vitejs/plugin-react", "rollup"),
            )
        )

    def test_version_selection_skips_tried_branch_and_project_current(self) -> None:
        selected = choose_followup_version(
            ranked=(
                candidate("3.29.5"),
                candidate("4.50.0"),
                candidate("4.49.0"),
                candidate("4.48.0"),
            ),
            attempted_versions=("4.49.0",),
            branch_current_version="4.50.0",
            project_current_version="3.29.5",
        )
        self.assertEqual("4.48.0", selected)

    def test_policy_is_strictly_bounded(self) -> None:
        policy = PredicateBranchPolicy.from_sources(
            {
                "predicateBranchMaxDepth": 99,
                "predicateBranchProbeBudget": 99,
            },
            {},
        )
        self.assertEqual(3, policy.max_depth)
        self.assertEqual(4, policy.probe_budget)


class Psi54GeneratorContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (ROOT / "dependency_live_roadmap_generator.py").read_text(
            encoding="utf-8"
        )

    def test_branch_is_same_run_physical_navigation_only(self) -> None:
        self.assertIn(
            "# BLOCK_PSI54_PREDICATE_BRANCH_CONTINUATION_V1",
            self.source,
        )
        self.assertIn("fresh_predicate_branch_seed(", self.source)
        self.assertIn("controlled_probe_assignment(", self.source)
        self.assertIn('"predicate-branch-probe-started"', self.source)
        self.assertIn('"predicate-branch-probe-observed"', self.source)
        self.assertIn('"predicate-branch-preference-selected"', self.source)

    def test_branch_does_not_create_solver_authority(self) -> None:
        marker = "# BLOCK_PSI54_PREDICATE_BRANCH_CONTINUATION_V1"
        start = self.source.index(marker)
        end = self.source.index(
            "predicate_search_handoff_to_generalization = bool(",
            start,
        )
        branch = self.source[start:end]
        self.assertIn("verify_assignment(", branch)
        self.assertIn("run_project_checks=True", branch)
        self.assertIn("predicate_diagnostic_preferences", branch)
        self.assertIn("authority=EVIDENCE_DIAGNOSTIC_HINT", branch)
        self.assertNotIn("global_exact_exclusions", branch)
        self.assertNotIn("learned[", branch)
        self.assertNotIn("EVIDENCE_CONFIRMED_CONSTRAINT", branch)

    def test_branch_budget_is_per_project_mode(self) -> None:
        self.assertIn(
            "predicate_branch_probes_used: Dict[Tuple[str, str], int]",
            self.source,
        )
        self.assertIn(
            "predicate_branch_policy.probe_budget",
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
