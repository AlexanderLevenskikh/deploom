from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import baseline_constraint_verifier as verifier
import package_manager_profile as pm
import verification_observability as observability


class Psi4InstallCostTests(unittest.TestCase):
    def test_intermediate_roles_are_never_durable(self) -> None:
        for purpose in (
            "intermediate-candidate",
            "exact-failure-confirmation",
            "diagnostic-probe",
        ):
            with self.subTest(purpose=purpose):
                config = verifier.BaselineVerifyConfig(
                    publish_durable_prepared_artifact=True,
                    verification_purpose=purpose,
                )
                allowed, reason = verifier._durable_prepared_publication_policy(
                    config
                )
                self.assertFalse(allowed)
                self.assertEqual("intermediate-role-private", reason)

    def test_baseline_control_is_explicitly_durable_eligible(self) -> None:
        config = verifier.BaselineVerifyConfig(
            publish_durable_prepared_artifact=True,
            verification_purpose="baseline-control",
        )
        allowed, reason = verifier._durable_prepared_publication_policy(config)
        self.assertTrue(allowed)
        self.assertEqual("stable-baseline-control", reason)

    def test_legacy_callers_keep_boolean_semantics(self) -> None:
        for requested in (False, True):
            config = verifier.BaselineVerifyConfig(
                publish_durable_prepared_artifact=requested,
                verification_purpose="legacy",
            )
            allowed, reason = verifier._durable_prepared_publication_policy(
                config
            )
            self.assertEqual(requested, allowed)
            self.assertEqual("legacy-explicit-policy", reason)

    def test_yarn_classic_seed_continuation_is_fail_closed(self) -> None:
        profile = pm.PackageManagerProfile(
            manager="yarn",
            family="yarn-classic",
            declared="yarn@1.22.22",
            declared_version="1.22.22",
            lockfile_name="yarn.lock",
            node_linker="node-modules",
            authoritative_supported=True,
        )
        capability = pm.resolver_seed_continuation_capability(profile)
        self.assertFalse(capability.supported)
        self.assertIn(
            "YARN1_RESOLVER_SEED_CONTINUATION_UNSUPPORTED",
            capability.reason,
        )
        self.assertFalse(
            pm.logical_resolution_without_materialization_supported(profile)
        )

    def test_neighbor_seed_is_default_off(self) -> None:
        self.assertFalse(
            verifier.BaselineVerifyConfig().enable_neighbor_resolver_seed
        )

    def test_observability_accounts_psi4_operations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sink = Path(tmp) / "telemetry.jsonl"
            observability.configure_observability_path(sink, reset=True)
            observability.emit_observability_event(
                "verify.resolver.start", operationId="resolver-1"
            )
            observability.emit_observability_event(
                "verify.resolver.finish",
                operationId="resolver-1",
                durationMs=100,
                outcome="passed",
            )
            observability.emit_observability_event(
                "verify.resolved-state.capture.finish",
                operationId="state-1",
                durationMs=7,
                outcome="passed",
            )
            observability.emit_observability_event(
                "prepared.publication.skipped",
                operationId="publication-1",
                role="intermediate-candidate",
                decisionReason="intermediate-role-private",
            )
            observability.emit_observability_event(
                "persistent-control.prepared.hit",
                operationId="control-1",
            )
            summary = observability.run_summary_payload()
            self.assertEqual(1, summary["resolverProcessesStarted"])
            self.assertEqual(100, summary["resolverInstallMs"])
            self.assertEqual(7, summary["resolvedStateCaptureMs"])
            self.assertEqual(1, summary["intermediateDurableSealsAvoided"])
            self.assertEqual(1, summary["persistentControlPreparedHits"])
            self.assertEqual(1, summary["persistentControlLifecycleAvoided"])

    def test_generator_uses_role_based_publication(self) -> None:
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'verification_purpose="intermediate-candidate"', source
        )
        self.assertIn('verification_purpose="baseline-control"', source)
        self.assertIn(
            'verification_purpose="exact-failure-confirmation"', source
        )
        self.assertNotIn(
            "publish_durable_prepared_artifact=not "
            "_baseline_preseal_screening_enabled",
            source,
        )

    def test_incumbent_record_precedes_durable_promotion(self) -> None:
        source = (ROOT / "dependency_live_roadmap_generator.py").read_text(
            encoding="utf-8"
        )
        record = source.index(
            "completion_status = record_verified_incumbent("
        )
        promotion = source.index(
            "promoted_incumbent = promote_same_run_prepared_artifact("
        )
        self.assertLess(record, promotion)

    def test_acceptance_markers_exist(self) -> None:
        source = (ROOT / "baseline_constraint_verifier.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("PSI4_INTERMEDIATE_DURABLE_SEAL_SKIPPED", source)
        self.assertIn("PSI4_PERSISTENT_CONTROL_PREPARED_HIT", source)
        self.assertIn(
            "PSI4_RESOLVER_SEED_CONTINUATION_UNSUPPORTED", source
        )

    def test_arbitrary_shell_project_proof_policy_unchanged(self) -> None:
        self.assertFalse(
            verifier.project_proof_cache_reusable(("yarn lint:types",))
        )
        self.assertTrue(verifier.project_proof_cache_reusable(()))


if __name__ == "__main__":
    unittest.main()
