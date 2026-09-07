from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from block_psi56_causal_anytime_search import (
    AUTHORITY,
    PromisingAssignmentQueue,
    build_promising_assignment,
    causal_decision_projection,
    select_projected_followup,
)


class Psi56CausalProjectionTests(unittest.TestCase):
    def test_direct_subject_has_highest_rank(self) -> None:
        projection = causal_decision_projection(
            predicate="duplicate-type-universe:rollup",
            direct_packages=("rollup", "vite", "eslint"),
            subject_consumers={"rollup": ("vite",)},
            interaction_graph={"vite": {"rollup"}},
        )
        self.assertEqual("rollup", projection.ranked_packages[0])
        self.assertIn("predicate-direct-package", projection.reasons_for("rollup"))
        self.assertEqual(AUTHORITY, projection.authority)

    def test_transitive_rollup_projects_to_direct_consumers(self) -> None:
        projection = causal_decision_projection(
            predicate="duplicate-type-universe:rollup",
            direct_packages=(
                "vite",
                "vitest",
                "@storybook/builder-vite",
                "eslint",
                "postcss",
            ),
            subject_consumers={
                "rollup": ("vite", "vitest", "@storybook/builder-vite"),
            },
            interaction_graph={
                "vite": {"vitest", "@storybook/builder-vite"},
                "vitest": {"vite"},
                "@storybook/builder-vite": {"vite"},
            },
        )
        self.assertTrue(projection.ranked_packages)
        self.assertNotIn("rollup", projection.direct_packages)
        self.assertEqual(
            {"vite", "vitest", "@storybook/builder-vite"},
            set(projection.graph_backed_packages),
        )
        self.assertNotIn("eslint", projection.graph_backed_packages)
        self.assertNotIn("postcss", projection.graph_backed_packages)
        for package in ("vite", "vitest", "@storybook/builder-vite"):
            self.assertIn(
                "reverse-transitive-consumer",
                projection.reasons_for(package),
            )

    def test_unrelated_packages_do_not_outrank_graph_backed_consumers(self) -> None:
        projection = causal_decision_projection(
            predicate="duplicate-type-universe:rollup",
            direct_packages=("eslint", "postcss", "vite", "vitest"),
            subject_consumers={"rollup": ("vite", "vitest")},
            interaction_graph={},
        )
        first_two = projection.ranked_packages[:2]
        self.assertEqual({"vite", "vitest"}, set(first_two))
        self.assertFalse({"eslint", "postcss"} & set(first_two))

    def test_existing_ecosystem_inference_is_only_a_fallback(self) -> None:
        projection = causal_decision_projection(
            predicate="duplicate-type-universe:rollup",
            direct_packages=(
                "vite",
                "vitest",
                "@vitejs/plugin-react",
                "eslint",
                "postcss",
            ),
            subject_consumers={},
            interaction_graph={},
        )
        self.assertTrue(projection.ranked_packages)
        self.assertFalse(projection.graph_backed_packages)
        self.assertTrue(projection.prior_only_packages)
        self.assertNotIn("eslint", projection.ranked_packages)
        self.assertNotIn("postcss", projection.ranked_packages)

    def test_projected_followup_works_when_subject_is_not_direct(self) -> None:
        selected = select_projected_followup(
            predicates=("duplicate-type-universe:rollup",),
            assignment={
                "vite": "4.3.9",
                "vitest": "3.2.6",
                "eslint": "9.0.0",
            },
            subject_consumers={"rollup": ("vite", "vitest")},
            interaction_graph={"vite": {"vitest"}},
            visited_packages=("vite",),
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        predicate, package, projection = selected
        self.assertEqual("duplicate-type-universe:rollup", predicate)
        self.assertEqual("vitest", package)
        self.assertNotIn("vite", projection.ranked_packages)


class Psi56PromisingAssignmentTests(unittest.TestCase):
    def test_queue_prioritizes_exact_promising_point_over_similar_solver_point(self) -> None:
        queue = PromisingAssignmentQueue()
        point = build_promising_assignment(
            assignment={"a": "2.0.0", "b": "3.0.0"},
            assignment_fingerprint="exact-physical-point",
            originating_predicate="duplicate-type-universe:rollup",
            removed_predicates=("duplicate-type-universe:rollup",),
        )
        queue.offer("fixture", "yellow", point)
        solver_proposal = {"a": "2.0.0", "b": "4.0.0"}
        scheduled = queue.pop("fixture", "yellow")
        self.assertIsNotNone(scheduled)
        assert scheduled is not None
        observed_by_full_verifier: list[dict[str, str]] = []

        def fake_full_verifier(assignment: dict[str, str]) -> bool:
            observed_by_full_verifier.append(dict(assignment))
            return True

        self.assertTrue(fake_full_verifier(scheduled.assignment_dict))
        self.assertEqual([{"a": "2.0.0", "b": "3.0.0"}], observed_by_full_verifier)
        self.assertNotEqual(solver_proposal, observed_by_full_verifier[0])

    def test_promising_assignment_retains_exact_combination(self) -> None:
        point = build_promising_assignment(
            assignment={"a": "2.0.0", "b": "3.0.0", "c": "1.0.0"},
            assignment_fingerprint="abc123",
            originating_predicate="duplicate-type-universe:rollup",
            removed_predicates=("duplicate-type-universe:rollup",),
            remaining_predicates=("esm-cjs:stylelint",),
            preparation_proof_key="f" * 64,
        )
        self.assertEqual(
            {"a": "2.0.0", "b": "3.0.0", "c": "1.0.0"},
            point.assignment_dict,
        )
        self.assertEqual("abc123", point.assignment_fingerprint)
        self.assertEqual(AUTHORITY, point.authority)
        self.assertEqual(("esm-cjs:stylelint",), point.remaining_predicates)


class Psi56PreparedCachePriorityTests(unittest.TestCase):
    def test_promising_artifact_is_retained_ahead_of_normal_old_candidate(self) -> None:
        import block_v_prepared_artifact as cache

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / "index"
            index.mkdir(parents=True)
            (root / "trees").mkdir()
            keys = {
                "promising": "a" * 64,
                "normal_old": "b" * 64,
                "normal_new": "c" * 64,
            }
            now = time.time()
            for offset, key in enumerate(keys.values()):
                path = index / f"{key}.json"
                path.write_text("{}\n", encoding="utf-8")
                os.utime(path, (now + offset, now + offset))

            with mock.patch.object(cache, "_CONFIGURED_ROOT", root):
                cache._clear_prepared_artifact_search_priorities_for_tests()
                self.assertTrue(
                    cache.prioritize_prepared_artifact_record(
                        keys["promising"], priority=50, ttl_seconds=600
                    )
                )
                removed: list[str] = []

                def fake_invalidate(key: str, *, remove_tree: bool = False) -> bool:
                    removed.append(key)
                    return True

                with mock.patch.object(
                    cache,
                    "invalidate_prepared_artifact_record",
                    side_effect=fake_invalidate,
                ):
                    count = cache.prune_prepared_artifact_store(
                        max_count=2,
                        max_removals=1,
                    )
                self.assertEqual(1, count)
                self.assertEqual([keys["normal_old"]], removed)
                cache._clear_prepared_artifact_search_priorities_for_tests()

    def test_priority_does_not_make_invalid_artifact_authoritative(self) -> None:
        import block_v_prepared_artifact as cache

        key = "d" * 64
        cache._clear_prepared_artifact_search_priorities_for_tests()
        self.assertTrue(cache.prioritize_prepared_artifact_record(key, priority=99))
        # Search priority is performance-only and does not manufacture a record.
        with mock.patch.object(cache, "_CONFIGURED_ROOT", None):
            self.assertIsNone(cache.load_prepared_artifact_record(key, Path.cwd()))
        cache._clear_prepared_artifact_search_priorities_for_tests()


class Psi56GeneratorContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (ROOT / "dependency_live_roadmap_generator.py").read_text(
            encoding="utf-8"
        )

    def test_generator_projects_transitive_predicates(self) -> None:
        self.assertIn("# BLOCK_PSI56_CAUSAL_ANYTIME_SEARCH_V1", self.source)
        self.assertIn("causal_decision_projection(", self.source)
        self.assertIn('"causal-projection-created"', self.source)
        self.assertIn("select_projected_followup(", self.source)

    def test_exact_promising_point_is_scheduled_whole(self) -> None:
        self.assertIn("pending_promising_assignments", self.source)
        self.assertIn("build_promising_assignment(", self.source)
        self.assertIn('"promising-assignment-created"', self.source)
        self.assertIn('"promising-assignment-prioritized"', self.source)
        self.assertIn("promising_candidate.assignment_dict", self.source)

    def test_promising_point_does_not_create_solver_authority(self) -> None:
        marker = "# BLOCK_PSI56_CAUSAL_ANYTIME_SEARCH_V1"
        start = self.source.index(marker)
        # The scheduling block must remain before ordinary physical verification.
        end = self.source.index("# BLOCK_PSI56_CAUSAL_ANYTIME_SEARCH_END", start)
        block = self.source[start:end]
        self.assertIn("authority=EVIDENCE_DIAGNOSTIC_HINT", block)
        self.assertNotIn("learned[project][mode].append", block)
        self.assertNotIn("EVIDENCE_CONFIRMED_CONSTRAINT", block)

    def test_psi55_transitive_override_boundary_is_preserved(self) -> None:
        psi55 = (ROOT / "tests" / "test_block_psi55_resolver_overrides.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("test_transitive_only_subject_is_not_guessed", psi55)


if __name__ == "__main__":
    unittest.main()
