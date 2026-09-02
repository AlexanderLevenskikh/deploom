from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import baseline_constraint_verifier as verifier
from verification_observability import (
    configure_observability_path,
    record_verification_event,
    run_summary_payload,
)
from verification_proof import build_project_trial_key


ROOT = Path(__file__).resolve().parents[1]


class BlockXSameRunReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        verifier.reset_same_run_verification_reuse()
        configure_observability_path(None, reset=True)

    def test_same_assignment_concurrent_requests_have_one_preparation_producer(self) -> None:
        config = verifier.BaselineVerifyConfig(
            project_checks="adaptive", commands=("yarn lint:types",)
        )
        prepared = False
        builds = 0
        calls = 0
        guard = threading.Lock()

        def fake_verify(*args, **kwargs):
            nonlocal prepared, builds, calls
            calls += 1
            with guard:
                if not prepared:
                    builds += 1
                    time.sleep(0.05)
                    prepared = True
            return verifier.BaselineVerifyResult(True, "passed", "ok")

        results = []
        with patch.object(verifier, "_verify_assignment_cache_aware_impl", fake_verify):
            threads = [
                threading.Thread(
                    target=lambda: results.append(
                        verifier.verify_assignment(
                            ROOT,
                            {"react": "19.0.0"},
                            config=config,
                            run_project_checks=True,
                        )
                    )
                )
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)

        self.assertEqual(2, calls)
        self.assertEqual(1, builds)
        self.assertEqual(2, len(results))
        self.assertTrue(all(result.ok for result in results))

    def test_failed_producer_releases_key_for_retry(self) -> None:
        config = verifier.BaselineVerifyConfig(
            project_checks="strict", commands=("yarn test",)
        )
        calls = 0

        def flaky(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("producer failed")
            return verifier.BaselineVerifyResult(True, "passed", "retry")

        with patch.object(verifier, "_verify_assignment_cache_aware_impl", flaky):
            with self.assertRaisesRegex(RuntimeError, "producer failed"):
                verifier.verify_assignment(
                    ROOT, {"react": "19.0.0"}, config=config, run_project_checks=True
                )
            retry = verifier.verify_assignment(
                ROOT, {"react": "19.0.0"}, config=config, run_project_checks=True
            )
        self.assertTrue(retry.ok)
        self.assertEqual(2, calls)

    def test_coordination_identity_changes_fail_closed(self) -> None:
        config = verifier.BaselineVerifyConfig(
            project_checks="adaptive", commands=("yarn lint",), registry="https://registry"
        )
        first = verifier._preparation_coordination_key(
            ROOT, {"react": "18.0.0"}, (), config
        )
        assignment_changed = verifier._preparation_coordination_key(
            ROOT, {"react": "19.0.0"}, (), config
        )
        registry_changed = verifier._preparation_coordination_key(
            ROOT,
            {"react": "18.0.0"},
            (),
            verifier.BaselineVerifyConfig(
                project_checks="adaptive",
                commands=("yarn lint",),
                registry="https://other-registry",
            ),
        )
        self.assertNotEqual(first, assignment_changed)
        self.assertNotEqual(first, registry_changed)

    def test_project_trial_identity_binds_state_source_command_and_policy(self) -> None:
        common = dict(
            resolver_trial_key="a" * 64,
            resolved_state_key="b" * 64,
            source_snapshot_key="source-1",
            project_checks="adaptive",
            commands=("yarn lint:types",),
        )
        base = build_project_trial_key(**common)
        for changed in (
            {**common, "resolved_state_key": "c" * 64},
            {**common, "source_snapshot_key": "source-2"},
            {**common, "commands": ("yarn test",)},
            {**common, "project_checks": "strict"},
        ):
            self.assertNotEqual(base, build_project_trial_key(**changed))

    def test_block_x_telemetry_counts_reuse_without_claiming_wall_clock_savings(self) -> None:
        record_verification_event("same-run.prepared-artifact.build", {})
        record_verification_event("same-run.prepared-artifact.hit", {})
        record_verification_event("same-run.control-proof.hit", {})
        record_verification_event("same-run.project-observation.hit", {})
        record_verification_event("same-run.independent-reproduction", {})
        record_verification_event("same-run.prepared-artifact.invalidated", {})
        summary = run_summary_payload()
        self.assertEqual(1, summary["preparedArtifactBuilds"])
        self.assertEqual(1, summary["sameRunPreparedArtifactHits"])
        self.assertEqual(1, summary["lifecycleInstallsAvoided"])
        self.assertEqual(1, summary["integritySealsAvoided"])
        self.assertEqual(1, summary["controlProofHits"])
        self.assertEqual(1, summary["projectObservationHits"])
        self.assertEqual(1, summary["independentReproductions"])
        self.assertEqual(1, summary["artifactInvalidations"])

    def test_production_flow_keeps_fresh_trials_and_reuses_exact_evidence(self) -> None:
        verifier_source = (ROOT / "baseline_constraint_verifier.py").read_text(encoding="utf-8")
        roadmap_source = (ROOT / "dependency_live_roadmap_generator.py").read_text(encoding="utf-8")
        self.assertIn("SAME_RUN_PREPARED_ARTIFACT_HIT", verifier_source)
        self.assertIn("_fresh_project_check_root(", verifier_source)
        self.assertIn("PROJECT_OBSERVATION_HIT", roadmap_source)
        self.assertIn("CONTROL_PROOF_HIT", roadmap_source)
        self.assertIn('confirmation.kind not in {"infrastructure", "unknown"}', roadmap_source)
        self.assertIn("same-run.independent-reproduction", roadmap_source)


if __name__ == "__main__":
    unittest.main()
