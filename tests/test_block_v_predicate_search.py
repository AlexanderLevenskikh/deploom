from __future__ import annotations

import os
import unittest

from block_v_predicate_search import (
    DiagnosticHint,
    PredicateObservation,
    PredicateProbePolicy,
    controlled_probe_assignment,
    predicate_package,
    predicate_repeat_count,
    prioritize_probe_preference,
    rank_version_probes,
)


class PredicateSearchTests(unittest.TestCase):
    def test_wrong_or_stale_hints_never_prune(self):
        versions = ["1.0.0", "1.1.0", "2.0.0", "2.1.0", "3.0.0"]
        observations = [PredicateObservation("pkg", "1.0.0", "P", True)]
        hints = [DiagnosticHint("pkg", "P", "2.1.0", "high", "changelog")]
        ranked = rank_version_probes(
            package="pkg", predicate="P", versions=versions,
            observations=observations, hints=hints,
        )
        self.assertEqual({item.version for item in ranked}, set(versions) - {"1.0.0"})
        self.assertEqual(ranked[0].version, "2.1.0")

    def test_non_monotonic_points_do_not_remove_interior(self):
        versions = ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0"]
        observations = [
            PredicateObservation("pkg", "1.0.0", "P", True),
            PredicateObservation("pkg", "1.4.0", "P", False),
        ]
        ranked = rank_version_probes(
            package="pkg", predicate="P", versions=versions,
            observations=observations,
        )
        self.assertEqual({item.version for item in ranked}, {"1.1.0", "1.2.0", "1.3.0"})
        self.assertEqual(ranked[0].version, "1.2.0")

    def test_conflicting_point_has_no_boundary_implication_and_remains_probe_eligible(self):
        versions = ["1.0.0", "2.0.0", "3.0.0", "4.0.0"]
        observations = [
            PredicateObservation("pkg", "1.0.0", "P", False),
            PredicateObservation("pkg", "4.0.0", "P", True),
            PredicateObservation("pkg", "4.0.0", "P", False),
        ]
        ranked = rank_version_probes(
            package="pkg", predicate="P", versions=versions,
            observations=observations,
        )
        self.assertEqual({item.version for item in ranked}, {"2.0.0", "3.0.0", "4.0.0"})
        conflicting = next(item for item in ranked if item.version == "4.0.0")
        self.assertNotIn("predicate-status-bracket-midpoint", conflicting.reasons)

    def test_repeat_count_uses_distinct_stable_present_versions(self):
        observations = [
            PredicateObservation("pkg", "5.1.1", "P", True),
            PredicateObservation("pkg", "5.1.1", "P", True, "other"),
            PredicateObservation("pkg", "5.1.0", "P", True),
            PredicateObservation("pkg", "4.0.0", "P", True),
            PredicateObservation("pkg", "4.0.0", "P", False),
        ]
        self.assertEqual(
            predicate_repeat_count(package="pkg", predicate="P", observations=observations),
            2,
        )

    def test_soft_preference_preserves_complete_domain_and_exact_first(self):
        domain = ["5.1.1", "5.1.0", "5.0.0", "4.0.0", "2.0.0"]
        reordered = prioritize_probe_preference(
            domain, preferred_version="4.0.0", current_version="2.0.0"
        )
        self.assertEqual(reordered[0], "5.1.1")
        self.assertEqual(reordered[1], "4.0.0")
        self.assertEqual(set(reordered), set(domain))
        self.assertEqual(len(reordered), len(domain))
        self.assertEqual(
            prioritize_probe_preference(
                domain, preferred_version="2.0.0", current_version="2.0.0"
            ),
            domain,
        )

    def test_controlled_intervention_changes_one_dimension(self):
        base = {"a": "1.0.0", "b": "2.0.0"}
        self.assertEqual(
            controlled_probe_assignment(base, package="a", version="1.1.0"),
            {"a": "1.1.0", "b": "2.0.0"},
        )

    def test_structural_predicate_package(self):
        self.assertEqual(
            predicate_package("ts-module-resolution:@vitejs/plugin-react"),
            "@vitejs/plugin-react",
        )
        self.assertEqual(
            predicate_package("toolchain-runtime-api:sass.initasynccompiler"), ""
        )

    def test_policy_defaults_and_bounded_overrides(self):
        self.assertEqual(PredicateProbePolicy(), PredicateProbePolicy(True, 2, 3))
        policy = PredicateProbePolicy.from_sources(
            {
                "predicateActiveSearch": False,
                "predicateProbeRepeatThreshold": 99,
                "predicateProbeBudget": 0,
            },
            {
                "DEPLOOM_PREDICATE_ACTIVE_SEARCH": "true",
                "DEPLOOM_PREDICATE_REPEAT_THRESHOLD": "3",
                "DEPLOOM_PREDICATE_PROBE_BUDGET": "5",
            },
        )
        self.assertEqual(policy, PredicateProbePolicy(True, 3, 5))


if __name__ == "__main__":
    unittest.main()
