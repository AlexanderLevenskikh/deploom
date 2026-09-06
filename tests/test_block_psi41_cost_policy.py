from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import baseline_constraint_verifier as verifier
import verification_observability as observability


class Psi41CostPolicyTests(unittest.TestCase):
    def test_cross_run_control_is_same_run_only_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(verifier._persistent_control_cross_run_enabled())

    def test_force_mode_is_explicit(self) -> None:
        with patch.dict(
            os.environ,
            {"DEPLOOM_BASELINE_CONTROL_DURABLE_MODE": "force"},
            clear=False,
        ):
            self.assertTrue(verifier._persistent_control_cross_run_enabled())

    def test_resolver_avoided_counts_explicit_skip_not_cache_hits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sink = Path(tmp) / "telemetry.jsonl"
            observability.configure_observability_path(sink, reset=True)
            observability.emit_observability_event(
                "proof.cache.hit", proofType="resolver", cacheKey="x"
            )
            observability.emit_observability_event(
                "proof.cache.hit", proofType="resolver", cacheKey="x"
            )
            self.assertEqual(
                0,
                observability.run_summary_payload()["resolverProcessesAvoided"],
            )
            observability.emit_observability_event(
                "verify.resolver.skipped",
                operationId="resolver-skip-1",
                reason="test",
            )
            self.assertEqual(
                1,
                observability.run_summary_payload()["resolverProcessesAvoided"],
            )

    def test_publication_success_requires_confirmed_receipt(self) -> None:
        source = (ROOT / "baseline_constraint_verifier.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("durable_snapshot_confirmed = bool(", source)
        self.assertIn(
            "and snapshot.durable_publication_confirmed",
            source,
        )
        self.assertIn(
            "snapshot.durable_seal_duration_ms",
            source,
        )
        self.assertIn("totalPublicationMs=snapshot_duration_ms", source)

    def test_control_cold_validation_has_acceptance_marker(self) -> None:
        source = (ROOT / "baseline_constraint_verifier.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "PSI41_PERSISTENT_CONTROL_COLD_REUSE_SKIPPED",
            source,
        )
        self.assertIn(
            "persistent-control.prepared.publication-cost-skip",
            source,
        )

    def test_yarn_resolver_seed_remains_fail_closed(self) -> None:
        source = (ROOT / "package_manager_profile.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "YARN1_RESOLVER_SEED_CONTINUATION_UNSUPPORTED",
            source,
        )

    def test_arbitrary_shell_project_proof_policy_unchanged(self) -> None:
        self.assertFalse(
            verifier.project_proof_cache_reusable(("yarn lint:types",))
        )


if __name__ == "__main__":
    unittest.main()
