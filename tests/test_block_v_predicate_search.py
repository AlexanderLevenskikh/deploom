from __future__ import annotations

import unittest

from block_v_predicate_search import (
    DiagnosticHint,
    PredicateObservation,
    controlled_probe_assignment,
    predicate_package,
    rank_version_probes,
)


class PredicateSearchTests(unittest.TestCase):
    def test_wrong_or_stale_hints_never_prune(self):
        versions = ["1.0.0", "1.1.0", "2.0.0", "2.1.0", "3.0.0"]
        observations = [PredicateObservation("pkg", "1.0.0", "P", True)]
        hints = [DiagnosticHint("pkg", "P", "2.1.0", "high", "changelog")]
        ranked = rank_version_probes(package="pkg", predicate="P", versions=versions, observations=observations, hints=hints)
        self.assertEqual({item.version for item in ranked}, set(versions) - {"1.0.0"})
        self.assertEqual(ranked[0].version, "2.1.0")

    def test_non_monotonic_points_do_not_remove_interior(self):
        versions = ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0"]
        observations = [
            PredicateObservation("pkg", "1.0.0", "P", True),
            PredicateObservation("pkg", "1.4.0", "P", False),
        ]
        ranked = rank_version_probes(package="pkg", predicate="P", versions=versions, observations=observations)
        self.assertEqual({item.version for item in ranked}, {"1.1.0", "1.2.0", "1.3.0"})
        self.assertEqual(ranked[0].version, "1.2.0")

    def test_controlled_intervention_changes_one_dimension(self):
        base = {"a": "1.0.0", "b": "2.0.0"}
        self.assertEqual(controlled_probe_assignment(base, package="a", version="1.1.0"), {"a": "1.1.0", "b": "2.0.0"})

    def test_structural_predicate_package(self):
        self.assertEqual(predicate_package("ts-module-resolution:@vitejs/plugin-react"), "@vitejs/plugin-react")
        self.assertEqual(predicate_package("toolchain-runtime-api:sass.initasynccompiler"), "")


if __name__ == "__main__":
    unittest.main()
