from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from block_psi55_resolver_overrides import (
    ResolverOverrideMaterializationError,
    apply_resolver_overrides,
    propose_duplicate_type_override,
    resolver_override_fingerprint,
)


class Psi55ResolverOverrideUnitTests(unittest.TestCase):
    def test_duplicate_type_direct_subject_proposes_exact_unification(self) -> None:
        proposal = propose_duplicate_type_override(
            signatures=("duplicate-type-universe:rollup",),
            assignment={"rollup": "4.50.0", "vite": "7.1.0"},
            current_versions={"rollup": "3.29.5"},
            manager="yarn",
        )
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertEqual({"rollup": "4.50.0"}, proposal.overrides)
        self.assertEqual("resolutions", proposal.manifest_path)

    def test_transitive_only_subject_is_not_guessed(self) -> None:
        self.assertIsNone(
            propose_duplicate_type_override(
                signatures=("duplicate-type-universe:rollup",),
                assignment={"vite": "7.1.0"},
                current_versions={},
                manager="yarn",
            )
        )

    def test_non_duplicate_predicate_does_not_propose_override(self) -> None:
        self.assertIsNone(
            propose_duplicate_type_override(
                signatures=("ts-module-resolution:vite",),
                assignment={"vite": "7.1.0"},
                current_versions={"vite": "4.3.9"},
                manager="yarn",
            )
        )

    def test_manifest_materialization_for_supported_managers(self) -> None:
        cases = {
            "yarn": ("resolutions",),
            "npm": ("overrides",),
            "pnpm": ("pnpm", "overrides"),
        }
        for manager, path in cases.items():
            with self.subTest(manager=manager), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "package.json").write_text(
                    json.dumps({"name": "fixture", "private": True}) + "\n",
                    encoding="utf-8",
                )
                field, changed = apply_resolver_overrides(
                    root,
                    manager=manager,
                    overrides={"rollup": "4.50.0"},
                )
                self.assertEqual(("rollup",), changed)
                payload = json.loads((root / "package.json").read_text(encoding="utf-8"))
                value = payload
                for segment in path:
                    value = value[segment]
                self.assertEqual("4.50.0", value["rollup"])
                self.assertTrue(field)

    def test_user_authored_conflicting_override_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(
                json.dumps({"resolutions": {"rollup": "3.29.5"}}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(ResolverOverrideMaterializationError):
                apply_resolver_overrides(
                    root,
                    manager="yarn",
                    overrides={"rollup": "4.50.0"},
                )
            payload = json.loads((root / "package.json").read_text(encoding="utf-8"))
            self.assertEqual("3.29.5", payload["resolutions"]["rollup"])

    def test_override_identity_changes_with_version(self) -> None:
        self.assertNotEqual(
            resolver_override_fingerprint({"rollup": "4.50.0"}),
            resolver_override_fingerprint({"rollup": "4.49.0"}),
        )


class Psi55StaticProofContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.verifier = (ROOT / "baseline_constraint_verifier.py").read_text(
            encoding="utf-8"
        )
        cls.proof = (ROOT / "verification_proof.py").read_text(encoding="utf-8")
        cls.generator = (ROOT / "dependency_live_roadmap_generator.py").read_text(
            encoding="utf-8"
        )
        cls.envelope = (ROOT / "proven_dependency_state.py").read_text(
            encoding="utf-8"
        )

    def test_override_is_bound_into_resolver_identity_and_coordination(self) -> None:
        self.assertIn('"resolverOverrides": sorted(', self.proof)
        self.assertIn("config.resolver_overrides", self.verifier)
        self.assertIn("apply_resolver_overrides(", self.verifier)
        self.assertIn("resolverOverrideKey", self.verifier)

    def test_verified_envelope_carries_override(self) -> None:
        self.assertIn('"resolverOverrides"', self.envelope)
        self.assertIn("resolver_overrides=resolver_overrides", self.generator)

    def test_search_block_is_diagnostic_until_physical_pass(self) -> None:
        marker = "# BLOCK_PSI55_VERIFIED_RESOLVER_OVERRIDES_V1"
        start = self.generator.index(marker)
        end = self.generator.index(
            "# Proof-preserving fast path: forbid exactly the full assignment",
            start,
        )
        block = self.generator[start:end]
        self.assertIn("verify_assignment(", block)
        self.assertIn("resolver-override-candidate-proposed", block)
        self.assertIn("resolver-override-verified", block)
        self.assertIn("authority=EVIDENCE_DIAGNOSTIC_HINT", block)
        self.assertNotIn("global_exact_exclusions", block)
        self.assertNotIn("learned[project][mode].append", block)
        self.assertNotIn("learned[project][mode] =", block)
        self.assertNotIn("EVIDENCE_CONFIRMED_CONSTRAINT", block)

    def test_minimized_single_literal_is_human_readable(self) -> None:
        self.assertIn("isolated diagnostic culprit", self.generator)
        self.assertIn("minimizedLiteralValues=dict(nogood)", self.generator)


if __name__ == "__main__":
    unittest.main()
