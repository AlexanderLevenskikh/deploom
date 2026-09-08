from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from block_psi58_bounded_causal_search import (
    AUTHORITY as CAUSAL_AUTHORITY,
    CausalSearchPolicy,
    bounded_causal_probe_domain,
    causal_version_signature,
    family_budget_exhausted,
)
from block_psi58_reuse_economics import (
    AUTHORITY as REUSE_AUTHORITY,
    RESOLVER_SEED_PUBLICATION_BRIDGE,
    resolver_seed_publication_decision,
)


class Psi58ResolverSeedEconomicsTests(unittest.TestCase):
    def test_only_resolver_only_bridge_is_published(self) -> None:
        yes = resolver_seed_publication_decision(
            capability_enabled=True,
            fresh_resolver=True,
            run_project_checks=False,
            publication_hint=RESOLVER_SEED_PUBLICATION_BRIDGE,
            verification_purpose="intermediate-candidate",
        )
        self.assertTrue(yes.publish)
        self.assertEqual("expected-later-lifecycle", yes.reason)

        inline = resolver_seed_publication_decision(
            capability_enabled=True,
            fresh_resolver=True,
            run_project_checks=True,
            publication_hint=RESOLVER_SEED_PUBLICATION_BRIDGE,
            verification_purpose="intermediate-candidate",
        )
        self.assertFalse(inline.publish)
        self.assertEqual("inline-lifecycle-no-copy", inline.reason)

        one_off = resolver_seed_publication_decision(
            capability_enabled=True,
            fresh_resolver=True,
            run_project_checks=False,
            publication_hint="",
            verification_purpose="diagnostic-probe",
        )
        self.assertFalse(one_off.publish)
        self.assertEqual("no-expected-consumer", one_off.reason)
        self.assertEqual("PERFORMANCE_ONLY", REUSE_AUTHORITY)


class Psi58CausalBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.meta = {
            "versions": {
                "7.3.1": {"peerDependencies": {"vite": "^4.0.0"}},
                "7.5.1": {"peerDependencies": {"vite": "^4.0.0"}},
                "8.0.0": {"peerDependencies": {"vite": "^5.0.0"}},
                "9.0.0": {"peerDependencies": {"vite": "^6.0.0"}},
            }
        }
        self.policy = CausalSearchPolicy(
            per_package_probe_budget=2,
            family_probe_budget=6,
            max_parents=3,
            representative_limit=2,
        )

    def test_signature_groups_versions_by_relevant_relationships(self) -> None:
        a = causal_version_signature(
            self.meta, version="7.3.1", relevant_packages=("rollup", "vite")
        )
        b = causal_version_signature(
            self.meta, version="7.5.1", relevant_packages=("rollup", "vite")
        )
        c = causal_version_signature(
            self.meta, version="8.0.0", relevant_packages=("rollup", "vite")
        )
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_direct_package_signature_uses_peer_surface(self) -> None:
        a = causal_version_signature(
            self.meta, version="7.5.1", relevant_packages=("builder",)
        )
        b = causal_version_signature(
            self.meta, version="8.0.0", relevant_packages=("builder",)
        )
        self.assertNotEqual(a, b)

    def test_at_most_two_distinct_signature_representatives_are_probed(self) -> None:
        domain = bounded_causal_probe_domain(
            metadata=self.meta,
            versions=("7.3.1", "7.5.1", "8.0.0", "9.0.0"),
            relevant_packages=("rollup", "vite"),
            attempted_versions=(),
            policy=self.policy,
        )
        self.assertEqual(2, len(domain.versions))
        signatures = {
            causal_version_signature(
                self.meta, version=version, relevant_packages=("rollup", "vite")
            )
            for version in domain.versions
        }
        self.assertEqual(2, len(signatures))
        self.assertFalse(domain.exhausted)
        self.assertEqual("DIAGNOSTIC_HINT", CAUSAL_AUTHORITY)

    def test_attempted_signature_is_not_reprobed_and_budget_is_global_per_parent(self) -> None:
        first = bounded_causal_probe_domain(
            metadata=self.meta,
            versions=("7.3.1", "7.5.1", "8.0.0", "9.0.0"),
            relevant_packages=("rollup", "vite"),
            attempted_versions=("7.5.1",),
            policy=self.policy,
        )
        self.assertEqual(1, len(first.versions))
        exhausted = bounded_causal_probe_domain(
            metadata=self.meta,
            versions=("7.3.1", "7.5.1", "8.0.0", "9.0.0"),
            relevant_packages=("rollup", "vite"),
            attempted_versions=("7.5.1", "8.0.0"),
            policy=self.policy,
        )
        self.assertTrue(exhausted.exhausted)
        self.assertEqual("per-package-physical-budget-exhausted", exhausted.reason)

    def test_family_budget_counts_physical_attempts_not_solver_iterations(self) -> None:
        self.assertFalse(
            family_budget_exhausted(
                {"storybook": 2, "vite": 2, "vitest": 1}, policy=self.policy
            )
        )
        self.assertTrue(
            family_budget_exhausted(
                {"storybook": 2, "vite": 2, "vitest": 2}, policy=self.policy
            )
        )


class Psi58StaticIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.verifier = (ROOT / "baseline_constraint_verifier.py").read_text(encoding="utf-8")
        cls.generator = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        cls.observability = (ROOT / "verification_observability.py").read_text(encoding="utf-8")

    def test_resolver_only_bridge_hint_is_wired(self) -> None:
        self.assertIn("# BLOCK_PSI58_RESOLVER_SEED_ECONOMICS_V1", self.verifier)
        self.assertIn("resolver_seed_publication_hint", self.verifier)
        self.assertIn("RESOLVER_SEED_PUBLICATION_BRIDGE", self.generator)
        self.assertIn('"resolver-seed.publish-skipped"', self.verifier)

    def test_causal_budget_rotates_parents_and_handoffs(self) -> None:
        self.assertIn("# BLOCK_PSI58_BOUNDED_CAUSAL_SEARCH_V1", self.generator)
        self.assertIn("bounded_causal_probe_domain(", self.generator)
        self.assertIn('"causal-package.no-gain"', self.generator)
        self.assertIn('"causal-package.exhausted"', self.generator)
        self.assertIn('"causal-family.cohort-handoff"', self.generator)

    def test_seed_observability_matches_emitted_event_names(self) -> None:
        self.assertIn(
            'event in {"resolver-seed.created", "resolver-seed.published"}',
            self.observability,
        )
        self.assertIn('"resolverSeedPublishMs"', self.observability)
        self.assertIn('"resolverSeedMissReasons"', self.observability)


if __name__ == "__main__":
    unittest.main()
